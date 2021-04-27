"""Tools to build layers"""
import collections
import numpy as np
import torch
import copy

from torch.nn import ModuleDict, Sequential
from nff.nn.activations import shifted_softplus
from nff.nn.layers import Dense, Diagonalize
from nff.utils.scatter import scatter_add


layer_types = {
    "linear": torch.nn.Linear,
    "Tanh": torch.nn.Tanh,
    "ReLU": torch.nn.ReLU,
    "Dense": Dense,
    "shifted_softplus": shifted_softplus,
    "sigmoid": torch.nn.Sigmoid,
    "Dropout": torch.nn.Dropout,
    "LeakyReLU": torch.nn.LeakyReLU,
    "ELU": torch.nn.ELU,
    "Diagonalize": Diagonalize
}


def construct_sequential(layers):
    """Construct a sequential model from list of params

    Args:
        layers (list): list to describe the stacked layer params. Example:
            layers = [
                {'name': 'linear', 'param' : {'in_features': 10, 'out_features': 20}},
                {'name': 'linear', 'param' : {'in_features': 10, 'out_features': 1}}
            ]

    Returns:
        Sequential: Stacked Sequential Model
    """
    return Sequential(collections.OrderedDict(
        [layer['name'] + str(i), layer_types[layer['name']](**layer['param'])]
        for i, layer in enumerate(layers)
    ))


def construct_module_dict(moduledict):
    """construct moduledict from a dictionary of layers

    Args:
        moduledict (dict): Description

    Returns:
        ModuleDict: Description
    """
    models = ModuleDict()
    for key in moduledict:
        models[key] = construct_sequential(moduledict[key])
    return models


def get_default_readout(n_atom_basis):
    """Default setting for readout layers. Predicts only the energy of the system.

    Args:
        n_atom_basis (int): number of atomic basis. Necessary to match the dimensions of
            the linear layer.

    Returns:
        DEFAULT_READOUT (dict)
    """

    default_readout = {
        'energy': [
            {'name': 'linear', 'param': {'in_features': n_atom_basis,
                                         'out_features': int(n_atom_basis / 2)}},
            {'name': 'shifted_softplus', 'param': {}},
            {'name': 'linear', 'param': {'in_features': int(
                n_atom_basis / 2), 'out_features': 1}}
        ]
    }

    return default_readout


def torch_nbr_list(atomsobject, cutoff, device='cuda:0', directed=True):
    """Pytorch implementations of nbr_list for minimum image convention, the offsets are only limited to 0, 1, -1:
    it means that no pair interactions is allowed for more than 1 periodic box length. It is so much faster than
    neighbor_list algorithm in ase.

    It is similar to the output of neighbor_list("ijS", atomsobject, cutoff) but a lot faster

    Args:
        atomsobject (TYPE): Description
        cutoff (float): cutoff for
        device (str, optional): Description

    Returns:
        i, j, cutoff: just like ase.neighborlist.neighbor_list
    """
    xyz = torch.Tensor(atomsobject.get_positions(wrap=True)).to(device)
    dis_mat = xyz[None, :, :] - xyz[:, None, :]

    if any(atomsobject.pbc):
        cell_dim = torch.Tensor(atomsobject.get_cell()).diag().to(device)

        offsets = -dis_mat.ge(0.5 * cell_dim).to(torch.float) + \
            dis_mat.lt(-0.5 * cell_dim).to(torch.float)
        dis_mat = dis_mat + offsets * cell_dim

    dis_sq = dis_mat.pow(2).sum(-1)
    mask = (dis_sq < cutoff ** 2) & (dis_sq != 0)

    nbr_list = mask.nonzero()
    if not directed:
        nbr_list = nbr_list[nbr_list[:, 1] > nbr_list[:, 0]]

    i, j = nbr_list[:, 0].detach().to("cpu").numpy(
    ), nbr_list[:, 1].detach().to("cpu").numpy()

    if any(atomsobject.pbc):
        offsets = offsets[nbr_list[:, 0],
                          nbr_list[:, 1], :].detach().to("cpu").numpy()
    else:
        offsets = np.zeros((nbr_list.shape[0], 3))

    return i, j, offsets


def chemprop_msg_update(h,
                        nbrs,
                        ji_idx=None,
                        kj_idx=None):
    r"""

        Function for updating the messages in a GCNN, as implemented in ChemProp
        (Yang, Kevin, et al. "Analyzing learned molecular representations for 
        property prediction."  Journal of chemical information and modeling 
        59.8 (2019): 3370-3388. https://doi.org/10.1021/acs.jcim.9b00237). 

        Args:
                h (torch.tensor): hidden edge vector h_vw. It is a tensor of 
                        dimension `edge` x `hidden`, where edge is the number of 
                        directed edges, and `hidden` is the dimension of the hidden
                        edge features. The indices vw can be obtained from the 
                        first index of `nbrs`, as described below.

                nbrs (torch.tensor): bond directed neighbor list. It is a 
                        tensor of dimension `edge` x 2. The indices vw of h[j] 
                        for an arbitrary index j are given by nbrs[j].

                ji_idx (torch.LongTensor, optional): a set of indices for the neighbor list
                kj_idx (torch.LongTensor, optional): a set of indices for the neighbor list
                    such that nbrs[kj_idx[n]][0] == nbrs[ji_idx[n]][1] for any
                    value of n.

            Returns:
                message (torch.tensor): updated message m_vw =
                 \sum_{k \in N(v) \ w} h_{kv}, of dimension `edge` x `hidden. 
                 More details in the example below.

    Example:
        h = torch.tensor([[0.5488, 0.7152, 0.6028],
                        [0.5449, 0.4237, 0.6459],
                        [0.4376, 0.8918, 0.9637],
                        [0.3834, 0.7917, 0.5289],
                        [0.5680, 0.9256, 0.0710],
                        [0.0871, 0.0202, 0.8326]])
        nbrs = torch.tensor([[1, 2],
                        [2, 1],
                        [2, 3],
                        [3, 2],
                        [2, 4],
                        [4, 2]])
        h_12, h_21, h_23, h_32, h_24, h_42 = h

        # m_{vw} = \sum_{k \in N(v) \ w} h_{kv}
        # m = [m_{12}, m_{21}, m_{23}, m_{32}, m_{24}, m_{42}]
        # = [0, h_{32} + h_{42}, h_{12} + h_{42}, 0, h_{12} + h_{32},
        #    0]

        m = chemprop_msg_update(h, nbrs)
        print(m)

        # >> tensor([[0.0000, 0.0000, 0.0000],
        #         [0.4706, 0.8119, 1.3615],
        #         [0.6359, 0.7354, 1.4354],
        #         [0.0000, 0.0000, 0.0000],
        #         [0.9323, 1.5069, 1.1317],
        #         [0.0000, 0.0000, 0.0000]])

        expec_m = torch.stack(
            [torch.zeros_like(h_12), h_32 + h_42, h_12 + h_42, h_12 + h_32,
            torch.zeros_like(h_12)]
            )

        print(expec_m)

        # >> tensor([[0.0000, 0.0000, 0.0000],
        #         [0.4706, 0.8119, 1.3615],
        #         [0.6359, 0.7354, 1.4354],
        #         [0.0000, 0.0000, 0.0000],
        #         [0.9323, 1.5069, 1.1317],
        #         [0.0000, 0.0000, 0.0000]])

    """

    if all([kj_idx is not None, ji_idx is not None]):
        graph_size = h.shape[0]
        # get the h's of these indices
        h_to_add = h[ji_idx]
        message = scatter_add(src=h_to_add,
                              index=kj_idx,
                              dim=0,
                              dim_size=graph_size)

        return message

    # nbr_dim x nbr_dim matrix, e.g. for nbr_dim = 4, all_idx =
    # [[0, 1, 2, 3], [0, 1, 2, 3], [0, 1, 2, 3], [0, 1, 2, 3]]
    all_idx = torch.stack([torch.arange(0, len(nbrs))] * len(nbrs)).long()

    # We want to sum m_{vw} = \sum h_{kv}, such that k is a neighbour
    # of v but not equal to w. i.e. nbrs[:, :] = kv and
    # nbrs[:, :, None] = vw, so that  nbrs[:, 0, None] = nbrs[:, 1] = v.
    # Also, we want k != w, so that (nbrs[:, 0] != nbrs[:, 1, None].
    # To have both of these met, we multiply the two results together.

    # In the example here, we would get mask =
    # >> tensor([[False, False, False, False, False, False],
    #     [False, False, False,  True, False,  True],
    #     [ True, False, False, False, False,  True],
    #     [False, False, False, False, False, False],
    #     [ True, False, False,  True, False, False],
    #     [False, False, False, False, False, False]])

    mask = (nbrs[:, 1] == nbrs[:, 0, None]) * (nbrs[:, 0] != nbrs[:, 1, None])

    # select the values of all_idx that are allowed by `mask`
    ji_idx = all_idx[mask]
    # map from indices `h_to_add` to the indices of `message`
    kj_idx = mask.nonzero()[:, 0]

    graph_size = h.shape[0]
    # get the h's of these indices
    h_to_add = h[ji_idx]

    message = scatter_add(src=h_to_add,
                          index=kj_idx,
                          dim=0,
                          dim_size=graph_size)

    return message


def chemprop_msg_to_node(h,
                         nbrs,
                         num_nodes):
    r"""

        Converts message hidden edge vectors into node messages
        after the last convolution, as implemented in ChemProp.

        Args:
                h (torch.tensor): hidden edge tensor h_vw. It is a tensor of 
                        dimension `edge` x `hidden`, where edge is the number of 
                        directed edges, and `hidden` is the dimension of the hidden
                        edge features. The indices vw can be obtained from the 
                        first index of h as described below.

                nbrs (torch.tensor): bond directed neighbor list. It is a 
                        tensor of dimension `edge` x 2. The indidces vw of h[j] 
                        for an arbitrary index j are given by nbrs[j].

                num_nodes (int): number of nodes in the graph

        Returns:
                node_features (torch.Tensor): Updated node message 
                m_v = \sum_{w \in N(v)} h_{vw}, of dimension `num_nodes` x
                `hidden`. More details in the example below.

    Example:
            h = torch.tensor([[0.5488, 0.7152, 0.6028],
                            [0.5449, 0.4237, 0.6459],
                            [0.4376, 0.8918, 0.9637],
                            [0.3834, 0.7917, 0.5289],
                            [0.5680, 0.9256, 0.0710],
                            [0.0871, 0.0202, 0.8326]])
            nbrs = torch.tensor([[1, 2],
                            [2, 1],
                            [2, 3],
                            [3, 2],
                            [2, 4],
                            [4, 2]])
            h_12, h_21, h_23, h_32, h_24, h_42 = h
            num_nodes = 5

            # m_v = \sum_{w \in N(v)} h_{vw}
            # = [m_0, m_1, m_2, m_3, m_4]
            # = [0, h_12, h_21 + h_23 + h_24, h_32, h_42]

            m = chemprop_msg_to_node(h, nbrs, 5)
            print(m)

            # >> tensor([[0.0000, 0.0000, 0.0000],
            #         [0.5488, 0.7152, 0.6028],
            #         [1.5505, 2.2411, 1.6806],
            #         [0.3834, 0.7917, 0.5289],
            #         [0.0871, 0.0202, 0.8326]])

            expec_m = torch.stack([torch.zeros_like(h_12),
                        h_12, h_21 + h_23 + h_24, h_32, h_42])

            print(expec_m)

            # >> tensor([[0.0000, 0.0000, 0.0000],
            #         [0.5488, 0.7152, 0.6028],
            #         [1.5505, 2.2411, 1.6806],
            #         [0.3834, 0.7917, 0.5289],
            #         [0.0871, 0.0202, 0.8326]])

    """

    node_idx = torch.arange(num_nodes).to(h.device)
    nbr_idx = torch.arange(len(nbrs)).to(h.device)
    # nbr_dim x node_idx matrix, e.g. for nbr_dim = 4, node_dim = 5,
    # we'd get [[0, 1, 2, 3], [0, 1, 2, 3], [0, 1, 2, 3],
    # [0, 1, 2, 3], [0, 1, 2, 3]]
    node_nbr_idx = torch.stack([nbr_idx] * len(node_idx))

    # we want neighbours vw for which v is equal to the node index
    # of interest
    mask = (nbrs[:, 0] == node_idx[:, None])
    match_idx = mask.nonzero()[:, 0]

    # get the indices of h to add for each node
    good_idx = node_nbr_idx[mask]

    h_to_add = h[good_idx]

    # add together
    node_features = scatter_add(src=h_to_add,
                                index=match_idx,
                                dim=0,
                                dim_size=num_nodes)

    return node_features


def remove_bias(layers):
    """
    Update a list of layers so that the linear layers don't have a bias.
    Args:
            layers (list): list of dictionaries of the form {"name": "...",
                    "param": {...}}
    Returns:
            new_layers (list): same idea as `layers`, but with "param" of
                    linear layers updated to contain {"bias": False}.

    """
    new_layers = copy.deepcopy(layers)
    for layer in new_layers:
        if layer['name'] == 'linear':
            layer['param'].update({'bias': False})
    return new_layers


def single_spec_nbrs(dset,
                     cutoff,
                     device,
                     directed=True):

    xyz = torch.stack(dset.props['nxyz'])[:, :, 1:]

    ###
#    xyz = [dset.props['nxyz'][0],
#          dset.props['nxyz'][0] * float('inf'),
#          dset.props['nxyz'][1]]
#    xyz = torch.stack(xyz)[:, :, 1:]
    ###

    dist_mat = ((xyz[:, :, None, :] - xyz[:, None, :, :])
                .to(device).norm(dim=-1))
    nbr_mask = (dist_mat <= cutoff) * (dist_mat > 0)
    nbrs = nbr_mask.nonzero()

    if not directed:
        nbrs = nbrs[nbrs[:, 2] > nbrs[:, 1]]

    split_idx = ((nbrs[:, 0][1:] != nbrs[:, 0][:-1])
                .nonzero().reshape(-1) + 1)

    split_sizes = []
    num_mols = xyz.shape[0]
    for i in range(num_mols):
        match_nbrs = (nbrs[:, 0] == i)
        if match_nbrs.shape[0] != 0:
            split_size = match_nbrs.nonzero().shape[0]
        else:
            split_size = 0
        split_sizes.append(split_size)
    split_nbrs = list(torch.split(nbrs[:, 1:].cpu(), split_sizes))

    return split_nbrs
