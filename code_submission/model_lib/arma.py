import torch
import torch.nn.functional as F
from torch.nn import Linear
from torch_geometric.nn import ARMAConv
import copy
from sklearn.metrics import f1_score
from utils.tools import fix_seed, AverageMeter
from nni.hyperopt_tuner.hyperopt_tuner import HyperoptTuner
from torch_geometric.utils import dropout_adj
import random
fix_seed(1234)

class ARMA(torch.nn.Module):

    def __init__(self, info):
        super(ARMA, self).__init__()

        self.info = info

        self.best_score = 0
        self.hist_score = []

        self.best_preds = None
        self.current_round_best_preds = None
        self.best_valid_score = 0
        self.max_patience = 100
        self.max_epochs = 1600
        
        self.name = 'ARMA'

        self.hidden = 16
        self.lr = 0.005
        self.hyperparameters = {
            'num_layers': self.info['num_layers'],
            'lr': self.info['lr'],
            'num_stacks': 1,
            'conv_layers': 1,
            'dropedge_rate': self.info['dropedge_rate'],
            'dropout_rate': 0.5,
            'hidden': self.info['init_hidden_size'],
            'use_linear': self.info['use_linear']
        }
        self.best_hp = None
        self.tuner = HyperoptTuner(algorithm_name='tpe', optimize_mode='maximize')
        search_space = {
                "dropedge_rate": {
                    "_type": "choice",
                    "_value": [self.info['dropedge_rate']]
                },
                "dropout_rate": {
                    "_type": "choice",
                    "_value": [self.info['dropout_rate']]
                },
                "num_layers": {
                    "_type": "quniform",
                    "_value": [1, 3, 1]
                },
                "hidden": {
                    "_type": "quniform",
                    "_value": [4, 7, 1]
                },
                "lr":{
                    "_type": "choice",
                    "_value": [0.005]
                },
                'num_stacks' : {
                    "_type": "quniform",
                    "_value": [1, 5, 1]
                },
                'conv_layers' : {
                    "_type": "quniform",
                    "_value": [1, 5, 1]
                },
                'use_linear': {
                    "_type":"choice",
                    "_value":[True, False]
                }
            }
        self.tuner.update_search_space(search_space)

    def init_model(self, n_class, feature_num):
        hidden_size = int(2 ** self.hyperparameters['hidden'])
        num_stacks = int(self.hyperparameters['num_stacks'])
        conv_layers = int(self.hyperparameters['conv_layers'])
        lr = self.hyperparameters['lr']
        dropout = self.hyperparameters['dropout_rate']
        num_layers = int(self.hyperparameters['num_layers'])
        if self.hyperparameters['use_linear']:
            self.input_lin = Linear(feature_num, hidden_size)
            self.convs = torch.nn.ModuleList()
            for i in range(num_layers):
                self.convs.append(ARMAConv(hidden_size, hidden_size, num_stacks=num_stacks, num_layers=conv_layers, dropout=dropout))
            self.output_lin = Linear(hidden_size, n_class)
        else:
            if num_layers == 1:
                self.conv1 = ARMAConv(feature_num, n_class, num_stacks=num_stacks,\
                 num_layers=conv_layers, shared_weights=False, dropout=dropout)
            else:
                self.conv1 = ARMAConv(feature_num, hidden_size, num_stacks=num_stacks,\
                    num_layers=conv_layers, shared_weights=False, dropout=dropout)
                self.convs = torch.nn.ModuleList()
                for i in range(num_layers - 2):    
                    self.convs.append(ARMAConv(hidden_size, hidden_size, num_stacks=num_stacks,\
                        num_layers=conv_layers, shared_weights=False, dropout=dropout))
                self.conv2 = ARMAConv(hidden_size, n_class, num_stacks=num_stacks,\
                        num_layers=conv_layers, shared_weights=False, dropout=dropout)
        self.optimizer = torch.optim.Adam(self.parameters(), lr=lr, weight_decay=5e-4)

        self = self.to('cuda')

        torch.cuda.empty_cache()

    def forward(self, data):
        x, edge_index, edge_weight = data.x, data.edge_index, data.edge_weight
        if self.hyperparameters['dropedge_rate'] is not None:
            edge_index, edge_weight = dropout_adj(edge_index, edge_weight, p=self.hyperparameters['dropedge_rate'],\
                 force_undirected=False, num_nodes=None, training=self.training)
        
        if self.hyperparameters['use_linear']:
            x = F.relu(self.input_lin(x))
        else:
            x = F.relu(self.conv1(x, edge_index,edge_weight))
            if self.hyperparameters['num_layers'] == 1:
                return x

        x = F.dropout(x, p=self.hyperparameters['dropout_rate'], training=self.training)
        for conv in self.convs:
            x = F.relu(conv(x, edge_index, edge_weight=edge_weight))
        if self.hyperparameters['use_linear']:
            x = self.output_lin(x)
        else:
            x = self.conv2(x, edge_index,edge_weight)
        return x
    
    def trial(self, data, round_num):
        n_class, feature_num = self.info['n_class'], data.x.shape[1]
        if round_num >= 2:
            self.hyperparameters = self.tuner.generate_parameters(round_num-1)
        print(self.hyperparameters)    
           
        while True:
            try:
                self.init_model(n_class, feature_num)
                val_score = self.train_valid(data, round_num)
                if round_num > 1:
                    self.tuner.receive_trial_result(round_num-1,self.hyperparameters,val_score)
                if val_score > self.best_score:
                    self.best_hp = copy.deepcopy(self.hyperparameters)
                break
            except RuntimeError as e:
                print(self.name,e, 'OOM with Hidden Size', self.hyperparameters['hidden'])
                if round_num > 1:
                    self.tuner.receive_trial_result(round_num-1,self.hyperparameters,0)
                return 0
        print("Best Hyperparameters of", self.name, self.best_hp)
        return val_score

    def train_valid(self, data, round_num):
        y, train_mask, valid_mask, test_mask, label_weights = data.y, data.train_mask, data.valid_mask, data.test_mask, data.label_weights


        patience = self.max_patience
        best_valid_score = 0
        valid_acc_meter = AverageMeter()
        for epoch in range(self.max_epochs):

            # train
            self.train()
            self.optimizer.zero_grad()
            preds = self.forward(data)
            loss = F.cross_entropy(preds[train_mask], y[train_mask], label_weights)
            loss.backward()
            self.optimizer.step()

            # valid
            self.eval()
            with torch.no_grad():
                preds = F.softmax(self.forward(data), dim=-1)
                valid_preds, test_preds = preds[valid_mask], preds[test_mask]
                valid_score = f1_score(y[valid_mask].cpu(), valid_preds.max(1)[1].flatten().cpu(), average='micro')

            valid_acc_meter.update(valid_score)
            # patience
            if valid_acc_meter.avg > best_valid_score:
                best_valid_score = valid_acc_meter.avg
                self.current_round_best_preds = test_preds
                patience = self.max_patience
            else:
                patience -= 1

            if patience == 0:
                break

        return best_valid_score

    def predict(self):
        return self.current_round_best_preds.cpu().numpy()

    def __repr__(self):
        return self.__class__.__name__