import copy
import importlib
import json
import os
import random
import torch
import torch.distributed
import numpy as np
from collections import OrderedDict
from pathlib import Path
from typing import Union, Dict


def read_json(file: Union[str, os.PathLike]):
    """
    Read json from file
    :param file: the path to the json file
    :return: ordered dictionary content
    """
    file = Path(file)
    with file.open("rt") as handle:
        return json.load(handle, object_hook=OrderedDict)


def write_json(content: Dict, file: Union[str, os.PathLike]):
    """
    Write content to a json file
    :param content: the content dictionary
    :param file: the path to save json file
    """
    file = Path(file)
    with file.open("wt") as handle:
        json.dump(content, handle, indent=4, sort_keys=False)


def write_to_file(file: Union[str, os.PathLike], text: Union[str, list], mode: str = "w"):
    with open(file, mode, encoding="utf-8") as w:
        if isinstance(text, str):
            w.write(text)
        elif isinstance(text, list):
            w.write("\n".join(text))


def del_index_column(df):
    return df.loc[:, ~df.columns.str.contains("^Unnamed")]


def get_project_root(**kwargs):
    project_name = kwargs.pop("project_name", "explainable_nrs")
    file_parts = Path(os.getcwd()).parts
    abs_path = Path(f"{os.sep}".join(file_parts[:file_parts.index(project_name) + 1]))
    return os.path.relpath(abs_path, os.getcwd())


def news_sampling(news, ratio):
    """ Sample ratio samples from news list.
    If length of news is less than ratio, pad zeros.

    Args:
        news (list): packed_input news list
        ratio (int): sample number

    Returns:
        list: output of sample list.
    """
    if ratio > len(news):
        return news + [0] * (ratio - len(news))
    else:
        return random.sample(news, ratio)


def init_obj(module_name: str, module_config: dict, module: object, *args, **kwargs):
    """
    Finds a function handle with the name given as 'type' in config, and returns the
    instance initialized with corresponding arguments given.

    `object = init_obj('Baseline', module, a, b=1)`
    is equivalent to
    `object = module.module_name(a, b=1)`
    """
    module_args = copy.deepcopy(module_config)
    module_args.update(kwargs)  # update extra configuration
    return getattr(module, module_name)(*args, **module_args)


def init_data_loader(config, *args, **kwargs):
    # setup data_loader instances
    module_data = importlib.import_module("modules.data_loader")
    data_loader = init_obj(config.dataloader_type, config.final_configs, module_data, *args, **kwargs)
    return data_loader


def init_model_class(config, *args, **kwargs):
    # setup model class
    module_model = importlib.import_module("modules.models")
    model_class = init_obj(config.arch_type, config.final_configs, module_model, *args, **kwargs)
    return model_class


def gather_dict(dict_object, process_num=2):
    """
    gather vectors from all processes
    :param process_num: number of process
    :param dict_object: vectors to gather
    :return: gathered numpy array vectors
    """
    if torch.distributed.is_initialized():
        dicts_object = [{} for _ in range(process_num)]  # used for distributed inference
        torch.distributed.barrier()
        torch.distributed.all_gather_object(dicts_object, dict_object)
        for i in range(process_num):
            dict_object.update(dicts_object[i])
    return dict_object


def convert_dict_to_numpy(dict_object):
    """
    convert dict to numpy array
    :param dict_object: dict to convert
    :return: numpy array
    """
    return np.array([dict_object[i] for i in range(len(dict_object))])


def load_batch_data(batch_dict, device, multi_gpu=True):
    """
    load batch data to default device
    """
    if torch.distributed.is_initialized() and multi_gpu:  # use multi-gpu
        return batch_dict
    return {k: v.to(device) for k, v in batch_dict.items()}
