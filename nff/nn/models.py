import torch
import torch.nn as nn
import copy
import torch.nn.functional as F

from nff.nn.layers import Dense, GaussianSmearing
from nff.nn.modules import (GraphDis, SchNetConv, BondEnergyModule, SchNetEdgeUpdate, NodeMultiTaskReadOut,
                            AuTopologyReadOut)
from nff.nn.activations import shifted_softplus
from nff.nn.graphop import batch_and_sum, get_atoms_inside_cell
from nff.nn.utils import get_default_readout
from nff.utils.scatter import compute_grad
import numpy as np
import pdb


class SchNet(nn.Module):

    """SchNet implementation with continous filter.

    Attributes:
        atom_embed (torch.nn.Embedding): Convert atomic number into an
            embedding vector of size n_atom_basis

        atomwise1 (Dense): dense layer 1 to compute energy
        atomwise2 (Dense): dense layer 2 to compute energy
        convolutions (torch.nn.ModuleList): include all the convolutions
        prop_dics (dict): A dictionary of the form {name: prop_dic}, where name is the
            property name and prop_dic is a dictionary for that property.
        module_dict (ModuleDict): a dictionary of modules. Each entry has the form
            {name: mod_list}, where name is the name of a property object and mod_list
            is a ModuleList of layers to predict that property.
    """

    def __init__(self, modelparams):
        """Constructs a SchNet model.

        Args:
            modelparams (TYPE): Description
        """

        super().__init__()

        n_atom_basis = modelparams['n_atom_basis']
        n_filters = modelparams['n_filters']
        n_gaussians = modelparams['n_gaussians']
        n_convolutions = modelparams['n_convolutions']
        cutoff = modelparams['cutoff']
        trainable_gauss = modelparams.get('trainable_gauss', False)

        # default predict var
        readoutdict = modelparams.get(
            'readoutdict', get_default_readout(n_atom_basis))
        post_readout = modelparams.get('post_readout', None)

        self.atom_embed = nn.Embedding(100, n_atom_basis, padding_idx=0)

        self.convolutions = nn.ModuleList([
            SchNetConv(n_atom_basis=n_atom_basis,
                       n_filters=n_filters,
                       n_gaussians=n_gaussians,
                       cutoff=cutoff,
                       trainable_gauss=trainable_gauss)
            for _ in range(n_convolutions)
        ])

        # ReadOut
        self.atomwisereadout = NodeMultiTaskReadOut(
            multitaskdict=readoutdict, post_readout=post_readout)
        self.device = None

    def convolve(self, batch):
        """

        Apply the convolutional layers to the batch.

        Args:
            batch (dict): dictionary of props

        Returns:
            r: new feature vector after the convolutions
            N: list of the number of atoms for each molecule in the batch
            xyz: xyz (with a "requires_grad") for the batch
        """

        r = batch['nxyz'][:, 0]
        xyz = batch['nxyz'][:, 1:4]
        N = batch['num_atoms'].reshape(-1).tolist()
        a = batch['nbr_list']

        # offsets take care of periodic boundary conditions
        offsets = batch.get('offsets', 0)
        xyz.requires_grad = True

        # calculating the distances
        e = (xyz[a[:, 0]] - xyz[a[:, 1]] -
             offsets).pow(2).sum(1).sqrt()[:, None]

        # ensuring image atoms have the same vectors of their corresponding
        # atom inside the unit cell
        r = self.atom_embed(r.long()).squeeze()

        # update function includes periodic boundary conditions
        for i, conv in enumerate(self.convolutions):
            dr = conv(r=r, e=e, a=a)
            r = r + dr

        return r, N, xyz

    def forward(self, batch, other_results=False):
        """Summary

        Args:
            batch (dict): dictionary of props

        Returns:
            dict: dionary of results 
        """

        r, N, xyz = self.convolve(batch)
        r = self.atomwisereadout(r)
        results = batch_and_sum(r, N, list(batch.keys()), xyz)

        return results


class SchNetAuTopology(SchNet):

    """
    A neural network model that combines AuTopology with SchNet.
    Attributes:


        sorted_result_keys (list): a list of energies that you want the network to predict.
            These keys should be ordered by energy (e.g. ["energy_0", "energy_1"]).
        grad_keys (list): A list of gradients that you want the network to give (all members
            of this list should be elements of sorted_result_keys with "_grad" at the end)
        sort_results (bool): Whether or not to sort the final results by energy (i.e. enforce
            that E0 < E1 < E2 ... )
        atom_embed (torch.nn.Embedding): Convert atomic number into an
            embedding vector of size n_atom_basis
        convolutions (torch.nn.ModuleList): include all the convolutions
        schnet_readout (nn.Module): a module for reading out results from SchNet
        auto_readout (nn.Module): a module for reading out results from AuTopology
        device (int): GPU device number

    """

    def __init__(self, modelparams):
        """Constructs a SchNet model.

        Args:
            modelparams (dict): dictionary of parameters for the model
        Returns:
            None

        Example:

            modelparams =  { 
              "sorted_result_keys": ["energy_0", "energy_1"],
              "grad_keys": ["energy_0_grad", "energy_1_grad"],

              "n_atom_basis": 256,
              "n_filters": 256,
              "n_gaussians": 32,
              "n_convolutions": 4,
              "cutoff": 5.0,
              "trainable_gauss": True,

              "schnet_readout": {"energy_0":
                        [
                            {'name': 'Dense', 'param': {'in_features': 5, 'out_features': 20}},
                            {'name': 'shifted_softplus', 'param': {}},
                            {'name': 'Dense', 'param': {'in_features': 20, 'out_features': 1}}
                        ],

                    "energy_1":
                        [
                            {'name': 'linear', 'param': {'in_features': 5, 'out_features': 20}},
                            {'name': 'Dense', 'param': {'in_features': 20, 'out_features': 1}}
                        ]
                }, # parameters for the SchNet part of the readout


              "trainable_prior": True, # whether the AuTopology parameters are learnable or not
              "sort_results": True, # whether to sort the final results by energy
              "autopology_Lh": [40, 20], # layer parameters for AuTopology
              "bond_terms": ["morse"], # type of classical bond prior
              "angle_terms": ["harmonic"], # type of classical angle prior
              "dihedral_terms": ["OPLS"],  # type of classical dihedral prior
              "improper_terms": ["harmonic"], # type of classical improper prior
              "pair_terms": ["LJ"], # type of classical non-bonded pair prior

            }

            example_module = SchNetAuTopology(modelparams)

        """

        # Initialize SchNet
        schnet_params = copy.deepcopy(modelparams)
        schnet_params.update({"readoutdict": modelparams["schnet_readout"]})
        super().__init__(schnet_params)
        self.schnet_readout = self.atomwisereadout
        self.schnet_convolve = self.convolve


        # Initialize autopology
        auto_readout = copy.deepcopy(modelparams)
        auto_readout.update(
            {"Fr": modelparams["n_atom_basis"], "Lh": modelparams.get("autopology_Lh")})
        self.auto_readout = AuTopologyReadOut(multitaskdict=auto_readout)

        # Add some other useful attributes
        self.sorted_result_keys = modelparams["sorted_result_keys"]
        self.grad_keys = modelparams["grad_keys"]
        self.sort_results = modelparams["sort_results"]
        # the autopology keys are just the sorted_result_keys with "auto_" in front
        self.auto_keys = ["auto_{}".format(key) for key in self.sorted_result_keys]


    def get_sorted_results(self, pre_results, auto_results):

        # sort the energies for each molecule in the batch and put the results in
        # `final_results`.
        batch_length = len(pre_results[self.sorted_result_keys[0]])
        final_results = {key: [] for key in [*self.sorted_result_keys, *self.auto_keys]}

        for i in range(batch_length):
            # sort the outputs
            sorted_energies, sorted_idx = torch.sort(torch.cat([pre_results[key][i] for key in
                                                                self.sorted_result_keys]))

            # sort the autopology energies according to the ordering of the total energies
            sorted_auto_energies = torch.cat([auto_results[key][i] for key in
                                              self.sorted_result_keys])[sorted_idx]

            for key, sorted_energy in zip(self.sorted_result_keys, sorted_energies):
                final_results[key].append(sorted_energy)

            for auto_key, sorted_auto_energy in zip(self.auto_keys, sorted_auto_energies):
                final_results[auto_key].append(sorted_auto_energy)

        # re-batch the output energies  
        for key, auto_key in zip(self.sorted_result_keys, self.auto_keys):
            final_results[key] = torch.stack(final_results[key])
            final_results[auto_key] = torch.stack(final_results[auto_key])

        return final_results


    def forward(self, batch):
        """
        Applies the neural network to a batch.
        Args:
            batch (dict): dictionary of props
        Returns:
            final_results (dict): A dictionary of results for each key in
                self.sorted_results_keys and self.grad_keys. Also contains
                results of just the autopology part of the calculation, in
                case you want to also minimize the force error with respect
                to the autopology calculation.

        """

        # get features, N, and xyz from the convolutions
        r, N, xyz = self.schnet_convolve(batch)
        # apply the SchNet readout to r
        schnet_r = self.schnet_readout(r)
        # get the SchNet results for the energies by un-batching them
        # Gradients not included because gradient keys not in  self.sorted_result_keys
        schnet_results = batch_and_sum(
            schnet_r, N, self.sorted_result_keys, xyz)
        # get the autopology results, which are automatically un-batched
        auto_results = self.auto_readout(r=r, batch=batch, xyz=xyz)


        # pre_results is the dictionary of results before sorting energies
        pre_results = dict()
        for key in self.sorted_result_keys:
            # get pre_results by adding schnet_results to auto_results
            pre_results[key] = schnet_results[key] + auto_results[key]

        if self.sort_results:
            final_results = self.get_sorted_results(pre_results, auto_results)

        else:
            final_results = {key: pre_results[key] for key in self.sorted_result_keys}
            final_results.update({auto_key: auto_results[key] for key, auto_key in zip(
                self.sorted_result_keys, self.auto_keys)})

        # compute gradients

        for key, auto_key in zip(self.sorted_result_keys, self.auto_keys):

            if "{}_grad".format(key) not in self.grad_keys:
                continue

            grad = compute_grad(inputs=xyz, output=final_results[key])
            final_results[key + "_grad"] = grad

            autopology_grad = compute_grad(
                inputs=xyz, output=final_results[auto_key])
            final_results[auto_key + "_grad"] = autopology_grad

        return final_results
