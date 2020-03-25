import numpy as np
import torch

class Metric:
    r"""
    Base class for all metrics.

    Metrics measure the performance during the training and evaluation.

    Args:
        target (str): name of target property
        model_output (int, str): index or key, in case of multiple outputs
            (Default: None)
        name (str): name used in logging for this metric. If set to `None`,
            `MSE_[target]` will be used (Default: None)
    """

    def __init__(self, target, name=None):
        self.target = target
        if name is None:
            self.name = self.__class__.__name__
        else:
            self.name = name

        self.loss = 0.0
        self.n_entries = 0.0

    def reset(self):
        """Reset metric attributes after aggregation to collect new batches."""
        self.loss = 0.0
        self.n_entries = 0.0

    def add_batch(self, batch, results):
        """ Add a batch to calculate the metric on """

        y = batch[self.target]
        yp = results[self.target]

        self.loss += self.loss_fn(y, yp)
        self.n_entries += np.prod(y.shape)

    def aggregate(self):
        """Aggregate metric over all previously added batches."""
        return self.loss / self.n_entries

    @staticmethod
    def loss_fn(y, yp):
        """Calculates loss function for y and yp"""
        raise NotImplementedError


class MeanSquaredError(Metric):
    r"""
    Metric for mean square error. For non-scalar quantities, the mean of all
    components is taken.

    Args:
        target (str): name of target property
        name (str): name used in logging for this metric. If set to `None`,
            `MSE_[target]` will be used (Default: None)
    """

    def __init__(
        self,
        target,
        name=None,
    ):
        name = "MSE_" + target if name is None else name
        super().__init__(
            target=target,
            name=name,
        )

    @staticmethod
    def loss_fn(y, yp):
        diff = y - yp.view(y.shape)
        return torch.sum(diff.view(-1) ** 2).detach().cpu().data.numpy()

class FalsePositives(Metric):


    def __init__(
        self,
        target,
        name=None,
    ):
        name = "FalsePositive_" + target if name is None else name
        super().__init__(
            target=target,
            name=name,
        )

    @staticmethod
    def loss_fn(y, yp):

        actual = y.detach().cpu().numpy().round().reshape(-1)
        pred = yp.detach().cpu().numpy().round().reshape(-1)
        delta = pred - actual

        false_positives = list(filter(lambda x: x>0, delta))
        num_pred = np.sum(pred)
        num_pred_false = np.sum(false_positives)
        false_rate = num_pred_false / num_pred

        return false_rate


class FalseNegatives(Metric):


    def __init__(
        self,
        target,
        name=None,
    ):
        name = "FalseNegative_" + target if name is None else name
        super().__init__(
            target=target,
            name=name,
        )

    @staticmethod
    def loss_fn(y, yp):

        actual = y.detach().cpu().numpy().round().reshape(-1)
        pred = yp.detach().cpu().numpy().round().reshape(-1)
        delta = pred - actual

        false_negatives= list(filter(lambda x: x < 0, delta))
        num_pred = len(pred) - np.sum(pred)
        num_pred_false = -np.sum(false_negatives)
        false_rate = num_pred_false / num_pred

        return false_rate

class TruePositives(Metric):


    def __init__(
        self,
        target,
        name=None,
    ):
        name = "TruePositive_" + target if name is None else name
        super().__init__(
            target=target,
            name=name,
        )

    @staticmethod
    def loss_fn(y, yp):

        actual = y.detach().cpu().numpy().round().reshape(-1)
        pred = yp.detach().cpu().numpy().round().reshape(-1)
        delta = pred - actual

        true_positives = [i for i, diff in enumerate(delta
            ) if diff == 0 and pred[i] == 1]
        num_pred = np.sum(pred)
        num_pred_correct = np.sum(true_positives)
        correct_rate = num_pred_correct / num_pred

        return correct_rate

class TrueNegatives(Metric):


    def __init__(
        self,
        target,
        name=None,
    ):
        name = "TruePositive_" + target if name is None else name
        super().__init__(
            target=target,
            name=name,
        )

    @staticmethod
    def loss_fn(y, yp):

        actual = y.detach().cpu().numpy().round().reshape(-1)
        pred = yp.detach().cpu().numpy().round().reshape(-1)
        delta = pred - actual

        true_negatives = [i for i, diff in enumerate(delta
            ) if diff == 0 and pred[i] == 0]
        num_pred = np.sum(pred)
        num_pred_correct = np.sum(true_negatives)
        correct_rate = num_pred_correct / num_pred

        return correct_rate

class RootMeanSquaredError(MeanSquaredError):
    r"""
    Metric for root mean square error. For non-scalar quantities, the mean of
    all components is taken.

    Args:
        target (str): name of target property
        name (str): name used in logging for this metric. If set to `None`,
            `RMSE_[target]` will be used (Default: None)
    """

    def __init__(
        self,
        target,
        name=None,
    ):
        name = "RMSE_" + target if name is None else name
        super().__init__(
            target, name
        )

    def aggregate(self):
        """Aggregate metric over all previously added batches."""
        return np.sqrt(self.loss / self.n_entries)


class MeanAbsoluteError(Metric):
    r"""
    Metric for mean absolute error. For non-scalar quantities, the mean of all
    components is taken.

    Args:
        target (str): name of target property
        name (str): name used in logging for this metric. If set to `None`,
            `MAE_[target]` will be used (Default: None)
    """

    def __init__(
        self,
        target,
        name=None,
    ):
        name = "MAE_" + target if name is None else name
        super().__init__(
            target=target,
            name=name,
        )

    @staticmethod
    def loss_fn(y, yp):
        # pdb.set_trace()
        diff = y - yp.view(y.shape)
        return torch.sum(torch.abs(diff).view(-1)).detach().cpu().data.numpy()

