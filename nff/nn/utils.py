"""Tools to build layers"""
import collections
from argparse import Namespace

import numpy as np
import torch

from torch.nn import ModuleDict, Sequential
from nff.nn.activations import shifted_softplus
from nff.nn.layers import Dense


layer_types = {
    "linear": torch.nn.Linear,
    "Tanh": torch.nn.Tanh,
    "ReLU": torch.nn.ReLU,
    "Dense": Dense,
    "shifted_softplus": shifted_softplus
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
