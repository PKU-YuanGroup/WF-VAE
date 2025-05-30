from typing import Union, Tuple
import torch
import torch.nn as nn
import torch.nn.functional as F
from .ops import cast_tuple, video_to_image
from .conv import CausalConv3d, CausalConv3d_GC, Conv2d
from einops import rearrange
from collections import deque


class Upsample(nn.Module):
    def __init__(self, in_channels, out_channels, **kwargs):
        super().__init__()
        self.with_conv = True
        if self.with_conv:
            self.conv = torch.nn.Conv2d(
                in_channels, out_channels, kernel_size=3, stride=1, padding=1
            )

    @video_to_image
    def forward(self, x):
        x = torch.nn.functional.interpolate(x, scale_factor=2.0, mode="nearest")
        if self.with_conv:
            x = self.conv(x)
        return x


class Downsample(nn.Module):
    def __init__(self, in_channels, out_channels, **kwargs):
        super().__init__()
        self.with_conv = True
        if self.with_conv:
            self.conv = torch.nn.Conv2d(
                in_channels, out_channels, kernel_size=3, stride=2, padding=0
            )

    @video_to_image
    def forward(self, x):
        if self.with_conv:
            pad = (0, 1, 0, 1)
            x = torch.nn.functional.pad(x, pad, mode="constant", value=0)
            x = self.conv(x)
        else:
            x = torch.nn.functional.avg_pool2d(x, kernel_size=2, stride=2)
        return x


class SpatialDownsample2x(nn.Module):
    def __init__(
        self,
        chan_in,
        chan_out,
        kernel_size: Union[int, Tuple[int]] = (3, 3),
        stride: Union[int, Tuple[int]] = (2, 2),
        **kwargs
    ):
        super().__init__()
        kernel_size = cast_tuple(kernel_size, 2)
        stride = cast_tuple(stride, 2)
        self.chan_in = chan_in
        self.chan_out = chan_out
        self.kernel_size = kernel_size
        self.conv = CausalConv3d(
            self.chan_in,
            self.chan_out,
            (1,) + self.kernel_size,
            stride=(1,) + stride,
            padding=0,
        )

    def forward(self, x):
        pad = (0, 1, 0, 1, 0, 0)
        x = torch.nn.functional.pad(x, pad, mode="constant", value=0)
        x = self.conv(x)
        return x


class SpatialUpsample2x(nn.Module):
    def __init__(
        self,
        chan_in,
        chan_out,
        kernel_size: Union[int, Tuple[int]] = (3, 3),
        stride: Union[int, Tuple[int]] = (1, 1),
        unup=False,
    ):
        super().__init__()
        self.chan_in = chan_in
        self.chan_out = chan_out
        self.kernel_size = kernel_size
        self.unup = unup
        self.conv = CausalConv3d(
            self.chan_in,
            self.chan_out,
            (1,) + self.kernel_size,
            stride=(1,) + stride,
            padding=1,
        )

    def forward(self, x):
        if not self.unup:
            t = x.shape[2]
            x = rearrange(x, "b c t h w -> b (c t) h w")
            x = F.interpolate(x, scale_factor=(2, 2), mode="nearest")
            x = rearrange(x, "b (c t) h w -> b c t h w", t=t)
        x = self.conv(x)
        return x


class TimeDownsample2x(nn.Module):
    def __init__(self, chan_in, chan_out, kernel_size: int = 3):
        super().__init__()
        self.kernel_size = kernel_size
        self.conv = nn.AvgPool3d((kernel_size, 1, 1), stride=(2, 1, 1))

    def forward(self, x):
        first_frame_pad = x[:, :, :1, :, :].repeat((1, 1, self.kernel_size - 1, 1, 1))
        x = torch.concatenate((first_frame_pad, x), dim=2)
        return self.conv(x)


class TimeUpsample2x(nn.Module):
    def __init__(self, chan_in, chan_out):
        super().__init__()

    def forward(self, x):
        if x.size(2) > 1:
            x, x_ = x[:, :, :1], x[:, :, 1:]
            x_ = F.interpolate(x_, scale_factor=(2, 1, 1), mode="trilinear")
            x = torch.concat([x, x_], dim=2)
        return x


class TimeDownsampleRes2x(nn.Module):
    def __init__(
        self,
        in_channels,
        out_channels,
        kernel_size: int = 3,
        mix_factor: float = 2.0,
    ):
        super().__init__()
        self.kernel_size = cast_tuple(kernel_size, 3)
        self.avg_pool = nn.AvgPool3d((kernel_size, 1, 1), stride=(2, 1, 1))
        self.conv = nn.Conv3d(
            in_channels,
            out_channels,
            self.kernel_size,
            stride=(2, 1, 1),
            padding=(0, 1, 1),
        )
        self.mix_factor = torch.nn.Parameter(torch.Tensor([mix_factor]))

    def forward(self, x):
        alpha = torch.sigmoid(self.mix_factor)
        first_frame_pad = x[:, :, :1, :, :].repeat(
            (1, 1, self.kernel_size[0] - 1, 1, 1)
        )
        x = torch.concatenate((first_frame_pad, x), dim=2)
        return alpha * self.avg_pool(x) + (1 - alpha) * self.conv(x)


class TimeUpsampleRes2x(nn.Module):
    def __init__(
        self,
        in_channels,
        out_channels,
        kernel_size: int = 3,
        mix_factor: float = 2.0,
    ):
        super().__init__()
        self.conv = CausalConv3d(in_channels, out_channels, kernel_size, padding=1)
        self.mix_factor = torch.nn.Parameter(torch.Tensor([mix_factor]))

    def forward(self, x):
        alpha = torch.sigmoid(self.mix_factor)
        if x.size(2) > 1:
            x, x_ = x[:, :, :1], x[:, :, 1:]
            x_ = F.interpolate(x_, scale_factor=(2, 1, 1), mode="trilinear")
            x = torch.concat([x, x_], dim=2)
        return alpha * x + (1 - alpha) * self.conv(x)


class Spatial2xTime2x3DDownsample(nn.Module):
    def __init__(self, in_channels, out_channels):
        super().__init__()
        self.conv = CausalConv3d(
            in_channels, out_channels, kernel_size=3, padding=0, stride=2
        )

    def forward(self, x):
        pad = (0, 1, 0, 1, 0, 0)
        x = torch.nn.functional.pad(x, pad, mode="constant", value=0)
        x = self.conv(x)
        return x


class Spatial2x3DDownsample(nn.Module):
    def __init__(self, in_channels, out_channels):
        super().__init__()
        self.conv = CausalConv3d(
            in_channels, out_channels, kernel_size=3, padding=0, stride=(1, 2, 2)
        )

    def forward(self, x):
        pad = (0, 1, 0, 1, 0, 0)
        x = torch.nn.functional.pad(x, pad, mode="constant", value=0)
        x = self.conv(x)
        return x


class Spatial2x3DUpsample(nn.Module):
    def __init__(self, in_channels, out_channels):
        super().__init__()
        self.conv = CausalConv3d(in_channels, out_channels, kernel_size=3, padding=1)

    def forward(self, x):
        x = F.interpolate(x, scale_factor=(1, 2, 2), mode="trilinear")
        return self.conv(x)


class Spatial2xTime2x3DUpsample(nn.Module):
    def __init__(
        self,
        in_channels,
        out_channels,
        t_interpolation="trilinear",
        enable_cached=False,
        depth=0,
    ):
        super().__init__()
        self.t_interpolation = t_interpolation
        self.conv = CausalConv3d(in_channels, out_channels, kernel_size=3, padding=1)
        self.enable_cached = enable_cached
        self.causal_cached = deque()
        self.depth = depth
        
    def forward(self, x):
        if x.size(2) == 1:
            x = F.interpolate(x, scale_factor=(1, 2, 2), mode="trilinear")
            
        elif self.enable_cached:
            drop_cached = False
            if len(self.causal_cached) > 0:
                cached = self.causal_cached.popleft()
                x = torch.cat([cached, x], dim=2)
                drop_cached = True
            self.causal_cached.append(
                x[:, :, -(2**self.depth) - 1 : -(2**self.depth)].clone()
            )
            x = F.interpolate(x, scale_factor=(2, 1, 1), mode=self.t_interpolation)
            if drop_cached:
                x = x[:, :, 2:]
            x = F.interpolate(x, scale_factor=(1, 2, 2), mode="trilinear")
        else:
            x, x_ = x[:, :, :1], x[:, :, 1:]
            x_ = F.interpolate(
                x_, scale_factor=(2, 1, 1), mode=self.t_interpolation
            )
            x_ = F.interpolate(x_, scale_factor=(1, 2, 2), mode="trilinear")
            x = F.interpolate(x, scale_factor=(1, 2, 2), mode="trilinear")
            x = torch.concat([x, x_], dim=2)
            
        return self.conv(x)
