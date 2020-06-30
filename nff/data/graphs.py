import numpy as np
import networkx as nx
import torch
from ase import io
import numpy as np
import nff

DISTANCETHRESHOLDICT_SYMBOL = {
    ("H", "H"): 1.00,
    ("H", "Li"): 1.30,
    ("H", "N"): 1.50,
    ("H", "C"): 1.30,
    ("H", "N"): 1.30,
    ("H", "O"): 1.30,
    ("H", "F"): 1.30,
    ("H", "Na"): 1.65,
    ("H", "Si"): 1.65,
    ("H", "Mg"): 1.40,
    ("H", "S"): 1.50,
    ("H", "Cl"): 1.60,
    ("H", "Br"): 1.60,
    ("Li", "C"): 0.0,
    ("Li", "N"): 0.0,
    ("Li", "O"): 0.0,
    ("Li", "F"): 0.0,
    ("Li", "Mg"): 0.0,
    ("B", "C"): 1.70,
    ("B", "N"): 1.70,
    ("B", "O"): 1.70,
    ("B", "F"): 1.70,
    ("B", "Na"): 1.8,
    ("B", "Mg"): 1.8,
    ("B", "Cl"): 2.1,
    ("B", "Br"): 2.1,
    ("C", "C"): 1.70,
    ("C", "O"): 1.70,
    ("C", "N"): 1.8,
    ("C", "F"): 1.65,
    ("C", "Na"): 1.80,
    ("C", "Mg"): 1.70,
    ("C", "Si"): 2.10,
    ("C", "S"): 2.20,
    ("N", "O"): 1.55,
    ("N", "Na"): 1.70,
    ("N", "S"): 2.0,
    ("O", "Na"): 1.70,
    ("O", "Mg"): 1.35,
    ("O", "S"): 2.00,
    ("O", "Cl"): 1.80,
    ("O", "O"): 1.70,
    ("O", "F"): 1.50,
    ("O", "Si"): 1.85,
    ("O", "Br"): 1.70,
    ("F", "Mg"): 1.35, }

DISTANCETHRESHOLDICT_Z = {
    (1., 1.): 1.00,
    (1., 3.): 1.30,
    (1., 5.): 1.50,
    (1., 6.): 1.30,
    (1., 7.): 1.30,
    (1., 8.): 1.30,
    (1., 9.): 1.30,
    (1., 11.): 1.65,
    (1., 14.): 1.65,
    (1., 12.): 1.40,
    (1., 16.): 1.50,
    (1., 17.): 1.60,
    (1., 35.): 1.60,
    (3., 6.): 0.0,
    (3., 7.): 0.0,
    (3., 8.): 0.0,
    (3., 9.): 0.0,
    (3., 12.): 0.0,
    (5., 6.): 1.70,
    (5., 7.): 1.70,
    (5., 8.): 1.70,
    (5., 9.): 1.70,
    (5., 11.): 1.8,
    (5., 12.): 1.8,
    (5., 17.): 2.1,
    (5., 35.): 2.1,
    (6., 6.): 1.70,
    (6., 8.): 1.70,
    (6., 7.): 1.8,
    (6., 9.): 1.65,
    (6., 11.): 1.80,
    (6., 12.): 1.70,
    (6., 14.): 2.10,
    (6., 16.): 2.20,
    (7., 8.): 1.55,
    (7., 11.): 1.70,
    (7., 16.): 2.0,
    (8., 11.): 1.70,
    (8., 12.): 1.35,
    (8., 16.): 2.00,
    (8., 17.): 1.80,
    (8., 8.): 1.70,
    (8., 9.): 1.50,
    (8., 14.): 1.85,
    (8., 35.): 1.70,
    (9., 12.): 1.35}


def get_neighbor_list(xyz, cutoff=5, undirected=True):
    """Get neighbor list from xyz positions of atoms.

    Args:
        xyz (torch.Tensor or np.array): (N, 3) array with positions
            of the atoms.
        cutoff (float): maximum distance to consider atoms as
            connected.

    Returns:
        nbr_list (torch.Tensor): (num_edges, 2) array with the
            indices of connected atoms.
    """

    xyz = torch.Tensor(xyz)
    n = xyz.size(0)

    # calculating distances
    dist = (xyz.expand(n, n, 3) - xyz.expand(n, n,
                                             3).transpose(0, 1)).pow(2).sum(dim=2).sqrt()

    # neighbor list
    mask = (dist <= cutoff)
    mask[np.diag_indices(n)] = 0
    nbr_list = mask.nonzero()

    if undirected:
        nbr_list = nbr_list[nbr_list[:, 1] > nbr_list[:, 0]]

    return nbr_list


def get_dist_mat(xyz, box_len, unwrap=True):
    dis_mat = (xyz[:, None, :] - xyz[None, ...])

    # build minimum image convention
    mask_pos = dis_mat.ge(0.5*box_len).type(torch.FloatTensor)
    mask_neg = dis_mat.lt(-0.5*box_len).type(torch.FloatTensor)

    # modify distance
    if unwrap:
        dis_add = mask_neg * box_len
        dis_sub = mask_pos * box_len
        dis_mat = dis_mat + dis_add - dis_sub

    # create cutoff mask
    # compute squared distance of dim (B, N, N)
    dis_sq = dis_mat.pow(2).sum(-1)
    # mask = (dis_sq <= cutoff ** 2) & (dis_sq != 0)                 # byte tensor of dim (B, N, N)
    #A = mask.unsqueeze(3).type(torch.FloatTensor).to(self.device) #

    # 1) PBC 2) # gradient of zero distance
    dis_sq = dis_sq.unsqueeze(-1)
    # dis_sq = (dis_sq * A) + 1e-8# to make sure the distance is not zero, otherwise there will be inf gradient
    dis_mat = dis_sq.sqrt().squeeze()

    return dis_mat


def adjdistmat(atoms, threshold=DISTANCETHRESHOLDICT_Z, unwrap=True):
    #dmat = (xyz[:, None, :] - xyz[None, ...]).pow(2).sum(-1).numpy()
    xyz = torch.Tensor(atoms.get_positions(wrap=True))
    atomicnums = atoms.get_atomic_numbers().tolist()
    box_len = torch.Tensor(np.diag(atoms.get_cell()))

    dmat = get_dist_mat(xyz, box_len, unwrap=unwrap).numpy()

    thresholdmat = np.array([[threshold.get(tuple(
        sorted((i, j))), 2.0) for i in atomicnums] for j in atomicnums])
    adjmat = (dmat < thresholdmat).astype(int)

    np.fill_diagonal(adjmat, 0)
    return np.array(atomicnums), np.array(adjmat), np.array(dmat), thresholdmat


def generate_mol_atoms(atomic_nums, xyz, cell):
    return Atoms(numbers=atomic_nums, positions=xyz, cell=cell, pbc=True)


def generate_subgraphs(atomsobject, unwrap=True, get_edge=False):

    atoms = nff.io.ase.AtomsBatch(atomsobject)
    z, adj, dmat,  threshold = adjdistmat(atoms, unwrap=unwrap)
    box_len = torch.Tensor(np.diag(atoms.get_cell()))
    G = nx.from_numpy_matrix(adj)

    for i, item in enumerate(z):
        G.nodes[i]['z'] = item

    sub_graphs = nx.connected_component_subgraphs(G)

    edge_list = []
    partitions = []

    for i, sg in enumerate(sub_graphs):
        partitions.append(list(sg.nodes))
        if get_edge:
            edge_list.append(list(sg.edges))

    if len(edge_list) != 0:
        return partitions, edge_list
    else:
        return partitions


def get_single_molecule(atomsobject, mol_idx, single_mol_id):
    z = atomsobject.get_atomic_numbers()[mol_idx[single_mol_id]]
    pos = atomsobject.get_positions()[mol_idx[single_mol_id]]
    return Atoms(numbers=z, positions=pos,
                 cell=atomsobject.cell, pbc=True)


def reconstruct_atoms(atomsobject, mol_idx):
    sys_xyz = torch.Tensor(atomsobject.get_positions(wrap=True))
    box_len = torch.Tensor(atomsobject.get_cell_lengths_and_angles()[:3])

    print(box_len)
    for idx in mol_idx:
        mol_xyz = sys_xyz[idx]
        center = mol_xyz.shape[0]//2
        intra_dmat = (mol_xyz[None, ...] - mol_xyz[:, None, ...])[center]
        sub = (intra_dmat > 0.5 * box_len).to(torch.float) * box_len
        add = (intra_dmat <= -0.5 * box_len).to(torch.float) * box_len
        traj_unwrap = mol_xyz + add - sub
        sys_xyz[idx] = traj_unwrap

    new_pos = sys_xyz.numpy()

    return new_pos


def list2adj(bond_list, size=None):
    E = bond_list
    if size is None:
        size = max(set([n for e in E for n in e])) + 1
    # make an empty adjacency list
    adjacency = [[0]*size for _ in range(size)]
    # populate the list for each edge
    for sink, source in E:
        adjacency[sink][source] = 1
    return adjacency


def make_directed(nbr_list):
    """
    Check if a neighbor list is directed, and make it
    directed if it isn't.
    Args:
        nbr_list (torch.LongTensor): neighbor list
    Returns:
        new_nbrs (torch.LongTensor): directed neighbor
            list
        directed (bool): whether the old one was directed
            or not  
    """

    gtr_ij = (nbr_list[:, 0] > nbr_list[:, 1]).any().item()
    gtr_ji = (nbr_list[:, 1] > nbr_list[:, 0]).any().item()
    directed = gtr_ij and gtr_ji

    if directed:
        return nbr_list, directed

    new_nbrs = torch.cat([nbr_list, nbr_list.flip(1)], dim=0)
    return new_nbrs, directed


def get_angle_list(nbr_lists):
    """
    Get angle lists from neighbor lists.
    Args:
        nbr_lists (list): list of neighbor
            lists.
    Returns:
        angles (list): list of angle lists
        new_nbrs (list): list of new neighbor
            lists (directed if they weren't
            already).
    """

    new_nbrs = []
    angles = []
    for nbr_list in nbr_lists:
        nbr_list, _ = make_directed(nbr_list)

        # Condition that the second index of a nbr
        # list item is equal to the first index of
        # another item.  Tthe exception is if the
        # second item is just the first
        # item reversed (e.g. [0, 1] and [1, 0])

        # e.g. nbr_list = tensor([[0, 1],
        # [0, 2],
        # [1, 0],
        # [1, 2],
        # [2, 0],
        # [2, 1]])
        # then mask  = tensor([[False, False, False,  True, False, False],
        # [False, False, False, False, False,  True],
        # [False,  True, False, False, False, False],
        # [False, False, False, False,  True, False],
        # [True, False, False, False, False, False],
        # [False, False,  True, False, False, False]])

        mask = (nbr_list[:, 1, None] == nbr_list[:, 0]) * (
            nbr_list[:, 0, None] != nbr_list[:, 1])

        # The index of the third atom in each angle.
        # In this case it would be tensor([2, 1, 2, 0, 1, 0])
        third_atoms = nbr_list[:, 1].repeat(nbr_list.shape[0], 1)[mask]

        # number of angles for each item in the nbr_list
        # In this case it would be tensor([1, 1, 1, 1, 1, 1])
        num_angles = mask.sum(-1)

        # idx = np.arange(nbr_len)
        # scatter_idx = torch.LongTensor(np.repeat(idx, num_angles.tolist(), axis=0))

        # the nbr list, but with each item repeated num_angle times
        nbr_repeats = torch.LongTensor(
            np.repeat(nbr_list.numpy(), num_angles.tolist(), axis=0))

        # the concatenation of `nbr_repeats` with
        # `third_atoms` is the angle list
        angle_list = torch.cat(
            (nbr_repeats, third_atoms.reshape(-1, 1)), dim=1)

        new_nbrs.append(nbr_list)
        angles.append(angle_list)

    return angles, new_nbrs


def m_idx_of_angles(angle_list,
                    nbr_list,
                    angle_start,
                    angle_end):
    """
    Get the array index of elements of an angle list.
    Args:
        angle_list (torch.LongTensor): directed indices
            of sets of three atoms that are all in each
            other's neighborhood.
        nbr_list (torch.LongTensor): directed indices
            of pairs of atoms that are in each other's
            neighborhood.
        angle_start (int): the first index in the angle
            list you want.
        angle_end (int): the last index in the angle list
            you want.
    Returns:
        idx (torch.LongTensor): `m` indices.
    Example:
        angle_list = torch.LongTensor([[0, 1, 2],
                                       [0, 1, 3]])
        nbr_list = torch.LongTensor([[0, 1],
                                    [0, 2],
                                    [0, 3],
                                    [1, 0],
                                    [1, 2],
                                    [1, 3],
                                    [2, 0],
                                    [2, 1],
                                    [2, 3],
                                    [3, 0],
                                    [3, 1],
                                    [3, 2]])

        # This means that message vectors m_ij are ordered
        # according to m = {m_01, m_01, m_03, m_10,
        # m_12, m_13, m_30, m_31, m_32}. Say we are interested
        # in indices 2 and 1 for each element in the angle list.
        # If we want to know what the corresponding indices
        # in m (or the nbr list) are, we would call `m_idx_of_angles`
        # with angle_start = 2, angle_end = 1 (if we want the
        # {2,1} and {3,1} indices), or angle_start = 1,
        # angle_end = 0 (if we want the {1,2} and {1,3} indices).
        # Say we choose angle_start = 2 and angle_end = 1. Then
        # we get the indices of {m_21, m_31}, which we can see
        # from the nbr list are [7, 10].


    """

    # expand nbr_list[:, 0] so it's repeated once
    # for every element of `angle_list`.
    repeated_nbr = nbr_list[:, 0].repeat(angle_list.shape[0], 1)
    reshaped_angle = angle_list[:, angle_start].reshape(-1, 1)
    # gives you a matrix that shows you where each angle is equal
    # to nbr_list[:, 0]
    mask = repeated_nbr == reshaped_angle

    # same idea, but with nbr_list[:, 1] and angle_list[:, angle_end]

    repeated_nbr = nbr_list[:, 1].repeat(angle_list.shape[0], 1)
    reshaped_angle = angle_list[:, angle_end].reshape(-1, 1)

    # the full mask is the product of both
    mask *= (repeated_nbr == reshaped_angle)

    # get the indices where everything is true
    idx = mask.nonzero()[:, 1]

    return idx


def add_ji_kj(angle_lists, nbr_lists):
    """
    Get ji and kj idx (explained more below):
    Args:
        angle_list (list[torch.LongTensor]): list of angle
            lists
        nbr_list (list[torch.LongTensor]): list of directed neighbor
            lists
    Returns:
        ji_idx_list (list[torch.LongTensor]): ji_idx for each geom
        kj_idx_list (list[torch.LongTensor]): kj_idx for each geom

    """

    # given an angle a_{ijk}, we want
    # ji_idx, which is the array index of m_ji.
    # We also want kj_idx, which is the array index
    # of m_kj. For example, if i,j,k = 0,1,2,
    # and our neighbor list is [[0, 1], [0, 2],
    # [1, 0], [1, 2], [2, 0], [2, 1]], then m_10 occurs
    # at index 2, and m_21 occurs at index 5. So
    # ji_idx = 2 and kj_idx = 5.

    ji_idx_list = []
    kj_idx_list = []
    for angle_list, nbr_list in zip(angle_lists, nbr_lists):
        ji_idx = m_idx_of_angles(angle_list=angle_list,
                                 nbr_list=nbr_list,
                                 angle_start=1,
                                 angle_end=0)

        kj_idx = m_idx_of_angles(angle_list=angle_list,
                                 nbr_list=nbr_list,
                                 angle_start=2,
                                 angle_end=1)
        ji_idx_list.append(ji_idx)
        kj_idx_list.append(kj_idx)

    return ji_idx_list, kj_idx_list
