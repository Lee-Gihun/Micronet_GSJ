# -*- coding: utf-8 -*-
import os
os.environ['OMP_NUM_THREADS'] = '1'
import sys
import math
import copy
import torch
import numpy as np
import torch.nn as nn
import torch.optim as optim
import torch.optim.lr_scheduler as lr_scheduler
import torchvision.models as models

from data_utils import *
from train_tools import *
from models import *
from counting import *
    
DATASETTER = {'cifar10': cifar_10_setter,
              'cifar100': cifar_100_setter}
    
CRITERION = {'mse': nn.MSELoss,
             'cross_entropy': nn.CrossEntropyLoss,
             'label_smoothing': LabelSmoothingLoss}

OPTIMIZER = {'sgd': optim.SGD,
             'adam': optim.Adam,
             'adagrad': optim.Adagrad,
             'rmsprop': optim.RMSprop,
             'radam': RAdam}

SCHEDULER = {'step': lr_scheduler.StepLR,
             'multistep': lr_scheduler.MultiStepLR,
             'cosine': lr_scheduler.CosineAnnealingLR}

PRUNE_METHOD = {'weight': weight_prune,
                'filter': filter_prune}

def _get_dataset(opt):
    param = opt.data
    
    dataloaders, dataset_sizes = DATASETTER[param.dataset](batch_size=param.batch_size, 
                                                           valid_size=param.valid_size,
                                                           root=param.root,
                                                           fixed_valid=param.fixed_valid,
                                                           autoaugment=param.autoaugment,
                                                           aug_policy=param.aug_policy)
    
    return dataloaders, dataset_sizes

def _get_model(opt):
    param = opt.model.param
    
    # AutoML results 
    avail_resource = param.avail_resource
    resolution_coefficient = round(math.pow(param.resolution_coefficient, avail_resource), 2)
    
    print('[model information]')
    print('resolution_coefficient : %.2f' % resolution_coefficient)
    print('width_coefficient : %.2f' % param.width_coefficient)
    print('depth_coefficient : %.2f' % param.depth_coefficient)
    print('=' * 50)
    
    # if you want other blocks_args setting, define if here.
    blocks_args, global_params = efficientnet(blocks_args='default',
                                              activation=param.activation,
                                              activation_param=param.get('activation_param',  {}),
                                              ghost_bn=param.ghost_bn,
                                              resolution_coefficient=resolution_coefficient,
                                              width_coefficient=param.width_coefficient, 
                                              depth_coefficient=param.depth_coefficient, 
                                              image_size=param.image_size, 
                                              num_classes=param.num_classes)
    
    
    model = EfficientNet(blocks_args, 
                         global_params)
    
    model.to(opt.trainhandler.device)
    
    return model, blocks_args, global_params

def _count_params_flops(opt, blocks_args, global_params, sparsity=0):
    # define different value according to your structure
    conv_stem = {'kernel': 3, 'stride': 2, 'out_channel': 24}
    last_ops = {'out_channel': 150, 'num_classes': global_params.num_classes}
    activation = global_params.activation
    input_size = int(math.ceil(32 * global_params.resolution_coefficient))
    use_bias = False
    
    counter = MicroNetCounter(conv_stem, blocks_args, global_params, last_ops, activation, input_size, use_bias, add_bits_base=32, mul_bits_base=32)

    # Constants
    INPUT_BITS = opt.trainhandler.precision
    ACCUMULATOR_BITS = opt.trainhandler.precision
    PARAMETER_BITS = INPUT_BITS
    SUMMARIZE_BLOCKS = True
    if sparsity != 0:
        SPARSITY = sparsity / 100
    else:
        SPARSITY = sparsity

    params, flops, blocks_params_flops, blocks_res_channel = counter.print_summary(SPARSITY, PARAMETER_BITS, ACCUMULATOR_BITS, INPUT_BITS, summarize_blocks=SUMMARIZE_BLOCKS)
    print('flops: {:.6f}M, params: {:.6f}M'.format(flops, params))
    print('score: {:.6f} + {:.6f} = {:.6f}'.format(flops/(10490), params/(36.5 * 4), flops/(10490) + params/(36.5 * 4)))
    print('=' * 50)
    
    return blocks_params_flops, blocks_res_channel

def _get_trainhanlder(opt, model, dataloaders, dataset_sizes):    
    criterion = CRITERION[opt.criterion.algo](**opt.criterion.param)
    
    params = adapted_weight_decay(model, opt.optimizer.param.get('weight_decay', 1e-5))
    optimizer = OPTIMIZER[opt.optimizer.algo](params, **opt.optimizer.param)
    #optimizer = OPTIMIZER[opt.optimizer.algo](model.parameters(), **opt.optimizer.param)
    
    if opt.scheduler.enabled:
        scheduler = SCHEDULER[opt.scheduler.type](optimizer, **opt.scheduler.param)
    else:
        scheduler = None
        
    train_handler = TrainHandler(model, 
                                 dataloaders, 
                                 dataset_sizes, 
                                 criterion, 
                                 optimizer, 
                                 scheduler, 
                                 device=opt.trainhandler.device, 
                                 path=opt.trainhandler.path,
                                 mixup=opt.trainhandler.mixup.enabled,
                                 alpha=opt.trainhandler.mixup.alpha,
                                 precision=opt.trainhandler.precision)
    
    train_handler.set_name(opt.trainhandler.name)
    
    return train_handler

def __get_sparsity(param, round, prev_sparsity):
    assert param.sparsity > prev_sparsity
    
    if param.gradually:
        round_sparsity = prev_sparsity + (param.sparsity - prev_sparsity) * ((1 / param.rounds) * (round + 1))
    else:
        round_sparsity = prev_sparsity + math.pow((param.sparsity - prev_sparsity), ((1 / param.rounds) * (round + 1)))

    return round_sparsity
    
def __get_masks(opt, param, train_handler, round_sparsity, masks):
    if not masks:
        masks = PRUNE_METHOD[param.method](train_handler.model, round_sparsity, norm=param.norm, device=opt.trainhandler.device)
    else:
        masks = PRUNE_METHOD[param.method](train_handler.model, round_sparsity, prev_masks=masks, norm=param.norm, device=opt.trainhandler.device)

    return masks
        
def __get_model_name(param, name, round_sparsity):
    if param.rounds == 1:
        model_name = name + '_oneshot' + '_sparsity_%.2f' % round_sparsity
    else:
        model_name = name + '_iterative' + '_sparsity_%.2f' % round_sparsity
        
    return model_name
    
def __reset_states(param, train_handler):
    if param.weight_reset:
        train_handler.reset_model_state()
    else:
        train_handler.optimizer.load_state_dict(train_handler.init_states['optimizer'])
        if train_handler.scheduler != None:
            train_handler.scheduler.load_state_dict(train_handler.init_states['scheduler'])

    return train_handler

def __update_states(opt, train_handler, optimizer_param, scheduler_param):
    params = adapted_weight_decay(train_handler.model, optimizer_param.get('weight_decay', 1e-5))
    optimizer = OPTIMIZER[opt.optimizer.algo](params, **optimizer_param)
    train_handler.optimizer = optimizer
    if train_handler.scheduler != None:
        train_handler.scheduler = SCHEDULER[opt.scheduler.type](optimizer, **scheduler_param)        
    return train_handler

def _pruning(opt, train_handler, blocks_args, global_params):
    masks = None
    param = opt.model.prune
    prev_sparsity = opt.model.pretrained.sparsity if opt.model.pretrained.enabled else 0
        
    for round in range(param.rounds):
        round_sparsity = __get_sparsity(param, round, prev_sparsity)
        blocks_params_flops, _ = _count_params_flops(opt, blocks_args, global_params, sparsity=round_sparsity)

        masks = __get_masks(opt, param, train_handler, round_sparsity, masks)
        train_handler.model.set_masks(masks)
        train_handler.prune = True

        model_name = __get_model_name(param, opt.trainhandler.name, round_sparsity)
        train_handler.set_name(model_name)

        train_handler.test_model(pretrained=True)
        
        train_handler = __reset_states(param, train_handler)
        train_handler = __update_states(opt, train_handler, opt.model.prune.optimizer, opt.model.prune.scheduler)
        
        train_handler.train_model(num_epochs=opt.model.prune.num_epochs)
        train_handler.test_model()

    return blocks_params_flops

def __get_early_exit_model(opt, train_handler, blocks_args, global_params, blocks_res_channel):
    param = opt.early_exit.param
    
    early_exit = get_early_exit(in_channels=blocks_res_channel[param.blocks_idx+1][1], final_channels=param.final_channels, input_size=blocks_res_channel[param.blocks_idx+1][0], use_bias=param.use_bias, thres=param.thres, blocks_idx=param.blocks_idx, device=opt.trainhandler.device)
    early_exit_model = EfficientNet_EarlyExiting(blocks_args, global_params, early_exit)
        
    early_exit_model.load_state_dict(train_handler.model.state_dict(), strict=False)
    
    for name, params in early_exit_model.named_parameters():
        for comp, _ in train_handler.model.named_parameters():
            if name == comp:
                params.requires_grad = False
            
    train_handler.model = early_exit_model.to(opt.trainhandler.device)
    
    if train_handler.precision == 16:
        train_handler.model.half()
    
    
    return train_handler, early_exit

def __set_trainhandler(opt, train_handler):
    train_handler.prune = False
    train_handler.early_exit = True
    train_handler.set_criterion(OverHaulLoss(**opt.early_exit.criterion))
    train_handler.set_prediction(early_exit_pred_mark)
    train_handler.set_name(train_handler.name + opt.early_exit.name)
    train_handler = __update_states(opt, train_handler, opt.early_exit.optimizer, opt.early_exit.scheduler)

    train_handler.init_states['optimizer'] = copy.deepcopy(train_handler.optimizer.state_dict())
    train_handler.init_states['scheduler'] = copy.deepcopy(train_handler.scheduler.state_dict())
    train_handler.init_states['model'] = copy.deepcopy(train_handler.model.state_dict())
    
    return train_handler

def _count_early_exit_params_flops(global_params, early_exit, sparsity, blocks_params_flops, exit_percent):
    counter = MicroNetCounter(global_params=global_params, early_exit=early_exit)

    # Constants
    INPUT_BITS = opt.trainhandler.precision
    ACCUMULATOR_BITS = opt.trainhandler.precision
    PARAMETER_BITS = INPUT_BITS
    SUMMARIZE_BLOCKS = True
    if sparsity != 0:
        SPARSITY = sparsity / 100
    else:
        SPARSITY = sparsity

    params, flops, _, _ = counter.print_summary(SPARSITY, PARAMETER_BITS, ACCUMULATOR_BITS, INPUT_BITS, summarize_blocks=SUMMARIZE_BLOCKS)
    
    total_params = params
    exit_flops, not_exit_flops = flops, flops
    
    exit = False
    for idx, (block_params, block_flops) in enumerate(blocks_params_flops):
        total_params += block_params
        not_exit_flops += block_flops
        if not exit:
            exit_flops += block_flops
            
        # when idx is 0, it is 'conv_stem'
        if early_exit.blocks_idx == (idx - 1):
            exit = True

    total_flops = (exit_flops * exit_percent) + (not_exit_flops * (1 - exit_percent))
    print('flops: {:.6f}M, params: {:.6f}M'.format(total_flops, total_params))
    print('score: {:.6f} + {:.6f} = {:.6f}'.format(total_flops/(10490), total_params/(36.5 * 4), total_flops/(10490) + total_params/(36.5 * 4)))
    print('=' * 50)
    
def _early_exit_pruning(opt, train_handler, blocks_args, global_params, early_exit, blocks_params_flops):
    masks = None
    param = opt.early_exit.prune
    prev_sparsity = opt.early_exit.pretrained.sparsity if opt.early_exit.pretrained.enabled else 0
    name = train_handler.name + opt.early_exit.name
        
    for round in range(param.rounds):
        round_sparsity = __get_sparsity(param, round, prev_sparsity)

        masks = __get_masks(opt, param, train_handler, round_sparsity, masks)
        train_handler.model.set_masks(masks)
        train_handler.prune = True

        model_name = __get_model_name(param, name, round_sparsity)
        train_handler.set_name(model_name)

        train_handler.test_model(pretrained=True)
        
        train_handler = __reset_states(param, train_handler)
        train_handler = __update_states(opt, train_handler, opt.early_exit.prune.optimizer, opt.early_exit.prune.scheduler)
        
        train_handler.train_model(num_epochs=opt.early_exit.prune.num_epochs)
        _, _, exit_percent = train_handler.test_model()
        
        _count_early_exit_params_flops(global_params, early_exit, round_sparsity, blocks_params_flops, exit_percent)

def _early_exit(opt, train_handler, blocks_args, global_params, blocks_params_flops, blocks_res_channel):
    train_handler, early_exit = __get_early_exit_model(opt, train_handler, blocks_args, global_params, blocks_res_channel)
    
    train_handler = __set_trainhandler(opt, train_handler)
    # pretrained
    if not opt.early_exit.pretrained.enabled:
        train_handler.train_model(num_epochs=opt.early_exit.num_epochs)
        _, _, exit_percent = train_handler.test_model()
        sparsity = 0
    else:
        initial_model = torch.load(os.path.join(opt.early_exit.pretrained.fpath, 'initial_model.pth'), map_location=opt.trainhandler.device)
        train_handler.init_states['model'] = copy.deepcopy(initial_model)
        
        fpath = opt.early_exit.pretrained.fpath
                    
        pretrained_dict = torch.load(os.path.join(fpath, 'trained_model.pth'), map_location=opt.trainhandler.device)
        train_handler.model.load_state_dict(pretrained_dict, strict=False)
        
        _, _, exit_percent = train_handler.test_model(pretrained=True)
        sparsity = opt.early_exit.pretrained.sparsity / 100
        
    # counting
    _count_early_exit_params_flops(global_params, early_exit, sparsity, blocks_params_flops, exit_percent)
        
    # TODO: pruning about early_exit
    if opt.early_exit.prune.enabled:
        _early_exit_pruning(opt, train_handler, blocks_args, global_params, early_exit, blocks_params_flops)

def run(opt):
    dataloaders, dataset_sizes = _get_dataset(opt)

    model, blocks_args, global_params = _get_model(opt)

    blocks_params_flops, blocks_res_channel = _count_params_flops(opt, blocks_args, global_params)
        
    train_handler = _get_trainhanlder(opt, model, dataloaders, dataset_sizes)
    
    if not opt.model.pretrained.enabled:
        train_handler.train_model(num_epochs=opt.trainhandler.num_epochs)
    else:
        print('Pretrained model is loaded')
        print('=' * 50)
        blocks_params_flops, blocks_res_channel = _count_params_flops(opt, blocks_args, global_params, sparsity=opt.model.pretrained.sparsity)
        initial_model = torch.load(os.path.join(opt.model.pretrained.fpath, 'initial_model.pth'), map_location=opt.trainhandler.device)
        train_handler.init_states['model'] = copy.deepcopy(initial_model)
        
        fpath = opt.model.pretrained.fpath
                    
        pretrained_dict = torch.load(os.path.join(fpath, 'trained_model.pth'), map_location=opt.trainhandler.device)
        train_handler.model.load_state_dict(pretrained_dict, strict=False)

    train_handler.test_model(pretrained=opt.model.pretrained.enabled)
    
    if opt.model.prune.enabled:
        blocks_params_flops = _pruning(opt, train_handler, blocks_args, global_params)
        
    if opt.early_exit.enabled:
        _early_exit(opt, train_handler, blocks_args, global_params, blocks_params_flops, blocks_res_channel)
        
if __name__ == "__main__":
    opt = ConfLoader(sys.argv[1]).opt

    if opt.trainhandler.get('seed', None):
        torch.manual_seed(opt.trainhandler.seed)
    
    run(opt)