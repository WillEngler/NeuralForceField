"""Summary
"""
import numpy as np
import copy

import torch
from torch.autograd import grad
from torch.autograd.gradcheck import zero_gradients
import torch.nn.functional as F

from nff.nn.graphop import batch_and_sum


def compute_jacobian(inputs, output, device):
    """
    Compute Jacobians 

    Args:
        inputs (torch.Tensor): size (N_in, )
        output (torch.Tensor): size (N_in, N_out, )
        device (torch.Tensor): integer

    Returns:
        torch.Tensor: size (N_in, N_in, N_out)
    """
    assert inputs.requires_grad

    num_classes = output.size()[1]
    jacobian = torch.zeros(num_classes, *inputs.size())
    grad_output = torch.zeros(*output.size())
    if inputs.is_cuda:
        grad_output = grad_output.to(device)
        jacobian = jacobian.to(device)

    for i in range(num_classes):
        zero_gradients(inputs)
        grad_output.zero_()
        grad_output[:, i] = 1
        output.backward(grad_output, retain_graph=True)
        jacobian[i] = inputs.grad.data

    return torch.transpose(jacobian, dim0=0, dim1=1)


def compute_grad(inputs, output):
    '''
    Args:
        inputs (torch.Tensor): size (N_in, )
        output (torch.Tensor): size (..., -1)

    Returns:
        torch.Tensor: size (N_in, )
    '''
    assert inputs.requires_grad

    gradspred, = grad(output,
                      inputs,
                      grad_outputs=output.data.new(output.shape).fill_(1),
                      create_graph=True,
                      retain_graph=True)

    return gradspred


def compute_hess(inputs, output, device):
    '''
    Compute Hessians for arbitary model

    Args:
        inputs (torch.Tensor): size (N_in, )
        output (torch.Tensor): size (N_out, )
        device (torch.Tensor): int

    Returns:
        torch.Tensor: N_in, N_in, N_out
    '''
    gradient = compute_grad(inputs, output)
    hess = compute_jacobian(inputs, gradient, device=device)

    return hess


def get_schnet_hessians(batch, model, device=0):
    """Get Hessians from schnet models 

    Args:
        batch (dict): batch of data
        model (TYPE): Description
        device (int, optional): Description
    """
    N_atom = batch['nxyz'].shape[0]
    xyz_reshape = batch["nxyz"][:, 1:].reshape(1, N_atom * 3)
    xyz_reshape.requires_grad = True
    xyz_input = xyz_reshape.reshape(N_atom, 3)

    r, N, xyz = model.convolve(batch, xyz_input)
    energy = model.atomwisereadout.readout["energy"](r).sum()
    hess = compute_hess(inputs=xyz_reshape, output=energy, device=device)

    return hess


def adj_nbrs_and_z(batch, xyz, max_dim, stacked):

    nan_dims = [i for i, row in enumerate(xyz) if torch.isnan(row).all()]
    new_nbrs = copy.deepcopy(batch["nbr_list"])
    new_z = copy.deepcopy(batch["nxyz"][:, 0])

    for dim in nan_dims:

        # adjust the neighbor list to account for the increased length
        # of the nxyz

        mask = torch.any(new_nbrs > dim, dim=1)
        new_nbrs[mask] += 1

        # add dummy atomic numbers for these new nan's
        new_z = torch.cat([new_z[:dim],
                           torch.Tensor([float("nan")]).to(new_z.device),
                           new_z[dim:]])

    # change the neighbor list in the batch
    batch["real_nbrs"] = copy.deepcopy(batch["nbr_list"])
    batch["nbr_list"] = new_nbrs

    # change the nxyz in the batch
    batch["real_nxyz"] = copy.deepcopy(batch["nxyz"])
    batch["nxyz"] = torch.cat([new_z.reshape(-1, 1), xyz],
                              dim=-1)

    # change the number of atoms in the batch
    batch["real_num_atoms"] = copy.deepcopy(batch["num_atoms"])

    # `max_dim` is the number of added nan's in the nxyz for each
    # geometry. We divide by `max_dim` by 4 for z + 3 coordinates
    # for each atom
    batch["num_atoms"] = torch.LongTensor([max_dim // 4] * len(stacked))

    return batch


def pad(batch):

    nxyz = batch["nxyz"]
    N = batch["num_atoms"].tolist()

    # figure out how much we need to pad each geometry

    nan = float(np.nan)
    split = torch.split(nxyz, N)
    reshaped = [i.reshape(-1) for i in split]
    max_dim = max([i.shape[0] for i in reshaped])

    num_pads = [max_dim - i.shape[0] for i in reshaped]

    # pad each geometry and stack the resulting nxyz's
    stacked = torch.stack([F.pad(i, [0, num_pad],
                                 value=nan)
                           for i, num_pad in
                           zip(reshaped, num_pads)])

    # Get the stacked `xyz` by applying a mask to 
    # remove the atomic numbers in the nxyz. We need
    # a stacked `xyz` so that we can compute Hessians
    # of geometries' energies only with respect to 
    # that geometry's coordinates, without needing
    # gradients with respect to other geometries' 
    # coordinates, too. 

    num_batch = stacked.shape[0]
    mask = torch.ones_like(stacked).reshape(-1, 4)
    mask[:, 0] = 0
    mask = mask.reshape(*stacked.shape).to(torch.bool)

    stack_xyz = stacked[mask].reshape(num_batch, -1)
    stack_xyz.requires_grad = True

    # Reshape the stacked `xyz` into normal batch form.

    xyz = stack_xyz.reshape(-1, 3)

    # adjust the neighbor list, atomic numbers, and number
    # of atoms to take into account the
    # new nan's.

    batch = adj_nbrs_and_z(batch, xyz, max_dim, stacked)

    return stack_xyz, xyz, batch


def hess_from_pad(stacked, output, device, N):

    gradient = compute_grad(stacked, output)
    pad_hess = compute_jacobian(stacked, gradient, device=device)
    hess_list = []
    for n, pad in zip(N, pad_hess):
        dim = n * 3
        hess = pad[:dim, :dim]
        hess_list.append(hess)

    return hess_list


def schnet_batched_hessians(batch,
                            model,
                            device=0,
                            energy_keys=["energy"]):

    stack_xyz, xyz, batch = pad(batch)
    r, N, xyz = model.convolve(batch, xyz)
    r = model.atomwisereadout(r)
    results = batch_and_sum(r, N, list(batch.keys()), xyz)
    hess_dic = {}
    N = batch["real_num_atoms"]

    for key in energy_keys:
        output = results[key]
        hess = hess_from_pad(stacked=stack_xyz,
                             output=output,
                             device=device,
                             N=N)
        hess_dic[key + "_hess"] = hess

    # change these keys back to their original values

    batch.pop("nbr_list")
    batch.pop("nxyz")
    batch.pop("num_atoms")

    batch["nbr_list"] = batch["real_nbrs"]
    batch["nxyz"] = batch["real_nxyz"]
    batch["num_atoms"] = batch["real_num_atoms"]

    return hess_dic
