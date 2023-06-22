import os
from typing import List

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch_geometric.data import Data
from torch_geometric.loader import DataLoader
from torch_geometric.nn import GAE, GCNConv
from torch_geometric.utils import scatter

from src.dataset.dataset_base import Dataset
from src.dataset.torch_geometric.dataset_geometric import TorchGeometricDataset
from src.explainer.explainer_base import Explainer
from src.oracle.oracle_base import Oracle


class AdaptiveGCE(Explainer):
    
    def __init__(self,
                 id,
                 explainer_store_path,
                 time=0,
                 num_classes=2,
                 in_channels=1,
                 out_channels=64,
                 fold_id=0,
                 batch_size=24,
                 lr=1e-3,
                 epochs_ae=100,
                 epochs_siamese=100,
                 device='cpu',
                 config_dict=None) -> None:
        super().__init__(id, config_dict)
        
        self.time = time
        self.num_classes = num_classes
        self.fold_id = fold_id
        self.batch_size = batch_size
        self.epochs_ae = epochs_ae
        self.epochs_siamese = epochs_siamese
        self.device = device
        
        self.autoencoders = [
            GAE(encoder=GCNEncoder(
                in_channels=in_channels,
                out_channels=out_channels)).to(self.device) for _ in range(num_classes) 
        ]
        
        self.optimisers = [
            torch.optim.Adam(self.autoencoders[i].parameters(), lr=lr) for i in range(num_classes)
        ]
        
        self._fitted = False
        
        self.explainer_store_path = explainer_store_path

        
        
    def explain(self, instance, oracle: Oracle, dataset: Dataset):        
        if(not self._fitted):
            self.fit(oracle, dataset, self.fold_id)
            
        return instance        
        
    def fit(self, oracle: Oracle, dataset: Dataset, fold_id=0):
        explainer_name = f'{self.__class__.name}_fit_on_{dataset.name}_fold_id_{fold_id}'
        explainer_uri = os.path.join(self.explainer_store_path, explainer_name)
        self.name = explainer_name
        
        if os.path.exists(explainer_uri):
            # Load the weights of the trained model
            self.load_explainers()
        else:
            # Create the folder to store the oracle if it does not exist
            os.mkdir(explainer_uri)                    
            self.__fit(oracle, dataset)
            # self.save_explainers()        
        # setting the flag to signal the explainer was already trained
        self._fitted = True    
        

    
    def __fit(self, oracle: Oracle, dataset: Dataset):
        data_loaders = self.transform_data(dataset)
        
        for cls, data_loader in enumerate(data_loaders):
            optimiser = self.optimisers[cls]
            autoencoder: GAE = self.autoencoders[cls]
            
            autoencoder.train()
            
            for epoch in range(self.epochs_ae):
                
                losses = []
                for item in data_loader:
                    edge_index = item.edge_index.squeeze(dim=0).to(self.device)
                    edge_attr = item.edge_attr.squeeze(dim=0).to(self.device)
                    
                    optimiser.zero_grad()
                    
                    z = autoencoder.encode(torch.FloatTensor([]), edge_index, edge_attr)
                    loss = autoencoder.recon_loss(z, edge_index)
                    
                    loss.backward()
                    optimiser.step()
                    
                    losses.append(loss.item())
                
                print(f'Class {cls}, Epoch = {epoch} ----> Loss = {np.mean(losses)}')
                
            self.save_autoencoder(autoencoder, cls)
                    
                    
    def save_autoencoder(self, model, cls):
        torch.save(model.state_dict(),
                 os.path.join(self.explainer_store_path, self.name, f'autoencoder_{cls}'))

    
    
    def transform_data(self, dataset: Dataset) -> List[DataLoader]:
        adj  = torch.from_numpy(np.array([i.to_numpy_array() for i in dataset.instances]))
        x = torch.from_numpy(np.array([i.features for i in dataset.instances]))
        y = torch.from_numpy(np.array([i.graph_label for i in dataset.instances]))
        
        indices = dataset.get_split_indices()[self.fold_id]['train'] 
        x, adj, y = x[indices], adj[indices], y[indices]
        
        classes = dataset.get_classes()

        data_dict_cls = {cls:[] for cls in classes}
        w = None
        a = None
        for i in range(len(y)):
            # weights is an adjacency matrix n x n x d
            # where d is the dimensionality of the edge weight vector
            # get all non zero vectors. now the shape will be m x d
            # where m is the number of edges and 
            # d is the dimensionality of the edge weight vector
            w = adj[i]                    
            # get the edge indices
            # shape m x 2
            a = torch.nonzero(adj[i])
            w = w[a[:,0], a[:,1]]
                    
            data_dict_cls[y[i].item()].append(Data(x=x[i], y=y[i], edge_index=a.T, edge_attr=w))            
        
        data_loaders = []
        for cls in data_dict_cls.keys():
            data_loaders.append(DataLoader(
                TorchGeometricDataset(data_dict_cls[cls]),
                                      batch_size=1,
                                      shuffle=True,
                                      num_workers=2)
            )
        
        return data_loaders
        
        
class GCNEncoder(nn.Module):
    
    def __init__(self,
                 in_channels=1,
                 out_channels=64):
        
        super(GCNEncoder, self).__init__()
        self.conv1 = GCNConv(in_channels, out_channels)
        self.conv2 = GCNConv(out_channels, out_channels)
        self.conv3 = GCNConv(out_channels, in_channels)
        self.training = False
        
    def forward(self, x, edge_index, edge_weight):
        print(x)
        print(edge_index)
        x = self.conv1(x=x, edge_index=edge_index, edge_weight=edge_weight)
        x = F.leaky_relu(x, negative_slope=.2)
        x = F.dropout(x, p=.2, training=self.training)
        x = self.conv2(x=x, edge_index=edge_index, edge_weight=edge_weight)
        x = F.leaky_relu(x, negative_slope=.2)
        x = F.dropout(x, p=.2, training=self.training)
        x = self.conv3(x=x, edge_index=edge_index, edge_weight=edge_weight)
        x = torch.tanh(x)
        return x

    def set_training(self, training):
        self.training = training
