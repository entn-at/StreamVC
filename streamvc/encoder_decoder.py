from functools import partial
import torch
import torch.nn as nn
import torch.nn.functional as F
from einops.layers.torch import Rearrange


class Encoder(nn.Module):
    def __init__(self, scale: int, embedding_dim: int):
        super().__init__()
        self.encoder = nn.Sequential(
            Rearrange('... samples -> ... 1 samples'),
            CausalConv1d(in_channels=1, out_channels=scale, kernel_size=7),
            nn.ELU(),
            EncoderBlock(scale, 2*scale, stride=2),
            EncoderBlock(2*scale, 4*scale, stride=4),
            EncoderBlock(4*scale, 8*scale, stride=5),
            EncoderBlock(8*scale, 16*scale, stride=8),
            CausalConv1d(16*scale, embedding_dim, kernel_size=3),
            nn.ELU(),
            Rearrange('... embedding frames -> ... frames embedding')
        )

    def forward(self, x: torch.Tensor):
        return self.encoder(x)


class Decoder(nn.Module):
    def __init__(self, scale: int, embedding_dim: int, conditioning_dim: int):
        super().__init__()
        self.decoder = SequentialWithFiLM(
            Rearrange('... frames embedding -> ... embedding frames'),
            CausalConv1d(embedding_dim, 16*scale, kernel_size=7),
            nn.ELU(),
            DecoderBlock(16*scale, 8*scale, stride=8),
            FiLM(8*scale, conditioning_dim),
            DecoderBlock(8*scale, 4*scale, stride=5),
            FiLM(4*scale, conditioning_dim),
            DecoderBlock(4*scale, 2*scale, stride=4),
            FiLM(2*scale, conditioning_dim),
            DecoderBlock(2*scale, scale, stride=2),
            FiLM(scale, conditioning_dim),
            CausalConv1d(scale, 1, kernel_size=7),
            Rearrange('... 1 samples -> ... samples')
        )

    def forward(self, x: torch.Tensor, condition: torch.Tensor):
        return self.decoder(x, condition)


class LearnablePooling(nn.Module):
    def __init__(self, dim: int):
        super().__init__()
        self.pooling_weights = nn.Parameter(torch.ones(1, dim))
        self.attention = nn.MultiheadAttention(
            dim, num_heads=1, bias=False, batch_first=True)

    def forward(self, x: torch.Tensor):
        return self.attention(self.pooling_weights, x, x).squeeze(-2)


class SequentialWithFiLM(nn.Sequential):
    def forward(self, input, condition):
        for module in self:
            if isinstance(module, FiLM):
                input = module(input, condition)
            else:
                input = module(input)
        return input


def EncoderBlock(in_channels: int, out_channels: int, stride: int) -> nn.Module:
    return nn.Sequential(
        ResidualUnit(in_channels, dilation=1),
        ResidualUnit(in_channels, dilation=3),
        ResidualUnit(in_channels, dilation=9),
        CausalConv1d(in_channels, out_channels,
                     kernel_size=2*stride, stride=stride),
        nn.ELU()
    )


def DecoderBlock(in_channels: int, out_channels: int, stride: int) -> nn.Module:
    return nn.Sequential(
        CausalConvTranspose1d(in_channels, out_channels,
                              kernel_size=2*stride, stride=stride),
        ResidualUnit(out_channels, dilation=1),
        ResidualUnit(out_channels, dilation=3),
        ResidualUnit(out_channels, dilation=9),
    )


class Residual(nn.Module):
    def __init__(self, fn: nn.Module):
        super().__init__()
        self.fn = fn

    def forward(self, x: torch.Tensor, *args, **kwargs):
        return self.fn(x, *args, **kwargs) + x


def ResidualUnit(channels: int, dilation: int, kernel_size: int = 7, **kwargs):
    return Residual(
        nn.Sequential(
            CausalConv1d(channels, channels, kernel_size,
                         dilation=dilation, **kwargs),
            nn.ELU(),
            CausalConv1d(channels, channels, kernel_size=1, **kwargs),
            nn.ELU()
        )
    )


class CausalConv1d(nn.Conv1d):
    def __init__(self, in_channels: int, out_channels: int, kernel_size: int,
                 stride: int = 1, dilation: int = 1,
                 padding_mode: str = 'zeros', **kwargs):

        assert 'padding' not in kwargs

        super().__init__(
            in_channels=in_channels,
            out_channels=out_channels,
            kernel_size=kernel_size,
            stride=stride,
            padding=0,
            dilation=dilation,
            **kwargs
        )

        causal_padding = dilation * (kernel_size - 1) - (stride - 1)

        if padding_mode == 'zeros':
            self.pad = partial(F.pad, pad=(causal_padding, 0),
                               mode='constant', value=0)
        else:
            self.pad = partial(F.pad, pad=(causal_padding, 0),
                               mode=padding_mode)

    def forward(self, x):
        return super().forward(self.pad(x))


class CausalConvTranspose1d(nn.ConvTranspose1d):
    def __init__(self, in_channels: int, out_channels: int, kernel_size: int,
                 stride: int = 1, dilation=1, **kwargs):

        assert 'padding' not in kwargs
        assert 'output_padding' not in kwargs

        super().__init__(
            in_channels=in_channels,
            out_channels=out_channels,
            kernel_size=kernel_size,
            stride=stride,
            padding=0,
            output_padding=0,
            dilation=dilation,
            **kwargs
        )

        self.causal_trim = dilation * (kernel_size - 1) - (stride - 1)

    def forward(self, x: torch.Tensor):
        out = super().forward(x)
        # we trim the output on the right side
        # see https://github.com/lucidrains/audiolm-pytorch/issues/8
        return out[..., :-self.causal_trim]


class FiLM(nn.Module):
    def __init__(self, dim: int, conditioning_dim: int):
        super().__init__()
        self.to_gamma = nn.Linear(conditioning_dim, dim)
        self.to_beta = nn.Linear(conditioning_dim, dim)

    def forward(self, x: torch.Tensor, condition: torch.Tensor):
        gamma = self.to_gamma(condition).unsqueeze(dim=-1)
        beta = self.to_beta(condition).unsqueeze(dim=-1)
        return x * gamma + beta
