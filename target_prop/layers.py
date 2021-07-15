from functools import singledispatch
import torch
from torch import nn, Tensor
from torch.nn import functional as F
from typing import (
    Optional,
    NamedTuple,
    Iterable,
    Callable,
    List,
    Sequence,
    Tuple,
    Union,
    ClassVar,
)
from abc import ABC, abstractmethod
from torchvision.models import resnet18
from torch.nn import Sequential
from torch.nn.modules.conv import _size_2_t
from functools import wraps


class Conv2dActivation(nn.Conv2d):
    activation: Callable[[Tensor], Tensor]

    def __init__(
        self,
        in_channels: int,
        out_channels: int,
        kernel_size: _size_2_t,
        stride: _size_2_t = 1,
        padding: _size_2_t = 0,
        dilation: _size_2_t = 1,
        groups: int = 1,
        bias: bool = True,
        padding_mode: str = "zeros",
        activation: Callable[[Tensor], Tensor] = None,
    ):
        super().__init__(
            in_channels=in_channels,
            out_channels=out_channels,
            kernel_size=kernel_size,
            stride=stride,
            padding=padding,
            dilation=dilation,
            groups=groups,
            bias=bias,
            padding_mode=padding_mode,
        )
        if activation is None:
            if not hasattr(type(self), "activation"):
                raise RuntimeError(
                    "Need to either pass an activation as an argument to the "
                    "constructor, or have a callable `activation` class attribute."
                )
            activation = type(self).activation
        self._activation = activation
        assert callable(self._activation)

    def forward(self, input: Tensor) -> Tensor:
        return self._activation(super().forward(input))


class Conv2dReLU(Conv2dActivation):
    activation = F.relu


class Conv2dELU(Conv2dActivation):
    activation = F.elu


class ConvTranspose2dActivation(nn.ConvTranspose2d):
    activation: ClassVar[Callable[[Tensor], Tensor]]

    def __init__(
        self,
        in_channels: int,
        out_channels: int,
        kernel_size: _size_2_t,
        stride: _size_2_t = 1,
        padding: _size_2_t = 0,
        output_padding: _size_2_t = 0,
        groups: int = 1,
        bias: bool = True,
        dilation: int = 1,
        padding_mode: str = "zeros",
        activation: Callable[[Tensor], Tensor] = None,
    ):
        super().__init__(
            in_channels=in_channels,
            out_channels=out_channels,
            kernel_size=kernel_size,
            stride=stride,
            padding=padding,
            output_padding=output_padding,
            groups=groups,
            bias=bias,
            dilation=dilation,
            padding_mode=padding_mode,
        )
        if activation is None:
            if not hasattr(type(self), "activation"):
                raise RuntimeError(
                    "Need to either pass an activation as an argument to the "
                    "constructor, or have a callable `activation` class attribute."
                )
            activation = type(self).activation
        self._activation = activation
        assert callable(self._activation)

    def forward(self, input: Tensor, output_size: Optional[List[int]] = None) -> Tensor:
        out: Tensor = super().forward(input, output_size=output_size)
        return self._activation(out)


class ConvTranspose2dReLU(ConvTranspose2dActivation):
    activation = F.relu


class ConvTranspose2dELU(ConvTranspose2dActivation):
    activation = F.elu


class Reshape(nn.Module):
    def __init__(
        self, target_shape: Tuple[int, ...], source_shape: Tuple[int, ...] = None
    ):
        self.target_shape = tuple(target_shape)
        self.source_shape = tuple(source_shape) if source_shape else ()
        super().__init__()

    def forward(self, inputs):
        if self.source_shape:
            if inputs.shape[1:] != self.source_shape:
                raise RuntimeError(
                    f"Inputs have unexpected shape {inputs.shape[1:]}, expected "
                    f"{self.source_shape}."
                )
        else:
            self.source_shape = inputs.shape[1:]
        outputs = inputs.reshape([inputs.shape[0], *self.target_shape])
        if self.target_shape == (-1,):
            self.target_shape = outputs.shape[1:]
        return outputs

    def __repr__(self):
        return f"{type(self).__name__}({self.source_shape} -> {self.target_shape})"


class AdaptiveAvgPool2d(nn.AdaptiveAvgPool2d):
    def __init__(
        self, output_size: Tuple[int, ...], input_shape: Tuple[int, ...] = None
    ):
        super().__init__(output_size=output_size)
        self.input_shape: Tuple[int, ...] = input_shape or ()
        self.output_shape: Tuple[int, ...] = ()

    def forward(self, input):
        if self.input_shape == ():
            assert len(input.shape[1:]) == 3
            input_shape = input.shape[1:]
            self.input_shape = (input_shape[0], input_shape[1], input_shape[2])
        elif input.shape[1:] != self.input_shape:
            raise RuntimeError(
                f"Inputs have unexpected shape {input.shape[1:]}, expected "
                f"{self.input_shape}."
            )
        out = super().forward(input)
        if not self.output_shape:
            self.output_shape = out.shape[1:]
        elif out.shape[1:] != self.output_shape:
            raise RuntimeError(
                f"Outputs have unexpected shape {out.shape[1:]}, expected "
                f"{self.output_shape}."
            )
        return out


class BatchUnNormalize(nn.Module):
    """ TODO: Implement the 'opposite' of batchnorm2d """

    def __init__(self, num_features: int, dtype=torch.float32):
        super().__init__()
        self.scale = nn.Parameter(
            torch.ones(num_features, dtype=dtype), requires_grad=True
        )
        torch.nn.init.xavier_uniform_(self.scale)
        self.offset = nn.Parameter(
            torch.zeros(num_features, dtype=dtype), requires_grad=True
        )

    def forward(self, input: Tensor) -> Tensor:
        return input * self.scale + self.offset
