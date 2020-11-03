from functools import partial
import sympy as sym
import torch
import torch.nn as nn
from torch.nn.init import xavier_uniform_, constant_
import numpy as np

from nff.utils import bessel_basis, real_sph_harm

zeros_initializer = partial(constant_, val=0.0)
DEFAULT_DROPOUT_RATE = 0.0


def gaussian_smearing(distances, offset, widths, centered=False):

    if not centered:
        # Compute width of Gaussians (using an overlap of 1 STDDEV)
        # widths = offset[1] - offset[0]
        coeff = -0.5 / torch.pow(widths, 2)
        diff = distances - offset

    else:
        # If Gaussians are centered, use offsets to compute widths
        coeff = -0.5 / torch.pow(offset, 2)
        # If centered Gaussians are requested, don't substract anything
        diff = distances

    # Compute and return Gaussians
    gauss = torch.exp(coeff * torch.pow(diff, 2))

    return gauss


class GaussianSmearing(nn.Module):
    """
    Wrapper class of gaussian_smearing function. Places a predefined number of Gaussian functions within the
    specified limits.

    sample struct dictionary:

        struct = {'start': 0.0, 'stop':5.0, 'n_gaussians': 32, 'centered': False, 'trainable': False}

    Args:
        start (float): Center of first Gaussian.
        stop (float): Center of last Gaussian.
        n_gaussians (int): Total number of Gaussian functions.
        centered (bool):  if this flag is chosen, Gaussians are centered at the origin and the
              offsets are used to provide their widths (used e.g. for angular functions).
              Default is False.
        trainable (bool): If set to True, widths and positions of Gaussians are adjusted during training. Default
              is False.
    """

    def __init__(self,
                 start,
                 stop,
                 n_gaussians,
                 centered=False,
                 trainable=False):
        super().__init__()
        offset = torch.linspace(start, stop, n_gaussians)
        widths = torch.FloatTensor(
            (offset[1] - offset[0]) * torch.ones_like(offset))
        if trainable:
            self.width = nn.Parameter(widths)
            self.offsets = nn.Parameter(offset)
        else:
            self.register_buffer("width", widths)
            self.register_buffer("offsets", offset)
        self.centered = centered

    def forward(self, distances):
        """
        Args:
            distances (torch.Tensor): Tensor of interatomic distances.

        Returns:
            torch.Tensor: Tensor of convolved distances.

        """
        result = gaussian_smearing(
            distances, self.offsets, self.width, centered=self.centered
        )

        return result


class Dense(nn.Linear):
    """ Applies a dense layer with activation: :math:`y = activation(Wx + b)`

    Args:
        in_features (int): number of input feature
        out_features (int): number of output features
        bias (bool): If set to False, the layer will not adapt the bias. (default: True)
        activation (callable): activation function (default: None)
        weight_init (callable): function that takes weight tensor and initializes (default: xavier)
        bias_init (callable): function that takes bias tensor and initializes (default: zeros initializer)
    """

    def __init__(
        self,
        in_features,
        out_features,
        bias=True,
        activation=None,
        dropout_rate=DEFAULT_DROPOUT_RATE,
        weight_init=xavier_uniform_,
        bias_init=zeros_initializer,
    ):

        self.weight_init = weight_init
        self.bias_init = bias_init

        super().__init__(in_features, out_features, bias)

        self.activation = activation
        self.dropout = nn.Dropout(p=dropout_rate)

    def reset_parameters(self):
        """
            Reinitialize model parameters.
        """
        self.weight_init(self.weight)
        if self.bias is not None:
            self.bias_init(self.bias)

    def forward(self, inputs):
        """
        Args:
            inputs (dict of torch.Tensor): SchNetPack format dictionary of input tensors.

        Returns:
            torch.Tensor: Output of the dense layer.
        """
        y = super().forward(inputs)

        # kept for compatibility with earlier versions of nff
        if hasattr(self, "dropout"):
            y = self.dropout(y)

        if self.activation:
            y = self.activation(y)

        return y


class Envelope(nn.Module):
    """
    Layer for adding a polynomial envelope to the spherical and
    radial Bessel functions in DimeNet.
    """

    def __init__(self, p):
        """
        Args:
            p (int): exponent in the damping envelope
        Returns:
            None
        """
        super().__init__()
        self.p = p

    def forward(self, d):
        """
        Args:
            d (torch.Tensor): tensor of distances
        Returns:
            u (torch.Tensor): polynomial of the distances
        """
        p = self.p
        u = 1 - (p + 1) * (p + 2) / 2 * d ** p \
            + p * (p + 2) * d ** (p + 1) \
            - p * (p + 1) / 2 * d ** (p + 2)
        return u


class DimeNetSphericalBasis(nn.Module):
    """
    Spherical basis layer for DimeNet.
    """

    def __init__(self,
                 l_spher,
                 n_spher,
                 cutoff,
                 envelope_p):
        """
        Args:
            l_spher (int): maximum l value in the spherical
                basis functions
            n_spher (int): maximum n value in the spherical
                basis functions
            cutoff (float): cutoff distance in the neighbor list
            envelope_p (int): exponent in the damping envelope
        Returns:
            None
        """

        super().__init__()

        assert n_spher <= 64

        self.n_spher = n_spher
        self.l_spher = l_spher
        self.cutoff = cutoff
        self.envelope = Envelope(envelope_p)

        # retrieve formulas
        self.bessel_formulas = bessel_basis(l_spher, n_spher)
        self.sph_harm_formulas = real_sph_harm(l_spher)
        self.sph_funcs = []
        self.bessel_funcs = []

        # create differentiable Torch functions through
        # sym.lambdify

        x = sym.symbols('x')
        theta = sym.symbols('theta')
        modules = {'sin': torch.sin, 'cos': torch.cos}
        for i in range(l_spher):
            if i == 0:
                first_sph = sym.lambdify(
                    [theta], self.sph_harm_formulas[i][0], modules)(0)
                self.sph_funcs.append(
                    lambda tensor: torch.zeros_like(tensor) + first_sph)
            else:
                self.sph_funcs.append(sym.lambdify(
                    [theta], self.sph_harm_formulas[i][0], modules))
            for j in range(n_spher):
                self.bessel_funcs.append(sym.lambdify(
                    [x], self.bessel_formulas[i][j], modules))

    def forward(self, d, angles, kj_idx):
        """
        Args:
            d (torch.Tensor): tensor of distances
            angles (torch.Tensor): tensor of angles
            kj_idx (torch.LongTensor): nbr_list indices corresponding
                to the k,j indices in the angle list.
        """

        # compute the radial functions with arguments d / cutoff
        d_scaled = d / self.cutoff
        rbf = [f(d_scaled) for f in self.bessel_funcs]
        rbf = torch.stack(rbf, dim=1)

        # multiply the radial basis functions by the envelope
        u = self.envelope(d_scaled)
        rbf_env = u[:, None] * rbf

        # we want d_kj for each angle alpha_{kj, ji}
        # = angle_{ijk}, so we want to order the distances
        # so they align with the kj indices of `angles`

        rbf_env = rbf_env[kj_idx.long()]
        rbf_env = rbf_env.reshape(*torch.tensor(
            rbf_env.shape[:2]).tolist())

        # get the angular functions
        cbf = [f(angles) for f in self.sph_funcs]
        cbf = torch.stack(cbf, dim=1)
        # repeat for n_spher
        cbf = cbf.repeat_interleave(self.n_spher, dim=1)

        # multiply with rbf and return

        return rbf_env * cbf


class DimeNetRadialBasis(nn.Module):
    """
    Radial basis layer for DimeNet.
    """
    def __init__(self,
                 n_rbf,
                 cutoff,
                 envelope_p):

        """
        Args:
            n_rbf (int): number of radial basis functions
            cutoff (float): cutoff distance in the neighbor list
            envelope_p (int): exponent in the damping envelope
        Returns:
            None
        """

        super().__init__()
        n = torch.arange(1, n_rbf + 1).float()
        # initialize k_n but let it be learnable 
        self.k_n = nn.Parameter(n * np.pi / cutoff)
        self.envelope = Envelope(envelope_p)
        self.cutoff = cutoff

    def forward(self, d):
        """
        Args:
            d (torch.Tensor): tensor of distances
        """
        pref = (2 / self.cutoff) ** 0.5
        arg = torch.sin(self.k_n * d) / d
        u = self.envelope(d / self.cutoff)

        return pref * arg * u
