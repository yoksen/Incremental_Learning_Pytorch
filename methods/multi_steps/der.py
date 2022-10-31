import numpy as np
import torch
from torch import nn
from torch.nn import functional as F
from torch.nn.functional import cross_entropy
from torch.utils.data import DataLoader
from torch import optim

from backbone.inc_net import DERNet
from methods.multi_steps.finetune_il import Finetune_IL
from utils.toolkit import count_parameters, target2onehot, tensor2numpy

'''
hyper-parameters:
resnet32:
    opt_type: sgd (have a huge impact to result)
    epochs: 170 # 170
    lrate: 0.1
    scheduler: multi_step
    milestones: [100,120]
    lrate_decay: 0.1
    weight_decay: 0.0005
    batch_size: 128
    num_workers: 8

CIFAR100 result:
| Method Name       | exp seting | Avg Acc | Final Acc |
| ----------------- | ---------- | ------- | --------- |
| DER               | b0i10      | 75.15   | 67.77     |
'''

EPSILON = 1e-8

class DER(Finetune_IL):
    def __init__(self, logger, config):
        super().__init__(logger, config)
        self._T = self._config.T
        self._is_finetuning = False
        if self._incre_type != 'cil':
            raise ValueError('DER is a class incremental method!')

    def prepare_model(self):
        if self._network == None:
            self._network = DERNet(self._logger, self._config.backbone, self._config.pretrained)
        self._network.update_fc(self._total_classes)

        if self._cur_task>0:
            for i in range(self._cur_task):
                for p in self._network.feature_extractor[i].parameters():
                    p.requires_grad = False
                self._network.feature_extractor[i].eval()
                self._logger.info('Freezing task extractor {} !'.format(i))
        self._logger.info('All params: {}'.format(count_parameters(self._network)))
        self._logger.info('Trainable params: {}'.format(count_parameters(self._network, True)))
        self._network = self._network.cuda()

    def incremental_train(self):
        if len(self._multiple_gpus) > 1:
            self._network = nn.DataParallel(self._network, self._multiple_gpus)
        self._logger.info('-'*10 + ' Learning on task {}: {}-{} '.format(self._cur_task, self._known_classes, self._total_classes-1) + '-'*10)
        optimizer = self._get_optimizer(filter(lambda p: p.requires_grad, self._network.parameters()), self._config, self._cur_task==0)
        scheduler = self._get_scheduler(optimizer, self._config, self._cur_task==0)
        if self._cur_task == 0:
            epochs = self._init_epochs
        else:
            epochs = self._epochs
        self._is_finetuning = False
        self._network = self._train_model(self._network, self._train_loader, self._test_loader, optimizer, scheduler, epochs)

        if self._cur_task > 0:
            self._logger.info('Finetune the network (classifier part) with the balanced dataset!')
            finetune_train_dataset = self._memory_bank.get_unified_sample_dataset(self._train_dataset, self._network)
            finetune_train_loader = DataLoader(finetune_train_dataset, batch_size=self._batch_size,
                                            shuffle=True, num_workers=self._num_workers)
            self._network.reset_fc_parameters()
            ft_optimizer = optim.SGD(filter(lambda p: p.requires_grad, self._network.fc.parameters()), momentum=0.9, lr=0.1)
            ft_scheduler = optim.lr_scheduler.MultiStepLR(optimizer=ft_optimizer, milestones=[15], gamma=0.1)
            self._is_finetuning = True
            self._network = self._train_model(self._network, finetune_train_loader, self._test_loader, ft_optimizer, ft_scheduler, 30)

        if len(self._multiple_gpus) > 1:
            self._network = self._network.module
        if self._memory_bank != None:
            self._memory_bank.store_samplers(self._sampler_dataset, self._network)

    def _epoch_train(self, model, train_loader, optimizer, scheduler):
        losses = 0.
        correct, total = 0, 0
        losses_clf = 0.
        losses_aux = 0.
        
        if len(self._multiple_gpus) > 1:
            model.module.feature_extractor[-1].train()
        else:
            model.feature_extractor[-1].train()

        for _, inputs, targets in train_loader:
            inputs, targets = inputs.cuda(), targets.cuda()

            logits, output_features = model(inputs)
            aux_logits = output_features["aux_logits"]
            
            if self._is_finetuning:
                loss_clf = cross_entropy(logits/self._T, targets)
                loss = loss_clf
            else:
                loss_clf = cross_entropy(logits, targets)
                if self._cur_task > 0:
                    aux_targets = targets.clone()
                    aux_targets = torch.where(aux_targets-self._known_classes+1>0, aux_targets-self._known_classes+1, 0)
                    loss_aux = F.cross_entropy(aux_logits, aux_targets)
                    loss = loss_clf + loss_aux
                    losses_aux += loss_aux.item()
                else:
                    loss = loss_clf

            preds = torch.max(logits, dim=1)[1]
    
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            losses += loss.item()
            losses_clf += loss_clf.item()

            correct += preds.eq(targets).cpu().sum()
            total += len(targets)
        
        scheduler.step()
        train_acc = np.around(tensor2numpy(correct)*100 / total, decimals=2)
        if self._cur_task > 0:
            train_loss = ['Loss', losses/len(train_loader), 'Loss_clf', losses_clf/len(train_loader), 'Loss_aux', losses_aux/len(train_loader)]
        else:
            train_loss = ['Loss', losses/len(train_loader), 'Loss_clf', losses_clf/len(train_loader)]
        return model, train_acc, train_loss