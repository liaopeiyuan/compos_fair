from comet_ml import Experiment
import torch
import torch.nn as nn
import torch.nn.functional as F
import shutil
import torch.optim as optim
from torch.autograd import Variable
from torch.utils.data import Dataset, DataLoader
from torch.nn.init import xavier_normal, xavier_uniform
from torch.distributions import Categorical
from sklearn.metrics import precision_recall_fscore_support
from sklearn.metrics import roc_auc_score, accuracy_score
from sklearn.metrics import f1_score
import numpy as np
import random
import argparse
import pickle
import json
import sys, os
import subprocess
from tqdm import tqdm
tqdm.monitor_interval = 0
from utils import create_or_append, compute_rank
import joblib
from collections import Counter
import ipdb
sys.path.append('../')
import gc
from collections import OrderedDict
from model import *

class MarginRankingLoss(nn.Module):
    def __init__(self, margin, num_nce):
        super(MarginRankingLoss, self).__init__()
        self.margin = margin
        self.num_nce = num_nce

    def forward(self, p_enrgs, n_enrgs, weights=None):
        scores = (self.margin + p_enrgs.repeat(self.num_nce) - n_enrgs).clamp(min=0)

        if weights is not None:
            scores = scores * weights / weights.mean()
        return scores.mean(), scores

def corrupt_reddit_batch(batch, num_users, num_sr):
    # batch: ltensor type, contains positive triplets
    batch_size, _ = batch.size()
    corrupted = batch.clone()
    _cb_var_user = []
    _cb_var_sr = []

    if len(_cb_var_user) == 0 and len(_cb_var_sr) == 0:
        _cb_var_user.append(torch.LongTensor(batch_size//2).cuda())
        _cb_var_sr.append(torch.LongTensor(batch_size//2).cuda())

    q_samples_user = _cb_var_user[0].random_(0, num_users)
    q_samples_sr = _cb_var_sr[0].random_(0, num_sr)

    corrupted[:batch_size//2, 0] = q_samples_user
    corrupted[batch_size//2:, 1] = q_samples_sr

    return corrupted.contiguous()

def mask_fairDiscriminators(discriminators, mask):
    # compress('ABCDEF', [1,0,1,0,1,1]) --> A C E F
    return (d for d, s in zip(discriminators, mask) if s)

def train_reddit_nce(data_loader,train_hash,counter,args,modelD,optimizerD,\
        fairD_set, optimizer_fairD_set, filter_set, masks, experiment):
    lossesD = []
    total_ent = 0
    loss_func = MarginRankingLoss(args.margin,args.num_nce)
    data_itr = tqdm(enumerate(data_loader))
    correct_array = np.zeros(len(fairD_set))
    for idx, p_batch in data_itr:
        ''' Sample Fairness Discriminators '''
        if args.sample_mask:
            mask = random.choice(masks)
            # mask = np.random.choice([0, 1], size=(args.num_sensitive,))
            masked_fairD_set = list(mask_fairDiscriminators(fairD_set,mask))
            masked_optimizer_fairD_set = list(mask_fairDiscriminators(optimizer_fairD_set,mask))
            masked_filter_set = list(mask_fairDiscriminators(filter_set,mask))
        else:
            ''' No mask applied despite the name '''
            masked_fairD_set = fairD_set
            masked_optimizer_fairD_set = optimizer_fairD_set
            masked_filter_set = filter_set

        nce_list = []
        for i in range(0,args.num_nce):
            nce_batch = corrupt_reddit_batch(p_batch,args.num_users,args.num_sr)
            if args.filter_false_negs:
                nce_falseNs = torch.FloatTensor(np.array([int(x.tobytes() in train_hash)\
                        for x in nce_batch.numpy()], dtype=np.float32))
                nce_falseNs = Variable(nce_falseNs).to(args.device)
            else:
                nce_falseNs = None
            nce_list.append(nce_batch)

        nce_batch = torch.cat(nce_list)
        p_batch_var = Variable(p_batch).cuda()
        nce_batch = Variable(nce_batch).cuda()

        ''' Number of Active Discriminators '''
        constant = len(masked_fairD_set) - masked_fairD_set.count(None)
        d_ins = torch.cat([p_batch_var, nce_batch], dim=0).contiguous()

        ''' Update Encoder '''
        if constant != 0:
            d_outs,lhs_emb,rhs_emb = modelD(d_ins,True,filters=masked_filter_set)
            filter_l_emb = lhs_emb[:len(p_batch_var)]
            p_rhs_emb = rhs_emb[:len(p_batch_var)]
            nce_lhs_emb = lhs_emb[len(p_batch_var):(len(p_batch_var)+len(nce_batch))]
            nce_rhs_emb = rhs_emb[len(p_batch_var):(len(p_batch_var)+len(nce_batch))]
            l_penalty = 0

            ''' Apply Discriminators '''
            for fairD_disc, fair_optim in zip(masked_fairD_set,masked_optimizer_fairD_set):
                if fairD_disc is not None and fair_optim is not None:
                    l_penalty += fairD_disc(filter_l_emb,p_batch[:,0],True)

            if not args.use_cross_entropy:
                fair_penalty = constant - l_penalty
            else:
                fair_penalty = -1*l_penalty

            if not args.freeze_encoder:
                optimizerD.zero_grad()
                p_enrgs = d_outs[:len(p_batch_var)]
                nce_enrgs = d_outs[len(p_batch_var):(len(p_batch_var)+len(nce_batch))]
                nce_term, nce_term_scores = loss_func(p_enrgs, nce_enrgs)
                lossD = nce_term + args.gamma*fair_penalty
                lossD.backward(retain_graph=False)
                optimizerD.step()

            for k in range(0,args.D_steps):
                l_penalty_2 = 0
                for fairD_disc, fair_optim in zip(masked_fairD_set,\
                        masked_optimizer_fairD_set):
                    if fairD_disc is not None and fair_optim is not None:
                        fair_optim.zero_grad()
                        l_penalty_2 += fairD_disc(filter_l_emb.detach(),\
                                p_batch[:,0],True)
                        if not args.use_cross_entropy:
                            fairD_loss = -1*(1 - l_penalty_2)
                        else:
                            fairD_loss = l_penalty_2
                        fairD_loss.backward(retain_graph=True)
                        fair_optim.step()
        else:
            d_outs = modelD(d_ins)
            fair_penalty = Variable(torch.zeros(1)).cuda()
            p_enrgs = d_outs[:len(p_batch_var)]
            nce_enrgs = d_outs[len(p_batch_var):(len(p_batch_var)+len(nce_batch))]
            optimizerD.zero_grad()
            nce_term, nce_term_scores = loss_func(p_enrgs, nce_enrgs)
            lossD = nce_term + args.gamma*fair_penalty
            lossD.backward(retain_graph=False)
            optimizerD.step()

        if constant != 0:
            for fairD_disc in masked_fairD_set:
                if fairD_disc is not None:
                    ''' No Gradients Past Here '''
                    with torch.no_grad():
                        d_outs,lhs_emb,rhs_emb = modelD(d_ins,True,\
                                filters=masked_filter_set)
                        filter_l_emb = lhs_emb[:len(p_batch)]
                        probs, l_A_labels, l_preds = fairD_disc.predict(filter_l_emb,p_batch[:,0],True)
                        l_correct = l_preds.eq(l_A_labels.view_as(l_preds)).sum().item()
                        fairD_disc.num_correct += l_correct

    ''' Logging for end of epoch '''
    if args.do_log:
        if not args.freeze_encoder:
            experiment.log_metric("NCE Loss",float(lossD),step=counter)
            for fairD_disc in fairD_set:
                if fairD_disc is not None:
                    acc = 100. * fairD_disc.num_correct / len(data_loader.dataset)
                    fairD_disc.num_correct = 0
                    experiment.log_metric("Train "+fairD_disc.attribute +\
                            " Disc",float(acc),step=counter)

def train_fair_reddit(data_loader,train_hash,counter,args,modelD,optimizerD,\
         fairD_set, optimizer_fairD_set, filter_set, masks, experiment):
    train_reddit_nce(data_loader,train_hash,counter,args,modelD,optimizerD,\
            fairD_set,optimizer_fairD_set,filter_set,masks,experiment)
