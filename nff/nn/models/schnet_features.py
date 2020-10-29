import torch
from torch import nn
from torch.nn import Sequential


from nff.data.graphs import get_bond_idx
from nff.nn.models.conformers import WeightedConformers
from nff.nn.modules import SchNetEdgeFilter, MixedSchNetConv
from nff.nn.layers import Dense
from nff.nn.activations import shifted_softplus


class SchNetFeatures(WeightedConformers):
    """
    Model that uses a representation of a molecule in terms of different 3D
    conformers to predict properties. The fingerprints of each conformer are
    generated using a 3D extension of the ChemProp model to include distance
    information. The 3D information is featurized using a SchNet Gaussian
    filter.

    """

    def __init__(self, modelparams):
        """
        Initialize model.
        Args:
            modelparams (dict): dictionary of parameters for the model
        Returns:
            None
        """

        WeightedConformers.__init__(self, modelparams)
        # get rid of the atom embedding, as we'll be using graph-based
        # atom features instead of atomic number embeddings
        delattr(self, "atom_embed")

        n_convolutions = modelparams["n_convolutions"]
        dropout_rate = modelparams["dropout_rate"]
        n_bond_hidden = modelparams["n_bond_hidden"]
        n_bond_features = modelparams["n_bond_features"]
        n_atom_basis = modelparams["n_atom_basis"]
        n_filters = modelparams["n_filters"]
        trainable_gauss = modelparams["trainable_gauss"]
        n_gaussians = modelparams["n_gaussians"]
        cutoff = modelparams["cutoff"]

        self.convolutions = nn.ModuleList(
            [
                MixedSchNetConv(
                    n_atom_basis=n_atom_basis,
                    n_filters=n_filters,
                    dropout_rate=dropout_rate,
                    n_bond_hidden=n_bond_hidden
                )
                for _ in range(n_convolutions)
            ]
        )

        # for converting distances to features before concatenating with
        # bond features
        self.distance_filter = SchNetEdgeFilter(
            cutoff=cutoff,
            n_gaussians=n_gaussians,
            trainable_gauss=trainable_gauss,
            n_filters=n_filters,
            dropout_rate=dropout_rate)

        # for converting bond features to hidden feature vectors
        self.bond_filter = Sequential(
            Dense(
                in_features=n_bond_features,
                out_features=n_bond_hidden,
                dropout_rate=dropout_rate),
            shifted_softplus(),
            Dense(
                in_features=n_bond_hidden,
                out_features=n_bond_hidden,
                dropout_rate=dropout_rate)
        )

    def find_bond_idx(self, batch, nbr_list):
        """
        Get `bond_idx`, which map bond indices to indices
        in the neighbor list.
        Args:
            batch (dict): dictionary of props
            nbr_list (torch.LongTensor): neighbor list
        Returns:
            bond_idx (torch.LongTensor): index map
        """

        if "bond_idx" in batch:
            bond_idx = batch["bond_idx"]
        else:
            bonded_nbr_list = batch["bonded_nbr_list"]
            bond_idx = get_bond_idx(bonded_nbr_list, nbr_list)
        return bond_idx

    def convolve(self, batch, xyz=None):
        """

        Apply the convolutional layers to the batch.

        Args:
            batch (dict): dictionary of props

        Returns:
            r: new feature vector after the convolutions
            N: list of the number of atoms for each molecule in the batch
            xyz: xyz (with a "requires_grad") for the batch
        """

        # Note: we've given the option to input xyz from another source.
        # E.g. if you already created an xyz  and set requires_grad=True,
        # you don't want to make a whole new one.

        if xyz is None:
            xyz = batch["nxyz"][:, 1:4]
            xyz.requires_grad = True

        a = batch["nbr_list"]

        bond_features = self.bond_filter(batch["bond_features"])
        bond_dim = bond_features.shape[1]
        num_pairs = a.shape[0]
        bond_edge_features = torch.zeros(num_pairs, bond_dim
                                         ).to(a.device)

        bond_idx = self.find_bond_idx(batch, a)
        bond_edge_features[bond_idx] = bond_features

        # offsets take care of periodic boundary conditions
        offsets = batch.get("offsets", 0)
        distances = (xyz[a[:, 0]] - xyz[a[:, 1]] -
                     offsets).pow(2).sum(1).sqrt()[:, None]
        distance_feats = self.distance_filter(distances)

        e = torch.cat([bond_edge_features, distance_feats],
                      dim=-1)

        r = batch["atom_features"]

        # update function includes periodic boundary conditions

        for i, conv in enumerate(self.convolutions):
            dr = conv(r=r, e=e, a=a)
            r = r + dr

        return r, xyz
