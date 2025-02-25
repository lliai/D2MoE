import re
import torch
import subprocess
from config import cfg

MULTIGPUS_MODEL_NAME_LIST = ['llama-2-70b']

def list_available_gpus():
    cfg['custom_cuda_streams'] = {}
    cfg['default_cuda_streams'] = {}
    # Check if CUDA is available
    if torch.cuda.is_available():
        # Get the number of GPUs available
        num_gpus = torch.cuda.device_count()
        print(f"Number of CUDA Devices: {num_gpus}")
        
        for gpu_id in range(num_gpus):
            # Set the current device to the GPU
            stream = torch.cuda.Stream(device=gpu_id)
            # Store the stream for the corresponding GPU
            cfg['custom_cuda_streams'][gpu_id] = stream
            default_stream = torch.cuda.default_stream(device=gpu_id)
            cfg['default_cuda_streams'][gpu_id] = default_stream
    else:
        print("CUDA is not available. No GPU detected.")

def process_control():
    # print('is cuda available: ', torch.cuda.is_available())
    # print('torch version: ', torch.__version__)
    # print('cuda version: ', torch.version.cuda)
    # print('cudnn version: ', torch.backends.cudnn.version())
    cfg['cudatoolkit_version'] = float(torch.version.cuda)
    cfg['cudnn_version'] = float(torch.backends.cudnn.version())

    try:
        result = subprocess.run(['nvidia-smi', '--query-gpu=gpu_name', '--format=csv,noheader'], capture_output=True, text=True)
        gpu_name = result.stdout.strip()
        # print(f"GPU Name: {gpu_name}")
    except Exception as e:
        print(f"An error occurred: {e}")
    cfg['gpu_name'] = gpu_name
    list_available_gpus()
    cfg['data_type'] = torch.float16
    cfg['data_type_max'] = torch.finfo(cfg['data_type']).max
    cfg['data_type_min'] = torch.finfo(cfg['data_type']).min
    # cfg['data_type_min_positive'] = torch.finfo(cfg['data_type']).tiny
    cfg['data_type_min_positive'] = 1e-8
    # This can be implemented dynamically in each layer
    # tc stands for tensor core
    # https://docs.nvidia.com/deeplearning/performance/dl-performance-matrix-multiplication/index.html
    if cfg['data_type'] == torch.float16 or cfg['data_type'] == torch.float32:
        if cfg['cudatoolkit_version'] >= 11 and cfg['cudnn_version'] >= 7630:
            if gpu_name == 'NVIDIA A100-SXM4-40GB':
                cfg['tc_multiple'] = 64
            elif gpu_name == 'NVIDIA GeForce RTX 4090':
                cfg['tc_multiple'] = 8
            else:
                cfg['tc_multiple'] = 64
        else:
            cfg['tc_multiple'] = 64


    cfg['model_name'] = cfg['control']['model_name']
    model_name = cfg['model_name']
    cfg['task_name'] = cfg['control']['task_name']
    cfg['batch_size'] = int(cfg['control']['batch_size'])
    cfg['max_seq_len'] = int(cfg['control']['max_seq_len'])
    cfg['prune_metric'] = cfg['control']['prune_metric']
    cfg['prune_method'] = cfg['control']['prune_method']

    if 'default' in cfg['prune_method']:
        if 'flap' in cfg['prune_method']:
            cfg['prune_method'] += '-calib'
            cfg['prune_method'] += '-flapratio'
            cfg['prune_method'] += '-bias'
        elif 'wandasp' in cfg['prune_method']:
            cfg['prune_method'] += '-calib'
        elif 'probe' in cfg['prune_method']:
            cfg['prune_method'] += '-calib'
            cfg['prune_method'] += '-ema'
            cfg['prune_method'] += '-respick'

    cfg['mode'] = cfg['control']['mode']
    if 'probe' not in cfg['prune_method']:
        if 'dense' in cfg['prune_method']:
            pass
        elif cfg['mode'] != 'asyncinter':
            raise ValueError('mode is not valid, need to be asyncinter when not using probe')
    if 'probe' in cfg['prune_method'] and cfg['mode'] not in ['asyncintra', 'sync']:
        raise ValueError('mode is not valid, need to be asyncintra or sync when using probe')
    
    prune_ratio_list = cfg['control']['prune_ratio'].split('-')
    if len(prune_ratio_list) == 1:
        # further update prune_ratio in make_model to match the mean prune ratio
        cfg['prune_ratio'] = float(prune_ratio_list[0])
        # cfg['mean_prune_ratio'] = float(prune_ratio_list[0])
    elif len(prune_ratio_list) == 2:
        # only use this for grid search, first ratio for attn, second ratio for mlp
        cfg['prune_ratio'] = [float(x) for x in prune_ratio_list]
        # cfg['mean_prune_ratio'] = [float(x) for x in prune_ratio_list]
    else:
        raise ValueError('prune_ratio is not valid')
          
    cfg['ema_momentum'] = 0.99
    if 'ema' in cfg['prune_method']:
        match = re.search(r'ema(\d+\.\d+)', cfg['prune_method'])
        if match:
            # Convert the matched string to a float
            float_value = float(match.group(1))
            cfg['ema_momentum'] = float_value  
        else:
            float_value = None
    
    cfg['resinfo_ratio'] = 1
    if 'resinfo' in cfg['prune_method']:
        match = re.search(r'resinfo(\d+(?:\.\d+)?)', cfg['prune_method'])
        if match:
            # Convert the matched string to a float
            float_value = float(match.group(1))
            cfg['resinfo_ratio'] = float_value  
        else:
            float_value = None
        
    
    if torch.cuda.is_available():
        # cfg['cuda_default_stream'] = torch.cuda.default_stream()
        # # if 'asyncintra' in cfg['mode']:
        # #     cfg['cuda_default_stream'] = torch.cuda.Stream()
        # cfg['cuda_stream1'] = torch.cuda.Stream()
        pass
    else:
        raise ValueError('No cuda device available')

    cfg['calib_info'] = cfg['control']['calib_info']
    if cfg['calib_info'] != 'None':
        calib_info_list = cfg['calib_info'].split('-')
        cfg['calibration_dataset'] = calib_info_list[0]
        cfg['calibration_nsamples'] = int(calib_info_list[1])


    cfg['cust_tgt_modules'] = cfg['control']['cust_tgt_modules'].split('+')
    if 'llama' in cfg['model_name'] and cfg['cust_tgt_modules'] != ['default']:
        cfg['cust_tgt_modules'] = [module.replace('-', '_') for module in cfg['cust_tgt_modules']]
    elif 'opt' in cfg['model_name'] and cfg['cust_tgt_modules'] != ['default']:
        cfg['cust_tgt_modules'] = [module.replace('-', '_') for module in cfg['cust_tgt_modules']]
    elif cfg['cust_tgt_modules'] == ['default']:
        if 'llama' in cfg['model_name']:
            cfg['cust_tgt_modules'] = TRANSFORMERS_MODELS_TO_ERI_TARGET_MODULES_MAPPING['llama']
        elif 'opt' in cfg['model_name']:
            cfg['cust_tgt_modules'] = TRANSFORMERS_MODELS_TO_ERI_TARGET_MODULES_MAPPING['opt']
        else:
            raise ValueError('Not valid model name')

    
    cfg['calibration_stage'] = False
    # default skip 3 layers
    cfg['skip_layers'] = [0, 1, 2]
    if 'skip' in cfg['prune_method']:
        match = re.findall(r'skip(-?\d+(-\d+)*)', cfg['prune_method'])
        print('match: ', match)
        if match:
            # Convert found strings to integers
            numbers_str = match[0][0].split('-')
            numbers = [int(num) for num in numbers_str if num]  
            even_index_numbers = numbers[0::2]
            odd_index_numbers = numbers[1::2]     
            # Generate the list using range
            skip_layers = []
            for i in range(len(odd_index_numbers)):
                start = even_index_numbers[i]
                end = odd_index_numbers[i]
                skip_layers.extend(range(start, end + 1))
            cfg['skip_layers'] = skip_layers


    cfg['cur_batch_index'] = -1
    cfg['prune_dim'] = -1
    cfg['pq_p'] = 1
    cfg['pq_q'] = 2
    cfg['pq_beta'] = 0.9
    cfg['pq_gamma'] = 1
    make_data_name()
    
    if cfg['task_name'] in ['clm', 'csr']:
        cfg['collate_mode'] = 'transformer'
        cfg['gpt2'] = {'max_length': cfg['max_seq_len']}
        if 'llama' in cfg['model_name']:
            cfg[model_name] = {'max_length': cfg['max_seq_len']}
        if 'opt' in cfg['model_name']:
            cfg[model_name] = {'max_length': cfg['max_seq_len']}

        cfg['qk_prune_way'] = 'whole'
        cfg['vo_prune_way'] = 'whole'

        cfg['prune_info'] = cfg['control']['prune_info']
        if cfg['prune_info'] != 'None':
            prune_info_list = cfg['prune_info'].split('-')
            if 'llama' in cfg['model_name']:
                prune_keys = ['q', 'k', 'v', 'gate', 'up']
            elif 'opt' in cfg['model_name']:
                prune_keys = ['q', 'k', 'v', 'fc1']
            
            if 'probe' in cfg['prune_method']:
                cfg['probe_generation_type'] = prune_info_list[-1].split('+')
                for probe_type in cfg['probe_generation_type']:
                    if not any(term in probe_type for term in ['rank', 'mean', 'absnml']):
                        raise ValueError('probe_generation_type is not valid')
            
                for key in prune_keys:
                    # default
                    cfg[f'{key}_prune'] = prune_info_list[prune_keys.index(key)]
                    if cfg[f'{key}_prune'] != 'None':

                        match = re.search(r'\d*\.?\d+', cfg[f'{key}_prune'])
                        float_value = None
                        if match:
                            float_value = float(match.group(0))
                        else:
                            float_value = None

                        float_pattern = re.compile(r'\d*\.?\d+')
                        # Find all matches and convert them to floats
                        floats = [float(match) for match in float_pattern.findall(cfg[f'{key}_prune'])]
                        if not floats:
                            raise ValueError(f'probe ratio is not valid for {key}, please specify it in prune_info')
                        else:
                            cfg[f'{key}_probe_ratio'] = floats
            else:
                for key in prune_keys:
                    cfg[f'{key}_prune'] = prune_info_list[prune_keys.index(key)]


        if 'probe' in cfg['prune_method'] and 'probefixratio' in cfg['prune_method']:
            match = re.search(r'probefixratio(\d+\.\d+)', cfg['prune_method'])
            if match:
                # Convert the matched string to a float
                float_value = float(match.group(1))
                cfg['probefixratio'] = float_value 
            else:
                float_value = None
    elif cfg['task_name'] in ['ic']:
        cfg['collate_mode'] = 'dict'
        data_shape = {'MNIST': [1, 28, 28], 'FashionMNIST': [1, 28, 28], 'SVHN': [3, 32, 32], 'CIFAR10': [3, 32, 32],
                      'CIFAR100': [3, 32, 32]}
        target_size = {'MNIST': 10, 'FashionMNIST': 10, 'SVHN': 10, 'CIFAR10': 10, 'CIFAR100': 100}
        cfg['linear'] = {}
        cfg['mlp'] = {'hidden_size': 128, 'scale_factor': 2, 'num_layers': 2, 'activation': 'relu'}
        cfg['cnn'] = {'hidden_size': [64, 128, 256, 512]}
        cfg['resnet9'] = {'hidden_size': [64, 128, 256, 512]}
        cfg['resnet18'] = {'hidden_size': [64, 128, 256, 512]}
        cfg['wresnet28x2'] = {'depth': 28, 'widen_factor': 2, 'drop_rate': 0.0}
        cfg['data_shape'] = data_shape[cfg['data_name']]
        cfg['target_size'] = target_size[cfg['data_name']]
    else:
        raise ValueError('Not valid task name')
    if model_name not in cfg:
        cfg[model_name] = {}
    cfg[model_name]['shuffle'] = {'train': False, 'test': False}
    if cfg['task_name'] in ['clm', 'csr']:
        cfg[model_name]['batch_size'] = {'train': cfg['batch_size'], 'test': cfg['batch_size']}
    elif cfg['task_name'] in ['ic']:
        cfg[model_name]['batch_size'] = {'train': cfg['batch_size'], 'test': cfg['batch_size']}
    else:
        raise ValueError('Not valid task name')

    cfg['logger_detailed_info'] = False
    cfg['onlyprobe'] = False
    # cfg['onlyprobeinfo'] = True
    cfg['onlyprobeinfo'] = False
    cfg['test_speed'] = False

    cfg['asyncintra_on_diff_gpu'] = False
    cfg['pad_tokens'] = None
    # print('cfg: ', cfg, flush=True)
    return


def make_data_name():
    data_name_list = cfg['control']['data_name'].split('-')
    if len(data_name_list) == 2:
        cfg['data_name'], cfg['subset_name'] = data_name_list
    else:
        cfg['data_name'] = data_name_list[0]
        cfg['subset_name'] = 'none'
    if cfg['task_name'] in ['clm', 'csr']:
        data_name_dict = {
            'c4': {'data_name': 'c4',
                          'subset_name_dict': {'none': {'subset_name': None,
                                                   'text_column': None,
                                                   'label_column': None}
                                           }                       
                         },
            # https://huggingface.co/datasets/wikitext
            'wikitext': {'data_name': 'wikitext',
                          'subset_name_dict': {'2v1': {'subset_name': 'wikitext-2-raw-v1',
                                                   'text_column': ['text'],
                                                   'label_column': None}
                                           }                       
                         },
            # piqa: piqa
            # boolq: boolq , 
            # arc-e: arc-easy 
            # arc-c: arc-challenge (Clark et al., 2018), 
            # hellaswag: hellaswag (Zellers et al., 2019) 
            # winogrande: winogrande 
            # obqa: OpenBookQA (Mihaylov et al., 2018)
            # preprocessing according to: https://github.com/EleutherAI/lm-evaluation-harness/blob/master/lm_eval/tasks/arc.py
            # https://huggingface.co/datasets/piqa
            'boolq': {'data_name': 'google/boolq',
                    'subset_name_dict': {
                        'none': {'subset_name': None,
                              'text_column': ['hardcode'],
                              'label_column': 'hardcode'}
                        },
            },  
            'piqa': {'data_name': 'piqa',
                    'subset_name_dict': {
                        'none': {'subset_name': None,
                              'text_column': ['hardcode'],
                              'label_column': 'hardcode'}
                        },
            },          
            # https://huggingface.co/datasets/social_i_qa/viewer/default/validation
            'siqa': {'data_name': 'social_i_qa',
                    'subset_name_dict': {
                        'none': {'subset_name': None,
                              'text_column': ['hardcode'],
                              'label_column': 'hardcode'}
                        },
            },         
            # https://huggingface.co/datasets/ai2_arc          
           'arc': {'data_name': 'ai2_arc',
                    'subset_name_dict': {
                        'e': {'subset_name': 'ARC-Easy',
                              'text_column': ['hardcode'],
                             'label_column': 'hardcode'},   
                        'c': {'subset_name': 'ARC-Challenge',
                              'text_column': ['hardcode'],
                              'label_column': 'hardcode'}
                        },                        
            },
            # https://huggingface.co/datasets/Rowan/hellaswag
            'hellaswag': {'data_name': 'Rowan/hellaswag',
                    'subset_name_dict': {
                        'none': {'subset_name': None,
                              'text_column': 'hardcode',
                              'label_column': 'hardcode'}, 
                        },                        
            },
            'winogrande': {'data_name': 'winogrande',
                    'subset_name_dict': {
                        'none': {'subset_name': 'winogrande_debiased',
                              'text_column': 'hardcode',
                              'label_column': 'hardcode'}, 
                        },                        
            },
            # https://huggingface.co/datasets/openbookqa
            'obqa': {'data_name': 'openbookqa',
                    'subset_name_dict': {
                        'none': {'subset_name': 'main',
                              'text_column': ['hardcode'],
                              'label_column': 'hardcode'},    
                        },                        
            },
        }
        cfg['hf_data_name'] = data_name_dict[cfg['data_name']]['data_name']
        cfg['hf_subset_name'] = data_name_dict[cfg['data_name']]['subset_name_dict'][
            cfg['subset_name']]['subset_name']
        cfg['text_column'] = data_name_dict[cfg['data_name']]['subset_name_dict'][
            cfg['subset_name']]['text_column']
        cfg['label_column'] = data_name_dict[cfg['data_name']]['subset_name_dict'][
            cfg['subset_name']]['label_column']
    return


TRANSFORMERS_MODELS_TO_ERI_TARGET_MODULES_MAPPING = {
    "opt": ["q_proj", "v_proj", "k_proj", "out_proj", "fc1", "fc2"],
    "llama": ["q_proj", "v_proj", "o_proj", "k_proj", "gate_proj", "up_proj", "down_proj"],
}




# gpt2 layer
'''
key:  transformer.h.3 <class 'transformers.models.gpt2.modeling_gpt2.GPT2Block'>
key:  transformer.h.3.ln_1 <class 'torch.nn.modules.normalization.LayerNorm'>
key:  transformer.h.3.attn <class 'transformers.models.gpt2.modeling_gpt2.GPT2Attention'>
key:  transformer.h.3.attn.c_attn <class 'transformers.pytorch_utils.Conv1D'>
key:  transformer.h.3.attn.c_proj <class 'transformers.pytorch_utils.Conv1D'>
key:  transformer.h.3.attn.attn_dropout <class 'torch.nn.modules.dropout.Dropout'>
key:  transformer.h.3.attn.resid_dropout <class 'torch.nn.modules.dropout.Dropout'>
key:  transformer.h.3.ln_2 <class 'torch.nn.modules.normalization.LayerNorm'>
key:  transformer.h.3.mlp <class 'transformers.models.gpt2.modeling_gpt2.GPT2MLP'>
key:  transformer.h.3.mlp.c_fc <class 'transformers.pytorch_utils.Conv1D'>
key:  transformer.h.3.mlp.c_proj <class 'transformers.pytorch_utils.Conv1D'>
key:  transformer.h.3.mlp.act <class 'transformers.activations.NewGELUActivation'>
key:  transformer.h.3.mlp.dropout <class 'torch.nn.modules.dropout.Dropout'>
'''


# opt 1.3b layer

'''
125M、350M、1.3B、2.7B、6.7B、13B、30B、66B、175B
selected: 6.7B、13B、30B、66B
key:  model.decoder.layers.0 <class 'transformers.models.opt.modeling_opt.OPTDecoderLayer'>
key:  model.decoder.layers.0.self_attn <class 'transformers.models.opt.modeling_opt.OPTAttention'>
key:  model.decoder.layers.0.self_attn.k_proj <class 'torch.nn.modules.linear.Linear'>
key:  model.decoder.layers.0.self_attn.v_proj <class 'torch.nn.modules.linear.Linear'>
key:  model.decoder.layers.0.self_attn.q_proj <class 'torch.nn.modules.linear.Linear'>
key:  model.decoder.layers.0.self_attn.out_proj <class 'torch.nn.modules.linear.Linear'>
key:  model.decoder.layers.0.activation_fn <class 'torch.nn.modules.activation.ReLU'>
key:  model.decoder.layers.0.self_attn_layer_norm <class 'torch.nn.modules.normalization.LayerNorm'>
key:  model.decoder.layers.0.fc1 <class 'torch.nn.modules.linear.Linear'>
key:  model.decoder.layers.0.fc2 <class 'torch.nn.modules.linear.Linear'>
key:  model.decoder.layers.0.final_layer_norm <class 'torch.nn.modules.normalization.LayerNorm'>
'''

# llama-2-7b layer
'''
7b, 13b, 65b
key:  model.layers.0 <class 'transformers.models.llama.modeling_llama.LlamaDecoderLayer'>
key:  model.layers.0.self_attn <class 'transformers.models.llama.modeling_llama.LlamaAttention'>
key:  model.layers.0.self_attn.q_proj <class 'torch.nn.modules.linear.Linear'>
key:  model.layers.0.self_attn.k_proj <class 'torch.nn.modules.linear.Linear'>
key:  model.layers.0.self_attn.v_proj <class 'torch.nn.modules.linear.Linear'>
key:  model.layers.0.self_attn.o_proj <class 'torch.nn.modules.linear.Linear'>
key:  model.layers.0.self_attn.rotary_emb <class 'transformers.models.llama.modeling_llama.LlamaRotaryEmbedding'>
key:  model.layers.0.mlp <class 'transformers.models.llama.modeling_llama.LlamaMLP'>
key:  model.layers.0.mlp.gate_proj <class 'torch.nn.modules.linear.Linear'>
key:  model.layers.0.mlp.up_proj <class 'torch.nn.modules.linear.Linear'>
key:  model.layers.0.mlp.down_proj <class 'torch.nn.modules.linear.Linear'>
key:  model.layers.0.mlp.act_fn <class 'transformers.activations.SiLUActivation'>
key:  model.layers.0.input_layernorm <class 'transformers.models.llama.modeling_llama.LlamaRMSNorm'>
key:  model.layers.0.post_attention_layernorm <class 'transformers.models.llama.modeling_llama.LlamaRMSNorm'>
'''
