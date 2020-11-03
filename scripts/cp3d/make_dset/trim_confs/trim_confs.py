"""
Script to copy a reference dataset into a new dataset
with fewer conformers per species.
"""


import torch
import os
import argparse
import json
import pdb
from tqdm import tqdm

from nff.data import Dataset
from nff.utils import fprint, tqdm_enum


def assert_ordered(batch):
    """
    Make sure the conformers are ordered by weight.
    Args:
        batch (dict): dictionary of properties for one
            species.
    Returns:
        None
    """

    weights = batch["weights"].reshape(-1).tolist()
    sort_weights = sorted(weights,
                          key=lambda x: -x)
    assert weights == sort_weights


def get_batch_dic(batch,
                  idx_dic,
                  num_confs):
    """
    Get some conformer information about the batch.
    Args:
        batch (dict): Properties of the species in this batch
        idx_dic (dict): Dicationary of the form
            {smiles: indices}, where `indices` are the indices
            of the conformers you want to keep. If not specified,
            then the top `num_confs` conformers with the highest
            statistical weight will be used.
        num_confs (int): Number of conformers to keep
    Returns:
        info_dic (dict): Dictionary with extra conformer 
            information about the batch

    """

    mol_size = batch["mol_size"]
    old_num_atoms = batch["num_atoms"]

    # number of conformers in the batch is number of atoms / atoms per mol
    confs_in_batch = old_num_atoms // mol_size

    # new number of atoms after trimming
    new_num_atoms = int(mol_size * min(
        confs_in_batch, num_confs))

    if idx_dic is None:

        assert_ordered(batch)
        # new number of conformers after trimming
        real_num_confs = min(confs_in_batch, num_confs)
        conf_idx = list(range(real_num_confs))

    else:
        smiles = batch["smiles"]
        conf_idx = idx_dic[smiles]
        real_num_confs = len(conf_idx)

    info_dic = {"conf_idx": conf_idx,
                "real_num_confs": real_num_confs,
                "old_num_atoms": old_num_atoms,
                "new_num_atoms": new_num_atoms,
                "confs_in_batch": confs_in_batch,
                "mol_size": mol_size}

    return info_dic


def to_xyz_idx(batch_dic):
    """
    Get the indices of the nxyz corresponding to atoms in conformers
    we want to keep.
    Args:
        batch_dic (dict): Dictionary with extra conformer 
            information about the batch
    Returns:
        xyz_conf_all_idx (torch.LongTensor): nxyz indices of atoms
            in the conformers we want to keep.
    """

    confs_in_batch = batch_dic["confs_in_batch"]
    mol_size = batch_dic["mol_size"]
    # Indices of the conformers we're keeping (usually of the form [0, 1, ...
    # `max_confs`], unless specified otherwise)
    conf_idx = batch_dic["conf_idx"]

    # the xyz indices of where each conformer starts
    xyz_conf_start_idx = [i * mol_size for i in range(confs_in_batch + 1)]
    # a list of the full set of indices for each conformer
    xyz_conf_all_idx = []

    # go through each conformer index, get the corresponding xyz indices,
    # and append them to xyz_conf_all_idx

    for conf_num in conf_idx:

        start_idx = xyz_conf_start_idx[conf_num]
        end_idx = xyz_conf_start_idx[conf_num + 1]
        full_idx = torch.arange(start_idx, end_idx)

        xyz_conf_all_idx.append(full_idx)

    # concatenate into tensor
    xyz_conf_all_idx = torch.cat(xyz_conf_all_idx)

    return xyz_conf_all_idx


def split_nbrs(nbrs,
               mol_size,
               confs_in_batch,
               conf_idx):
    """
    Get the indices of the neighbor list that correspond to conformers 
    we're keeping.
    Args:
        nbrs (torch.LongTensor): neighbor list
        mol_size (int): Number of atoms in each conformer
        confs_in_batch (int): Total number of conformers in the batch
        conf_idx (list[int]): Indices of the conformers we're keeping 
    Returns:
        tens_idx (torch.LongTensor): nbr indices of conformers we're
            keeping.
    """

    split_idx = []
    # The cutoff of the atom indices for each conformer
    cutoffs = [i * mol_size - 1 for i in range(1, confs_in_batch + 1)]

    for i in conf_idx:

        # start index of the conformer
        start = cutoffs[i] - mol_size + 1
        # end index of the conformer
        end = cutoffs[i]

        # mask so that all elements of the neighbor list are
        # >= start and <= end
        mask = (nbrs[:, 0] <= end) * (nbrs[:, 1] <= end)
        mask *= (nbrs[:, 0] >= start) * (nbrs[:, 1] >= start)

        idx = mask.nonzero().reshape(-1)
        split_idx.append(idx)

    tens_idx = torch.cat(split_idx)

    return tens_idx


def to_nbr_idx(batch_dic, nbrs):
    """
    Apply `split_nbrs` given `batch_dic`
    Args:
        batch_dic (dict): Dictionary with extra conformer 
            information about the batch
        nbrs (torch.LongTensor): neighbor list
    Returns:
        split_nbr_idx (torch.LongTensor): nbr indices of conformers we're
            keeping. 
    """

    mol_size = batch_dic["mol_size"]
    confs_in_batch = batch_dic["confs_in_batch"]
    conf_idx = batch_dic["conf_idx"]

    split_nbr_idx = split_nbrs(nbrs=nbrs,
                               mol_size=mol_size,
                               confs_in_batch=confs_in_batch,
                               conf_idx=conf_idx)

    return split_nbr_idx


def update_weights(batch, batch_dic):
    """
    Readjust weights so they sum to 1.
    Args:
        batch_dic (dict): Dictionary with extra conformer 
            information about the batch
        batch (dict): Batch dictionary
    Returns:
        new_weights (torch.Tensor): renormalized weights
    """

    old_weights = batch["weights"]

    conf_idx = torch.LongTensor(batch_dic["conf_idx"])
    new_weights = old_weights[conf_idx]
    new_weights /= new_weights.sum()
    if torch.isnan(new_weights):
        new_weights = torch.ones_like(old_weights[conf_idx])
        new_weights /= new_weights.sum()
    return new_weights


def convert_nbrs(batch_dic, nbrs, nbr_idx):
    """
    Convert neighbor list to only include neighbors from the conformers
    we're looking at.
    Args:
        batch_dic (dict): Dictionary with extra conformer 
            information about the batch
        nbrs (torch.LongTensor): neighbor list
        nbr_idx (torch.LongTensor): nbr indices of conformers we're
            keeping. 
    Returns:
        new_nbrs (torch.LongTensor): updated neighbor list

    """

    conf_idx = batch_dic["conf_idx"]
    mol_size = batch_dic["mol_size"]
    new_nbrs = []

    for i in range(len(conf_idx)):
        conf_id = conf_idx[i]
        delta = -conf_id * mol_size + i * mol_size
        new_nbrs.append(nbrs[nbr_idx] + delta)

    new_nbrs = torch.cat(new_nbrs)

    return new_nbrs


def update_dset(batch, batch_dic, dataset, i):
    """
    Update the dataset with the new values for the requested
    number of conformers, for species at index i.
    Args:
        batch (dict): Batch dictionary
        batch_dic (dict): Dictionary with extra conformer 
            information about the batch
        dataset (nff.data.dataset): NFF dataset
        i (int): index of the species whose info we're updating
    Returns:
        dataset (nff.data.dataset): updated NFF dataset
    """

    bond_nbrs = batch["bonded_nbr_list"]
    nbr_list = batch["nbr_list"]
    bond_feats = batch["bond_features"]
    atom_feats = batch["atom_features"]
    nxyz = batch["nxyz"]

    # get the indices of the nyxz, nbr list, and bonded
    # nbr list that are contained within the requested number
    # of conformers

    conf_xyz_idx = to_xyz_idx(batch_dic)
    bond_nbr_idx = to_nbr_idx(batch_dic, bond_nbrs)
    all_nbr_idx = to_nbr_idx(batch_dic, nbr_list)

    # change the number of atoms to the proper value
    dataset.props["num_atoms"][i] = batch_dic["new_num_atoms"]
    # get the right nxyz
    dataset.props["nxyz"][i] = nxyz[conf_xyz_idx]

    # convert the neighbor lists
    dataset.props["bonded_nbr_list"][i] = convert_nbrs(batch_dic,
                                                       bond_nbrs,
                                                       bond_nbr_idx)

    dataset.props["nbr_list"][i] = convert_nbrs(batch_dic,
                                                nbr_list,
                                                all_nbr_idx)

    # get the atom and bond features at the right indices
    dataset.props["bond_features"][i] = bond_feats[bond_nbr_idx]
    dataset.props["atom_features"][i] = atom_feats[conf_xyz_idx]

    # renormalize weights
    dataset.props["weights"][i] = update_weights(batch,
                                                 batch_dic)

    return dataset


def trim_confs(dataset, num_confs, idx_dic):
    """
    Trim conformers for the entire dataset.
    Args:
        dataset (nff.data.dataset): NFF dataset
        num_confs (int): desired number of conformers
        idx_dic (dict): Dicationary of the form
            {smiles: indices}, where `indices` are the indices
            of the conformers you want to keep. If not specified,
            then the top `num_confs` conformers with the highest
            statistical weight will be used.
    Returns:
        dataset (nff.data.dataset): updated NFF dataset
    """

    for i, batch in tqdm_enum(dataset):

        batch_dic = get_batch_dic(batch=batch,
                                  idx_dic=idx_dic,
                                  num_confs=num_confs)

        dataset = update_dset(batch=batch,
                              batch_dic=batch_dic,
                              dataset=dataset,
                              i=i)

    return dataset


def main(from_model_path,
         to_model_path,
         num_confs,
         conf_file,
         **kwargs):
    """
    Load the dataset, reduce the number of conformers, and save it.
    Args:
        from_model_path (str): The path to the folder in which
            the old dataset is saved.
        to_model_path (str): The path to the folder in which
            the new dataset will be saved.
        num_confs (int): Desired number of conformers per species
        conf_file (str): Path to the JSON file that tells you which
            conformer indices to use for each species.
    Returns:
        None
    """

    # load `conf_file` if given

    if conf_file is not None:
        with open(conf_file, "r") as f:
            idx_dic = json.load(f)
    else:
        idx_dic = None

    # If the folder has sub_folders 0, 1, ..., etc.,
    # then load each dataset in each sub-folder. Otherwise
    # the dataset must be in the main folder.

    folders = sorted([i for i in os.listdir(from_model_path)
                      if i.isdigit()], key=lambda x: int(x))

    if folders == []:
        folders = [""]

    # Go through each dataset, update it, and save it

    for folder in tqdm(folders):

        fprint(folder)
        for name in ["train.pth.tar", "test.pth.tar", "val.pth.tar"]:
            load_path = os.path.join(from_model_path, folder, name)
            if not os.path.isfile(load_path):
                continue
            dataset = Dataset.from_file(load_path)
            dataset = trim_confs(dataset=dataset,
                                 num_confs=num_confs,
                                 idx_dic=idx_dic)

            save_folder = os.path.join(to_model_path, folder)
            if not os.path.isdir(save_folder):
                os.makedirs(save_folder)
            save_path = os.path.join(save_folder, name)
            dataset.save(save_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--from_model_path', type=str,
                        help="Path to model from which original data comes")
    parser.add_argument('--to_model_path', type=str,
                        help="Path to model to which new data is saved")
    parser.add_argument('--num_confs', type=int,
                        help="Number of conformers per species",
                        default=1)
    parser.add_argument('--conf_file', type=str,
                        help=("Path to json that says which conformer "
                              "to use for each species. This is optional. "
                              "If you don't specify the conformers, the "
                              "script will default to taking the `num_confs` "
                              "lowest conformers, ordered by statistical "
                              "weight."),
                        default=None)

    args = parser.parse_args()

    try:
        main(**args.__dict__)
    except Exception as e:
        fprint(e)
        pdb.post_mortem()
