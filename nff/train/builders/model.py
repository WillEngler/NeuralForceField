"""Helper functions to create models, functions and other classes
    while checking for the validity of hyperparameters.
"""
import os
import numpy as np
import torch
from nff.nn.models.schnet import SchNet


class ParameterError(Exception):
    """Raised when a hyperparameter is of incorrect type"""
    pass


def check_parameters(params_type, params):
     """Check whether the parameters correspond to the specified types
 
     Args:
         params (dict)
     """
     for key, val in params.items():
         if key in params_type and not isinstance(val, params_type[key]):
                 raise ParameterError(
                         '%s is not %s' % (str(key), params_type[key])
              )


def get_model(params):
    """Create new model with the given parameters.

    Args:
        params (dict): parameters used to construct the model

    Returns:
        model (nff.nn.models.SchNet)
    """

    params_type = {
        'n_atom_basis': int,
        'n_filters': int,
        'n_gaussians': int,
        'n_convolutions': int,
        'cutoff': float,
        'bond_par': float,
        'trainable_gauss': bool,
        'box_size': np.array
    }

    check_parameters(params_type, params)

    model = SchNet(params)

    return model


def load_model(path):
    """Load pretrained model from the path. If no epoch is specified,
        load the best model.

    Args:
        path (str): path where the model was trained.
    
    Returns:
        model
    """

    if os.path.isdir(path):
        return torch.load(os.path.join(path, 'best_model'))
    elif os.path.exists(path):
        return torch.load(path)
    else:
        raise FileNotFoundError('{} was not found'.format(path))
