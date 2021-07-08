""" NOTE: This is currently unused.

TODO: Not 100% sure I understand what this does
"""
from functools import singledispatch
from torch import nn, Tensor
import torch



@singledispatch
def weight_b_sym(forward_layer: nn.Module, backward_layer: nn.Module) -> None:
    raise NotImplementedError(forward_layer, backward_layer)


@weight_b_sym.register(nn.Conv2d)
def weight_b_sym_conv2d(
    forward_layer: nn.Conv2d, backward_layer: nn.ConvTranspose2d
) -> None:
    assert forward_layer.weight.shape == backward_layer.weight.shape
    with torch.no_grad():
        # NOTE: I guess the transposition isn't needed here?
        backward_layer.weight.data = forward_layer.weight.data


@weight_b_sym.register(nn.Linear)
def weight_b_sym_linear(forward_layer: nn.Linear, backward_layer: nn.Linear) -> None:
    assert forward_layer.in_features == backward_layer.out_features
    assert forward_layer.out_features == backward_layer.in_features
    # TODO: Not sure how this would work if a bias term was used, so assuming we don't
    # have one for now.
    assert forward_layer.bias is None and backward_layer.bias is None
    # assert forward_layer.bias is not None == backward_layer.bias is not None

    with torch.no_grad():
        # NOTE: I guess the transposition isn't needed here?
        backward_layer.weight.data = forward_layer.weight.data.t()




@singledispatch
def weight_b_normalize(
    backward_layer: nn.Module, dx: Tensor, dy: Tensor, dr: Tensor
) -> None:
    """ TODO: I don't yet understand what this is supposed to do. """
    return
    # raise NotImplementedError(f"No idea what this means atm.")


@weight_b_normalize.register
def linear_weight_b_normalize(
    backward_layer: nn.Linear, dx: Tensor, dy: Tensor, dr: Tensor
) -> None:
    # dy = dy.view(dy.size(0), -1)
    # dx = dx.view(dx.size(0), -1)
    # dr = dr.view(dr.size(0), -1)

    factor = ((dy ** 2).sum(1)) / ((dx * dr).view(dx.size(0), -1).sum(1))
    factor = factor.mean()

    with torch.no_grad():
        backward_layer.weight.data = factor * backward_layer.weight.data


@weight_b_normalize.register
def conv_weight_b_normalize(
    backward_layer: nn.ConvTranspose2d, dx: Tensor, dy: Tensor, dr: Tensor
) -> None:
    # first technique: same normalization for all out fmaps

    dy = dy.view(dy.size(0), -1)
    dx = dx.view(dx.size(0), -1)
    dr = dr.view(dr.size(0), -1)

    factor = ((dy ** 2).sum(1)) / ((dx * dr).sum(1))
    factor = factor.mean()
    # factor = 0.5*factor

    with torch.no_grad():
        backward_layer.weight.data = factor * backward_layer.weight.data

    # second technique: fmaps-wise normalization
    """
    dy_square = ((dy.view(dy.size(0), dy.size(1), -1))**2).sum(-1) 
    dx = dx.view(dx.size(0), dx.size(1), -1)
    dr = dr.view(dr.size(0), dr.size(1), -1)
    dxdr = (dx*dr).sum(-1)
    
    factor = torch.bmm(dy_square.unsqueeze(-1), dxdr.unsqueeze(-1).transpose(1,2)).mean(0)
    
    factor = factor.view(factor.size(0), factor.size(1), 1, 1)
        
    with torch.no_grad():
        self.b.weight.data = factor*self.b.weight.data
    """
