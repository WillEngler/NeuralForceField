import copy
import numbers
from copy import deepcopy

import nff.utils.constants as const
import numpy as np
import torch
from ase import Atoms
from ase.neighborlist import neighbor_list
from nff.data.features import ATOM_FEAT_TYPES, BOND_FEAT_TYPES
from nff.data.features import add_morgan as external_morgan
from nff.data.features import featurize_rdkit as external_rdkit
from nff.data.graphs import (
    DISTANCETHRESHOLDICT_Z,
    add_ji_kj,
    generate_subgraphs,
    get_angle_list,
    get_bond_idx,
    get_neighbor_list,
    make_dset_directed,
    reconstruct_atoms,
)
from nff.data.parallel import (
    NUM_PROCS,
    add_bond_idx_parallel,
    add_e3fp_parallel,
    add_kj_ji_parallel,
    featurize_parallel,
)
from sklearn.model_selection import train_test_split
from sklearn.utils import shuffle as skshuffle
from torch.utils.data import Dataset as TorchDataset
from tqdm import tqdm
# from typing import Dict


class Dataset(TorchDataset):
    """Dataset to deal with NFF calculations.

    Attributes:
        props (dict of lists): dictionary, where each key is the name of a property and
            each value is a list. The element of each list is the properties of a single
            geometry, whose coordinates are given by
            `nxyz`.

            Keys are the name of the property and values are the properties. Each value
            is given by `props[idx][key]`. The only mandatory key is 'nxyz'. If inputting
            energies, forces or hessians of different electronic states, the quantities
            should be distinguished with a "_n" suffix, where n = 0, 1, 2, ...
            Whatever name is given to the energy of state n, the corresponding force name
            must be the exact same name, but with "energy" replaced by "force".

            Example:

                props = {
                    'nxyz': [np.array([[1, 0, 0, 0], [1, 1.1, 0, 0]]),
                             np.array([[1, 3, 0, 0], [1, 1.1, 5, 0]])],
                    'energy_0': [1, 1.2],
                    'energy_0_grad': [np.array([[0, 0, 0], [0.1, 0.2, 0.3]]),
                                      np.array([[0, 0, 0], [0.1, 0.2, 0.3]])],
                    'energy_1': [1.5, 1.5],
                    'energy_1_grad': [np.array([[0, 0, 1], [0.1, 0.5, 0.8]]),
                                      np.array([[0, 0, 1], [0.1, 0.5, 0.8]])],
                    'dipole_2': [3, None]
                }

            Periodic boundary conditions must be specified through the 'offset' key in
                props. Once the neighborlist is created, distances between
                atoms are computed by subtracting their xyz coordinates
                and adding to the offset vector. This ensures images
                of atoms outside of the unit cell have different
                distances when compared to atoms inside of the unit cell.
                This also bypasses the need for a reindexing.

        units (str): units of the energies, forces etc.

    """

    def __init__(self, props: dict, units="kcal/mol", check_props=True, do_copy=True):
        """Constructor for Dataset class.

        Args:
            props (dictionary of lists): dictionary containing the
                properties of the system. Each key has a list, and
                all lists have the same length.
            units (str): units of the system.
        """
        if check_props:
            if do_copy:
                self.props = self._check_dictionary(deepcopy(props))
            else:
                self.props = self._check_dictionary(props)
        else:
            self.props = props
        self.units = units
        self.to_units("kcal/mol")

    def __len__(self):
        """Summary

        Returns:
            TYPE: Description
        """
        return len(self.props["nxyz"])

    def __getitem__(self, idx):
        """Summary

        Args:
            idx (TYPE): Description

        Returns:
            TYPE: Description
        """
        return {key: val[idx] for key, val in self.props.items()}

    def __add__(self, other):
        """Summary

        Args:
            other (TYPE): Description

        Returns:
            TYPE: Description
        """
        if other.units != self.units:
            other = other.copy().to_units(self.units)

        new_props = self.props
        keys = list(new_props.keys())
        for key in keys:
            if key not in other.props:
                new_props.pop(key)
                continue
            val = other.props[key]
            if type(val) is list:
                new_props[key] += val
            else:
                old_val = new_props[key]
                new_props[key] = torch.cat([old_val, val.to(old_val.dtype)])
        self.props = new_props

        return copy.deepcopy(self)

    def _check_dictionary(self, props):
        """Check the dictionary or properties to see if it has the
        specified format.

        Args:
            props (TYPE): Description

        Returns:
            TYPE: Description
        """

        assert "nxyz" in props.keys()
        n_atoms = [len(x) for x in props["nxyz"]]
        n_geoms = len(props["nxyz"])

        if "num_atoms" not in props.keys():
            props["num_atoms"] = torch.LongTensor(n_atoms)
        else:
            props["num_atoms"] = torch.LongTensor(props["num_atoms"])

        for key, val in props.items():
            if val is None:
                props[key] = to_tensor([np.nan] * n_geoms)

            elif any([x is None for x in val]):
                bad_indices = [i for i, item in enumerate(val) if item is None]
                good_indices = [
                    index for index in range(len(val)) if index not in bad_indices
                ]
                if len(good_indices) == 0:
                    nan_list = np.array([float("NaN")]).tolist()
                else:
                    good_index = good_indices[0]
                    nan_list = (np.array(val[good_index]) * float("NaN")).tolist()
                for index in bad_indices:
                    props[key][index] = nan_list
                props.update({key: to_tensor(val)})

            else:
                assert len(val) == n_geoms, (
                    f"length of {key} is not "
                    f"compatible with {n_geoms} "
                    "geometries"
                )
                props[key] = to_tensor(val)

        return props

    def generate_neighbor_list(
        self, cutoff, undirected=True, key="nbr_list", offset_key="offsets"
    ):
        """Generates a neighbor list for each one of the atoms in the dataset.
            By default, does not consider periodic boundary conditions.

        Args:
            cutoff (float): distance up to which atoms are considered bonded.
            undirected (bool, optional): Description

        Returns:
            TYPE: Description
        """
        if "lattice" not in self.props:
            self.props[key] = [
                get_neighbor_list(nxyz[:, 1:4], cutoff, undirected)
                for nxyz in self.props["nxyz"]
            ]
            self.props[offset_key] = [
                torch.sparse.FloatTensor(nbrlist.shape[0], 3)
                for nbrlist in self.props[key]
            ]
        else:
            self._get_periodic_neighbor_list(
                cutoff=cutoff, undirected=undirected, offset_key=offset_key, nbr_key=key
            )
            return self.props[key], self.props[offset_key]

        return self.props[key]

    # def make_nbr_to_mol(self):
    #     nbr_to_mol = []
    #     for nbrs in self.props['nbr_list']:
    #         nbrs_to_mol.append(torch.zeros(len(nbrs)))

    def make_all_directed(self):
        make_dset_directed(self)

    def generate_angle_list(self):
        if "lattice" in self.props:
            raise NotImplementedError("Angles not implemented for PBC.")

        self.make_all_directed()

        angles, nbrs = get_angle_list(self.props["nbr_list"])
        self.props["nbr_list"] = nbrs
        self.props["angle_list"] = angles

        ji_idx, kj_idx = add_ji_kj(angles, nbrs)

        self.props["ji_idx"] = ji_idx
        self.props["kj_idx"] = kj_idx

        return angles

    def generate_kj_ji(self, num_procs=1):
        """
        Generate only the `ji_idx` and `kj_idx` without storing
        the full angle list.
        """

        self.make_all_directed()
        add_kj_ji_parallel(self, num_procs=num_procs)

    def _get_periodic_neighbor_list(
        self, cutoff, undirected=False, offset_key="offsets", nbr_key="nbr_list"
    ):
        from nff.io.ase import AtomsBatch

        nbrlist = []
        offsets = []
        for nxyz, lattice in zip(self.props["nxyz"], self.props["lattice"]):
            atoms = AtomsBatch(
                nxyz[:, 0].long(),
                props={"num_atoms": torch.LongTensor([len(nxyz[:, 0])])},
                positions=nxyz[:, 1:],
                cell=lattice,
                pbc=True,
                cutoff=cutoff,
                directed=(not undirected),
            )
            nbrs, offs = atoms.update_nbr_list()
            nbrlist.append(nbrs)
            offsets.append(offs)

        self.props[nbr_key] = nbrlist
        self.props[offset_key] = offsets
        return

    def generate_bond_idx(self, num_procs=1):
        """
        For each index in the bond list, get the
        index in the neighbour list that corresponds to the
        same directed pair of atoms.
        Args:
            None
        Returns:
            None
        """

        self.make_all_directed()
        add_bond_idx_parallel(self, num_procs)

    def copy(self):
        """Copies the current dataset

        Returns:
            TYPE: Description
        """
        return Dataset(self.props, self.units)

    def to_units(self, target_unit):
        """Converts the dataset to the desired unit. Modifies the dictionary
        of properties in place.

        Args:
            target_unit (str): unit to use as final one

        Returns:
            TYPE: Description

        Raises:
            NotImplementedError: Description
        """
        conversion_factors = {
            ("eV", "kcal/mol"): const.EV_TO_KCAL,
            ("eV", "atomic"): const.EV_TO_AU,
            ("kcal/mol", "eV"): const.KCAL_TO_EV,
            ("kcal/mol", "atomic"): const.KCAL_TO_AU,
            ("atomic", "eV"): const.AU_TO_EV,
            ("atomic", "kcal/mol"): const.AU_TO_KCAL,
        }

        if target_unit not in ["kcal/mol", "eV", "atomic"]:
            raise NotImplementedError(f"Unit {target_unit} not implemented")

        curr_unit = self.units

        if target_unit == curr_unit:
            return

        conversion_factor = conversion_factors.get((curr_unit, target_unit))

        if conversion_factor is None:
            raise NotImplementedError(
                f"Conversion from {curr_unit} to {target_unit} not implemented"
            )

        self.props = const.convert_units(self.props, conversion_factor)
        if "units" in self.props.keys() and isinstance(self.props["units"], list):
            self.props["units"] = [target_unit for x in self.props["units"]]
        self.units = target_unit

        return

    def change_idx(self, idx):
        """
        Change the dataset so that the properties are ordered by the
        indices `idx`. If `idx` does not contain all of the original
        indices in the dataset, then this will reduce the size of the
        dataset.
        """

        for key, val in self.props.items():
            if isinstance(val, list):
                self.props[key] = [val[i] for i in idx]
            else:
                self.props[key] = val[idx]

    def shuffle(self):
        """Summary

        Returns:
            TYPE: Description
        """
        idx = list(range(len(self)))
        reindex = skshuffle(idx)
        self.change_idx(reindex)

    def featurize(
        self,
        num_procs=NUM_PROCS,
        bond_feats=BOND_FEAT_TYPES,
        atom_feats=ATOM_FEAT_TYPES,
    ):
        """
        Featurize dataset with atom and bond features.
        Args:
            num_procs (int): number of parallel processes
            bond_feats (list[str]): names of bond features
            atom_feats (list[str]): names of atom features
        Returns:
            None
        """

        featurize_parallel(
            self, num_procs=num_procs, bond_feats=bond_feats, atom_feats=atom_feats
        )

    def add_morgan(self, vec_length):
        """
        Add Morgan fingerprints to each species in the dataset.
        Args:
            vec_length (int): length of fingerprint
        Returns:
            None
        """
        external_morgan(self, vec_length)

    def add_e3fp(self, fp_length, num_procs=NUM_PROCS):
        """
        Add E3FP fingerprints for each conformer of each species
        in the dataset.
        Args:
            fp_length (int): length of fingerprint
            num_procs (int): number of processes to use when
                featurizing.
        Returns:
            None
        """

        add_e3fp_parallel(self, fp_length, num_procs)

    def featurize_rdkit(self, method):
        """
        Add 3D-based RDKit fingerprints for each conformer of
        each species in the dataset.
        Args:
            method (str): name of RDKit feature method to use
        Returns:
            None
        """
        external_rdkit(self, method=method)

    def unwrap_xyz(self, mol_dic):
        """
        Unwrap molecular coordinates by displacing atoms by box vectors


        Args:
            mol_dic (dict): dictionary of nodes of each disconnected subgraphs
        """
        from nff.io.ase import AtomsBatch

        for i in range(len(self.props["nxyz"])):
            # makes atoms object

            atoms = AtomsBatch(
                positions=self.props["nxyz"][i][:, 1:4],
                numbers=self.props["nxyz"][i][:, 0],
                cell=self.props["cell"][i],
                pbc=True,
            )

            # recontruct coordinates based on subgraphs index
            if self.props["smiles"]:
                mol_idx = mol_dic[self.props["smiles"][i]]
                atoms.set_positions(reconstruct_atoms(atoms, mol_idx))
                nxyz = atoms.get_nxyz()
            self.props["nxyz"][i] = torch.Tensor(nxyz)

    def save(self, path):
        """Summary

        Args:
            path (TYPE): Description
        """

        # to deal with the fact that sparse tensors can't be pickled
        offsets = self.props.get("offsets", torch.LongTensor([0]))
        old_offsets = copy.deepcopy(offsets)

        # check if it's a sparse tensor. The first two conditions
        # Are needed for backwards compatability in case it's a float
        # or empty list

        if all([hasattr(offsets, "__len__"), len(offsets) > 0]):
            if isinstance(offsets[0], torch.sparse.FloatTensor):
                self.props["offsets"] = [val.to_dense() for val in offsets]

        torch.save(self, path)
        if "offsets" in self.props:
            self.props["offsets"] = old_offsets

    def gen_bond_stats(self):
        bond_len_dict = {}
        # generate bond statistics
        for i in range(len(self.props["nxyz"])):
            z = self.props["nxyz"][i][:, 0]
            xyz = self.props["nxyz"][i][:, 1:4]
            bond_list = self.props["bonds"][i]
            bond_len = (
                (xyz[bond_list[:, 0]] - xyz[bond_list[:, 1]])
                .pow(2)
                .sum(-1)
                .sqrt()[:, None]
            )

            bond_type_list = torch.stack((z[bond_list[:, 0]], z[bond_list[:, 1]])).t()
            for i, bond in enumerate(bond_type_list):
                bond = tuple(torch.LongTensor(sorted(bond)).tolist())
                if bond not in bond_len_dict.keys():
                    bond_len_dict[bond] = [bond_len[i]]
                else:
                    bond_len_dict[bond].append(bond_len[i])

        # compute bond len averages
        self.bond_len_dict = {
            key: torch.stack(bond_len_dict[key]).mean(0) for key in bond_len_dict.keys()
        }

        return self.bond_len_dict

    def gen_bond_prior(self, cutoff, bond_len_dict=None):
        from nff.io.ase import AtomsBatch

        if not self.props:
            raise TypeError("the dataset has no data yet")

        bond_dict = {}
        mol_idx_dict = {}

        # ---------This part can be simplified---------#
        for i in range(len(self.props["nxyz"])):
            z = self.props["nxyz"][i][:, 0]
            xyz = self.props["nxyz"][i][:, 1:4]

            # generate arguments for ase Atoms object
            cell = self.props["cell"][i] if "cell" in self.props.keys() else None
            ase_param = {"numbers": z, "positions": xyz, "pbc": True, "cell": cell}

            atoms = Atoms(**ase_param)
            sys_name = self.props["smiles"][i]
            if sys_name not in bond_dict.keys():
                print(sys_name)
                i, j = neighbor_list("ij", atoms, DISTANCETHRESHOLDICT_Z)

                bond_list = torch.LongTensor(np.stack((i, j), axis=1)).tolist()
                bond_dict[sys_name] = bond_list

                # generate molecular graph
                # TODO: there is redundant code in generate_subgraphs
                subgraph_index = generate_subgraphs(atoms)
                mol_idx_dict[sys_name] = subgraph_index

        # generate topologies
        # TODO: include options to only generate bond topology
        self.generate_topologies(bond_dic=bond_dict)
        if "cell" in self.props.keys():
            self.unwrap_xyz(mol_idx_dict)
        # ---------This part can be simplified---------#

        # generate bond length dictionary if not given
        if not bond_len_dict:
            bond_len_dict = self.gen_bond_stats()

        # update bond len and offsets
        all_bond_len = []
        all_offsets = []
        all_nbr_list = []
        for i in range(len(self.props["nxyz"])):
            z = self.props["nxyz"][i][:, 0]
            xyz = self.props["nxyz"][i][:, 1:4]

            bond_list = self.props["bonds"][i]
            bond_type_list = torch.stack((z[bond_list[:, 0]], z[bond_list[:, 1]])).t()
            bond_len_list = []
            for bond in bond_type_list:
                bond_type = tuple(torch.LongTensor(sorted(bond)).tolist())
                bond_len_list.append(bond_len_dict[bond_type])
            all_bond_len.append(torch.Tensor(bond_len_list).reshape(-1, 1))

            # update offsets
            cell = (self.props["cell"][i] if "cell" in self.props.keys() else None,)
            ase_param = {
                "numbers": z,
                "positions": xyz,
                "pbc": True,
                "cutoff": cutoff,
                "cell": cell,
                "nbr_torch": False,
            }

            # the coordinates have been unwrapped and try to results offsets
            atoms = AtomsBatch(**ase_param)
            atoms.update_nbr_list()
            all_offsets.append(atoms.offsets)
            all_nbr_list.append(atoms.nbr_list)

        # update
        self.props["bond_len"] = all_bond_len
        self.props["offsets"] = all_offsets
        self.props["nbr_list"] = all_nbr_list
        self._check_dictionary(deepcopy(self.props))

    def as_atoms_batches(
        self,
        cutoff=5.0,
        undirected=False,
        offset_key="offsets",
        nbr_key="nbr_list",
        device="cuda",
    ):
        """
        Converts the dataset to a list of AtomsBatch objects.
        Args:
            cutoff (float): distance up to which atoms are considered bonded.
            undirected (bool, optional): Description
            offset_key (str, optional): Description
            nbr_key (str, optional): Description
        Returns:
            List[AtomsBatch]: list of AtomsBatch objects
        """
        from nff.io.ase import AtomsBatch

        atoms_batches = []
        num_batches = len(self.props["nxyz"])
        for i in range(num_batches):
            nxyz = self.props["nxyz"][i]
            atoms = AtomsBatch(
                nxyz[:, 0].long(),
                props={key: val[i] for key, val in self.props.items()},
                positions=nxyz[:, 1:],
                cell=self.props["lattice"][i]
                if "lattice" in self.props.keys()
                else None,
                pbc="lattice" in self.props.keys(),
                cutoff=cutoff,
                directed=(not undirected),
                device=device,
            )
            nbrs, offs = atoms.update_nbr_list()
            atoms.props.update(
                {
                    "num_atoms": torch.LongTensor([len(nxyz[:, 0])]),
                    nbr_key: nbrs,
                    offset_key: offs,
                }
            )
            for key, val in atoms.props.items():
                if isinstance(val, torch.Tensor):
                    atoms.props[key] = torch.atleast_1d(
                        val
                    )  # make sure it's a 1D tensor min
            atoms.props["units"] = self.units
            atoms_batches.append(atoms)
        return atoms_batches

    def update_dtype(self, dtype: str) -> None:
        """Update the datatype of all torch objects in the batch
        to match the given dtype.

        Args:
            dtype (str): the desired data type, either
                "float" (torch.float32) or "double" (torch.float64)
        """
        for key in self.props.keys():
            if isinstance(self.props[key], torch.Tensor):
                print(f"{key} is a tensor")
                if dtype == "float":
                    self.props[key] = self.props[key].float()
                elif dtype == "double":
                    self.props[key] = self.props[key].double()
            elif isinstance(self.props[key], list) and isinstance(
                self.props[key][0], torch.Tensor
            ):
                print(f"{key} is a list of tensors")
                for i, val in enumerate(self.props[key]):
                    if dtype == "float":
                        self.props[key][i] = val.float()
                    elif dtype == "double":
                        self.props[key][i] = val.double()
            else:
                continue

    @classmethod
    def from_file(cls, path: str):
        """Summary

        Args:
            path (str): path to the file from which you want to load a dataset

        Returns:
            Dataset: a dataset from the file

        Raises:
            TypeError: Description
        """
        obj = torch.load(path)
        if isinstance(obj, cls):
            return obj
        else:
            print(type(obj))
            raise TypeError(f"{path} is not an instance from {type(cls)}")


def force_to_energy_grad(dataset):
    """
    Converts forces to energy gradients in a dataset. This conforms to
        the notation that a key with `_grad` is the gradient of the
        property preceding it. Modifies the database in-place.

    Args:
        dataset (TYPE): Description
        dataset (nff.data.Dataset)

    Returns:
        success (bool): if True, forces were removed and energy_grad
            became the new key.
    """
    if "forces" not in dataset.props.keys():
        return False
    else:
        dataset.props["energy_grad"] = [-x for x in dataset.props.pop("forces")]
        return True


def convert_nan(x):
    """
    If a list has any elements that contain nan, convert its contents
    to the right form so that it can eventually be converted to a tensor.
    Args:
        x (list): any list with floats, ints, or Tensors.
    Returns:
        new_x (list): updated version of `x`
    """

    new_x = []
    # whether any of the contents have nan
    has_nan = any([np.isnan(y).any() for y in x])
    for y in x:
        if has_nan:
            # if one is nan then they will have to become float tensors
            if type(y) in [int, float]:
                new_x.append(torch.Tensor([y]))
            elif isinstance(y, torch.Tensor):
                new_x.append(y.float())
            elif isinstance(y, list):
                new_x.append(torch.Tensor(y))
            else:
                msg = (
                    "Don't know how to convert sub-components of type "
                    f"{type(x)} when components might contain nan"
                )
                raise Exception(msg)
        else:
            # otherwise they can be kept as is
            new_x.append(y)

    return new_x


def to_tensor(x, stack=False):
    """
    Converts input `x` to torch.Tensor.
    Args:
        x (list of lists): input to be converted. Can be: number, string, list, array, tensor
        stack (bool): if True, concatenates torch.Tensors in the batching dimension
    Returns:
        torch.Tensor or list, depending on the type of x
    Raises:
        TypeError: Description
    """

    # a single number should be a list
    if isinstance(x, numbers.Number):
        return torch.Tensor([x])

    if isinstance(x, str):
        return [x]

    if isinstance(x, torch.Tensor):
        return x

    if isinstance(x, list) and not isinstance(x[0], str):
        if not isinstance(x[0], torch.sparse.FloatTensor):
            x = convert_nan(x)

    # all objects in x are tensors
    if isinstance(x, list) and all([isinstance(y, torch.Tensor) for y in x]):
        # list of tensors with zero or one effective dimension
        # flatten the tensor

        if all([len(y.shape) < 1 for y in x]):
            return torch.cat([y.view(-1) for y in x], dim=0)

        elif stack:
            return torch.cat(x, dim=0)

        # list of multidimensional tensors
        else:
            return x

    # some objects are not tensors
    elif isinstance(x, list):
        # list of strings
        if all([isinstance(y, str) for y in x]):
            return x

        # list of ints
        if all([isinstance(y, int) for y in x]):
            return torch.LongTensor(x)

        # list of floats
        if all([isinstance(y, numbers.Number) for y in x]):
            return torch.Tensor(x)

        # list of arrays or other formats
        if any([isinstance(y, (list, np.ndarray)) for y in x]):
            return [torch.Tensor(y) for y in x]

    raise TypeError("Data type not understood")


def concatenate_dict(*dicts):
    """Concatenates dictionaries as long as they have the same keys.
        If one dictionary has one key that the others do not have,
        the dictionaries lacking the key will have that key replaced by None.
    Args:
        *dicts: Description
        *dicts (any number of dictionaries)
            Example:
                dict_1 = {
                    'nxyz': [...],
                    'energy': [...]
                }
                dict_2 = {
                    'nxyz': [...],
                    'energy': [...]
                }
                dicts = [dict_1, dict_2]
    Returns:
        TYPE: Description
    """

    assert all(
        [type(d) == dict for d in dicts]
    ), "all arguments have to be dictionaries"

    # Old method
    # keys = set(sum([list(d.keys()) for d in dicts], []))

    # New method
    keys = set()
    for dic in dicts:
        for key in dic.keys():
            if key not in keys:
                keys.add(key)

    # While less pretty, the new method is MUCH faster. For example,
    # for a dataset of size 600,000, the old method literally
    # takes hours, while the new method takes 250 ms

    def is_list_of_lists(value):
        if isinstance(value, list):
            return isinstance(value[0], list)
        return False

    def get_length(value):
        if is_list_of_lists(value):
            if is_list_of_lists(value[0]):
                return len(value)
            return 1

        elif isinstance(value, list):
            return len(value)

        return 1

    def get_length_of_values(dict_):
        if "nxyz" in dict_:
            return get_length(dict_["nxyz"])
        return min([get_length(v) for v in dict_.values()])

    def flatten_val(value):
        """Given a value, which can be a number, a list or
        a torch.Tensor, return its flattened version
        to be appended to a list of values
        """
        if is_list_of_lists(value):
            if is_list_of_lists(value[0]):
                return value
            else:
                return [value]

        elif isinstance(value, list):
            return value

        elif isinstance(value, torch.Tensor):
            if len(value.shape) == 0:
                return [value]
            elif len(value.shape) == 1:
                return [item for item in value]
            else:
                return [value]

        elif get_length(value) == 1:
            return [value]

        return [value]

    # we have to see how many values the properties of each dictionary has.
    values_per_dict = [get_length_of_values(d) for d in dicts]

    # creating the joint dicionary
    joint_dict = {}
    for key in keys:
        # flatten list of values
        values = []
        for num_values, d in zip(values_per_dict, dicts):
            val = d.get(key, ([None] * num_values if num_values > 1 else None))
            values += flatten_val(val)
        joint_dict[key] = values

    return joint_dict


def binary_split(dataset, targ_name, test_size, seed):
    """
    Split the dataset with proportional amounts of a binary label in each.
    Args:
        dataset (nff.data.dataset): NFF dataset
        targ_name (str, optional): name of the binary label to use
            in splitting.
        test_size (float, optional): fraction of dataset for test
    Returns:
        idx_train (list[int]): indices of species in the training set
        idx_test (list[int]): indices of species in the test set
    """

    # get indices of positive and negative values
    pos_idx = [i for i, targ in enumerate(dataset.props[targ_name]) if targ]
    neg_idx = [i for i in range(len(dataset)) if i not in pos_idx]

    # split the positive and negative indices separately
    pos_idx_train, pos_idx_test = train_test_split(
        pos_idx, test_size=test_size, random_state=seed
    )
    neg_idx_train, neg_idx_test = train_test_split(
        neg_idx, test_size=test_size, random_state=seed
    )

    # combine the negative and positive test idx to get the test idx
    # do the same for train

    idx_train = pos_idx_train + neg_idx_train
    idx_test = pos_idx_test + neg_idx_test

    return idx_train, idx_test


def stratified_split(dataset, targ_name, test_size, seed, min_count=2):
    """
    Split the dataset with proportional amounts of k labels in each.
    Args:
        dataset (nff.data.dataset): NFF dataset
        targ_name (str, optional): name of the label to use
            in splitting.
        test_size (float, optional): fraction of dataset for test
        min_count (int, optional): minimum number of samples in each label
    Returns:
        idx_train (list[int]): indices of species in the training set
        idx_test (list[int]): indices of species in the test set
    """

    all_idx = list(range(len(dataset)))
    stratify_labels = dataset.props[targ_name]

    # ensure minimum number of samples in each label
    from collections import Counter

    label_counts = Counter(stratify_labels)

    # get labels with count less than min count
    labels_to_remove = [
        label for label, count in label_counts.items() if count < min_count
    ]

    # remove labels with count less than min count
    idx_to_remove = []
    for bad_label in labels_to_remove:
        idx_to_remove.extend(
            [i for i, x in enumerate(stratify_labels) if x == bad_label]
        )

    all_idx = [i for i in all_idx if i not in idx_to_remove]
    stratify_labels = [
        x for i, x in enumerate(stratify_labels) if i not in idx_to_remove
    ]

    assert len(all_idx) == len(
        stratify_labels
    ), "length of indices and labels do not match"

    # use sklearn to split the indices
    idx_train, idx_test = train_test_split(
        all_idx, test_size=test_size, random_state=seed, stratify=stratify_labels
    )

    return idx_train, idx_test


def split_train_test(
    dataset,
    test_size=0.2,
    binary=False,
    stratified=False,
    targ_name=None,
    seed=None,
    **kwargs,
):
    """Splits the current dataset in two, one for training and
    another for testing.

    Args:
        dataset (nff.data.dataset): NFF dataset
        test_size (float, optional): fraction of dataset for test
        binary (bool, optional): whether to split the dataset with
            proportional amounts of a binary label in each.
        stratified (bool, optional): whether to split the dataset with
            proportional amounts of k labels in each.
        targ_name (str, optional): name of the binary label to use
            in splitting.
    Returns:
        TYPE: Description
    """

    if binary:
        idx_train, idx_test = binary_split(
            dataset=dataset, targ_name=targ_name, test_size=test_size, seed=seed
        )
    elif stratified:
        idx_train, idx_test = stratified_split(
            dataset=dataset,
            targ_name=targ_name,
            test_size=test_size,
            seed=seed,
            **kwargs,
        )
    else:
        idx = list(range(len(dataset)))
        idx_train, idx_test = train_test_split(
            idx, test_size=test_size, random_state=seed
        )

    train = Dataset(
        props={key: [val[i] for i in idx_train] for key, val in dataset.props.items()},
        units=dataset.units,
    )
    test = Dataset(
        props={key: [val[i] for i in idx_test] for key, val in dataset.props.items()},
        units=dataset.units,
    )

    return train, test


def split_train_validation_test(
    dataset, val_size=0.2, test_size=0.2, seed=None, **kwargs
):
    """Summary

    Args:
        dataset (TYPE): Description
        val_size (float, optional): Description
        test_size (float, optional): Description

    Returns:
        TYPE: Description
    """
    train, validation = split_train_test(
        dataset, test_size=val_size, seed=seed, **kwargs
    )
    train, test = split_train_test(
        train, test_size=test_size / (1 - val_size), seed=seed, **kwargs
    )

    return train, validation, test
