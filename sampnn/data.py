import os
import re
import random
import sys
import math
import csv
import torch
import functools
import argparse
import numpy as np

# from scipy.special import gamma

from torch.utils.data import Dataset, DataLoader
from torch.utils.data.sampler import SubsetRandomSampler

from sampnn.features import LoadFeaturiser
from sampnn.parse import parse


def input_parser():
    '''
    parse input
    '''
    parser = argparse.ArgumentParser(description='Structure Agnostic Message Passing Neural Network')

    # misc inputs
    parser.add_argument('data_options', metavar='OPTIONS', nargs='+', help='dataset options, started with the path to root dir,then other options')
    parser.add_argument('--disable-cuda', action='store_true', help='Disable CUDA')
    parser.add_argument('--print-freq', default=10, type=int, metavar='N', help='print frequency (default: 10)')
    
    # restart inputs
    parser.add_argument('--resume', default='', type=str, metavar='PATH', help='path to latest checkpoint (default: none)')
    parser.add_argument('--start-epoch', default=0, type=int, metavar='N', help='manual epoch number (useful on restarts)')
    
    # dataloader inputs
    parser.add_argument('--workers', default=0, type=int, metavar='N', help='number of data loading workers (default: 0)')
    parser.add_argument('--batch-size', default=64, type=int, metavar='N', help='mini-batch size (default: 256)')    
    parser.add_argument('--train-size', default=0.6, type=float, metavar='N', help='proportion of data for training')
    parser.add_argument('--val-size', default=0.2, type=float, metavar='N', help='proportion of data for validation')
    parser.add_argument('--test-size', default=0.2, type=float, metavar='N', help='proportion of data for testing')
    
    # optimiser inputs
    parser.add_argument('--optim', default='SGD', type=str, metavar='SGD', help='choose an optimizer, SGD or Adam, (default: SGD)')
    parser.add_argument('--epochs', default=1100, type=int, metavar='N', help='number of total epochs to run (default: 30)')
    parser.add_argument('--learning-rate', default=0.001, type=float, metavar='LR', help='initial learning rate (default: 0.01)')
    parser.add_argument('--lr-milestones', default=[100], nargs='+', type=int, metavar='N', help='milestones for scheduler (default: [100])')
    parser.add_argument('--momentum', default=0.9, type=float, metavar='M', help='momentum')
    parser.add_argument('--weight-decay', default=0, type=float, metavar='W', help='weight decay (default: 0)')
    
    # graph inputs
    parser.add_argument('--atom-fea-len', default=64, type=int, metavar='N', help='number of hidden atom features in conv layers')
    parser.add_argument('--h-fea-list', default=[128,64,32], nargs='+', type=int, metavar='N', help='number of hidden features after pooling')
    parser.add_argument('--n-conv', default=3, type=int, metavar='N', help='number of conv layers')

    args = parser.parse_args(sys.argv[1:])

    args.cuda = not args.disable_cuda and torch.cuda.is_available()

    return args


def get_data_loaders(dataset, batch_size=64, train_size=0.6,
                    val_size=0.2, test_size=0.2,
                    num_workers=1, pin_memory=False):
    """
    Utility function for dividing a dataset to train, val, test datasets.
    """

    assert train_size + val_size + test_size <= 1
    total = len(dataset)
    indices = list(range(len(dataset)))
    train = math.floor(total * train_size)
    val = math.floor(total * val_size)
    test = math.floor(total * test_size)

    train_sampler = SubsetRandomSampler(indices[:train])
    val_sampler = SubsetRandomSampler(indices[train:train+val])
    test_sampler = SubsetRandomSampler(indices[-test:])

    train_loader = DataLoader(dataset, batch_size=batch_size,
                                sampler=train_sampler,
                                num_workers=num_workers,
                                collate_fn=collate_batch, 
                                pin_memory=pin_memory)

    val_loader = DataLoader(dataset, batch_size=batch_size,
                                sampler=val_sampler,
                                num_workers=num_workers,
                                collate_fn=collate_batch, 
                                pin_memory=pin_memory)

    test_loader = DataLoader(dataset, batch_size=batch_size,
                                sampler=test_sampler,
                                num_workers=num_workers,
                                collate_fn=collate_batch, 
                                pin_memory=pin_memory)

    return train_loader, val_loader, test_loader


def collate_batch(dataset_list):
    """
    Collate a list of data and return a batch for predicting crystal
    properties.

    Parameters
    ----------

    dataset_list: list of tuples for each data point.
      (atom_fea, nbr_fea, nbr_fea_idx, target)

      atom_fea: torch.Tensor shape (n_i, atom_fea_len)
      nbr_fea: torch.Tensor shape (n_i, M, nbr_fea_len)
      nbr_fea_idx: torch.LongTensor shape (n_i, M)
      target: torch.Tensor shape (1, )
      cif_id: str or int

    Returns
    -------
    N = sum(n_i); N0 = sum(i)

    batch_atom_fea: torch.Tensor shape (N, orig_atom_fea_len)
        Atom features from atom type
    batch_nbr_fea: torch.Tensor shape (N, M, nbr_fea_len)
        Bond features of each atom's M neighbors
    batch_nbr_fea_idx: torch.LongTensor shape (N, M)
        Indices of M neighbors of each atom
    crystal_atom_idx: list of torch.LongTensor of length N0
        Mapping from the crystal idx to atom idx
    target: torch.Tensor shape (N, 1)
        Target value for prediction
    batch_cif_ids: list
    """
    # define the lists
    batch_atom_fea = [] 
    batch_bond_fea = []
    batch_self_fea_idx = []
    batch_nbr_fea_idx = []
    atom_bond_idx = []
    crystal_atom_idx = [] 
    batch_target = []
    batch_cry_ids = []

    # define counters
    cry_base_idx = 0
    atom_base_idx = 0
    for (atom_fea, bond_fea, self_fea_idx, nbr_fea_idx), target, cry_id in dataset_list:
        n_i = atom_fea.shape[0]  # number of atoms for this crystal

        batch_atom_fea.append(atom_fea)
        batch_bond_fea.append(bond_fea)

        batch_self_fea_idx.append(self_fea_idx+cry_base_idx)
        batch_nbr_fea_idx.append(nbr_fea_idx+cry_base_idx)

        # mapping from bonds to atoms
        for _ in range(n_i):
            atom_idx = torch.arange(n_i-1, dtype=torch.long)+atom_base_idx
            atom_bond_idx.append(atom_idx)
            atom_base_idx += n_i-1

        # mapping from atoms to crystals
        cry_idx = torch.arange(n_i, dtype=torch.long)+cry_base_idx
        crystal_atom_idx.append(cry_idx)
        cry_base_idx += n_i

        batch_target.append(target)
        batch_cry_ids.append(cry_id)

    return (torch.cat(batch_atom_fea, dim=0),
            torch.cat(batch_bond_fea, dim=0),
            torch.cat(batch_self_fea_idx, dim=0),
            torch.cat(batch_nbr_fea_idx, dim=0),
            atom_bond_idx,
            crystal_atom_idx), \
            torch.stack(batch_target, dim=0), \
            batch_cry_ids


class CompositionData(Dataset):
    """
    The CompositionData dataset is a wrapper for a dataset data points are
    automatically constructed from composition strings.
    """
    def __init__(self, data_dir, random_seed=123):
        assert os.path.exists(data_dir), 'data_dir does not exist!'
        self.data_dir = data_dir

        id_comp_prop_file = os.path.join(self.data_dir, 'id_comp_prop.csv')
        assert os.path.exists(id_comp_prop_file), 'id_comp_prop.csv does not exist!'

        with open(id_comp_prop_file) as f:
            reader = csv.reader(f)
            self.id_prop_data = [row for row in reader]
        random.seed(random_seed)
        random.shuffle(self.id_prop_data)

        atom_fea_file = os.path.join(self.data_dir, 'atom_fea.json')
        assert os.path.exists(atom_fea_file), 'atom_fea.json does not exist!'

        bond_fea_file = os.path.join(self.data_dir, 'bond_fea.json')
        assert os.path.exists(atom_fea_file), 'bond_fea.json does not exist!'

        self.atom_features = LoadFeaturiser(atom_fea_file)
        self.bond_features = LoadFeaturiser(bond_fea_file)

    def __len__(self):
        return len(self.id_prop_data)

    @functools.lru_cache(maxsize=None)  # Cache loaded structures
    def __getitem__(self, idx):
        '''
        specify how to include weights into the featurisation

        TODO think about how we want to implement weights into the features
        '''
        cry_id, composition, target = self.id_prop_data[idx]
        elements, weights = parse(composition)
        weights = np.atleast_2d(weights).T
        print(composition)
        if len(elements) == 1:
            # bad data point work out how to handle
            pass
        atom_fea = np.vstack([self.atom_features.get_fea(element) for element in elements])
        atom_fea = np.hstack((atom_fea,weights))
        # print(atom_fea.shape, weights.shape)
        env_idx = list(range(len(elements)))
        self_fea_idx = []
        nbr_fea_idx = []
        bond_fea = []
        for i, element in enumerate(elements):
            nbrs = elements[:i]+elements[i+1:]
            bond_fea.append(torch.Tensor(np.vstack([self.bond_features.get_fea(element+nbr) for nbr in nbrs])))
            self_fea_idx += [i]*len(nbrs)
            nbr_fea_idx += env_idx[:i]+env_idx[i+1:]

        atom_fea = torch.Tensor(atom_fea)
        bond_fea = torch.cat(bond_fea, dim=0)
        self_fea_idx = torch.LongTensor(self_fea_idx)
        nbr_fea_idx = torch.LongTensor(nbr_fea_idx)
        target = torch.Tensor([float(target)])
        return (atom_fea, bond_fea, self_fea_idx, nbr_fea_idx), target, cry_id
