import pickle
import json
import os
import torch
import numpy as np
import argparse
from tqdm import tqdm
from rdkit import Chem
import logging

from nff.data import Dataset, concatenate_dict
from nff.utils import tqdm_enum, parse_args, fprint
import copy


KEY_MAP = {"rd_mol": "nxyz",
           "boltzmannweight": "weights",
           "relativeenergy": "energy"}

# these are keys that confuse the dataset
EXCLUDE_KEYS = ["totalconfs", "datasets", "conformerweights",
                "uncleaned_smiles"]

# disable logger to avoid annoying pickle messages
logger = logging.getLogger()
logger.disabled = True


def mol_to_smiles(rd_mol):
    """
    Get the canonical SMILES from an RDKit mol.
    Args:
        rd_mol (rdkit.Chem.rdchem.Mol): rdkit Mol
    Returns:
        smiles (str): canonical smiles
    """
    smiles = Chem.MolToSmiles(rd_mol)
    new_mol = Chem.MolFromSmiles(smiles)
    smiles = Chem.MolToSmiles(new_mol)

    return smiles


def trim_dset(dset, good_idx):
    """
    """
    for key, val in dset.props.items():
        if type(val) is list:
            dset.props[key] = [val[i] for i in good_idx]
        else:
            dset.props[key] = val[good_idx]
    return dset


def filter_same_smiles(dset):
    """
    Filter out species whose conformers don't all have the same SMILES. Can happen
    because, for example, CREST simulations can be reactive. This won't happen if
    conformers are generated using RDKit.
    """

    good_idx = []

    for i, batch in tqdm_enum(dset):
        rd_mols = batch["rd_mols"]
        smiles_list = [mol_to_smiles(mol) for mol in rd_mols]
        unique_smiles = list(set(smiles_list))
        if len(unique_smiles) == 1:
            good_idx.append(i)

    dset = trim_dset(dset, good_idx)

    return dset


def filter_bonds_in_nbr(cutoff, dset):
    """
    Filter out conformers whose bonds are not within the cutoff distance
    that defines the neighbor list. CP3D can't use these conformers because
    there will be bonds that don't have distance features, as the two atoms are
    not within each other's cutoff. Any conformer with bonds > 5 A is probably
    not too accurate anyway.
    """

    good_idx = []

    for i, batch in tqdm_enum(dset):
        bond_list = batch["bonded_nbr_list"]
        nxyz = batch["nxyz"]
        bond_lens = (nxyz[:, 1:][bond_list[:, 0]] -
                     nxyz[:, 1:][bond_list[:, 1]]).norm(dim=1)
        valid = (bond_lens < cutoff).all()
        if valid:
            good_idx.append(i)

    dset = trim_dset(dset, good_idx)

    return dset


def get_thread_dic(sample_dic, thread, num_threads):

    keys = np.array(sorted(list(
        sample_dic.keys())))
    split_keys = np.array_split(keys, num_threads)
    thread_keys = split_keys[thread]

    sample_dic = {key: sample_dic[key]
                  for key in thread_keys}

    return sample_dic


def get_splits(sample_dic,
               csv_folder):

    for name in ["train", "val", "test"]:
        path = os.path.join(csv_folder, f"{name}_smiles.csv")
        with open(path, "r") as f:
            lines = f.readlines()
        smiles_list = [i.split(",")[0].strip() for i in lines[1:]]
        for smiles in smiles_list:
            sample_dic[smiles].update({"split": name})

    keys = list(sample_dic.keys())
    for key in keys:
        if "split" not in sample_dic[key]:
            sample_dic.pop(key)

    return sample_dic


def resave_splits(csv_folder,
                  dset):
    """
    Re-save the SMILES splits accounting for the fact that not all
    species made it into this dataset
    """

    # create a dictionary to quickly see if a SMILES is in the dataset,
    # rather than having to loop over the entire thing every time we
    # want to see if a SMILES string is present

    dset_smiles = {smiles: i for i,
                   smiles in enumerate(dset.props["smiles"])}

    # new number of SMILES
    new_num = len(dset_smiles)
    # old number of smiles
    old_num = 0

    split_names = ["train", "val", "test"]
    suffixes = ["smiles", "full"]

    for name in split_names:
        for suffix in suffixes:

            path = os.path.join(csv_folder, f"{name}_{suffix}.csv")
            with open(path, "r") as f:
                lines = f.readlines()

            keep_lines = [lines[0]]
            old_num += len(lines) - 1

            for line in lines[1:]:
                smiles = line.split(",")[0].strip()
                if smiles in dset_smiles:
                    keep_lines.append(line)

            new_text = "".join(keep_lines)
            with open(path, "w") as f:
                f.write(new_text)

    return old_num, new_num


def get_sample(summary_dic,
               csv_folder,
               thread=None,
               num_threads=None):

    sample_dic = copy.deepcopy(summary_dic)

    # generate train/val/test labels

    sample_dic = get_splits(sample_dic=sample_dic,
                            csv_folder=csv_folder)

    if thread is not None:
        sample_dic = get_thread_dic(sample_dic=sample_dic,
                                    thread=thread,
                                    num_threads=num_threads)

    return sample_dic


def load_data_from_pickle(sample_dic, pickle_folder):

    overall_dic = {}
    keys = list(sample_dic.keys())

    for smiles in tqdm(keys):
        sub_dic = sample_dic[smiles]

        pickle_path = sub_dic["pickle_path"]
        full_path = os.path.join(pickle_folder, pickle_path)
        with open(full_path, "rb") as f:
            dic = pickle.load(f)
        overall_dic.update({smiles: dic})

    return overall_dic


def map_key(key):
    if key in KEY_MAP:
        return KEY_MAP[key]
    else:
        return key


def fix_iters(spec_dic, actual_confs):
    new_spec_dic = {}
    for key, val in spec_dic.items():
        if key in EXCLUDE_KEYS:
            continue
        elif type(val) in [int, float, str]:
            new_spec_dic[key] = [val] * actual_confs
        else:
            new_spec_dic[key] = val

    return new_spec_dic


def get_sorted_idx(sub_dic):

    confs = sub_dic["conformers"]
    weight_list = []
    for i, conf in enumerate(confs):
        weight_list.append([i, conf["boltzmannweight"]])
    sorted_tuples = sorted(weight_list, key=lambda x: -x[-1])
    sorted_idx = [i[0] for i in sorted_tuples]

    return sorted_idx


def get_xyz(rd_mol):

    atoms = rd_mol.GetAtoms()

    atom_nums = []
    for atom in atoms:
        atom_nums.append(atom.GetAtomicNum())

    rd_conf = rd_mol.GetConformers()[0]
    positions = rd_conf.GetPositions()

    xyz = []
    for atom_num, position in zip(atom_nums, positions):
        xyz.append([atom_num, *position])

    return xyz


def renorm_weights(spec_dic):

    new_weights = np.array(spec_dic["weights"]) / sum(spec_dic["weights"])
    spec_dic["weights"] = new_weights.tolist()

    return spec_dic


def convert_data(overall_dic, max_confs):

    spec_dics = []
    if max_confs is None:
        max_confs = float("inf")

    for key in tqdm(overall_dic.keys()):
        sub_dic = overall_dic[key]
        spec_dic = {map_key(key): val for key, val in sub_dic.items()
                    if key != "conformers"}

        actual_confs = min(max_confs, len(sub_dic["conformers"]))
        spec_dic = fix_iters(spec_dic, actual_confs)

        spec_dic.update({map_key(key): [] for key
                         in sub_dic["conformers"][0].keys()
                         if key not in EXCLUDE_KEYS})

        # conformers not always ordered by weight
        sorted_idx = get_sorted_idx(sub_dic)
        confs = sub_dic["conformers"]
        spec_dic["rd_mols"] = []

        for idx in sorted_idx[:actual_confs]:
            conf = confs[idx]
            for key in conf.keys():
                if key == "rd_mol":

                    nxyz = get_xyz(conf[key])
                    spec_dic["nxyz"].append(nxyz)
                    spec_dic["rd_mols"].append(conf[key])

                else:
                    new_key = map_key(key)
                    if new_key not in spec_dic:
                        continue
                    spec_dic[new_key].append(conf[key])

        spec_dic = renorm_weights(spec_dic)
        spec_dics.append(spec_dic)

    return spec_dics


def add_missing(props_list):

    key_list = [list(props.keys()) for props in props_list]
    # dictionary of the props that have each set of keys
    key_dic = {}
    for i, keys in enumerate(key_list):
        for key in keys:
            if key not in key_dic:
                key_dic[key] = []
            key_dic[key].append(i)

    # all the possible keys
    all_keys = []
    for keys in key_list:
        all_keys += keys
    all_keys = list(set(all_keys))

    # dictionary of which props dicts are missing certain keys

    missing_dic = {}
    prop_idx = list(range(len(props_list)))
    for key in all_keys:
        missing_dic[key] = [i for i in prop_idx if
                            i not in key_dic[key]]

    for key, missing_idx in missing_dic.items():
        for i in missing_idx:

            props = props_list[i]
            given_idx = key_dic[key][0]
            given_props = props_list[given_idx]
            given_val = given_props[key]

            if type(given_val) is list:
                props[key] = [None]
            elif type(given_val) is torch.Tensor:
                props[key] = torch.Tensor([np.nan])
                # in this case we need to change the
                # other props to have type float
                for good_idx in key_dic[key]:
                    other_props = props_list[good_idx]
                    other_props[key] = other_props[key].to(torch.float)
                    props_list[good_idx] = other_props

            props_list[i] = props

    return props_list


def clean_up_dset(dset,
                  nbr_list,
                  rd_mols_list,
                  nbrlist_cutoff,
                  strict_conformers,
                  csv_folder):
    """
    Do various things to clean up the dataset after you've made it
    """

    for i in tqdm(range(3)):

        # if requested, get rid of any species whose conformers have different
        # SMILES strings
        if i == 1:
            if strict_conformers:
                dset = filter_same_smiles(dset)

        elif i == 2:
            # Get rid of any conformers whose bond lists aren't subsets of the
            # neighbor list
            dset = filter_bonds_in_nbr(nbrlist_cutoff, dset)

        elif i == 3:
            # Add the indices of the neighbor list that correspond to
            # bonded atoms
            dset.generate_bond_idx()

    # Re-save the train/val/test splits accounting for the fact that some
    # species are no longer there

    old_num, new_num = resave_splits(csv_folder=csv_folder,
                                     dset=dset)

    changed_num = old_num != new_num
    if changed_num:
        msg = ("WARNING: the original SMILES splits have been re-saved with "
               f"{new_num} species, reduced from the original {old_num}, "
               f"because only {new_num} species made it into the final "
               "dataset. This could be because of conformers with bond "
                "lengths greater than the cutoff distance of %.2f"
               ) % nbrlist_cutoff

        if strict_conformers:
            msg += (", or because the conformers of certain species didn't "
                    "all have the same SMILES string")
        msg += "."

        fprint(msg)

    return dset


def make_nff_dataset(spec_dics,
                     nbrlist_cutoff,
                     parallel_feat_threads,
                     strict_conformers,
                     csv_folder):

    fprint("Making dataset with %d species" % (len(spec_dics)))

    props_list = []
    nbr_list = []
    rd_mols_list = []

    for j, spec_dic in tqdm_enum(spec_dics):

        # Treat each species' data like a regular dataset
        # and use it to generate neighbor lists
        # Ignore the graph features because there's only one
        # per species right now.

        conf_keys = ["rd_mols", "bonded_nbr_list", "bond_features",
                     "atom_features"]

        # Exclude keys related to individual conformers. These
        # include conformer features, in case you've already put
        # those in your pickle files. If not we'll generate them
        # below

        small_spec_dic = {key: val for key, val in spec_dic.items()
                          if key not in conf_keys}

        dataset = Dataset(small_spec_dic, units='kcal/mol')
        mol_size = len(dataset.props["nxyz"][0])

        dataset.generate_neighbor_list(cutoff=nbrlist_cutoff)

        # now combine the neighbor lists so that this set
        # of nxyz's can be treated like one big molecule

        nbrs = dataset.props['nbr_list']
        # number of atoms in the molecule
        new_nbrs = []

        # shift by i * mol_size for each conformer
        for i in range(len(nbrs)):
            new_nbrs.append(nbrs[i] + i * mol_size)

        # add to list of conglomerated neighbor lists
        nbr_list.append(torch.cat(new_nbrs))
        dataset.props.pop('nbr_list')

        # concatenate the nxyz's
        nxyz = np.concatenate([np.array(item) for item in spec_dic["nxyz"]]
                              ).reshape(-1, 4).tolist()

        # add properties as necessary
        new_dic = {"mol_size": mol_size,
                   "nxyz": nxyz,
                   "weights": torch.Tensor(spec_dic["weights"]
                                           ).reshape(-1, 1) / sum(
                       spec_dic["weights"]),
                   "degeneracy": torch.Tensor(spec_dic["degeneracy"]
                                              ).reshape(-1, 1),
                   "energy": torch.Tensor(spec_dic["energy"]
                                          ).reshape(-1, 1),
                   "num_atoms": [len(nxyz)]}

        new_dic.update({key: val[:1] for key, val in dataset.props.items(
        ) if key not in new_dic.keys()})

        props_list.append(new_dic)
        rd_mols_list.append(spec_dic["rd_mols"])

    # Add props that are in some datasets but not others
    props_list = add_missing(props_list)
    props_dic = concatenate_dict(*props_list)
    # make a combined dataset where the species look like they're
    # one big molecule
    big_dataset = Dataset(props_dic.copy(), units='kcal/mol')
    # give it the proper neighbor list and rdkit mols
    big_dataset.props['nbr_list'] = nbr_list
    big_dataset.props["rd_mols"] = rd_mols_list

    # generate features
    big_dataset.featurize(num_procs=parallel_feat_threads)

    # clean up
    fprint("Cleaning up dataset...")
    big_dataset = clean_up_dset(dset=big_dataset,
                                nbr_list=nbr_list,
                                rd_mols_list=rd_mols_list,
                                nbrlist_cutoff=nbrlist_cutoff,
                                strict_conformers=strict_conformers,
                                csv_folder=csv_folder)

    fprint("Adding E3FP fingerprints...")
    big_dataset.add_e3fp(256, num_procs=parallel_feat_threads)
    fprint("Adding whim fingerprints...")
    big_dataset.featurize_rdkit('whim')
    fprint("Adding Morgan fingerprints...")
    big_dataset.add_morgan(256)

    ########
    # must modify the smiles csvs that we started with so that cp3d and
    # chemprop won't train on different species. Also note this in the docs
    ########

    fprint("Complete!")

    return big_dataset


def get_data_folder(dataset_folder, thread):
    if thread is None:
        return dataset_folder
    new_path = os.path.join(dataset_folder, str(thread))
    if not os.path.isdir(new_path):
        os.makedirs(new_path)
    return new_path


def split_dataset(dataset, idx):
    new_dataset = copy.deepcopy(dataset)
    new_props = {}
    for key, val in dataset.props.items():
        if type(val) is list:
            new_props[key] = [val[i] for i in idx]
        else:
            new_props[key] = val[idx]
    new_dataset.props = new_props
    return new_dataset


def save_splits(dataset,
                dataset_folder,
                thread,
                sample_dic):

    split_names = ["train", "val", "test"]
    split_idx = {name: [] for name in split_names}
    split_dic = {name: [] for name in split_names}

    for i, smiles in enumerate(dataset.props['smiles']):
        split_name = sample_dic[smiles]["split"]
        split_idx[split_name].append(i)

    for name in split_names:
        split_dic[name] = split_dataset(dataset, split_idx[name])

    train = split_dic["train"]
    val = split_dic["val"]
    test = split_dic["test"]

    fprint("Saving...")
    data_folder = get_data_folder(dataset_folder, thread)
    names = ["train", "val", "test"]

    for name, dset in zip(names, [train, val, test]):
        dset_path = os.path.join(data_folder, name + ".pth.tar")
        dset.save(dset_path)


def main(max_confs,
         summary_path,
         dataset_folder,
         pickle_folder,
         num_threads,
         thread,
         nbrlist_cutoff,
         csv_folder,
         parallel_feat_threads,
         strict_conformers,
         ** kwargs):

    with open(summary_path, "r") as f:
        summary_dic = json.load(f)

    fprint("Loading splits...")

    sample_dic = get_sample(summary_dic=summary_dic,
                            thread=thread,
                            num_threads=num_threads,
                            csv_folder=csv_folder)

    fprint("Loading data from pickle files...")
    overall_dic = load_data_from_pickle(sample_dic, pickle_folder)

    fprint("Converting data...")
    spec_dics = convert_data(overall_dic, max_confs)

    fprint("Combining to make NFF dataset...")
    dataset = make_nff_dataset(spec_dics=spec_dics,
                               nbrlist_cutoff=nbrlist_cutoff,
                               parallel_feat_threads=parallel_feat_threads,
                               strict_conformers=strict_conformers,
                               csv_folder=csv_folder)

    fprint("Creating test/train/val splits...")
    save_splits(dataset=dataset,
                dataset_folder=dataset_folder,
                thread=thread,
                sample_dic=sample_dic)

    fprint("Complete!")


if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    parser.add_argument('--max_confs', type=int, default=None,
                        help=("Maximum number of conformers to allow in any "
                              "species in your dataset. No limit if "
                              "max_confs isn't specified."))

    parser.add_argument('--nbrlist_cutoff', type=float, default=5,
                        help=("Cutoff for 3D neighbor list"))

    parser.add_argument('--summary_path', type=str)
    parser.add_argument('--dataset_folder', type=str)
    parser.add_argument('--pickle_folder', type=str)
    parser.add_argument('--num_threads', type=int, default=None)
    parser.add_argument('--thread', type=int, default=None)
    parser.add_argument('--prop', type=str, default=None,
                        help=("Name of property for which to generate "
                              "a proportional classification sample"))
    parser.add_argument('--csv_folder', type=str,
                        help=("Name of the folder in which "
                              "you want to save the SMILES "
                              "splits"))
    parser.add_argument('--parallel_feat_threads', type=int,
                        default=5,
                        help=("Number of parallel threads to use "
                              "when generating features"))
    parser.add_argument('--strict_conformers', action='store_true',
                        help=("Exclude any species whose conformers don't "
                              "all have the same SMILES."))
    parser.add_argument('--config_file', type=str,
                        help=("Path to JSON file with arguments. If given, "
                              "any arguments in the file override the command "
                              "line arguments."))

    args = parse_args(parser)
    main(**args.__dict__)
