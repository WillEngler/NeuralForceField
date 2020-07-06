"""This file provides a trainer wrapper for the neural force field.

Adapted from https://github.com/atomistic-machine-learning/schnetpack/blob/dev/src/schnetpack/train/trainer.py
"""

import os
import numpy as np
import torch
import copy
import pickle

from nff.utils.cuda import batch_to
from nff.train.evaluate import evaluate
from nff.train.parallel import update_optim

MAX_EPOCHS = 100


class Trainer:
    r"""Class to train a model.

    This contains an internal training loop which takes care of validation
        and can be extended with custom functionality using hooks.

    Args:
       model_path (str): path to the model directory.
       model (torch.Module): model to be trained.
       loss_fn (callable): training loss function.
       optimizer (torch.optim.optimizer.Optimizer): training optimizer.
       train_loader (torch.utils.data.DataLoader): data loader for 
         training set.
       validation_loader (torch.utils.data.DataLoader): data loader for 
         validation set.
       checkpoints_to_keep (int, optional): number of saved checkpoints.
       checkpoint_interval (int, optional): intervals after which checkpoints 
         is saved.
       hooks (list, optional): hooks to customize training process.
       loss_is_normalized (bool, optional): if True, the loss per data point will be
           reported. Otherwise, the accumulated loss is reported.
       global_rank (int, optional): overall rank of the current gpu for parallel
            training (e.g. for the second gpu on the third node, with four
            gpus per node, the global rank is 7).
       world_size (int, optional): the total number of gpus over which training is
            parallelized.
       max_batch_iters (int, optional): if you're training in parallel and have pre-split
            the datasets that will be loaded on different nodes, the batch sizes per epoch
            may be different in each dataset. max_batch_iters is the smallest number of
            batches contained in an epoch among the split datasets.
       model_kwargs (dict, optional): any kwargs that may be needed when calling the model
       mol_loss_norm (bool, optional): whether to normalize the loss by the number of molecules
            in a batch
       del_grad_interval (int, optional): if training in parallel and writing gradients to disk,
            this is the number of batches that must pass before deleting old gradients.
       metric_as_loss (str, optional): if specified, use this metric to determine which validation
            epoch was the best, rather than the validation loss.
       metric_objective (str, optional): if metric_as_loss is specified, metric_objective indicates
            whether the goal is to maximize or minimize `metric_as_loss`.



   """

    def __init__(
        self,
        model_path,
        model,
        loss_fn,
        optimizer,
        train_loader,
        validation_loader,
        mini_batches=1,
        checkpoints_to_keep=3,
        checkpoint_interval=10,
        validation_interval=1,
        hooks=None,
        loss_is_normalized=True,
        global_rank=0,
        world_size=1,
        max_batch_iters=None,
        model_kwargs=None,
        mol_loss_norm=False,
        del_grad_interval=10,
        metric_as_loss=None,
        metric_objective=None
    ):
        self.model_path = model_path
        self.checkpoint_path = os.path.join(self.model_path, "checkpoints")
        self.best_model = os.path.join(self.model_path, "best_model")
        self.train_loader = train_loader
        self.validation_loader = validation_loader
        self.validation_interval = validation_interval
        self.checkpoints_to_keep = checkpoints_to_keep
        self.hooks = [] if hooks is None else hooks
        self.loss_is_normalized = loss_is_normalized
        self.mol_loss_norm = mol_loss_norm
        self.mini_batches = mini_batches

        self._model = model
        self._stop = False
        self.checkpoint_interval = checkpoint_interval

        self.loss_fn = loss_fn
        self.optimizer = optimizer

        self.torch_parallel = self._check_is_parallel()
        self.parallel = world_size > 1
        self.global_rank = global_rank
        self.world_size = world_size
        self.par_folders = self.get_par_folders()
        # whether this is the base process for parallel training
        self.base = global_rank == 0
        # how many times you've called loss.backward()
        self.back_count = 0
        self.max_batch_iters = max_batch_iters if (
            max_batch_iters is not None) else len(self.train_loader)
        self.model_kwargs = model_kwargs if (model_kwargs is not None
                                             ) else {}
        self.batch_stop = False
        self.nloss = 0
        self.del_grad_interval = del_grad_interval
        self.metric_as_loss = metric_as_loss
        self.metric_objective = metric_objective

        restore = False
        if os.path.exists(self.checkpoint_path):
            try:
                self.restore_checkpoint()
                restore = True
            except:
                pass
        if not restore:
            self.epoch = 0
            self.step = 0
            self.best_loss = float("inf")

            # only make the checkpoint and save to it
            # if this is the base process

            if self.base:
                os.makedirs(self.checkpoint_path)
                self.store_checkpoint()

    def to(self, device):
        """Changes the device"""
        self._model.device = device
        self._model.to(device)
        self.optimizer.load_state_dict(self.optimizer.state_dict())

    def _check_is_parallel(self):
        data_par = isinstance(self._model, torch.nn.DataParallel)
        dist_dat_par = isinstance(self._model,
                                  torch.nn.parallel.DistributedDataParallel)
        return any((data_par, dist_dat_par))

    def _load_model_state_dict(self, state_dict):
        if self.parallel:
            self._model.module.load_state_dict(state_dict)
        else:
            self._model.load_state_dict(state_dict)

    def get_best_model(self):
        try:
            return torch.load(self.best_model)
        except EOFError:
            # if we had tried to save a model and the
            # pickling failed (e.g. dimenet), then
            # load the best state_dict instead
            state_path = self.best_model + ".pth.tar"
            state_dict = torch.load(state_path)
            model = copy.deepcopy(self._model)
            model.load_state_dict(state_dict["model"])

            return model

    def call_model(self, batch, train):

        if (self.torch_parallel and self.parallel) and not train:
            model = self._model.module
        else:
            model = self._model

        return model(batch, **self.model_kwargs)

    @property
    def state_dict(self):
        state_dict = {
            "epoch": self.epoch,
            "step": self.step,
            "best_loss": self.best_loss,
            "optimizer": self.optimizer.state_dict(),
            "hooks": [h.state_dict for h in self.hooks],
        }
        if self.torch_parallel:
            state_dict["model"] = self._model.module.state_dict()
        else:
            state_dict["model"] = self._model.state_dict()
        return state_dict

    @state_dict.setter
    def state_dict(self, state_dict):
        self.epoch = state_dict["epoch"]
        self.step = state_dict["step"]
        self.best_loss = state_dict["best_loss"]
        self.optimizer.load_state_dict(state_dict["optimizer"])
        self._load_model_state_dict(state_dict["model"])

        for h, s in zip(self.hooks, self.state_dict["hooks"]):
            h.state_dict = s

    def store_checkpoint(self):
        chkpt = os.path.join(
            self.checkpoint_path, "checkpoint-" + str(self.epoch) + ".pth.tar"
        )
        torch.save(self.state_dict, chkpt)

        chpts = [f for f in os.listdir(
            self.checkpoint_path) if f.endswith(".pth.tar")]
        if len(chpts) > self.checkpoints_to_keep:
            chpt_epochs = [int(f.split(".")[0].split("-")[-1]) for f in chpts]
            sidx = np.argsort(chpt_epochs)
            for i in sidx[: -self.checkpoints_to_keep]:
                os.remove(os.path.join(self.checkpoint_path, chpts[i]))

    def restore_checkpoint(self, epoch=None):
        if epoch is None:
            epoch = max(
                [
                    int(f.split(".")[0].split("-")[-1])
                    for f in os.listdir(self.checkpoint_path)
                    if f.startswith("checkpoint")
                ]
            )

        chkpt = os.path.join(
            self.checkpoint_path, "checkpoint-" + str(epoch) + ".pth.tar"
        )
        self.state_dict = torch.load(chkpt, map_location='cpu')

    def loss_backward(self, loss):
        loss.backward()
        self.back_count += 1
        self.batch_stop = self.back_count == self.max_batch_iters

        if self.batch_stop:
            self.back_count = 0

    def grad_is_nan(self):

        for group in self.optimizer.param_groups:
            for param in group['params']:
                if torch.isnan(param.grad).any():
                    return True
        return False

    def get_loss(self, batch, results):

        if not any((self.mol_loss_norm,
                    self.loss_is_normalized)):

            loss = self.loss_fn(batch, results)
            return loss

        if self.mol_loss_norm:
            vsize = len(batch["num_atoms"])

        elif self.loss_is_normalized:
            vsize = batch['nxyz'].size(0)

        self.nloss += vsize
        loss = self.loss_fn(batch, results) * vsize

        return loss

    def optim_step(self, batch_num, device):

        if self.parallel and not self.torch_parallel:
            self.optimizer = update_optim(optimizer=self.optimizer,
                                          loss_size=self.nloss,
                                          rank=self.global_rank,
                                          world_size=self.world_size,
                                          weight_path=self.model_path,
                                          batch_num=batch_num,
                                          epoch=self.epoch,
                                          del_interval=self.del_grad_interval,
                                          device=device,
                                          max_batch_iters=self.max_batch_iters)
            if not self.grad_is_nan():
                self.optimizer.step()
            self.nloss = 0

            return

        if self.nloss != 0:
            for group in self.optimizer.param_groups:
                for param in group['params']:
                    param.grad /= self.nloss
            self.nloss = 0

        if not self.grad_is_nan():
            self.optimizer.step()

    def train(self, device, n_epochs=MAX_EPOCHS):
        """Train the model for the given number of epochs on a specified 
        device.

        Args:
            device (torch.torch.Device): device on which training takes place.
            n_epochs (int): number of training epochs.

        Note: Depending on the `hooks`, training can stop earlier than `n_epochs`.

        """
        self.to(device)

        self._stop = False
        # initialize loss, num_batches, and optimizer grad to 0
        loss = torch.tensor(0.0).to(device)
        num_batches = 0
        self.optimizer.zero_grad()

        for h in self.hooks:
            h.on_train_begin(self)
            if hasattr(h, "mini_batches"):
                h.mini_batches = self.mini_batches

        try:
            for _ in range(n_epochs):
                self._model.train()

                self.epoch += 1

                for h in self.hooks:
                    h.on_epoch_begin(self)

                if self._stop:
                    break

                for j, batch in enumerate(self.train_loader):

                    batch = batch_to(batch, device)

                    for h in self.hooks:
                        h.on_batch_begin(self, batch)

                    results = self.call_model(batch, train=True)
                    mini_loss = self.get_loss(batch, results)
                    self.loss_backward(mini_loss)
                    if not torch.isnan(mini_loss):
                        loss += mini_loss.cpu().detach().to(device)

                    self.step += 1
                    # update the loss self.minibatches number
                    # of times before taking a step
                    num_batches += 1

                    if num_batches == self.mini_batches:

                        num_batches = 0
                        # effective number of batches so far
                        eff_batches = int((j + 1) / self.mini_batches)

                        self.optim_step(batch_num=eff_batches,
                                        device=device)

                        for h in self.hooks:
                            h.on_batch_end(self, batch, results, loss)

                        # reset loss and the optimizer grad

                        loss = torch.tensor(0.0).to(device)
                        self.optimizer.zero_grad()

                    if self.batch_stop:
                        break

                    if self._stop:
                        break

                # reset for next epoch

                del mini_loss
                num_batches = 0
                loss = torch.tensor(0.0).to(device)
                self.optimizer.zero_grad()

                # store the checkpoint only if this is the base model,
                # otherwise it will get stored unnecessarily from other
                # gpus, which will cause IO issues

                if (self.epoch % self.checkpoint_interval == 0
                        and self.base):
                    self.store_checkpoint()

                # validation
                if (self.epoch % self.validation_interval == 0 or self._stop):
                    self.validate(device)

                for h in self.hooks:
                    h.on_epoch_end(self)

                if self._stop:
                    break

            # Training Ends
            # run hooks & store checkpoint
            for h in self.hooks:
                h.on_train_ends(self)

            if self.base:
                self.store_checkpoint()

        except Exception as e:
            for h in self.hooks:
                h.on_train_failed(self)

            raise e

    def get_par_folders(self):
        """
        Get the folders inside the model folder that contain information
        about the other parallel training processes.
        Args: 
            None
        Returns:
            par_folders (list): paths to the folders of all parallel
                training processes.

        """

        # each parallel folder just has the name of its global rank

        par_folders = [os.path.join(self.model_path, str(i))
                       for i in range(self.world_size)]
        self_folder = par_folders[self.global_rank]

        # if the folder of this global rank doesn't exist yet then
        # create it

        if not os.path.isdir(self_folder):
            os.makedirs(self_folder)

        return par_folders

    def save_val_loss(self, val_loss):
        """
        Save the validation loss from this trainer. Necessary for averaging
        validation losses over all parallel trainers.
        Args:
            val_loss (torch.Tensor): validation loss from this trainer
        Returns:
            None
        """

        self_folder = self.par_folders[self.global_rank]

        # write the loss as a number to a file called "val_epoch_i"
        # for epoch i.

        info_file = os.path.join(
            self_folder, "val_epoch_{}".format(self.epoch))
        with open(info_file, "w") as f:
            f.write(str(val_loss.item()))

    def load_val_loss(self):
        """
        Load the validation losses from the other parallel processes.
        Args:
            None
        Returns:
            avg_loss (float): validation loss averaged among all
                processes if self.loss_is_normalized = True,
                and added otherwise.
        """

        # Initialize a dictionary with the loss from each parallel
        # process. We will know that all processes are done once
        # `None` is no longer in this dictionary.

        loaded_vals = {folder: None for folder in self.par_folders}

        while None in list(loaded_vals.values()):
            for folder in self.par_folders:
                # if the folder has a dictionary value already,
                # then no need to load anything
                if loaded_vals[folder] is not None:
                    continue
                val_file = os.path.join(
                    folder, "val_epoch_{}".format(self.epoch))
                # try opening the file and getting the value
                try:
                    with open(val_file, "r") as f:
                        val_loss = float(f.read())
                    loaded_vals[folder] = val_loss
                except (ValueError, FileNotFoundError):
                    continue

        # this isn't quite right for mol_loss_norm
        if self.loss_is_normalized or self.mol_loss_norm:
            # average the losses
            avg_loss = np.mean(list(loaded_vals.values()))
        else:
            # add the losses
            avg_loss = np.sum(list(loaded_vals.values()))

        return avg_loss

    def save(self, model):
        """
        Save the model
        Args:
            model (str): path of best model
        Returns:
            None
        """
        # try to save the model
        try:
            torch.save(model, self.best_model)
        # Sometimes you can't pickle the model (e.g. dimenet)
        # In that case just save the state dict, which can
        # be pickled
        except (AttributeError, pickle.PicklingError):
            state_path = self.best_model + ".pth.tar"
            torch.save(self.state_dict, state_path)

    def save_as_best(self):
        """
        Save model as the current best model.
        """

        # only save if you're the base process
        if not self.base:
            return

        if self.torch_parallel:
            # need to save model.module, not model, in the
            # parallel case
            self.save(self._model.module)
        else:
            self.save(self._model)

    def validate(self, device):
        """Validate the current state of the model using the validation set
        """

        self._model.eval()

        for h in self.hooks:
            h.on_validation_begin(self)

        val_loss = 0.0
        n_val = 0

        for val_batch in self.validation_loader:

            val_batch = batch_to(val_batch, device)

            # append batch_size
            if self.mol_loss_norm:
                vsize = len(val_batch["num_atoms"])

            elif self.loss_is_normalized:
                vsize = val_batch['nxyz'].size(0)

            n_val += vsize

            for h in self.hooks:
                h.on_validation_batch_begin(self)

            results = self.call_model(val_batch, train=False)

            val_batch_loss = self.loss_fn(
                val_batch, results).data.cpu().numpy()

            if self.loss_is_normalized or self.mol_loss_norm:
                val_loss += val_batch_loss * vsize
            else:
                val_loss += val_batch_loss

            for h in self.hooks:
                h.on_validation_batch_end(self, val_batch, results)

        # weighted average over batches
        if self.loss_is_normalized or self.mol_loss_norm:
            val_loss /= n_val

        # if running in parallel, savee the validation loss
        # and pick up the losses from the other processes too

        if self.parallel:
            self.save_val_loss(val_loss)
            val_loss = self.load_val_loss()

        for h in self.hooks:
            h.on_validation_end(self, val_loss)
            metric_dic = getattr(h, "metric_dic", None)
            if metric_dic is None:
                continue
            if self.metric_as_loss in metric_dic:
                val_loss = metric_dic[self.metric_as_loss]
                if self.metric_objective.lower() == "maximize":
                    val_loss *= -1

        if self.best_loss > val_loss:
            self.best_loss = val_loss
            self.save_as_best()

    def evaluate(self, device):
        """Evaluate the current state of the model using the validation loader
        """
        return evaluate(
            self._model,
            self.validation_loader,
            self.loss_fn,
            device,
            self.loss_is_normalized
        )
