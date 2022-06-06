from __future__ import annotations

from ast import literal_eval
from dataclasses import dataclass, field
from typing import Callable, Iterator

import torch
from pl_bolts.datamodules import CIFAR10DataModule
from simple_parsing import choice
from torch import Tensor, nn
from torch.nn import functional as F

from meulemans_dtp.final_configs.cifar10_DDTPConv import config as _config
from meulemans_dtp.lib import utils
from meulemans_dtp.lib.conv_layers import DDTPConvLayer
from meulemans_dtp.lib.conv_network import DDTPConvNetworkCIFAR
from meulemans_dtp.main import Args
from target_prop.config import MiscConfig
from target_prop.models.model import Model, StepOutputDict
from target_prop.networks import Network
from target_prop.networks.network import activations


def clean_up_config(config: dict):
    cleaned_up_config = config.copy()
    cleaned_up_config["epsilon"] = literal_eval(cleaned_up_config["epsilon"])
    return cleaned_up_config


DEFAULT_ARGS = Args.from_dict(clean_up_config(_config))


class MeulemansNetwork(DDTPConvNetworkCIFAR, Network):
    @dataclass
    class HParams(Network.HParams):
        activation: str = choice(*activations.keys(), default="elu")
        bias: bool = True
        hidden_activation: str = "tanh"  # Default was tanh, set to 'elu' to match ours.
        feedback_activation: str = "linear"
        initialization: str = "xavier_normal"
        sigma: float = 0.1
        plots: bool | None = None
        forward_requires_grad: bool = False
        nb_feedback_iterations: tuple[int, int, int, int] = (10, 20, 55, 20)

        def __post_init__(self):
            self.activation_class: type[nn.Module] = activations[self.activation]

    def __init__(
        self, in_channels: int, n_classes: int, hparams: MeulemansNetwork.HParams | None = None
    ):
        assert in_channels == 3
        assert n_classes == 10
        hparams = hparams or self.HParams()
        self.hparams = hparams
        super().__init__(
            bias=hparams.bias,
            hidden_activation=hparams.hidden_activation,
            feedback_activation=hparams.feedback_activation,
            initialization=hparams.initialization,
            sigma=hparams.sigma,
            plots=hparams.plots,
            forward_requires_grad=hparams.forward_requires_grad,
            nb_feedback_iterations=hparams.nb_feedback_iterations,
        )

    def __iter__(self) -> Iterator[nn.Module]:
        return iter(self._layers)

    def __len__(self) -> int:
        return len(self._layers)


class Meulemans(Model):
    @dataclass
    class HParams(Model.HParams):
        args: Args = field(default_factory=lambda: Args.from_dict(clean_up_config(_config)))
        """ The arguments form the Meulemans codebase. """

    def __init__(
        self,
        datamodule: CIFAR10DataModule,
        network: MeulemansNetwork,
        hparams: Meulemans.HParams | None = None,
        config: MiscConfig | None = None,
    ):
        if not isinstance(network, MeulemansNetwork):
            raise RuntimeError(
                f"Meulemans DTP only works with a specific network architecture. "
                f"Can't yet use networks of type {type(network)}."
            )
        super().__init__(datamodule=datamodule, network=network, hparams=hparams, config=config)
        self.hp: Meulemans.HParams
        self.config = config
        del self.forward_net
        self.network = network

        # TODO: Get rid of this overlap, either by making a wrapper around the
        # DDTPConvNetworkCIFAR that is compatible with the Network protocol, or by removing the
        # Network protocol entirely.
        # self.network = meulemans_network
        # self.network.hparams = args
        # self.net_hp = args.  # fixme: not quite right.

        self.automatic_optimization = False
        temp_forward_optimizer_list, temp_feedback_optimizer_list = utils.choose_optimizer(
            self.hp.args, self.network
        )
        self.n_forward_optimizers = len(temp_forward_optimizer_list._optimizer_list)
        self.n_feedback_optimizers = len(temp_feedback_optimizer_list._optimizer_list)

    def forward(self, x: Tensor) -> Tensor:
        return self.network(x)

    def configure_optimizers(self):
        forward_optimizer_list, feedback_optimizer_list = utils.choose_optimizer(
            self.hp.args, self.network
        )
        assert len(forward_optimizer_list._optimizer_list) == self.n_forward_optimizers
        assert len(feedback_optimizer_list._optimizer_list) == self.n_feedback_optimizers

        # TODO: For PL, we need to return the list of optimizers, but the code for Meulemans
        # expects
        # to get an OptimizerList object...
        return [*forward_optimizer_list._optimizer_list, *feedback_optimizer_list._optimizer_list]

    @property
    def feedback_optimizers(self) -> list[torch.optim.Optimizer]:
        return self.optimizers()[self.n_forward_optimizers :]

    @property
    def forward_optimizers(self) -> list[torch.optim.Optimizer]:
        return self.optimizers()[: self.n_forward_optimizers]

    def train_feedback_parameters(self):
        """Train the feedback parameters on the current mini-batch.

        Adapted from the meulemans codebase
        """
        args = self.hp.args
        feedback_optimizers = self.feedback_optimizers
        net = self.network

        def _optimizer_step():
            for optimizer in feedback_optimizers:
                optimizer.step()

        def _zero_grad(set_to_none: bool = False):
            for optimizer in feedback_optimizers:
                optimizer.zero_grad(set_to_none=set_to_none)

        _zero_grad()
        # TODO: Assuming these for now, to simplify the code a bit.
        assert args.direct_fb
        assert not args.train_randomized_fb
        assert not args.diff_rec_loss

        for layer_index, layer in enumerate(net.layers[:-1]):
            n_iter = layer._nb_feedback_iterations
            for iteration in range(n_iter):
                # TODO: Double-check if they are zeroing the gradients in each layer properly.
                # So far it seems like they aren't!
                net.compute_feedback_gradients(layer_index)
                # NOTE: @lebrice: Is it really necessary to step all optimizers for each
                # iteration, for each layer? Isn't this O(n^3) with n the number of layers?
                # Maybe it's necessary because of the weird direct feedback connections?
                _optimizer_step()

    def train_forward_parameters(
        self,
        inputs: Tensor,
        predictions: Tensor,
        targets: Tensor,
        loss_function: Callable[[Tensor, Tensor], Tensor],
    ):
        """Train the forward parameters on the current mini-batch."""

        args = self.hp.args
        assert not args.train_randomized

        # net = self.network
        forward_optimizers = self.forward_optimizers

        if predictions.requires_grad == False:
            # we need the gradient of the loss with respect to the network
            # output. If a LeeDTPNetwork is used, this is already the case.
            # The gradient will also be saved in the activations attribute of the
            # output layer of the network
            predictions.requires_grad = True

        # NOTE: Simplifying the code a bit by assuming that this is False, for now.
        # save_target = args.save_GN_activations_angle or args.save_BP_activations_angle

        # forward_optimizer.zero_grad()
        for optimizer in forward_optimizers:
            optimizer.zero_grad()

        loss = loss_function(predictions, targets)

        # Get the target using one backprop step with lr of beta.
        # NOTE: target_lr := beta in our paper.
        output_target = self.network.compute_output_target(loss, target_lr=args.target_stepsize)

        # Computes and saves the gradients for the forward parameters for that layer.
        self.network.layers[-1].compute_forward_gradients(
            h_target=output_target, h_previous=self.network.layers[-2].activations
        )
        # if save_target:
        #     self.layers[-1].target = output_target

        for i in range(self.network.depth - 1):
            h_target = self.network.propagate_backward(output_target, i)
            layer = self.network.layers[i]
            # if save_target:
            #     self.layers[i]._target = h_target
            if i == 0:
                assert isinstance(layer, DDTPConvLayer)
                layer.compute_forward_gradients(
                    h_target=h_target,
                    h_previous=inputs,
                    forward_requires_grad=self.network.forward_requires_grad,
                )
            else:
                previous_layer = self.network.layers[i - 1]
                previous_activations: Tensor | None = previous_layer.activations
                if i == self.network.nb_conv:  # flatten conv layer
                    assert previous_activations is not None
                    previous_activations = previous_activations.flatten(1)
                layer.compute_forward_gradients(
                    h_target, previous_activations, self.network.forward_requires_grad
                )

        if args.classification:
            if args.output_activation == "sigmoid":
                batch_accuracy = utils.accuracy(predictions, utils.one_hot_to_int(targets))
            else:  # softmax
                batch_accuracy = utils.accuracy(predictions, targets)
        else:
            batch_accuracy = None
        batch_loss = loss.detach()

        return batch_accuracy, batch_loss

    def training_step(self, batch: tuple[Tensor, Tensor], batch_idx: int) -> StepOutputDict:
        return self.shared_step(batch, batch_idx, phase="train")

    def shared_step(
        self, batch: tuple[Tensor, Tensor], batch_idx: int, phase: str
    ) -> StepOutputDict:
        x, y = batch
        # TODO: Not currently doing any of the pretraining stuff from their repo.
        # NOTE: Need to do the forward pass to store the activations, which are then used for
        # feedback training
        predictions = self.network(x)
        if phase == "train":
            self.train_feedback_parameters()

        output_activation_to_loss_fn = {"softmax": F.cross_entropy, "sigmoid": F.mse_loss}
        loss_function = output_activation_to_loss_fn[self.hp.args.output_activation]

        if phase == "train":
            batch_accuracy, batch_loss = self.train_forward_parameters(
                inputs=x, predictions=predictions, targets=y, loss_function=loss_function
            )
        else:
            with torch.no_grad():
                batch_loss = loss_function(predictions, y)

        # TODO: The 'batch_loss' here doesn't really mean anything.. We don't current have access
        # to the losses in the forward and feedback training steps.
        return {"logits": predictions, "y": y}
        # train.train_feedback_parameters(
        #     args=self.hp.args, net=self.network, feedback_optimizer=feedback_optimizer
        # )

        # Double-check that the forward parameters have not been updated:
        # _check_forward_params_havent_moved(
        #     meulemans_net=meulemans_network, initial_network_weights=initial_network_weights
        # )
        # loss_function: nn.Module
        # Get the loss function to use (extracted from their code, was saved on train_var).

        # # Make sure that there is nothing in the grads: delete all of them.
        # self.zero_grad(set_to_none=True)
        # predictions = meulemans_network(x)
        # # This propagates the targets backward, computes local forward losses, and sets the gradients
        # # in the forward parameters' `grad` attribute.
        # batch_accuracy, batch_loss = train.train_forward_parameters(
        #     args,
        #     net=meulemans_network,
        #     predictions=predictions,
        #     targets=y,
        #     loss_function=loss_function,
        #     forward_optimizer=forward_optimizer,
        # )
        # assert all(
        #     p.grad is not None for name, p in _get_forward_parameters(meulemans_network).items()
        # )

        # # NOTE: the values in `p.grad` are the gradients from their DTP algorithm.
        # meulemans_dtp_grads = {
        #     # NOTE: safe to ignore, from the check above.
        #     name: p.grad.detach()  # type: ignore
        #     for name, p in _get_forward_parameters(meulemans_network).items()
        # }

        # # Need to rescale these by 1 / beta as well.
        # scaled_meulemans_dtp_grads = {
        #     key: (1 / beta) * grad for key, grad in meulemans_dtp_grads.items()
        # }

        # distances: Dict[str, float] = {}
        # angles: Dict[str, float] = {}
        # with torch.no_grad():
        #     for name, meulemans_backprop_grad in meulemans_backprop_grads.items():
        #         # TODO: Do we need to scale the DRL grads like we do ours DTP?
        #         meulemans_dtp_grad = scaled_meulemans_dtp_grads[name]
        #         distance, angle = compute_dist_angle(meulemans_dtp_grad, meulemans_backprop_grad)

        #         distances[name] = distance
        #         angles[name] = angle
        #     # NOTE: We can actually find the parameter for these: