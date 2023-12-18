"""Code based of "minlora" implementation 

Reference: https://github.com/cccntu/minLoRA
"""

import math
from functools import partial

import torch
import torch.nn.utils.parametrize as parametrize
from torch import nn


class LoRAParametrization(nn.Module):
    def __init__(self, fan_in, fan_out, fan_in_fan_out=False, rank=4, lora_dropout_p=0.0, lora_alpha=1, init_method="kaiming", original_weights=None, cache_V=False):
        super().__init__()
        # if weight is stored as (fan_out, fan_in), the memory layout of A & B follows (W + BA)x
        # otherwise, it's x(W + AB). This allows us to tie the weights between linear layers and embeddings
        self.swap = (lambda x: (x[1], x[0])) if fan_in_fan_out else (lambda x: x)
        self.lora_alpha, self.rank = lora_alpha, rank
        self.lora_A = nn.Parameter(torch.zeros(self.swap((rank, fan_in))))
        self.lora_B = nn.Parameter(torch.zeros(self.swap((fan_out, rank))))
        self.original_weights = original_weights
        self.cache_V = cache_V # for regularization
        self._init_AB(init_method)
        #nn.init.kaiming_uniform_(self.lora_A, a=math.sqrt(5))
        self.scaling = lora_alpha / rank
        self.lora_dropout = nn.Dropout(p=lora_dropout_p) if lora_dropout_p > 0 else lambda x: x
        self.dropout_fn = self._dropout if lora_dropout_p > 0 else lambda x: x
        self.register_buffer("lora_dropout_mask", torch.ones(self.swap((1, fan_in)), dtype=self.lora_A.dtype))
        self.forward_fn = self.lora_forward


    def _dropout(self, A):
        # to mimic the original implementation: A @ dropout(x), we do (A * dropout(ones)) @ x
        return A * self.lora_dropout(self.lora_dropout_mask)

    def lora_forward(self, X):
        return X + torch.matmul(*self.swap((self.lora_B, self.dropout_fn(self.lora_A)))).view(X.shape) * self.scaling

    def forward(self, X):
        return self.forward_fn(X)

    def disable_lora(self):
        self.forward_fn = lambda x: x

    def enable_lora(self):
        self.forward_fn = self.lora_forward

    def _init_AB(self, init_method):
        if init_method == "kaiming":
            nn.init.kaiming_uniform_(self.lora_A, a=math.sqrt(5))
        elif init_method == "svd":
            if self.original_weights is None:
                raise ValueError("original_weights must be provided for svd init")
            u, s, v = torch.linalg.svd(self.original_weights)
            self.lora_A.data = (u[:, :self.rank] * s[:self.rank]).T
            self.lora_B.data = v[:, :self.rank] 
            
            if self.cache_V:
                self.register_buffer("V", v[:, :self.rank].clone()) # should make this optional


    @classmethod
    def from_linear(cls, layer, rank=4, lora_dropout_p=0.0, lora_alpha=1, init_method="kaiming", original_weights=None, cache_V=False):
        fan_out, fan_in = layer.weight.shape
        return cls(
            fan_in, 
            fan_out, 
            fan_in_fan_out=False, 
            rank=rank, 
            lora_dropout_p=lora_dropout_p,
            lora_alpha=lora_alpha, 
            init_method=init_method, 
            original_weights=original_weights,
            cache_V=cache_V
        )

    @classmethod
    def from_conv2d(cls, layer, rank=4, lora_dropout_p=0.0, lora_alpha=1):
        fan_out, fan_in = layer.weight.view(layer.weight.shape[0], -1).shape
        return cls(
            fan_in, fan_out, fan_in_fan_out=False, rank=rank, lora_dropout_p=lora_dropout_p, lora_alpha=lora_alpha
        )

    @classmethod
    def from_embedding(cls, layer, rank=4, lora_dropout_p=0.0, lora_alpha=1):
        fan_in, fan_out = layer.weight.shape
        return cls(
            fan_in, fan_out, fan_in_fan_out=True, rank=rank, lora_dropout_p=lora_dropout_p, lora_alpha=lora_alpha
        )
    

class LoRAFAParametrization(LoRAParametrization):
    def __init__(self, fan_in, fan_out, fan_in_fan_out=False, rank=4, lora_dropout_p=0.0, lora_alpha=1, init_method="svd", original_weights=None, cache_V=False):
        super().__init__(fan_in, fan_out, fan_in_fan_out, rank, lora_dropout_p, lora_alpha, init_method, original_weights, cache_V)
        self.lora_A.requires_grad_(False)

    def _init_AB(self, init_method):
        if init_method == "kaiming":
            nn.init.kaiming_uniform_(self.lora_A, a=math.sqrt(5))
            # note - from paper: initialize frozen A with orthogonal basis obtained from QR decomposition of A
            Q, R = torch.linalg.qr(self.lora_A.data.T)
            self.lora_A.data = Q.T
            self.lora_B.data = torch.matmul(self.lora_B, R)
        elif init_method == "svd":
            super()._init_AB('svd')


default_lora_config = {  # specify which layers to add lora to, by default only add to linear layers
    nn.Linear: {
        "weight": partial(LoRAParametrization.from_linear, rank=4),
    },
}


def apply_lora(layer, register=True, merge=False, lora_config=default_lora_config):
    """add lora parametrization to a layer, designed to be used with model.apply"""
    if register:
        if type(layer) in lora_config:
            for attr_name, parametrization in lora_config[type(layer)].items():
                parametrize.register_parametrization(layer, attr_name, parametrization(layer))
    else:  # this will remove all parametrizations, use with caution
        if hasattr(layer, "parametrizations"):
            for attr_name in layer.parametrizations.keys():
                parametrize.remove_parametrizations(layer, attr_name, leave_parametrized=merge)


def add_lora(model, lora_config=default_lora_config):
    """add lora parametrization to all layers in a model. Calling it twice will add lora twice"""
    model.apply(partial(apply_lora, lora_config=lora_config))


def add_lora_by_name(model, target_module_names, lora_config=default_lora_config):
    """Add LoRA parameterization to specific layers in a model by names"""
    for name, layer in model.named_modules():
        if any([m in name for m in target_module_names]):
            add_lora(layer, lora_config=lora_config)


def add_lora_by_layer_names(model, lora_named_config=None):
    """Add LoRA parameterization to layers specified by unique names in config"""
    for name, layer in model.named_modules():
        if name in lora_named_config:
            add_lora(layer, lora_config=lora_named_config[name])


def merge_lora(model):
    """merge lora parametrization to all layers in a model. This will remove all parametrization"""
    model.apply(partial(apply_lora, register=False, merge=True))


def remove_lora(model):
    """remove lora parametrization to all layers in a model. This will remove all parametrization"""
    model.apply(partial(apply_lora, register=False, merge=False))