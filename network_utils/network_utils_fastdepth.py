try:
    # if this file is ran as  a script
    from network_utils_abstract import NetworkUtilsAbstract
except ModuleNotFoundError:
    # if this file is imported as a module
    from .network_utils_abstract import NetworkUtilsAbstract
from collections import OrderedDict
import os
import sys
import copy
import time
import torch
import pickle
import warnings
import torch
import torch.optim as optim
import torch.nn.functional as F
import torch.nn as nn
import torchvision.transforms as transforms
import torchvision.datasets as datasets
import torch.utils.data.sampler as sampler
from torch.utils.data import DataLoader
try:
    # if this file is ran as  a script
    sys.path.append(os.path.abspath('../'))
    from monodepth2 import datasets
    from monodepth2.utils import readlines
    from monodepth2.networks.layers import *
except ModuleNotFoundError:
    # if this file is imported as a module
    from .monodepth2 import datasets
    from .monodepth2.utils import readlines
    from .monodepth2.networks.layers import *

try:
    # If ran as a script
    sys.path.append(os.path.abspath('../'))
    from constants import *
    import functions as fns
except ModuleNotFoundError:
    # If imported as a module
    from ..constants import *
    from .. import functions as fns
'''
    This is an example of NetAdapt applied to fastdepth.
    We measure the latency on GPU.
'''

'''
    The size of feature maps of simplified layers along channel dimmension 
    are multiples of '_MIN_FEATURE_SIZE'.
    The reason is that on mobile devices, the computation of (B, 7, H, W) tensors 
    would take longer time than that of (B, 8, H, W) tensors.
'''
_MIN_CONV_FEATURE_SIZE = 8
_MIN_FC_FEATURE_SIZE   = 64

'''
    How many times to run the forward function of a layer in order to get its latency.
'''
_MEASURE_LATENCY_SAMPLE_TIMES = 500

'''
    The batch size of input data when running forward functions to measure latency.
'''
_MEASURE_LATENCY_BATCH_SIZE = 128

class networkUtils_fastdepth(NetworkUtilsAbstract):
    num_simplifiable_blocks = None
    input_data_shape = None
    train_loader = None
    holdout_loader = None
    val_loader = None
    optimizer = None

    def __init__(self, model, input_data_shape, dataset_path, finetune_lr=1e-3):
        '''
            Initialize:
                (1) network definition 'network_def'
                (2) num of simplifiable blocks 'num_simplifiable_blocks'. 
                (3) loss function 'criterion'
                (4) data loader for training/validation set 'train_loader' and 'holdout_loader',
                (5) optimizer 'optimizer'
                
            Need to be implemented:
                (1) finetune/evaluation data loader
                (2) loss function
                (3) optimizer
                
            Input: 
                `model`: model from which we will get network_def.
                `input_data_shape`: (list) [C, H, W].
                `dataset_path`: (string) path to dataset.
                `finetune_lr`: (float) short-term fine-tune learning rate.
        '''
        
        super().__init__()

        # Set the shape of the input data.
        self.input_data_shape = input_data_shape
        # Set network definition (conv & fc)
        network_def = self.get_network_def_from_model(model)        
        # Set num_simplifiable_blocks.
        self.num_simplifiable_blocks = 0
        for layer_name, layer_properties in network_def.items():
            if not layer_properties[KEY_IS_DEPTHWISE]:
                self.num_simplifiable_blocks += 1                
        # We cannot reduce the number of filters in the output layer (1).
        self.num_simplifiable_blocks -= 1 

        '''
            The following variables need to be defined depending on tasks:
                (1) finetune/evaluation data loader
                (2) loss function
                (3) optimizer
        '''
        # Data loaders for fine tuning and evaluation.
        self.batch_size = 128
        self.num_workers = 4
        self.momentum = 0.9
        self.weight_decay = 1e-4
        self.finetune_lr = finetune_lr

        # Variables for loading data
        split = "eigen_zhou"
        img_ext = '.png'
        self.height = 192 # 'height' must be a multiple of 32
        self.width = 640 # 'width' must be a multiple of 32
        # Scales for converting disparity to depth
        self.min_depth = 0.1
        self.max_depth = 100
        batch_size = 4
        disparity_smoothness = 1e-7 # disparity smoothness weight"
        num_layers = 18 # Number of ResNet layers
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


        # Architecture choices
        self.scales = [0,1,2,3] # scales used in the loss
        num_scales = len(self.scales)
        self.frame_ids = [0, -1, 1] # frame_ids must start with 0
        num_input_frames = len(self.frame_ids)
        num_pose_frames = 2 # Number of inputs the pose network gets

        self.depth_metric_names = [
            "de/abs_rel", "de/sq_rel", "de/rms", "de/log_rms", "da/a1", "da/a2", "da/a3"]

        data_dir = "C:/Users/z/Desktop/work/data/noncommercial/kitti-raw"

        fpath = os.path.join("monodepth2", "splits", split, "{}_files.txt")
        train_filenames = readlines(fpath.format("train"))
        val_filenames = readlines(fpath.format("val"))

        dataset = datasets.KITTIDepthDataset # Niantic
        train_dataset = dataset(data_dir, train_filenames, self.height, self.width, 
                            self.frame_ids, 4, is_train=True, img_ext=img_ext)
        train_loader = DataLoader(train_dataset, batch_size, True,
                                num_workers=self.num_workers, pin_memory=True,
                                drop_last=True)
        val_dataset = dataset(data_dir, val_filenames, self.height, self.width,
                                self.frame_ids, 4, is_train=False, img_ext=img_ext)
        val_loader = DataLoader(val_dataset, batch_size, True,
                                num_workers=self.num_workers, pin_memory=True,
                                drop_last=True)
        self.train_loader = train_loader
        self.val_loader = val_loader   
        self.criterion = torch.nn.MSELoss()
        #self.criterion = compute_reprojection_loss()
        # self.optimizer = torch.optim.SGD(model.parameters(),
        #                                  finetune_lr, momentum=self.momentum, weight_decay=self.weight_decay)
        
        # self.models = {}
        # self.parameters_to_train = []

        # self.models["encoder"] = networks.ResnetEncoder(num_layers, True)
        # self.models["encoder"].to(self.device)
        # self.parameters_to_train += list(self.models["encoder"].parameters())

        # self.models["depth"] = networks.DepthDecoder(self.models["encoder"].num_ch_enc,
        #                                         self.scales) # Niantic
        # self.models["depth"].to(self.device)
        # self.parameters_to_train += list(self.models["depth"].parameters())

        # # Pose encoder is chosen to be same as the depth encoder for faster training
        # self.models["pose"] = networks.PoseDecoder(self.models["encoder"].num_ch_enc,
        #                                         num_pose_frames) # Niantic
        # self.models["pose"].to(self.device)
        # parameters_to_train += list(self.models["pose"].parameters())
        
        self.optimizer = optim.Adam(model.parameters(), self.learning_rate)
        # self.lr_scheduler = optim.lr_scheduler.StepLR(
        #     self.optimizer, scheduler_step_size, 0.1)

    def _get_layer_by_param_name(self, model, param_name):
        '''
            please refer to def get_layer_by_param_name(...) in functions.py
        '''
        return fns.get_layer_by_param_name(model, param_name)
    

    def _get_keys_from_ordered_dict(self, ordered_dict):
        '''
            please refer to def get_keys_from_ordered_dict(...) in functions.py
        '''
        return fns.get_keys_from_ordered_dict(ordered_dict)
    

    def get_network_def_from_model(self, model):
        '''
            please refer to get_network_def_from_model(...) in functions.py
        '''
        return fns.get_network_def_from_model(model, self.input_data_shape)
    
    
    def simplify_network_def_based_on_constraint(self, network_def, block, constraint, resource_type,
                                                 lookup_table_path=None):
        '''
            Derive how much a certain block of layers ('block') should be simplified 
            based on resource constraints.
            
            Here we treat one block as one layer although a block can contain several layers.
            
            Input:
                `network_def`: simplifiable network definition (conv & fc). Get network def from self.get_network_def_from_model(...)
                `block`: (int) index of block to simplify
                `constraint`: (float) representing the FLOPs/weights/latency constraint the simplied model should satisfy
                `resource_type`: `FLOPs`, `WEIGHTS`, or `LATENCY`
                `lookup_table_path`: (string) path to latency lookup table. Needed only when resource_type == 'LATENCY'
                
            Output:
                `simplified_network_def`: simplified network definition. Indicates how much the network should
                be simplified/pruned.
                `simplified_resource`: (float) the estimated resource consumption of simplified models.
        '''
        return fns.simplify_network_def_based_on_constraint(network_def, block, constraint, 
                                                            resource_type, lookup_table_path)
        

    def simplify_model_based_on_network_def(self, simplified_network_def, model):
        '''
            Choose which filters to perserve
            
            Here filters with largest L2 magnitude will be kept
            
            please refer to def simplify_model_based_on_network_def(...) in functions.py
        '''
        return fns.simplify_model_based_on_network_def(simplified_network_def, model)
    
    
    def extra_history_info(self, network_def):
        '''
            return # of output channels per layer
            
            Input: 
                `network_def`: (dict)
            
            Output:
                `num_filters_str`: (string) show the num of output channels for each layer
        '''
        num_filters_str = [str(layer_properties[KEY_NUM_OUT_CHANNELS]) for _, layer_properties in
                               network_def.items()]
        num_filters_str = ' '.join(num_filters_str)
        return num_filters_str
    

    def _compute_weights_and_flops(self, network_def):
        '''
            please refer to def compute_weights_and_macs(...) in functions.py
        '''
        return fns.compute_weights_and_macs(network_def)
    
    
    def _compute_latency_from_lookup_table(self, network_def, lookup_table_path):
        '''
            please refer to def compute_latency_from_lookup_table(...) in functions.py
        '''
        return fns.compute_latency_from_lookup_table(network_def, lookup_table_path)

    
    def build_lookup_table(self, network_def_full, resource_type, lookup_table_path, 
                           min_conv_feature_size=_MIN_CONV_FEATURE_SIZE, 
                           min_fc_feature_size=_MIN_FC_FEATURE_SIZE, 
                           measure_latency_batch_size=_MEASURE_LATENCY_BATCH_SIZE, 
                           measure_latency_sample_times=_MEASURE_LATENCY_SAMPLE_TIMES, 
                           verbose=True):
        # Build lookup table for latency
        '''
            please refer to def build_latency_lookup_table(...) in functions.py
        '''
        return fns.build_latency_lookup_table(network_def_full, lookup_table_path,
                                      min_conv_feature_size=min_conv_feature_size, 
                                      min_fc_feature_size=min_fc_feature_size,
                                      measure_latency_batch_size=measure_latency_batch_size,
                                      measure_latency_sample_times=measure_latency_sample_times,
                                      verbose=verbose)
        
        
    def compute_resource(self, network_def, resource_type, lookup_table_path=None):
        '''
            please refer to def compute_resource(...) in functions.py
        '''
        return fns.compute_resource(network_def, resource_type, lookup_table_path)
    
    
    def get_num_simplifiable_blocks(self):
        return self.num_simplifiable_blocks
    
    def fine_tune(self, model, iterations, print_frequency=100):
        '''
            short-term fine-tune a simplified model
            
            Input:
                `model`: model to be fine-tuned.
                `iterations`: (int) num of short-term fine-tune iterations.
                `print_frequency`: (int) how often to print fine-tune info.
            
            Output:
                `model`: fine-tuned model.
        '''
        
        #_NUM_CLASSES = 10
        optimizer = torch.optim.SGD(model.parameters(), self.finetune_lr, 
                                         momentum=self.momentum, weight_decay=self.weight_decay)
        model = model.cuda()
        model.train()
        dataloader_iter = iter(self.train_loader)
        for i in range(iterations):
            try:
                inputs = dataloader_iter.next()
            except StopIteration:
                val_iter = iter(dataloader_iter)
                inputs = dataloader_iter.next()
                
            if i % print_frequency == 0:
                print('Fine-tuning iteration {}'.format(i))
                sys.stdout.flush()
            
            for key, ipt in inputs.items():
                inputs[key] = ipt.to(device) 

            # Compute depth prediction
            disp = model(inputs["color_aug", 0, 0])[("disp", 0)]
            # disp = F.interpolate(
            #         disp, [self.opt.height, self.opt.width], mode="bilinear", align_corners=False)

            _, depth_pred = disp_to_depth(disp, self.min_depth, self.max_depth)

            loss = self.compute_depth_losses(inputs, depth_pred)

            # target.unsqueeze_(1)
            # target_onehot = torch.FloatTensor(target.shape[0], _NUM_CLASSES)
            # target_onehot.zero_()
            # target_onehot.scatter_(1, target, 1)
            # target.squeeze_(1)
            # input, target = input.cuda(), target.cuda()
            # target_onehot = target_onehot.cuda()

            optimizer.zero_grad()
            loss.backward()  # compute gradient and do SGD step
            optimizer.step()
        return model

    def compute_depth_losses(self, inputs, depth_pred):
        """Compute depth metrics, to allow monitoring during training

        This isn't particularly accurate as it averages over the entire batch,
        so is only used to give an indication of validation performance
        """
        #depth_pred = outputs[("depth", 0, 0)]
        depth_pred = torch.clamp(F.interpolate(
            depth_pred, [375, 1242], mode="bilinear", align_corners=False), 1e-3, 80)
        depth_pred = depth_pred.detach()

        depth_gt = inputs["depth_gt"]
        mask = depth_gt > 0

        # garg/eigen crop
        crop_mask = torch.zeros_like(mask)
        crop_mask[:, :, 153:371, 44:1197] = 1
        mask = mask * crop_mask

        depth_gt = depth_gt[mask]
        depth_pred = depth_pred[mask]
        depth_pred *= torch.median(depth_gt) / torch.median(depth_pred)

        depth_pred = torch.clamp(depth_pred, min=1e-3, max=80)

        # rmse = (depth_gt - depth_pred) ** 2
        # rmse = torch.sqrt(rmse.mean())
        #depth_errors = compute_depth_errors(depth_gt, depth_pred)

        # for i, metric in enumerate(self.depth_metric_names):
        #     losses[metric] = np.array(depth_errors[i].cpu())
        return self.criterion(depth_gt, depth_pred)


    def evaluate(self, model, print_frequency=10):
        '''
            Evaluate the accuracy of the model
            
            Input:
                `model`: model to be evaluated.
                `print_frequency`: how often to print evaluation info.
                
            Output:
                accuracy: (float) (0~100)
        '''
        
        model = model.cuda()
        model.eval()
        mse = .0
        num_samples = .0
        with torch.no_grad():
            for i, inputs in enumerate(self.val_loader):
                for key, ipt in inputs.items():
                    inputs[key] = ipt.to(device) 

                disp = model(inputs["color_aug", 0, 0])[("disp", 0)]
                # disp = F.interpolate(
                #         disp, [self.opt.height, self.opt.width], mode="bilinear", align_corners=False)

                _, depth_pred = disp_to_depth(disp, self.min_depth, self.max_depth)

                mse += self.compute_depth_losses(inputs, depth_pred)
                #pred = model(input)
                #pred = pred.argmax(dim=1)
                # batch_acc = torch.sum(target == pred)
                # acc += batch_acc.item()
                # num_samples += pred.shape[0]
                
                if i % print_frequency == 0:
                    fns.update_progress(i, len(self.val_loader))
                    print(' ')
        print(' ')
        print('Total MSE: {:4.2f}% '.format(float(mse)))
        print('===================================================================')
        return mse
    

def fastdepth(model, input_data_shape, dataset_path, finetune_lr=1e-3):
    return networkUtils_fastdepth(model, input_data_shape, dataset_path, finetune_lr)