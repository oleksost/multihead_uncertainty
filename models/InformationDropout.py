"""Implementation of Achille & Soatto's 2016 paper "Information Dropout."
https://arxiv.org/pdf/1611.01353
https://github.com/ucla-vision/information-dropout
See docstring for `InformationDropoutLayer` for more details.
"""

import attr
from attr.validators import instance_of
import numpy as np

from typing import Union, Tuple

from torch.autograd import Variable
from torch.nn import Module, Parameter
from torch.nn.init import kaiming_normal_
import torch.nn.functional as F

from utils.mypy_shim import MyPyShim

import torch as T
if T.cuda.is_available():  # Define TP for accessing GPU-agnostic operations
    import torch.cuda as TP
else:
    import torch as TP

ParameterOrVariable = Union[Parameter, Variable]


@attr.s(frozen=True, slots=True)
class InfoActivations(MyPyShim):
    """Return value of `InformationDropoutLayer.forward`."""
    # Outputs from primary layer, corrupted by posterior
    activations: Variable = attr.ib(validator=instance_of(Variable))
    kl: Variable = attr.ib()  # KL divergence of posterior from prior.

    def __attrs_post_init__(self):
        assert self.kl.size() == self.activations.size()[:1]


class NormalParameters(Module):
    """Tensor parameters of normal distribution with diagonal covariance.
    Args:
        `mean`: Tensor of mean values
        `alpha`: Tensor of standard deviations. Same shape as mean.
    """
    def __init__(self, mean: ParameterOrVariable,
                 alpha: ParameterOrVariable) -> None:
        super(NormalParameters, self).__init__()
        self.mean: ParameterOrVariable = mean
        self.alpha: ParameterOrVariable = alpha
        assert self.mean.size() == self.alpha.size()

    def kl_divergence(self, other: 'NormalParameters') -> Variable:
        """KL(self || other)
        Corresponds to eq. (5), Proposition 3, p. 5 of Achille & Soatto, and is
        implemented in passing in their `cluttered.MyTask.information_pool`.
        https://github.com/ucla-vision/information-dropout/blob/c80b5/cluttered.py#L88
        |------------------------+--------------------- +---------------+
        | Their notation Prop. 3 | Their implementation | Our variables |
        |------------------------+--------------------- +---------------+
        | 0                      | `mu1`                | `self.mean`   |
        | σ                      | `sigma1`             | `self.alpha`  |
        | μ                      | `network`            | `other.mean`  |
        | α                      | `alpha`              | `other.alpha` |
        |------------------------+--------------------- +---------------+
        They assume the prior mean is 0 in Prop. 3, but it is learnable in the
        code.
        Their implementation of the actual eq. (5) formula is at
        https://github.com/ucla-vision/information-dropout/blob/c80b5/utils.py#10
        This skips the constant 1/2 term, since it has no impact on learning.
        In each example in the batch, takes the sum of the KL's of the
        posteriors for each activation, since the activations are assumed to be
        independent. This assumption justified is by Proposition 1, p. 4 in
        Achille & Soatto. Corresponds to
        https://github.com/ucla-vision/information-dropout/blob/c80b5/cluttered.py#L164
        """
        denom = 2 * self.alpha**2
        kls = (other.alpha**2 + (other.mean - self.mean)**2) / denom - \
            other.alpha.log() + self.alpha.log() - 0.5
        assert kls.max().data.item() < np.inf
        assert kls.min().data.item() + 1e-6 >= 0
        #self.print_max_kl(kls)
        flattened_example_kls = kls.view(kls.size(0), -1)  # Size (N, C*H*W)
        return T.sum(flattened_example_kls, dim=1)  # KL for each example

    def print_max_kl(self, kls: ParameterOrVariable) -> None:
        "Print the max KL info for the first image"
        mkl, mpos = kls[0].view(-1).max(0)  # Largest KL, flat coords
        # Coords in shaped tensor
        print(mpos)
        kidx, xidx, yidx = np.unravel_index(mpos.data.cpu().item(), kls[0].size())
        bds = lambda i: (max(0, i - 3), min(kls.size(-1), i + 3))  # noqa: E731
        ((xmin, xmax), (ymin, ymax)) = map(bds, (xidx, yidx))
        print('max kl at', (kidx, xidx, yidx), 'of',
              f'{mkl.data.cpu()[0]:.3f}',
              'output shape', tuple(kls.shape))
        print('KL around max\n', kls[0, kidx, xmin:xmax, ymin:ymax])

    def size(self, *args):
        return self.mean.size(*args)

    def sample(self) -> ParameterOrVariable:
        """Return a sample from the normal distribution with these parameters.
        Returns:
            Tensor of N(self.mean, self.alpha) samples, same shape.
        This uses the reparametrization trick, so the return value is
        differentiable w.r.t. `self.mean` and `self.alpha`, and can be used as
        such in a backpropagation operation.
        """
        epsilon = TP.FloatTensor(*self.size()).normal_(mean=0, std=1)
        return self.mean + Variable(epsilon) * self.alpha


class InformationDropoutLayer(Module):
    """Information-dropout layer with softplus activations
    Args (to `forward`):
        `inp`: 'Activations to be passed through the layer'
    Returns:
        An `InfoActivations` instance. The `activations` field should be passed
        on to the next layer. The `kl` field should be incorporated into the
        loss, as in Achille & Soatto eq. (6), p. 5.
    Using the softplus activation function instead of ReLU, because the math is
    more solid for that. (No improper prior.)
    Input is passed through a parallel layer of the same type and shape, to
    generate standard deviations for multiplicative log-normal noise applied to
    outputs.
    Prior is independent log-normal for each activation, with learnable mean
    and standard deviation constant over the activations. Initial mean /
    standard deviation are 0, 1, respectively.
    """

    def __init__(self, layer_type: Module, #output_size: Tuple[int],
                 max_alpha=1.0, activate=True, activation_function=T.nn.Softplus(), learnable_prior=1, *args, **kw) -> None:
        """Args:
            `layer_type`: `T.nn.Module`. Probably a convolutional layer.
            `output_size`: Dimensions of the output activations.
            `args`, `kw`: Arguments to initialize `layer_type`
        """
        super(InformationDropoutLayer, self).__init__()
        self.max_alpha = max_alpha
        self.activate = activate
        #self.layer, self.noise = lyrs = [layer_type(*args, **kw) for _ in '..']
        self.layer = layer_type(*args, **kw) #for _ in '..']
        #for l in lyrs:
        kaiming_normal_(self.layer.weight)  # Initialize weights
        self.key=None
        self.activation_function = activation_function

        # Initialize log-space prior with standard normal.
        def pp(f):  # Generate param same shape as output, maybe send to GPU
            return Parameter(f(1).type(TP.FloatTensor))
            #return Parameter(f(kw['out_features']).type(TP.FloatTensor))
        #self.prior = NormalParameters(mean=pp(T.zeros), alpha=T.ones(1))
        if learnable_prior:
            self.prior = NormalParameters(mean=pp(T.zeros), alpha=pp(T.ones))
        else:
            self.prior = NormalParameters(mean=T.zeros(1).type(TP.FloatTensor), alpha=T.ones(1).type(TP.FloatTensor))

    def forward(self, inp: Variable, noise=True) -> list:
        """See class docstring for args."""
        if self.activation_function is not None:
            clean_values = self.activation_function(self.layer(inp)) + 1e-4
        else:
            clean_values = self.layer(inp) + 1e-4
        #print(clean_values)
        if not noise:
            return [clean_values,T.zeros(clean_values.size()[:1]).type(TP.FloatTensor)] #InfoActivations(activations=clean_values, kl=T.zeros(clean_values.size()[:1]))
        raw_noise = self.layer(inp)
        #print('noise stats:', raw_noise.mean().data.item(), #[0],
        #      raw_noise.std().data.item()) #[0])
        # Achille & Soatto use sigmoid to get the standard deviation: see
        # https://github.com/ucla-vision/information-dropout/blob/c80b5/cluttered.py#L90
        # I found that sigmoid caused post_alpha to tend to 1, with vanishing
        # gradients, and have seen more reasonable results with softplus.
        post_alpha = T.sigmoid(raw_noise)
        #if F_ is not None:
        #    eigen_g = F_[1].repeat(post_alpha.shape[0]).view(post_alpha.shape[1],post_alpha.shape[0]) #100 - g
        #    eigen_a = F_[0].repeat(post_alpha.shape[0]).view(post_alpha.shape[0],-1) #785 - a
        #    a = T.mm(eigen_g, post_alpha)
        #    F_max_alpha = T.mm(a, eigen_a)
        post_alpha = post_alpha * self.max_alpha + 1e-3
        #if F_ is not None:
        #    F_ = T.abs(F_)
        #    F_ = F_ / F_.sum(0).expand_as(F_)
        #    F_[T.isnan(F_)] = 0
        #    min_v = T.min(F_)
        #    range_v = T.max(F_) - min_v
        #    if range_v > 0:
        #        normalised = (F_ - min_v) / range_v
        #    else:
        #        normalised = torch.zeros(F_.size())
        #    post_alpha*=(1-normalised)

        #print(post_alpha.shape)
        #print(T.min(post_alpha))
        posterior = NormalParameters(mean=clean_values.log(), alpha=post_alpha)

        # Exploits the fact that KL-divergence is invariant under push-forward
        # (in this case by `.exp()`). So log-normals have same KL as normals.
        kls = self.prior.kl_divergence(posterior)
        return [posterior.sample().exp(),kls] #InfoActivations(
            #activations=posterior.sample().exp(), kl=kls) #, post_alpha = post_alpha)