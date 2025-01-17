from __future__ import division
import os
import logging
import time
import argparse

import torch
from torchvision import datasets, transforms
import torch.optim as optim
import torch.nn as nn
from torch.optim.lr_scheduler import StepLR, MultiStepLR
import torch.nn.functional as F
from torch.utils.tensorboard import SummaryWriter

import numpy as np
from numpy.linalg import norm

# dataset
from torch.utils.data import DataLoader
from torch.utils.data import Dataset
from torch.nn import DataParallel

import tqdm

from loss import CSQLoss, ContrastiveLoss
import pickle

from UtilsPolyUEval import compute_result_prints_vein, eval_eer_hashcenter, eval_eer_fusion_center, \
    eval_eer_fusion, eval_eer_cross, eval_top1, eval_fusion_top1, \
    normalized, compute_result_prints_vein_2path2,compute_result_prints_vein_2path_iitd,eval_eer
from utils import accuracy, AverageMeter, save_checkpoint, visualize_graph, get_parameters_size

parser = argparse.ArgumentParser(description='PyTorch GCN MNIST Training')

parser.add_argument('--epochs', default=2000, type=int, metavar='N',
                    help='number of total epochs to run')  # resnet 2000 epoch; efficiency 500
parser.add_argument('--start-epoch', default=0, type=int, metavar='N',
                    help='manual epoch number (useful on restarts)')
parser.add_argument('-b', '--batch-size', default=128, type=int,
                    metavar='N', help='mini-batch size (default: 64)')
parser.add_argument('--lr', '--learning-rate', default=0.00001, type=float,
                    metavar='LR', help='initial learning rate')
parser.add_argument('--pretrained', default='', type=str, metavar='PATH',
                    help='path to pretrained checkpoint (default: none)')
parser.add_argument('--gpu', default=0, type=int,
                    metavar='N', help='GPU device ID (default: -1)')
parser.add_argument('--bits', default=64, type=int,
                    metavar='N', help='bits length')
parser.add_argument('--comment', default='', type=str, metavar='INFO',
                    help='Extra description for tensorboard')
parser.add_argument('--ds', default='polyu', type=str, metavar='DS',
                    help='Dataset: polyu, casiam, iitd, tjppv')  # use double mark
parser.add_argument('--model', default='effb5', type=str, metavar='NETWORK',
                    help='Network to train,res18 or effb5')
args = parser.parse_args()

use_cuda = (args.gpu >= 0) and torch.cuda.is_available()
iteration = 0


def get_config():
    config = {
        "lambda": 0.1,
        "optimizer": {"type": optim.RMSprop, "optim_params": {"lr": 1e-5, "weight_decay": 10 ** -5}},
        "info": args.comment,
        "batch_size": args.batch_size,
        "net": args.model,
        "dataset": args.ds,
        "n_class": 1,  # pay attention, update below
        "epoch": args.epochs,
        "device": torch.device("cuda:"+str(args.gpu)) if use_cuda else torch.device("cpu"),
        "bit_list": [0],  #
    }
    return config


config = get_config()
print(config)
device = config['device']

bit = args.bits

remarks = config['net'] + config['dataset'] + config['info'] + str(bit)
writer = SummaryWriter(comment='_' + remarks)
logging.basicConfig(level=logging.DEBUG,  # 控制台打印的日志级别
                    filename=remarks + '_app.log',
                    filemode='a',  ##模式，有w和a，w就是写模式，a是追加模式
                    format=
                    '%(asctime)s - %(pathname)s[line:%(lineno)d] - %(levelname)s: %(message)s')

# from net_factory import mobilenet_v3_largehashing2Path
# # Load model
# model = mobilenet_v3_largehashing2Path(inchannel1=3, inchannel2=3,bits = bit)#GCNCNN
# # print(model)
#
# from net_factory import efficientnet_b72Path
# # Load model
# model = efficientnet_b72Path(inchannel1=3, inchannel2=3,bits = bit)#GCNCNN
# # print(model)

from net_factory import Resent18hashing,efficientnet_b7hashing,mobilenet_v3_largehashing
from net_factory import Resent182Path,efficientnet_b72Path,mobilenet_v3_largehashing2Path
# Dataset
import math

if args.ds == 'iitd':
    inchannel = 1
    from DsZoo import load_data_single_channel as load_data
    if args.model == 'res18':
        # Load model
        model = Resent18hashing(inchannel=inchannel, bits=bit)
        # print(model)
    elif args.model == 'effb5':
        model = efficientnet_b7hashing(inchannel=inchannel, bits=bit)
    elif args.model == 'mobile':
        model = mobilenet_v3_largehashing(inchannel=inchannel, bits=bit)

else:
    inchannel = 3
    from DsZoo import load_data

    if args.model == 'res18':
        # Load model
        model = Resent182Path(inchannel1=inchannel, inchannel2=inchannel, bits=bit)  # GCNCNN
        # print(model)
    elif args.model == 'effb5':
        model = efficientnet_b72Path(inchannel1=inchannel, inchannel2=inchannel, bits=bit)
    elif args.model == 'mobile':
        model = mobilenet_v3_largehashing2Path(inchannel1=inchannel, inchannel2=inchannel, bits=bit)

# Try to visulize the model
# try:
# 	visualize_graph(model, writer, input_size=(1, 3, 128, 128))
# except:
# 	print('\nNetwork Visualization Failed! But the training procedure continue.')

# Calculate the total parameters of the model

if args.pretrained:
    if os.path.isfile(args.pretrained):
        print("=> loading checkpoint '{}'".format(args.pretrained))
        checkpoint = torch.load(args.pretrained)
        model.load_state_dict(checkpoint['state_dict'])
        print(checkpoint['best_prec1'])
    else:
        print("=> no checkpoint found at '{}'".format(args.pretrained))

sample_ratio = 0.5
if args.ds == 'polyu':
    clsses = 500
    sampesCls = math.ceil(12 * sample_ratio)
elif args.ds == 'tjppv':
    clsses = 600
    sampesCls = math.ceil(20 * sample_ratio)
elif args.ds == 'iitd':
    clsses = 460
    sampesCls = math.ceil(5 * sample_ratio)  # 1:1 -> 3:2
elif args.ds == 'casiam':
    clsses = 200
    sampesCls = math.ceil(6 * sample_ratio)
else:
    print('wrong DS', args.ds)

config["n_class"] = clsses  # pay attention

batch_size = config["batch_size"]
train_loader = DataLoader(load_data(ds=args.ds, training=True, train_ratio=1, sample_ratio=sample_ratio),
                          batch_size=batch_size, shuffle=True, num_workers=8, pin_memory=True)  # ,prefetch_factor=2
test_loader = DataLoader(load_data(ds=args.ds, training=False, train_ratio=1, sample_ratio=sample_ratio),
                         batch_size=batch_size, shuffle=False)

num_train = len(train_loader.dataset)
num_test = len(test_loader.dataset)
print('train num: ', len(train_loader.dataset))
print('test num: ', len(test_loader.dataset))
print('train num per class: ', sampesCls)

# batch_size = 32
model = model.to(device)
print('\nTrainable parameters : {}\n'.format(sum(p.numel() for p in model.parameters() if p.requires_grad)))

optimizer = config["optimizer"]["type"](model.parameters(), **(config["optimizer"]["optim_params"]))
criterion = CSQLoss(config, bit)
criterion_ctive = ContrastiveLoss()  # ContrastiveLoss as the domain gap loss

# optimizer = optim.SGD(model.parameters(), lr=0.0001, momentum=args.momentum, weight_decay=3e-05)
scheduler = StepLR(optimizer, step_size=100, gamma=0.9)


def test(net, test_loader, epoch, clsses, sampesCls):
    FEATS_prints, FEATS_vein, GT = compute_result_prints_vein_2path2(test_loader, net, device)
    FEATS_prints_binay, FEATS_vein_binay = FEATS_prints > 0, FEATS_vein > 0
    FEATS_prints, FEATS_vein = normalized(FEATS_prints, 1), normalized(FEATS_vein, 1)

    accprints = eval_top1(FEATS_prints_binay, criterion, clsses=clsses, sampesCls=sampesCls)
    accveins = eval_top1(FEATS_vein_binay, criterion, clsses=clsses, sampesCls=sampesCls)
    eer_prints_center = eval_eer_hashcenter(FEATS_prints_binay, criterion, clsses=clsses, sampesCls=sampesCls)
    eer_veins_center = eval_eer_hashcenter(FEATS_vein_binay, criterion, clsses=clsses, sampesCls=sampesCls)
    eer_fusion_center = eval_eer_fusion_center(FEATS_prints_binay, FEATS_vein_binay, criterion, clsses=clsses,
                                               sampesCls=sampesCls)
    eer_fusion = eval_eer_fusion(FEATS_prints, FEATS_vein, clsses=clsses, sampesCls=sampesCls)
    eer_cross = eval_eer_cross(FEATS_prints, FEATS_vein, clsses=clsses, sampesCls=sampesCls)
    acc = eval_fusion_top1(FEATS_prints_binay, FEATS_vein_binay, criterion, clsses=clsses, sampesCls=sampesCls)

    print('The top1 acc for prints is: \t {:.5f}'.format(accprints))
    print('The top1 acc for veins is: \t {:.5f}'.format(accveins))
    print('The top1 acc for fusion is: \t {:.5f}'.format(acc))
    print('The equal error rate for center hash prints: \t {:.5f}'.format(eer_prints_center))
    print('The equal error rate for center hash veins: \t  {:.5f}'.format(eer_veins_center))
    print('The equal error rate for fusion on center is: {:.5f}'.format(eer_fusion_center))
    print('The equal error rate for fusion is: \t {:.5f}'.format(eer_fusion))
    print('The equal error rate for cross is: \t {:.5f}'.format(eer_cross))

    return eer_cross,FEATS_prints, FEATS_vein

def test_iitd(net,test_loader, epoch,clsses,sampesCls):
    FEATS_prints, FEATS_vein, GT = compute_result_prints_vein_2path_iitd(test_loader, net, device)
    FEATS_prints_binay = FEATS_prints>0
    FEATS_prints = normalized(FEATS_prints,1)


    accprints = eval_top1(FEATS_prints_binay,criterion, clsses = clsses, sampesCls = sampesCls)
    accveins = 0
    
    eer_prints_center = eval_eer_hashcenter(FEATS_prints_binay,criterion, clsses = clsses, sampesCls = sampesCls)
    eer_prints = eval_eer(FEATS_prints, clsses = clsses, sampesCls = sampesCls, disfun='cossim')
    eer_prints_hash = eval_eer(FEATS_prints_binay, clsses = clsses, sampesCls = sampesCls, disfun='hamming')

    eer_veins_center =0
    eer_fusion_center =0
    eer_fusion = 0
    eer_cross = 0
    acc = 0
    
    print('The top1 acc for prints is: \t {:.5f}'.format(accprints))    
    # print('The top1 acc for veins is: \t {:.5f}'.format(accveins))
    # print('The top1 acc for fusion is: \t {:.5f}'.format(acc))
    print('The equal error rate for center hash prints: \t {:.5f}'.format(eer_prints_center))
    # print('The equal error rate for center hash veins: \t  {:.5f}'.format(eer_veins_center))
    # print('The equal error rate for fusion on center is: {:.5f}'.format(eer_fusion_center))
    # print('The equal error rate for fusion is: \t {:.5f}'.format(eer_fusion))
    
    print('The equal error rate for prints is: \t {:.5f}'.format(eer_prints))
    print('The equal error rate for prints hashing is: \t {:.5f}'.format(eer_prints_hash))

    return eer_prints,FEATS_prints, FEATS_vein


remarks = config['net'] + config['dataset'] + config['info'] + str(bit)
epoch = 0
if args.ds == 'iitd':
    eer,FEATS_prints, FEATS_vein = test_iitd(model, test_loader, epoch, clsses, 5-sampesCls)
else:
    eer,FEATS_prints, FEATS_vein = test(model, test_loader, epoch, clsses, sampesCls)

    
mydict = {'FEATS_prints': FEATS_prints, 'FEATS_vein': FEATS_vein, 'epoch': epoch + 1, 'best_eer': 0 }
output = open('FEATS_EVAL_' + remarks + '.pkl', 'wb')
pickle.dump(mydict, output)
output.close()
