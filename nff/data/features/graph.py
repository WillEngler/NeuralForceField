"""
Tools for generating graph-based features
"""

import torch
import numpy as np
import copy
from rdkit import Chem
from rdkit.Chem import AllChem
from e3fp.pipeline import fprints_from_mol
import logging


from nff.utils.xyz2mol import xyz2mol
from nff.utils import tqdm_enum

# default options for xyz2mol

QUICK = True
EMBED_CHIRAL = True
USE_HUCKEL = False
CHARGED_FRAGMENTS = True

# default feature types and options

BOND_FEAT_TYPES = ["bond_type",
                   "conjugated",
                   "in_ring",
                   "stereo",
                   "in_ring_size"]

ATOM_FEAT_TYPES = ["atom_type",
                   "num_bonds",
                   "formal_charge",
                   "chirality",
                   "num_bonded_h",
                   "hybrid",
                   "aromaticity",
                   "mass"]

CHIRAL_OPTIONS = ["chi_unspecified",
                  "chi_tetrahedral_cw",
                  "chi_tetrahedral_ccw",
                  "chi_other"]

HYBRID_OPTIONS = ["s",
                  "sp",
                  "sp2",
                  "sp3",
                  "sp3d",
                  "sp3d2"]

BOND_OPTIONS = ["single",
                "double",
                "triple",
                "aromatic"]

STEREO_OPTIONS = ["stereonone",
                  "stereoany",
                  "stereoz",
                  "stereoe",
                  "stereocis",
                  "stereotrans"]

AT_NUM = list(range(1, 100))
FORMAL_CHARGES = [-2, -1, 0, 1, 2]
BONDS = [0, 1, 2, 3, 4, 5]
NUM_H = [0, 1, 2, 3, 4]
RING_SIZE = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]

# dictionary with feature names, their options, type,
# and size when stored as a vector

FEAT_DIC = {"bond_type": {"options": BOND_OPTIONS,
                          "num": len(BOND_OPTIONS) + 1},
            "conjugated": {"options": [bool],
                           "num": 1},
            "in_ring": {"options": [bool],
                        "num": 1},
            "stereo": {"options": STEREO_OPTIONS,
                       "num": len(STEREO_OPTIONS) + 1},
            "in_ring_size": {"options": RING_SIZE,
                             "num": len(RING_SIZE) + 1},
            "atom_type": {"options": AT_NUM,
                          "num": len(AT_NUM) + 1},
            "num_bonds": {"options": BONDS,
                          "num": len(BONDS) + 1},
            "formal_charge": {"options": FORMAL_CHARGES,
                              "num": len(FORMAL_CHARGES) + 1},
            "chirality": {"options": CHIRAL_OPTIONS,
                          "num": len(CHIRAL_OPTIONS) + 1},
            "num_bonded_h": {"options": NUM_H,
                             "num": len(NUM_H) + 1},
            "hybrid": {"options": HYBRID_OPTIONS,
                       "num": len(HYBRID_OPTIONS) + 1},
            "aromaticity": {"options": [bool],
                            "num": 1},
            "mass": {"options": [float],
                     "num": 1}}

META_DATA = {"bond_features": BOND_FEAT_TYPES,
             "atom_features": ATOM_FEAT_TYPES,
             "details": FEAT_DIC}

# default number of atom features

NUM_ATOM_FEATS = sum([val["num"] for key, val in FEAT_DIC.items()
                      if key in ATOM_FEAT_TYPES])

# default number of bond features

NUM_BOND_FEATS = sum([val["num"] for key, val in FEAT_DIC.items()
                      if key in BOND_FEAT_TYPES])


def remove_bad_idx(dataset, smiles_list, bad_idx, verbose=True):
    """
    Remove items in dataset that have indices in `bad_idx`.
    Args:
        dataset (nff.data.dataset): NFF dataset
        smiles_list (list[str]): SMiLES strings originally in dataset
        bad_idx (list[int]): indices to get rid of in the dataset
        verbose (bool): whether to print the progress made
    Returns:
        None
    """

    bad_idx = sorted(list(set(bad_idx)))
    new_props = {}
    for key, values in dataset.props.items():
        new_props[key] = [val for i, val in enumerate(
            values) if i not in bad_idx]
        if not new_props[key]:
            continue
        if type(values) is torch.Tensor:
            new_props[key] = torch.stack(new_props[key])

    dataset.props = new_props

    total_len = len(smiles_list)
    good_len = total_len - len(bad_idx)
    conv_pct = good_len / total_len * 100

    if verbose:
        print(("Converted %d of %d "
               "species (%.2f%%)" % (
                   good_len, total_len, conv_pct)))


def smiles_from_smiles(smiles):
    """
    Convert a smiles to its canonical form.
    Args:
        smiles (str): smiles string
    Returns:
        new_smiles (str): canonicial smiles
        new_mol (rdkit.Chem.rdchem.Mol): rdkit Mol created
            from the canonical smiles.
    """

    mol = Chem.MolFromSmiles(smiles)
    new_smiles = Chem.MolToSmiles(mol)
    new_mol = Chem.MolFromSmiles(new_smiles)
    new_smiles = Chem.MolToSmiles(new_mol)

    return new_smiles, new_mol


def smiles_from_mol(mol):
    """
    Get the canonical smiles from an rdkit mol.
    Args:
        mol (rdkit.Chem.rdchem.Mol): rdkit Mol 
    Returns:
        new_smiles (str): canonicial smiles
        new_mol (rdkit.Chem.rdchem.Mol): rdkit Mol created
            from the canonical smiles. 
    """

    new_smiles = Chem.MolToSmiles(mol)
    new_mol = Chem.MolFromSmiles(new_smiles)
    new_smiles = Chem.MolToSmiles(new_mol)

    return new_smiles, new_mol


def get_undirected_bonds(mol):
    """
    Get an undirected bond list from an RDKit mol. This
    means that bonds between atoms 1 and 0 are stored as 
    [0, 1], whereas in a directed list they would be stored as
    both [0, 1] and [1, 0].
    Args:
        mol (rdkit.Chem.rdchem.Mol): rdkit Mol 
    Returns:
        bond_list (list): undirected bond list
    """

    bond_list = []
    bonds = mol.GetBonds()

    for bond in bonds:

        start = bond.GetBeginAtomIdx()
        end = bond.GetEndAtomIdx()
        lower = min((start, end))
        upper = max((start, end))

        bond_list.append([lower, upper])

    return bond_list


def undirected_bond_atoms(mol):
    """
    Get a list of the atomic numbers comprising a bond
    in each bond of an undirected bond list.
    Args:
        mol (rdkit.Chem.rdchem.Mol): rdkit Mol 
    Returns:
        atom_num_list (list): list of the form [[num__00, num_01],
        [num_10, num_11], [num_20, num_21], ...], where the `num_ij`
        is the atomic number of atom `j` in bond `i`.
    """

    atom_num_list = []
    bonds = mol.GetBonds()

    for bond in bonds:

        start = bond.GetBeginAtom().GetAtomicNum()
        end = bond.GetEndAtom().GetAtomicNum()
        lower = min((start, end))
        upper = max((start, end))

        atom_num_list.append([lower, upper])

    return atom_num_list


def check_connectivity(mol_0, mol_1):
    """
    Check if the atom connectivity in two mol objects is the same.
    Args:
        mol_0 (rdkit.Chem.rdchem.Mol): first rdkit Mol 
        mol_1 (rdkit.Chem.rdchem.Mol): second rdkit Mol 
    Returns:
        same (bool): whether or not the connectivity is the same
    """

    bonds_0 = undirected_bond_atoms(mol_0)
    bonds_1 = undirected_bond_atoms(mol_1)
    same = (bonds_0 == bonds_1)

    return same


def verify_smiles(rd_mol, smiles):
    """
    Verify that an RDKit mol has the same smiles as the original smiles
    that made it.
    Args:
        rd_mol (rdkit.Chem.rdchem.Mol): rdkit Mol 
        smiles (str): claimed smiles
    Returns:
        None
    """

    # get the canonical smiles of each

    rd_smiles, new_rd_mol = smiles_from_mol(rd_mol)
    db_smiles, db_mol = smiles_from_smiles(smiles)

    # if they're the same then we're good

    if rd_smiles == db_smiles:
        return

    # try ignoring stereochemistry

    Chem.RemoveStereochemistry(new_rd_mol)
    rd_smiles, new_rd_mol = smiles_from_mol(new_rd_mol)

    Chem.RemoveStereochemistry(db_mol)
    db_smiles, db_mol = smiles_from_mol(db_mol)

    if rd_smiles == db_smiles:
        return

    # try checking bond connectivity

    good_con = check_connectivity(mol_0=new_rd_mol,
                                  mol_1=db_mol)

    if good_con:
        msg = (("WARNING: xyz2mol SMILES is {} "
                "and database SMILES is {}. "
                "However, the connectivity is the same. "
                "Check to make sure the SMILES are resonances "
                "structures.".format(rd_smiles, db_smiles)))
        return

    # otherwise raise an exception

    msg = (("SMILES created by xyz2mol is {}, "
            "which doesn't match the database "
            "SMILES {}.".format(rd_smiles, db_smiles)))
    raise Exception(msg)


def log_failure(bad_idx, i):
    """
    Log how many smiles have conformers that you've successfully converted 
    to RDKit mols.
    Args:
        bad_idx (list[int]): indices to get rid of in the dataset
        i (int): index of the smiles in the dataset
    Returns:
        None
    """

    if i == 0:
        return

    good_len = i - len(bad_idx)
    conv_pct = good_len / i * 100

    print(("Converted %d of %d "
           "species (%.2f%%)" % (
               good_len, i, conv_pct)))


def log_missing(missing_e):
    """
    Log any atom types that are missing from `xyz2mol` that cause
    conversion errors.
    Args:
        misisng_e (list[int]): atomic numbers of any atoms that caused
            exceptions
    Returns:
        None
    """
    if not missing_e:
        print("No elements are missing from xyz2mol")
    else:
        missing_e = list(set(missing_e))
        print("Elements {} are missing from xyz2mol".format(
            ", ".join(missing_e)))


def get_enum_func(track):
    """
    Get the enumerate function.
    Args:
        track (bool): whether to track progress with tqdm_enum
    Returns:
        func (callable): enumerate function that tracks progress with
            tqdm if track == True.
    """

    if track:
        func = tqdm_enum
    else:
        func = enumerate
    return func


def make_rd_mols(dataset,
                 verbose=True,
                 check_smiles=True,
                 track=True):

    num_atoms = dataset.props['num_atoms']
    mol_size = dataset.props.get("mol_size", num_atoms).tolist()
    smiles_list = dataset.props["smiles"]
    all_nxyz = dataset.props["nxyz"]
    charges = dataset.props["charge"]
    dataset.props["rd_mols"] = []

    all_mols = []
    bad_idx = []

    # for i, smiles in tqdm(enumerate(smiles_list)):

    enum = get_enum_func(track)

    for i, smiles in enum(smiles_list):

        num_confs = (num_atoms[i] // mol_size[i]).item()
        split_sizes = [mol_size[i]] * num_confs
        nxyz_list = torch.split(all_nxyz[i], split_sizes)
        charge = charges[i]

        spec_mols = []
        missing_e = []

        for j, nxyz in enumerate(nxyz_list):

            if i in bad_idx:
                continue

            xyz = nxyz[:, 1:].tolist()
            atoms = nxyz[:, 0].numpy().astype('int').tolist()

            try:

                mol = xyz2mol(atoms=atoms,
                              coordinates=xyz,
                              charge=charge,
                              use_graph=QUICK,
                              allow_charged_fragments=CHARGED_FRAGMENTS,
                              embed_chiral=EMBED_CHIRAL,
                              use_huckel=USE_HUCKEL)
                if check_smiles:
                    verify_smiles(rd_mol=mol, smiles=smiles)

            except Exception as e:

                # import pdb
                # print(e)
                # pdb.post_mortem()

                print(("xyz2mol failed "
                       "with error '{}' ".format(e)))
                print("Removing smiles {}".format(smiles))
                bad_idx.append(i)

                if verbose:
                    log_failure(bad_idx=bad_idx, i=i)

                if str(e).isdigit():
                    missing_e.append(int(str(e)))

                continue

            spec_mols.append(mol)

        all_mols.append(spec_mols)
        dataset.props["rd_mols"].append(spec_mols)

    remove_bad_idx(dataset=dataset,
                   smiles_list=smiles_list,
                   bad_idx=bad_idx,
                   verbose=verbose)

    if verbose:
        log_missing(missing_e)

    return dataset


def make_one_hot(options, result):

    index = options.index(result) if result in options else -1
    one_hot = torch.zeros(len(options) + 1)
    one_hot[index] = 1

    return one_hot


def bond_feat_to_vec(feat_type, feat):

    if feat_type == "conjugated":
        conj = feat
        result = torch.Tensor([conj])
        return result

    elif feat_type == "bond_type":
        options = BOND_OPTIONS
        bond_type = feat
        one_hot = make_one_hot(options=options,
                               result=bond_type)
        return one_hot

    elif feat_type == "in_ring_size":
        options = RING_SIZE
        ring_size = - 1
        for is_in_size, option in zip(feat, options):
            if is_in_size:
                ring_size = option
                break

        one_hot = make_one_hot(options=options,
                               result=ring_size)

        return one_hot

    elif feat_type == "in_ring":
        in_ring = feat
        result = torch.Tensor([in_ring])
        return result

    elif feat_type == "stereo":
        stereo = feat
        options = STEREO_OPTIONS
        one_hot = make_one_hot(options=options,
                               result=stereo)
        return one_hot


def get_bond_features(bond, feat_type):

    if feat_type == "conjugated":
        feat = bond.GetIsConjugated()

    elif feat_type == "bond_type":
        feat = bond.GetBondType().name.lower()

    elif feat_type == "in_ring_size":
        feat = [bond.IsInRingSize(option) for option in RING_SIZE]

    elif feat_type == "in_ring":
        feat = bond.IsInRing()

    elif feat_type == "stereo":
        feat = bond.GetStereo().name.lower()

    return bond_feat_to_vec(feat_type, feat)


def atom_feat_to_vec(feat_type, feat):

    if feat_type == "atom_type":
        options = AT_NUM
        one_hot = make_one_hot(options=options,
                               result=feat)

        return one_hot

    elif feat_type == "num_bonds":
        options = BONDS
        one_hot = make_one_hot(options=options,
                               result=feat)

        return one_hot

    elif feat_type == "formal_charge":

        options = FORMAL_CHARGES
        one_hot = make_one_hot(options=options,
                               result=feat)

        return one_hot

    elif feat_type == "chirality":
        options = CHIRAL_OPTIONS
        one_hot = make_one_hot(options=options,
                               result=feat)

        return one_hot

    elif feat_type == "num_bonded_h":

        options = NUM_H
        one_hot = make_one_hot(options=options,
                               result=feat)

        return one_hot

    elif feat_type == "hybrid":

        options = HYBRID_OPTIONS
        one_hot = make_one_hot(options=options,
                               result=feat)

        return one_hot

    elif feat_type == "aromaticity":
        one_hot = torch.Tensor([feat])

        return one_hot

    elif feat_type == "mass":
        result = torch.Tensor([feat / 100])

        return result


def get_atom_features(atom, feat_type):

    if feat_type == "atom_type":
        feat = atom.GetAtomicNum()

    elif feat_type == "num_bonds":
        feat = atom.GetTotalDegree()

    elif feat_type == "formal_charge":

        feat = atom.GetFormalCharge()

    elif feat_type == "chirality":
        feat = atom.GetChiralTag().name.lower()

    elif feat_type == "num_bonded_h":

        neighbors = [at.GetAtomicNum() for at
                     in atom.GetNeighbors()]
        feat = len([i for i in neighbors if
                    i == 1])

    elif feat_type == "hybrid":

        feat = atom.GetHybridization().name.lower()

    elif feat_type == "aromaticity":
        feat = atom.GetIsAromatic()

    elif feat_type == "mass":
        feat = atom.GetMass()

    return atom_feat_to_vec(feat_type=feat_type,
                            feat=feat)


def bond_feats_from_dic(dic_list, feat_types=BOND_FEAT_TYPES):

    key_map = {key: key for key in feat_types}
    key_map.update({"bond_type": "type", "in_ring_size": "ring_size"})

    bond_feats = []
    for bond_dic in dic_list:
        vec_list = []
        for feat_type in feat_types:
            if feat_type == "in_ring_size":
                feat = [False] * len(RING_SIZE)
                ring_size_idx = bond_dic[key_map[feat_type]]
                if ring_size_idx != -1:
                    feat[ring_size_idx] = True
            else:
                feat = bond_dic[key_map[feat_type]]
            vec = bond_feat_to_vec(feat_type, feat)
            vec_list.append(vec)

        feats = torch.cat(vec_list)
        bond_feats.append(feats)

    return torch.stack(bond_feats)


def atom_feats_from_dic(dic_list, feat_types=ATOM_FEAT_TYPES):

    key_map = {key: key for key in feat_types}
    key_map.update({"hybrid": "hybridization",
                    "aromaticity": "aromatic"})

    atom_feats = []
    for atom_dic in dic_list:
        vec_list = []
        for feat_type in feat_types:
            feat = atom_dic[key_map[feat_type]]
            vec = atom_feat_to_vec(feat_type, feat)
            vec_list.append(vec)

        feats = torch.cat(vec_list)
        atom_feats.append(feats)

    return torch.stack(atom_feats)


def nbr_list_from_dic(dic_list):
    nbr_list = []
    for bond_dic in dic_list:
        nbr_list.append(bond_dic["indices"])

    return nbr_list


def get_all_bond_feats(bond, feat_types):

    feat_dic = {}

    for feat_type in feat_types:
        feature = get_bond_features(bond=bond,
                                    feat_type=feat_type)
        feat_dic[feat_type] = feature

    return feat_dic


def get_all_atom_feats(atom, feat_types):

    feat_dic = {}

    for feat_type in feat_types:
        feature = get_atom_features(atom=atom,
                                    feat_type=feat_type)
        feat_dic[feat_type] = feature

    return feat_dic


def featurize_bonds(dataset,
                    feat_types=BOND_FEAT_TYPES,
                    track=True):

    props = dataset.props

    props["bond_list"] = []
    props["bond_features"] = []
    props["num_bonds"] = []

    num_atoms = dataset.props['num_atoms']
    mol_size = dataset.props.get("mol_size", num_atoms).tolist()
    enum = get_enum_func(track)
    # for i, rd_mols in enum(dataset.props["rd_mols"], track):

    for i, rd_mols in enum(dataset.props["rd_mols"]):

        num_confs = (num_atoms[i] // mol_size[i]).item()
        split_sizes = [mol_size[i]] * num_confs

        props["bond_list"].append([])
        props["num_bonds"].append([])

        all_props = []

        for j, rd_mol in enumerate(rd_mols):

            bonds = rd_mol.GetBonds()
            bond_list = []

            for bond in bonds:

                all_props.append(torch.tensor([]))

                start = bond.GetBeginAtomIdx()
                end = bond.GetEndAtomIdx()
                lower = min((start, end))
                upper = max((start, end))

                bond_list.append([lower, upper])
                feat_dic = get_all_bond_feats(bond=bond,
                                              feat_types=feat_types)

                for key, feat in feat_dic.items():
                    all_props[-1] = torch.cat((all_props[-1], feat))

            other_atoms = sum(split_sizes[:j])
            shifted_bond_list = np.array(bond_list) + other_atoms

            props["bond_list"][-1].append(torch.LongTensor(
                shifted_bond_list))
            props["num_bonds"][-1].append(len(bonds))

        props["num_bonds"][-1] = torch.LongTensor(props["num_bonds"][-1])
        props["bond_list"][-1] = torch.cat(props["bond_list"][-1])
        props["bond_features"].append(torch.stack(all_props))

    return dataset


def featurize_atoms(dataset,
                    feat_types=ATOM_FEAT_TYPES,
                    track=True):

    props = dataset.props
    props["atom_features"] = []

    enum = get_enum_func(track)

    for i, rd_mols in enum(dataset.props["rd_mols"]):

        all_props = []
        for rd_mol in rd_mols:
            atoms = rd_mol.GetAtoms()

            for atom in atoms:
                all_props.append(torch.tensor([]))
                feat_dic = get_all_atom_feats(atom=atom,
                                              feat_types=feat_types)

                for key, feat in feat_dic.items():
                    all_props[-1] = torch.cat((all_props[-1], feat))

        props["atom_features"].append(torch.stack(all_props))

    return dataset


def decode_one_hot(options, vector):

    if options == [bool]:
        return bool(vector.item())
    elif options == [float]:
        return vector.item()

    index = vector.nonzero()
    if len(index) == 0 or index >= len(options):
        result = None
    else:
        result = options[index]

    return result


def decode_atomic(features, meta_data=META_DATA):

    feat_names = meta_data["atom_features"]
    details = meta_data["details"]
    indices = [details[feat]["num"] for feat in feat_names]
    options_list = [details[feat]["options"] for feat in feat_names]

    vectors = torch.split(features, indices)

    dic = {}
    for i, vector in enumerate(vectors):
        options = options_list[i]
        name = feat_names[i]

        result = decode_one_hot(options=options,
                                vector=vector)
        dic[name] = result
        if name == "mass":
            dic[name] *= 100

    return dic


def decode_bond(features, meta_data=META_DATA):

    feat_names = meta_data["bond_features"]
    details = meta_data["details"]
    indices = [details[feat]["num"] for feat in feat_names]
    options_list = [details[feat]["options"] for feat in feat_names]

    vectors = torch.split(features, indices)

    dic = {}

    for i, vector in enumerate(vectors):
        options = options_list[i]
        name = feat_names[i]

        result = decode_one_hot(options=options,
                                vector=vector)
        dic[name] = result

    return dic


def featurize_dataset(dataset,
                      bond_feats=BOND_FEAT_TYPES,
                      atom_feats=ATOM_FEAT_TYPES):

    print("Converting xyz to RDKit mols...")
    dataset = make_rd_mols(dataset)
    print("Completed conversion to RDKit mols.")

    print("Featurizing bonds...")
    dataset = featurize_bonds(dataset, feat_types=bond_feats)
    print("Completed featurizing bonds.")

    print("Featurizing atoms...")
    dataset = featurize_atoms(dataset, feat_types=atom_feats)
    print("Completed featurizing atoms.")

    props = dataset.props
    props.pop("rd_mols")
    props["bonded_nbr_list"] = copy.deepcopy(props["bond_list"])
    props.pop("bond_list")


def add_morgan(dataset, vec_length):

    dataset.props["morgan"] = []
    for smiles in dataset.props['smiles']:
        mol = Chem.MolFromSmiles(smiles)
        if vec_length != 0:
            morgan = AllChem.GetMorganFingerprintAsBitVect(
                mol, radius=2, nBits=vec_length)
        else:
            morgan = []

        # shouldn't be a long tensor if we're going
        # to apply an NN to it

        arr_morgan = np.array(list(morgan)).astype('float32')
        morgan_tens = torch.tensor(arr_morgan)
        dataset.props["morgan"].append(morgan_tens)


def add_e3fp(rd_dataset, fp_length, verbose=False, track=True):

    # disable verbose logging from e3fp

    if not verbose:
        logger = logging.getLogger()
        logger.disabled = True

    e3fp_list = []
    enum = get_enum_func(track)

    for i, batch in enum(rd_dataset):

        mols = batch["rd_mols"]
        smiles = batch["smiles"]

        fps = []

        for mol in mols:

            mol.SetProp("_Name", smiles)
            fprint_params = {"bits": fp_length}
            fp = fprints_from_mol(mol, fprint_params=fprint_params)
            fp_array = np.zeros(len(fp[0]))
            indices = fp[0].indices
            fp_array[indices] = 1

            fps.append(torch.Tensor(fp_array))

        e3fp_list.append(torch.stack(fps))

    rd_dataset.props['e3fp'] = e3fp_list

    return rd_dataset
