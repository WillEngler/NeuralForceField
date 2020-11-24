# Neural Force Field

The Neural Force Field (NFF) code is an API based on SchNet [1-4] and DimeNet [5]. It provides an interface to train and evaluate neural networks for force fields. It can also be used as a property predictor that uses both 3D geometries and 2D graph information [6].

This code repository is developed in the Learning Matter Lab (led by prof. Rafael Gomez-Bombarelli) at MIT. Please do not distribute.

## Installation from source

This software requires the following packages:

- [scikit-learn=0.23.1](http://scikit-learn.org/stable/)
- [PyTorch=1.4](http://pytorch.org)
- [ase=3.19.1](https://wiki.fysik.dtu.dk/ase/)
- [pandas=1.0.5](https://pandas.pydata.org/)
- [networkx=2.4](https://networkx.github.io/)
- [pymatgen=2020.7.3](https://pymatgen.org/)
- [sympy=1.6.1](https://www.sympy.org/)
- [rdkit=2020.03.3](https://www.rdkit.org/)
- [sigopt=5.3.1](https://sigopt.com/)
- [e3fp=1.2.1](https://github.com/keiserlab/e3fp)
- [ipykernel==5.3.0](https://github.com/ipython/ipykernel)
- [hyperopt=0.2.5](https://github.com/hyperopt/hyperopt)

We highly recommend to create a `conda` environment to run the code. To do that, use the following commands:

```bash
conda upgrade conda
conda create -n nff python=3.7 scikit-learn pytorch\>=1.2.0 cudatoolkit=10.0 ase pandas pymatgen sympy rdkit hyperopt jq -c pytorch -c conda-forge -c rdkit
```

Next install remaining pip requirements:

```bash
conda activate nff
pip install sigopt e3fp ipykernel
```

To ensure that the `nff` environment is accessible through Jupyter, add the the `nff` display name:
```bash
python -m ipykernel install --user --name nff --display-name "Python [conda env:nff"]
```

Finally, install the `nff` package by running:

```bash
pip install .
```



## Usage

### Command line

#### Force field
The simplest way to use the `nff` package is to use the premade scripts (in the `scripts`) folder. As an example, to train a SchNet model with the default parameters using the example dataset (ethanol geometries) from the command line, run the command

```bash
nff_train.py train schnet tutorials/data/dataset.pth.tar $HOME/train_model --device cuda:0
```
This will use 60% of the dataset for training, 20% for validation and 20% for testing. The training will happen on the device `cuda:0`. Results of training, checkpoints and hyperparameters will be saved on the path `$HOME/train_model`.

#### Property predictor
NFF also contains modules that predict properties from 3D geometries of conformers. These include the SchNet model, expanded to include multiple conformers, as well as the ChemProp3D (CP3D)  model, which also includes graph information. A series of scripts for these modules can be found in `scripts/cp3d`. An in-depth discussion of how to use these scripts can be found in `scripts/cp3d/README.md`.   


### Usage with Jupyter Notebooks and other scripts

#### Force field
A series of tutorials illustrating how `nff` can be used in conjunction with Jupyter Notebooks or other scripts is provided in the `tutorials/` folder. It also covers how to integrate a pre-trained model with an ASE calculator.

#### Property predictor
While `scripts/cp3d/README.md` explains in depth how to use the scripts, the notebook `06_cp3d.ipynb` goes into some detail about what happens behind the scenes. In this notebook you'll see how the datasets get made and what the models look like.

## References

* [1] K.T. Schütt. F. Arbabzadah. S. Chmiela, K.-R. Müller, A. Tkatchenko.  
*Quantum-chemical insights from deep tensor neural networks.*
Nature Communications **8**. 13890 (2017)   
[10.1038/ncomms13890](http://dx.doi.org/10.1038/ncomms13890)

* [2] K.T. Schütt. P.-J. Kindermans, H. E. Sauceda, S. Chmiela, A. Tkatchenko, K.-R. Müller.  
*SchNet: A continuous-filter convolutional neural network for modeling quantum interactions.*
Advances in Neural Information Processing Systems 30, pp. 992-1002 (2017) [link](http://papers.nips.cc/paper/6700-schnet-a-continuous-filter-convolutional-neural-network-for-modeling-quantum-interactions)

* [3] K.T. Schütt. P.-J. Kindermans, H. E. Sauceda, S. Chmiela, A. Tkatchenko, K.-R. Müller.  
*SchNet - a deep learning architecture for molecules and materials.* 
The Journal of Chemical Physics 148(24), 241722 (2018) [10.1063/1.5019779](https://doi.org/10.1063/1.5019779)

* [4] K.T. Schütt, P. Kessel, M. Gastegger, K. Nicoli, A. Tkatchenko, K.-R. Müller.
*SchNetPack: A Deep Learning Toolbox For Atomistic Systems.*
J. Chem. Theory Comput. **15**(1), 448-455 (2019). [10.1021/acs.jctc.8b00908](https://doi.org/10.1021/acs.jctc.8b00908)

* [5] J. Klicpera, G. Janek, S. Günnemann. *Directional message passing for molecular graphs.* ICLR (2020). [URL](https://openreview.net/attachment?id=B1eWbxStPH&name=original_pdf).

* [6] K. Yang, *et. al*. *Analyzing learned molecular representations for property prediction.*
J. Chem. Info. Model. **59**(8), 3370-3388 (2019). [10.1021/acs.jcim.9b00237](https://doi.org/10.1021/acs.jcim.9b00237)


