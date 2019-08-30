import torch
import torch.nn as nn
import copy
import torch.nn.functional as F

from nff.nn.layers import Dense, GaussianSmearing
from nff.nn.modules import GraphDis, InteractionBlock, BondEnergyModule
from nff.nn.activations import shifted_softplus
from .group import Architecture

ACTIVATION_DIC = {"shifted_softplus": shifted_softplus}

class Net(nn.Module):

    """SchNet implementation with continous filter.
        It is designed for two types computations: 1) xyz inputs 2) graph inputs
        If provide bond list (bond_adj) and bond length tensor (bond_len)
        with a specified bond parameter, a harmonic bond energy 
        priors will be added  
    
    Attributes:
        atom_embed (torch.nn.Embedding): Convert atomic number into an
            embedding vector of size n_atom_basis
        bond_energy_graph (BondEnergyModule): Description
        bond_energy_sample (BondEnergyModule): Description
        bond_par (float): Description
        convolutions (torch.nn.ModuleList): include all the convolutions
        graph_dis (Graphdis): graph distance module to convert xyz inputs
            into distance matrix
        prop_objects (dict): A dictionary of the form {name: prop_obj}, where prop_obj
            is an instance of the Property object and name is its name.
        module_dict (ModuleDict): a dictionary of modules. Each entry has the form
            {name: mod_list}, where name is the name of a property object and mod_list
            is a ModuleList of layers to predict that property.

        self.module_dict = self.make_module_dict(n_atom_basis=n_atom_basis)
    """
    
    def __init__(
        self,
        n_atom_basis,
        n_filters,
        n_gaussians,
        n_convolutions,
        cutoff,
        output_vars,
        specs,
        bond_par=50.0,
        trainable_gauss=False,
        box_size=None,
    ):
        """Constructs a SchNet model.

        Args:
            n_atom_basis (int): dimension of atomic embeddings.
            n_filters (int): dimension of filters.
            n_gaussians (int): dimension of the gaussian basis.
            n_convolutions (int): number of convolutions.
            cutoff (float): soft cutoff radius for convolution.
            bond_par (float):
            trainable_gauss (bool): if True, make the Gaussian parameter trainable.
            box_size (numpy.array): size of the box, dim = (3, )
            output_vars (list): a list of the output variables that you want the network to predict
            specs (dict): each key in specs is the name of a different output variable. Its value is
                a dictionary with specifications about the network (see Architecture for an example).
        """

        super().__init__()
        
        self.graph_dis = GraphDis(Fr=1,
                                  Fe=1,
                                  cutoff=cutoff,
                                  box_size=box_size)

        self.convolutions = nn.ModuleList([
            InteractionBlock(n_atom_basis=n_atom_basis,
                             n_filters=n_filters,
                             n_gaussians=n_gaussians, 
                             cutoff=cutoff,
                             trainable_gauss=trainable_gauss)
            for _ in range(n_convolutions)
        ])

        self.atom_embed = nn.Embedding(100, n_atom_basis, padding_idx=0)

        architecture = Architecture(output_vars=output_vars, specs=specs)
        self.prop_objects = {obj.name: obj for obj in architecture.prop_objects}
        self.module_dict = self.make_module_dict(n_atom_basis=n_atom_basis)

        # declare the bond energy module for two cases 
        self.bond_energy_graph = BondEnergyModule(batch=True)
        self.bond_par = bond_par

    def make_module_dict(self, n_atom_basis):

        """Make a dictionary of modules:
        Args:
            n_atom_basis (int): the number of input features from the convolution layers."""

        module_dict = nn.ModuleDict({})
        old_out_features = n_atom_basis

        for name, prop_object in self.prop_objects.items():
            module_list = nn.ModuleList([])
            for i, layer in enumerate(prop_object.layers):

                in_features = old_out_features
                # out_features is given by layer if layer is an integer; otherwise layer is a function
                # f(x) and out_features = f(old_out_features)
                out_features = int(layer(in_features)) if (hasattr(layer, "__call__")) else layer
                activation = ACTIVATION_DIC[prop_object.activation]

                module_list.append(Dense(in_features=in_features,
                                         out_features=out_features,
                                         activation=activation))

                old_out_features = out_features

            module_dict[name] = module_list
        return module_dict

    def batch_and_pool(self, output, pool_type, xyz, N, bond_adj, bond_len, is_energy=False):
        """Separate the outputs back into batches, pool the results, and use a physics
        prior if possible.

        Args:
            output (tensor): the inital output of the neural network
            pool_type (str): pooling approach (sum or average)
            xyz (tensor): xyz of the molecule
            N (int): number of batches
            bond_adj: description
            bond_len: description
            is_energy: whether the output of the neural network is an energy or not
        """
        out_batch = list(torch.split(output, N))
        # bond energy computed as a physics prior
        if is_energy and bond_adj is not None and bond_len is not None:
            ebond = self.bond_energy_graph(xyz=xyz,
                                           bond_adj=bond_adj,
                                           bond_len=bond_len,
                                           bond_par=self.bond_par)

            ebond_batch = list(torch.split(ebond, N))

            for b in range(len(N)):
                out_batch[b] = torch.sum(out_batch[b] + ebond_batch[b], dim=0)
            return torch.stack(out_batch)

        for b in range(len(N)):
            out_batch[b] = torch.sum(out_batch[b], dim=0)
        if pool_type == "average":
            out_batch /= N
        return torch.stack(out_batch)

    def get_atoms_inside_cell(self, r, N, pbc):
        # selecting only the atoms inside the unit cell
        atoms_in_cell = [
            set(x.cpu().data.numpy())
            for x in torch.split(pbc, N)
        ]

        N = [len(n) for n in atoms_in_cell]

        atoms_in_cell = torch.cat([
            torch.LongTensor(list(x))
            for x in atoms_in_cell
        ])

        r = r[atoms_in_cell]

        return r, N
        
    def forward(
        self,
        r,
        xyz,
        N,
        a=None,
        bond_adj=None,
        bond_len=None,
        pbc=None
    ):

        """Summary
        
        Args:
            r (torch.Tensor): Description
            xyz (torch.Tensor): Description
            bond_adj (torch.LongTensor): Description
            a (None, optional): Description
            N (list): Description
            pbc (torch.Tensor)
        
        Returns:
            TYPE: Description
        
        Raises:
            ValueError: Description
        """

        # a is None means non-batched case
        if a is None:
            assert len(set(N)) == 1 # all the graphs should correspond to the same molecule
            N_atom = N[0]
            e, A = self.graph_dis(xyz=xyz.reshape(-1, N_atom, 3))

            #  use only upper triangular to generative undirected adjacency matrix 
            A = A * torch.ones(N_atom, N_atom).triu()[None, :, :].to(A.device)
            e = e * A.unsqueeze(-1)

            # compute neighbor list 
            a = A.nonzero()

            # reshape distance list 
            e = e[a[:,0], a[:, 1], a[:,2], :].reshape(-1, 1)

            # reindex neighbor list 
            a = (a[:, 0] * N_atom)[:, None] + a[:, 1:3]

        # batched case
        else:
            # calculating the distances
            e = (xyz[a[:, 0]] - xyz[a[:, 1]]).pow(2).sum(1).sqrt()[:, None]

        assert len(r.shape) == 2
        assert len(xyz.shape) == 2
        assert r.shape[0] == xyz.shape[0]
        assert len(a.shape) == 2
        assert a.shape[0] == e.shape[0]

        if pbc is None:
            pbc = torch.LongTensor(range(r.shape[0]))
        else:
            assert pbc.shape[0] == r.shape[0]

        # ensuring image atoms have the same vectors of their corresponding
        # atom inside the unit cell
        r = self.atom_embed(r.long()).squeeze()[pbc]

        # update function includes periodic boundary conditions
        for i, conv in enumerate(self.convolutions):
            dr = conv(r=r, e=e, a=a)
            r = r + dr
            r = r[pbc]

        # remove image atoms outside the unit cell
        r, N = self.get_atoms_inside_cell(r, N, pbc)

        output_dic = {}
        for key, module_list in self.module_dict.items():
            output = copy.copy(r)
            for module in module_list:
                output = module(output)

            is_energy = "energy" in key
            pool_type = self.prop_objects[key].pool
            batch_output = self.batch_and_pool(output, pool_type, xyz, N, bond_adj, bond_len, is_energy)

            output_dic[key] = batch_output

        return output_dic


