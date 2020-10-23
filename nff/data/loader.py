import numpy as np
import torch

REINDEX_KEYS = ['atoms_nbr_list', 'nbr_list', 'angle_list']
NBR_LIST_KEYS = ['kj_idx', 'ji_idx']

TYPE_KEYS = {
    'atoms_nbr_list': torch.long,
    'nbr_list': torch.long,
    'num_atoms': torch.long,
    'angle_list': torch.long,
    'ji_idx': torch.long,
    'kj_idx': torch.long,
}


def collate_dicts(dicts):
    """Collates dictionaries within a single batch. Automatically reindexes neighbor lists
        and periodic boundary conditions to deal with the batch.

    Args:
        dicts (list of dict): each element of the dataset

    Returns:
        batch (dict)
    """

    # new indices for the batch: the first one is zero and the last does not matter

    cumulative_atoms = np.cumsum([0] + [d['num_atoms'] for d in dicts])[:-1]

    # same idea, but for quantities whose maximum value is the length of the nbr
    # list in each batch
    cumulative_nbrs = np.cumsum([0] + [len(d['nbr_list']) for d in dicts])[:-1]

    for n, d in zip(cumulative_atoms, dicts):
        for key in REINDEX_KEYS:
            if key in d:
                d[key] = d[key] + int(n)

    for n, d in zip(cumulative_nbrs, dicts):
        for key in NBR_LIST_KEYS:
            if key in d:
                d[key] = d[key] + int(n)

    # batching the data
    batch = {}
    for key, val in dicts[0].items():
        if type(val) == str:
            batch[key] = [data[key] for data in dicts]
        elif hasattr(val, 'shape') and len(val.shape) > 0:
            batch[key] = torch.cat([
                data[key]
                for data in dicts
            ], dim=0)
        else:
            batch[key] = torch.stack(
                [data[key] for data in dicts],
                dim=0
            )

    # adjusting the data types:
    for key, dtype in TYPE_KEYS.items():
        if key in batch:
            batch[key] = batch[key].to(dtype)

    return batch


class ImbalancedDatasetSampler(torch.utils.data.sampler.Sampler):
    """
    Sampling class to make sure positive and negative labels
    are represented equally during training. 
    Attributes:
        data_length (int): length of dataset
        weights (torch.Tensor): weights of each index in the
            dataset depending.

    """

    def __init__(self,
                 target_name,
                 props):
        """
        Args:
            target_name (str): name of the property being classified
            props (dict): property dictionary
        """

        data_length = len(props[target_name])

        negative_idx = [i for i, target in enumerate(
            props[target_name]) if round(target.item()) == 0]
        positive_idx = [i for i in range(data_length)
                        if i not in negative_idx]

        negative_weight = 1 / len(negative_idx)
        positive_weight = 1 / len(positive_idx)

        self.data_length = data_length
        self.weights = torch.zeros(data_length)
        self.weights[negative_idx] = negative_weight
        self.weights[positive_idx] = positive_weight

    def __iter__(self):

        return (i for i in torch.multinomial(
            self.weights, self.data_length, replacement=True))

    def __len__(self):
        return self.data_length
