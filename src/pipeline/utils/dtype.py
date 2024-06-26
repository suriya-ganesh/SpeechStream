from typing import Dict, Union

import torch

_str_to_dtype: Dict[str, torch.dtype] = dict(
    float32=torch.float32,
    float=torch.float32,
    float64=torch.float64,
    double=torch.float64,
    float16=torch.float16,
    half=torch.float16,
    bfloat16=torch.bfloat16,
    bf16=torch.bfloat16,
    uint8=torch.uint8,
    byte=torch.uint8,
    int8=torch.int8,
    char=torch.int8,
    int16=torch.int16,
    short=torch.int16,
    int32=torch.int32,
    int=torch.int32,
    int64=torch.int64,
    long=torch.int64,
    bool=torch.bool,
)


def str_to_dtype(dtype: Union[str, torch.dtype]) -> torch.dtype:
    """Convert a data type name to a PyTorch data type"""
    if isinstance(dtype, torch.dtype):
        return dtype
    name = str(dtype).strip().lower()
    if name.startswith("torch."):
        name = name.replace("torch.", "", 1)
    if name.startswith("fp"):
        name = name.replace("fp", "float", 1)
    if name not in _str_to_dtype:
        raise ValueError(f"Unrecognized dtype ({name})")
    return _str_to_dtype[name]
