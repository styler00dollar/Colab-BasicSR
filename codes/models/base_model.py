import os
from shutil import copyfile
import torch
import torch.nn as nn
from copy import deepcopy
from collections import Counter, OrderedDict
from models.networks import model_val


class BaseModel():
    def __init__(self, opt):
        self.opt = opt
        # self.device = torch.device('cuda' if opt['gpu_ids'] is not None else 'cpu')
        if opt['gpu_ids'] is not None:
            torch.cuda.current_device()
            torch.cuda.empty_cache()
            self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        else:
            self.device = 'cpu'
        
        self.is_train = opt['is_train']
        self.schedulers = []
        self.optimizers = []
        self.swa = None
        self.swa_start_iter = None

    def feed_data(self, data):
        pass

    def optimize_parameters(self):
        pass

    def get_current_visuals(self):
        pass

    def get_current_losses(self):
        pass

    def print_network(self):
        pass

    def save(self, label):
        pass

    def load(self):
        pass

    def _set_lr(self, lr_groups_l):
        ''' Set learning rate for warmup.
        Args:
            lr_groups_l (list): List for lr_groups, one for each for an optimizer.
        '''
        for optimizer, lr_groups in zip(self.optimizers, lr_groups_l):
            for param_group, lr in zip(optimizer.param_groups, lr_groups):
                param_group['lr'] = lr

    def _get_init_lr(self):
        ''' get the initial lr, which is set by the scheduler (for warmup) 
        '''
        init_lr_groups_l = []
        for optimizer in self.optimizers:
            init_lr_groups_l.append([v['initial_lr'] for v in optimizer.param_groups])
        return init_lr_groups_l

    def update_learning_rate(self, current_step=None, warmup_iter=-1):
        ''' Update learning rate.
        Args:
            current_step (int): Current iteration.
            warmup_iter (int)： Warmup iter numbers. -1 for no warmup.
                Default： -1.
        '''
        # SWA scheduler only steps if current_step > swa_start_iter
        if self.swa and current_step and isinstance(self.swa_start_iter, int) and current_step > self.swa_start_iter:
            self.swa_model.update_parameters(self.netG)
            self.swa_scheduler.step()
            
            #TODO: uncertain, how to deal with the discriminator schedule when the generator enters SWA regime
            # alt 1): D continues with its normal scheduler (current option)
            # alt 2): D also trained using SWA scheduler
            # alt 3): D lr is not modified any longer (Should D be frozen?)
            sched_count = 0
            for scheduler in self.schedulers:
                # first scheduler is G, skip
                if sched_count > 0:
                    scheduler.step()
                sched_count += 1
        # regular schedulers
        else:
            # print(self.schedulers)
            # print(str(scheduler.__class__) + ": " + str(scheduler.__dict__))
            for scheduler in self.schedulers:
                scheduler.step()
            #### if configured, set up warm up learning rate
            if current_step < warmup_iter:
                # get initial lr for each group
                init_lr_g_l = self._get_init_lr()
                # modify warming-up learning rates
                warm_up_lr_l = []
                for init_lr_g in init_lr_g_l:
                    warm_up_lr_l.append([v / warmup_iter * current_step for v in init_lr_g])
                # set learning rate
                self._set_lr(warm_up_lr_l)

    def get_current_learning_rate(self, current_step=None):
        if torch.__version__ >= '1.4.0':
            # Note: SWA only works for torch.__version__ >= '1.6.0'
            if self.swa and current_step and isinstance(self.swa_start_iter, int) and current_step > self.swa_start_iter:
                # SWA scheduler lr
                return self.swa_scheduler.get_last_lr()[0]
            else:
                # Regular G scheduler lr
                return self.schedulers[0].get_last_lr()[0]
        else:
            # return self.schedulers[0].get_lr()[0]
            return self.optimizers[0].param_groups[0]['lr']

    def get_network_description(self, network):
        '''Get the string and total parameters of the network'''
        if isinstance(network, (nn.DataParallel, nn.parallel.DistributedDataParallel)):
            network = network.module
        s = str(network)
        n = sum(map(lambda x: x.numel(), network.parameters()))
        return s, n

    def requires_grad(self, model, flag=True, target_layer=None, net_type=None):
        # for p in model.parameters():
        #     p.requires_grad = flag
        for name, param in model.named_parameters():
            if target_layer is None:  # every layer
                param.requires_grad = flag
            else: #elif target_layer in name:  # target layer
                if net_type == 'D':
                    if 'features.' in name: #vgg-d
                        layer=f'features.{target_layer}.'
                    elif 'conv' in name: # vgg-fea-d
                        layer=f'conv{target_layer}.'
                    elif 'model.' in name: # patch-d
                        layer=f'model.{target_layer}.'
                
                if layer in name:
                    # print(name, layer)
                    param.requires_grad = flag

    def save_network(self, network, network_label, iter_step, latest=None):
        if latest:
            save_filename = 'latest_{}.pth'.format(network_label)
        else:
            save_filename = '{}_{}.pth'.format(iter_step, network_label)
        save_path = os.path.join(self.opt['path']['models'], save_filename)
        if os.path.exists(save_path):
            prev_path = os.path.join(self.opt['path']['models'], 'previous_{}.pth'.format(network_label))
            copyfile(save_path, prev_path)
        if isinstance(network, (nn.DataParallel, nn.parallel.DistributedDataParallel)):
            network = network.module
        state_dict = network.state_dict()
        for key, param in state_dict.items():
            state_dict[key] = param.cpu()
        try: #save model in the pre-1.4.0 non-zipped format
            torch.save(state_dict, save_path, _use_new_zipfile_serialization=False)
        except: #pre 1.4.0, normal torch.save
            torch.save(state_dict, save_path)

    def load_network(self, load_path, network, strict=True, submodule=None, model_type=None, param_key=None):
        '''Load pretrained model into instantiated network.
        Args:
            load_path (str): The path of model to be loaded into the network.
            network (nn.Module): the network.
            strict (bool): Whether if the model will be strictly loaded.
            submodule (str): Specify a submodule of the network to load the model into.
            model_type (str): To do additional validations if needed (either 'G' or 'D').
            param_key (str): The parameter key of loaded model. If set to
                None, will use the root 'path'.
        '''
        
        #Get bare model, especially under wrapping with DistributedDataParallel or DataParallel.
        if isinstance(network, (nn.DataParallel, nn.parallel.DistributedDataParallel)):
            network = network.module
        # network.load_state_dict(torch.load(load_path), strict=strict)

        # load into a specific submodule of the network
        if not (submodule is None or submodule.lower() == 'none'.lower()):
            network = network.__getattr__(submodule)
        
        # load_net = torch.load(load_path)
        load_net = torch.load(
            load_path, map_location=lambda storage, loc: storage)

        # to allow loading state_dicts
        if 'state_dict' in load_net:
            load_net = load_net['state_dict']
        
        # load specific keys of the model
        if param_key is not None:
            load_net = load_net[param_key]
        
        # remove unnecessary 'module.' if needed
        for k, v in deepcopy(load_net).items():
            if k.startswith('module.'):
                load_net[k[7:]] = v
                load_net.pop(k)

        # validate model type to be loaded in the network can do
        # any additional conversion or modification steps here
        # (requires 'model_type', either 'G' or 'D')
        if model_type:
            load_net = model_val(
                opt_net=self.opt,
                state_dict=load_net,
                model_type=model_type)

        network.load_state_dict(load_net, strict=strict)
        
        # If loading a network with more parameters into a model with less parameters:
        # model = ABPN_v5(input_dim=3, dim=32)
        # model = model.to(device)
        # pretrained_dict = torch.load(model_name, map_location=lambda storage, loc: storage)
        # model_dict = model.state_dict()
        # pretrained_dict = {k: v for k, v in pretrained_dict.items() if k in model_dict}
        # model_dict.update(pretrained_dict)
        # model.load_state_dict(model_dict)
    
    def save_training_state(self, epoch, iter_step, latest=None):
        '''Saves training state during training, which will be used for resuming'''
        state = {'epoch': epoch, 'iter': iter_step, 'schedulers': [], 'optimizers': []}
        for s in self.schedulers:
            state['schedulers'].append(s.state_dict())
        for o in self.optimizers:
            state['optimizers'].append(o.state_dict())
        if self.opt['is_train'] and self.opt['use_swa']:
            # only save swa_scheduler if needed
            state['swa_scheduler'] = []
            if self.swa and isinstance(self.swa_start_iter, int) and iter_step > self.swa_start_iter:
                state['swa_scheduler'].append(self.swa_scheduler.state_dict())
        if latest:
            save_filename = 'latest.state'
        else:
            save_filename = '{}.state'.format(iter_step)
        save_path = os.path.join(self.opt['path']['training_state'], save_filename)
        if os.path.exists(save_path):
            prev_path = os.path.join(self.opt['path']['training_state'], 'previous.state')
            copyfile(save_path, prev_path)
        torch.save(state, save_path)

    def resume_training(self, resume_state):
        '''Resume the optimizers and schedulers for training'''
        resume_optimizers = resume_state['optimizers']
        resume_schedulers = resume_state['schedulers']
        assert len(resume_optimizers) == len(self.optimizers), 'Wrong lengths of optimizers'
        assert len(resume_schedulers) == len(self.schedulers), 'Wrong lengths of schedulers'
        for i, o in enumerate(resume_optimizers):
            self.optimizers[i].load_state_dict(o)
        for i, s in enumerate(resume_schedulers):
            if hasattr(self.schedulers[i], 'milestones'): # for schedulers without milestones attribute
                if isinstance(self.schedulers[i].milestones, Counter) and isinstance(s['milestones'], list):
                    s['milestones'] = Counter(s['milestones'])
            self.schedulers[i].load_state_dict(s)
        if self.opt['is_train'] and self.opt['use_swa']:
            # Only load the swa_scheduler if it exists in the state
            if resume_state.get('swa_scheduler', None):
                resume_swa_scheduler = resume_state['swa_scheduler']
                for i, s in enumerate(resume_swa_scheduler):
                    self.swa_scheduler.load_state_dict(s)
    
    #TODO: check all these updates 
    def update_schedulers(self, train_opt):
        '''Update scheduler parameters if they are changed in the JSON configuration'''
        if train_opt['lr_scheme'] == 'StepLR':
            for i, s in enumerate(self.schedulers):
                if self.schedulers[i].step_size != train_opt['lr_step_size'] and train_opt['lr_step_size'] is not None:
                    print("Updating step_size from {} to {}".format(self.schedulers[i].step_size, train_opt['lr_step_size']))
                    self.schedulers[i].step_size = train_opt['lr_step_size']
                #common
                if self.schedulers[i].gamma !=train_opt['lr_gamma'] and train_opt['lr_gamma'] is not None:
                    print("Updating lr_gamma from {} to {}".format(self.schedulers[i].gamma, train_opt['lr_gamma']))
                    self.schedulers[i].gamma =train_opt['lr_gamma']
        if train_opt['lr_scheme'] == 'StepLR_Restart':
            for i, s in enumerate(self.schedulers):
                if self.schedulers[i].step_sizes != train_opt['lr_step_sizes'] and train_opt['lr_step_sizes'] is not None:
                    print("Updating step_sizes from {} to {}".format(self.schedulers[i].step_sizes, train_opt['lr_step_sizes']))
                    self.schedulers[i].step_sizes = train_opt['lr_step_sizes']
                if self.schedulers[i].restarts != train_opt['restarts'] and train_opt['restarts'] is not None:
                    print("Updating restarts from {} to {}".format(self.schedulers[i].restarts, train_opt['restarts']))
                    self.schedulers[i].restarts = train_opt['restarts']
                if self.schedulers[i].restart_weights != train_opt['restart_weights'] and train_opt['restart_weights'] is not None:
                    print("Updating restart_weights from {} to {}".format(self.schedulers[i].restart_weights, train_opt['restart_weights']))
                    self.schedulers[i].restart_weights = train_opt['restart_weights']
                if self.schedulers[i].clear_state != train_opt['clear_state'] and train_opt['clear_state'] is not None:
                    print("Updating clear_state from {} to {}".format(self.schedulers[i].clear_state, train_opt['clear_state']))
                    self.schedulers[i].clear_state = train_opt['clear_state']
                #common
                if self.schedulers[i].gamma !=train_opt['lr_gamma'] and train_opt['lr_gamma'] is not None:
                    print("Updating lr_gamma from {} to {}".format(self.schedulers[i].gamma, train_opt['lr_gamma']))
                    self.schedulers[i].gamma =train_opt['lr_gamma']
        if train_opt['lr_scheme'] == 'MultiStepLR':
            for i, s in enumerate(self.schedulers):
                if list(self.schedulers[i].milestones) != train_opt['lr_steps'] and train_opt['lr_steps'] is not None:
                    if not list(train_opt['lr_steps']) == sorted(train_opt['lr_steps']):
                        raise ValueError('lr_steps should be a list of'
                             ' increasing integers. Got {}', train_opt['lr_steps'])
                    print("Updating lr_steps from {} to {}".format(list(self.schedulers[i].milestones), train_opt['lr_steps']))
                    if isinstance(self.schedulers[i].milestones, Counter):
                        self.schedulers[i].milestones = Counter(train_opt['lr_steps'])
                    else:
                        self.schedulers[i].milestones = train_opt['lr_steps']
                #common
                if self.schedulers[i].gamma !=train_opt['lr_gamma'] and train_opt['lr_gamma'] is not None:
                    print("Updating lr_gamma from {} to {}".format(self.schedulers[i].gamma, train_opt['lr_gamma']))
                    self.schedulers[i].gamma =train_opt['lr_gamma']
        if train_opt['lr_scheme'] == 'MultiStepLR_Restart':
            for i, s in enumerate(self.schedulers):
                if list(self.schedulers[i].milestones) != train_opt['lr_steps'] and train_opt['lr_steps'] is not None:
                    if not list(train_opt['lr_steps']) == sorted(train_opt['lr_steps']):
                        raise ValueError('lr_steps should be a list of'
                             ' increasing integers. Got {}', train_opt['lr_steps'])
                    print("Updating lr_steps from {} to {}".format(list(self.schedulers[i].milestones), train_opt['lr_steps']))
                    if isinstance(self.schedulers[i].milestones, Counter):
                        self.schedulers[i].milestones = Counter(train_opt['lr_steps'])
                    else:
                        self.schedulers[i].milestones = train_opt['lr_steps']
                if self.schedulers[i].restarts != train_opt['restarts'] and train_opt['restarts'] is not None:
                    print("Updating restarts from {} to {}".format(self.schedulers[i].restarts, train_opt['restarts']))
                    self.schedulers[i].restarts = train_opt['restarts']
                if self.schedulers[i].restart_weights != train_opt['restart_weights'] and train_opt['restart_weights'] is not None:
                    print("Updating restart_weights from {} to {}".format(self.schedulers[i].restart_weights, train_opt['restart_weights']))
                    self.schedulers[i].restart_weights = train_opt['restart_weights']
                if self.schedulers[i].clear_state != train_opt['clear_state'] and train_opt['clear_state'] is not None:
                    print("Updating clear_state from {} to {}".format(self.schedulers[i].clear_state, train_opt['clear_state']))
                    self.schedulers[i].clear_state = train_opt['clear_state']
                #common
                if self.schedulers[i].gamma !=train_opt['lr_gamma'] and train_opt['lr_gamma'] is not None:
                    print("Updating lr_gamma from {} to {}".format(self.schedulers[i].gamma, train_opt['lr_gamma']))
                    self.schedulers[i].gamma =train_opt['lr_gamma']
