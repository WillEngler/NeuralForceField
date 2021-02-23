import torch
from rdkit import Chem
import copy
import math

PERIODICTABLE = Chem.GetPeriodicTable()

HARTREE_TO_KCAL_MOL = 627.509
EV_TO_KCAL_MOL = 23.06052

# Distances
BOHR_RADIUS = 0.529177

# Masses
ATOMIC_MASS = {
    1: 1.008,
    3: 6.941,
    6: 12.01,
    7: 14.0067,
    8: 15.999,
    9: 18.998403,
    14: 28.0855,
    16: 32.06,
}

AU_TO_KCAL = {
    'energy': HARTREE_TO_KCAL_MOL,
    '_grad': 1.0 / BOHR_RADIUS,
}

KCAL_TO_AU = {
    'energy': 1.0 / HARTREE_TO_KCAL_MOL,
    '_grad': BOHR_RADIUS,
}

KB_EV = 0.0000861731
EV_TO_AU = 1/27.2114

# Hardness used in xtb, in eV. Source: Ghosh, D.C. and Islam, N., 2010.
# Semiempirical evaluation of the global hardness of the atoms
# of 103 elements of the periodic table using the most probable
# radii as their size descriptors. International Journal of
# Quantum Chemistry, 110(6), pp.1206-1213.

HARDNESS_EV = {"H": 6.4299,
               "He": 12.5449,
               "Li": 2.3746,
               "Be": 3.4968,
               "B": 4.6190,
               "C": 5.7410,
               "N": 6.6824,
               "O": 7.9854,
               "F": 9.1065,
               "Ne": 10.2303,
               "Na": 2.4441,
               "Mg": 3.0146,
               "Al": 3.5849,
               "Si": 4.1551,
               "P": 4.7258,
               "S": 5.2960,
               "Cl": 5.8662,
               "Ar": 6.4366,
               "K": 2.3273,
               "Ca": 2.7587,
               "Br": 5.9111,
               "I": 5.5839}

# Hardness in AU
HARDNESS_AU = {key: val * EV_TO_AU for key, val in
               HARDNESS_EV.items()}

# Hardness in AU as a matrix
HARDNESS_AU_MAT = torch.zeros(200)
for key, val in HARDNESS_AU.items():
    at_num = int(PERIODICTABLE.GetAtomicNumber(key))
    HARDNESS_AU_MAT[at_num] = val


# Times

FS_TO_AU = 41.341374575751
FS_TO_ASE = 0.098
ASE_TO_FS = 1/FS_TO_ASE

# Masses
AMU_TO_AU = 1.66e-27/(9.1093837015e-31)

# Weird units used by Gaussian
CM_TO_J = 1.98630e-23
DYN_TO_J_PER_M  = 0.00001
ANGS_TO_M = 1e-10
MDYN_PER_A_TO_J_PER_M = DYN_TO_J_PER_M / 1000 / ANGS_TO_M
KG_TO_AMU = 1 / (1.66e-27)
HBAR_SI = 6.626e-34/ (2 * math.pi)

AU_TO_KCAL = {
    'energy': HARTREE_TO_KCAL_MOL,
    '_grad': 1.0 / BOHR_RADIUS,
}

KCAL_TO_AU = {
    'energy': 1.0 / HARTREE_TO_KCAL_MOL,
    '_grad': BOHR_RADIUS,
}




def convert_units(props, conversion_dict):
    """Converts dictionary of properties to the desired units.

    Args:
        props (dict): dictionary containing the properties of interest.
        conversion_dict (dict): constants to convert.

    Returns:
        props (dict): dictionary with properties converted.
    """

    props = props.copy()
    for prop_key in props.keys():
        for conv_key, conv_const in conversion_dict.items():
            if conv_key in prop_key:
                props[prop_key] = [
                    x * conv_const
                    for x in props[prop_key]
                ]

    return props

def exc_ev_to_hartree(props, add_ground_energy=False):
    """ Note: only converts excited state energies from ev to hartree, not gradients.

    """

    assert "energy_0" in props.keys()
    exc_keys = [key for key in props.keys() if key.startswith('energy'
        ) and 'grad' not in key and key != 'energy_0']
    energy_0 = props['energy_0']
    new_props = copy.deepcopy(props)

    for key in exc_keys:
        new_props[key] *= EV_TO_AU
        if add_ground_energy:
            new_props[key] += energy_0

    return new_props

