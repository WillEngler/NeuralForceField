from projects.NeuralForceField.models import *
from projects.NeuralForceField.scatter import *
from projects.NeuralForceField.MD import * 

import matplotlib.pyplot as plt

import pickle
import numpy as np

import torch.optim as optim

import os
import json
import datetime
import time

class Model():

    """Summary
    
    Attributes:
        criterion (MSEloss): Description
        data (graph): Description
        device (TYPE): Description
        dir_loc (TYPE): Description
        energy_mae (float): Description
        f_predict (list): Description
        f_true (list): Description
        force_mae (float): Description
        graph_batching (Boolean): If True, use graph batch input
        job_name (str): name of the job or experimeent
        mae (TYPE): Description
        model (TYPE): Description
        model_path (TYPE): Description
        N_batch (int): Description
        N_test (int): Description
        N_train (int): Description
        optimizer (TYPE): Description
        par (dict): a dictionary file for hyperparameters
        root (str): the root path for the saving training results 
        scheduler (TYPE): Description
        train_f_log (list): Description
        train_u_log (list): Description
        u_predict (list): Description
        u_true (list): Description
    """

    def __init__(self,par, graph_data, device, job_name, graph_batching=False, root="./"):
        """Summary
        
        Args:
            par (TYPE): Description
            graph_data (TYPE): Description
            device (TYPE): Description
            job_name (TYPE): Description
            graph_batching (bool, optional): Description
            root (str, optional): Description
        """
        self.device = device
        self.par = par 
        self.data = graph_data
        self.job_name = job_name
        self.root = root 
        self.initialize_log()
        self.initialize_data()
        self.initialize_model()
        self.initialize_optim()
        self.graph_batching = graph_batching
        
    def initialize_model(self):

        with open(self.dir_loc + "/par.json", "w") as write_file:
            json.dump(self.par, write_file, indent=4)
        
        self.model = Net(n_atom_basis = self.par["n_atom_basis"],
                            n_filters = self.par["n_filters"],
                            n_gaussians= self.par["n_gaussians"], 
                            cutoff_soft= self.par["cutoff"], 
                            trainable_gauss = self.par["trainable_gauss"],
                            T = self.par["T"],
                            device= self.device).to(self.device)
    
    def initialize_optim(self):
        self.optimizer = optim.Adam(list(self.model.parameters()), lr=self.par["optim"])
        
        if self.par["scheduler"]:
            self.scheduler = optim.lr_scheduler.ReduceLROnPlateau(self.optimizer, 
                                                                  'min', 
                                                                  min_lr=1.5e-7, 
                                                                  verbose=True, factor = 0.5, patience= 50,
                                                                  threshold= 5e-5)
        self.criterion = torch.nn.MSELoss()
        self.mae = torch.nn.L1Loss()
        
    def initialize_log(self):
        
        self.train_u_log = []
        self.train_f_log = []

        # create directoires if not exists 
        if not os.path.exists(self.root):
            os.makedirs(self.root)
            
        # obtain a time stamp 
        currentDT = datetime.datetime.now()
        date = str(currentDT).split()[0].split("-")[1:] 
        self.dir_loc = self.root + self.job_name + "_" + "".join(date)
        
        if not os.path.exists(self.dir_loc):
            os.makedirs(self.dir_loc)
        
    def initialize_data(self):
        
        self.N_batch = len(self.data.batches)
        self.N_train = int(self.par["train_percentage"] * self.N_batch)
        self.N_test = self.N_batch - self.N_train - 1 # ignore the last batch 
        
    def parse_batch(self, index, data=None):
        """Summary
        
        Args:
            index (int): index of the batch in GraphDataset
        
        Returns:
            TYPE: Description
        """
        if data == None:
            data = self.data

        a = data.batches[index].data["a"].to(self.device)

        r = data.batches[index].data["r"][:, [0]].to(self.device)

        f = data.batches[index].data["r"][:, 1:4].to(self.device) #* (627.509 / 0.529177) # convert to kcal/mol A

        u = data.batches[index].data["y"].to(self.device) #* 627.509 # kcal/mol 
        
        N = data.batches[index].data["N"]
        
        xyz = data.batches[index].data["xyz"].to(self.device)
        
        return xyz, a, r, f, u, N
    
    def train(self, N_epoch):
        """Summary
        
        Args:
            N_epoch (TYPE): Description
        """

        self.start_time = time.time()

        for epoch in range(N_epoch):

            # check if max epoches are reached 
            if len(self.train_f_log) >= self.par["max_epoch"]:
                print("max epoches reached")
                break 

            train_u_mae = 0.0
            train_force_mae = 0.0
            
            for i in range(self.N_train):

                xyz, a, r, f, u, N = self.parse_batch(i)
                xyz.requires_grad = True

                # check if the input has graphs of various sizes 
                if len(set(N)) == 1:
                    graph_size_is_same = True
                else:
                    graph_size_is_same = False
                
                # Compute energies 
                if self.graph_batching:
                    U = self.model(r=r, xyz=xyz, a=a, N=N)
                else:
                    assert graph_size_is_same # make sure all the graphs needs to have the same size
                    U = self.model(r=r.reshape(-1, N[0]), xyz=xyz.reshape(-1, N[0], 3))
                    
                f_pred = -compute_grad(inputs=xyz, output=U)

                # comput loss
                loss_force = self.criterion(f_pred, f)
                loss_u = self.criterion(U, u)
                loss = loss_force + self.par["rho"] * loss_u

                # update parameters
                self.optimizer.zero_grad()
                loss.backward()
                self.optimizer.step()

                # compute MAE
                train_u_mae += self.mae(U, u) # compute MAE
                train_force_mae += self.mae(f_pred, f)

            # averaging MAE
            train_u = train_u_mae.data[0]/self.N_train
            train_force = train_force_mae.data[0]/self.N_train
            
            self.train_u_log.append(train_u.item())
            self.train_f_log.append(train_force.item())
            
            # scheduler
            if self.par["scheduler"] == True:
                self.scheduler.step(train_force)
            else:
                pass

            # print loss
            print("epoch %d  U train: %.3f  force train %.3f" % (epoch, train_u, train_force))

            self.time_elapsed = time.time() - self.start_time

            # check convergence 
            #if self.check_convergence():
            #    print("training converged")
            #    break
            #else:
            #    pass

        self.save_model()
        self.save_train_log()
            
    def validate(self, data=None):
        """Summary
        """
        self.f_predict = []
        self.f_true = []
        self.u_predict = []
        self.u_true = []

        # decide data 
        if data == None:
            data = self.data#.batches[self.N_train: self.N_train + self.N_test - 1]
            start_index = self.N_train - 1
            N_test = self.N_test
        else:
            start_index = 0 
            N_test = len(data.batches)

        for i in range(N_test):

            # parse_data
            xyz, a, r, f, u, N = self.parse_batch(start_index + i, data=data)
            xyz.requires_grad = True

            if self.graph_batching:
                u_pred = self.model(r=r, xyz=xyz, a=a, N=N)
            else:
                u_pred = self.model(r=r.reshape(-1, N[0]), xyz=xyz.reshape(-1, N[0], 3))
                
            f_pred = -compute_grad(inputs=xyz, output=u_pred).reshape(-1)

            self.f_predict.append(f_pred.detach().cpu().numpy())
            self.f_true.append(f.reshape(-1).detach().cpu().numpy())
            
            self.u_predict.append(u_pred.detach().cpu().numpy())
            self.u_true.append(u.reshape(-1).detach().cpu().numpy())

        self.f_true = np.concatenate( self.f_true, axis=0 ).reshape(-1)
        self.f_predict = np.concatenate( self.f_predict, axis=0 ).reshape(-1)
        self.u_true = np.concatenate( self.u_true, axis=0 ).reshape(-1)
        self.u_predict = np.concatenate( self.u_predict, axis=0 ).reshape(-1)
        
        # compute force & energy MAE
        self.force_mae = np.abs(self.f_predict - self.f_true).mean()
        self.energy_mae = np.abs(self.u_predict - self.u_true).mean()
        
        f = plt.figure(figsize=(13,6))
        ax = f.add_subplot(121)
        ax.set_title("force components validation")
        ax2 = f.add_subplot(122)
        ax2.set_title("energies validation")

        ax.scatter(self.f_true , self.f_predict, label="force MAE: " + str(self.force_mae) + " kcal/mol A")
        ax.set_xlabel("test")
        ax.set_ylabel("prediction")
        ax.legend()
        ax2.scatter(self.u_true, self.u_predict, label="energy MAE: " + str(self.energy_mae) + " kcal/mol")
        ax2.set_xlabel("test")
        ax2.set_ylabel("prediction")
        ax2.legend()
       
    def save_model(self):
        self.model_path = self.dir_loc + "/model.pt"
        torch.save(self.model.state_dict(), self.model_path)

    def load_model(self):
        if self.model_path is not None:
            self.model.load_state_dict(torch.load(self.model_path))
        else:
            print("no model saved for this training session, please save your model first")
    
    def save_train_log(self):
        # save the training log for energies and force 
        log = np.array([self.train_u_log, self.train_f_log]).transpose()
        np.savetxt(self.dir_loc + "/log.csv", log, delimiter=",")
        
    def load_train_log(self):
        log = np.loadtxt(self.dir_loc + "/log.csv", delimiter=",")
        return log

    def check_convergence(self):
        """function to check convergences, currently only check for the convergences of the forces 
        
        Args:
            epoch (int): 
        
        Returns:
            Boolean: True if training converged
        """
        eps = self.par["eps"] # convergence tolerence 
        patience = 50 # make patience tunable

        # compute improvement by running averages 
        if len(self.train_f_log) > patience * 2:
            dif = (np.array(self.train_f_log[-patience * 2: -patience :]) - np.array(self.train_f_log[-patience:])).mean()
            improvement = dif / np.array(self.train_f_log[-patience:]).mean()

            if improvement < eps:
                converge = True
            else: 
                converge = False
        else:
            converge = False 

        return converge

    def save_summary(self):
        # the final test loss, number of epochs trained

        self.validate() 

        train_state = dict()
        train_state["epoch_trained"] = len(self.train_f_log)
        train_state["test_force_mae"] = self.force_mae.item()
        train_state["test_energy_mae"] = self.energy_mae.item()
        train_state["time_per_epoch"] = self.time_elapsed / len(self.train_f_log)

        # dump json 
        with open(self.dir_loc+"/results.json", "w") as write_file:
            json.dump(train_state, write_file , indent=4)

        # dump test and predict energy and forces 
        val_energy = np.array([self.u_true, self.u_predict]).transpose()
        val_force = np.array([self.f_true, self.f_predict]).transpose()

        np.savetxt(self.dir_loc + "/val_energy.csv", val_energy, delimiter=",")
        np.savetxt(self.dir_loc + "/val_force.csv", val_force, delimiter=",")


    def NVE(self, T=450.0, dt=0.01, steps=1000, save_frequency=20):

        # save NVE energy fluctuations, Kinetic energies and movies 
        # choose a starting conformation 
        xyz, a, r, f, u, N = self.parse_batch(0)

        xyz = xyz.reshape(-1, N[0], 3)
        xyz = xyz[0].detach().cpu().numpy()
        r = r.reshape(-1, N[0])
        r = r[0].detach().cpu().numpy()

        structure = mol_state(r=r,xyz=xyz)
        structure.set_calculator(NeuralMD(model=self.model, device=self.device))
        # Set the momenta corresponding to T= 0.0 K
        MaxwellBoltzmannDistribution(structure, T * units.kB)
        # We want to run MD with constant energy using the VelocityVerlet algorithm.
        dyn = VelocityVerlet(structure, 0.1 * units.fs)
        # Now run the dynamics
        traj = []
        thermo = []
        
        n_epoch = int(steps/save_frequency)

        for i in range(n_epoch):
            dyn.run(save_frequency)
            traj.append(structure.get_positions())
            epot, ekin, Temp = get_energy(structure)
            thermo.append([epot, ekin, ekin+epot, Temp])

        # save thermo data 
        thermo = np.array(thermo)
        np.savetxt(self.dir_loc + "/thermo.dat", thermo, delimiter=",")

        # write movies 
        traj = np.array(traj)
        traj = traj - traj.mean(1).reshape(-1,1,3)
        Z = np.array([r] * len(traj)).reshape(len(traj), r.shape[0], 1)
        traj_write = np.dstack(( Z, traj))
        write_traj(filename=self.dir_loc+"/traj.xyz", frames=traj_write)