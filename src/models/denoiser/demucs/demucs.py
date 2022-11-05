import torch
import torch.nn as nn
import torch.nn.functional as F

import math

from src.models.denoiser.demucs.resample import upsample2, downsample2

################################################################################
# DEMUCS U-Net denoiser architecture
################################################################################


class Demucs(nn.Module):

    def __init__(self,
                 hidden_dim: int = 64,
                 growth: float = 1.0,
                 depth: int = 5,
                 causal: bool = True,
                 resample: int = 4,
                 rescale: float = 0.1,
                 stride_conv: int = 4,
                 kernel_conv: int = 8,
                 stride_glu: int = 1,
                 kernel_glu: int = 1,
                 original: bool = True,
                 use_bias: bool = True,
                 normalize: bool = True
                 ):
        """
        Construct Demucs-like waveform convolutional denoiser architecture.
        Adapted from https://github.com/facebookresearch/denoiser.

        :param growth: "growth" coefficient on the channel size of hidden representations
        :param hidden_dim: base channel size of hidden representations
        :param depth: number of convolutional blocks in the encoder and decoder
        :param causal: if False, use bidirectional LSTM in bottleneck
        :param resample: input resampling factor
        :param rescale: rescaling factor to apply to all convolutional weights
        :param stride_conv: stride of convolutional layers
        :param kernel_conv: kernel size of convolutional layers
        :param stride_glu: stride of channel-expanding pre-GLU convolutional layers
        :param kernel_glu: kernel size of channel-expanding pre-GLU convolutional layers
        :param original: if True, use ReLU activation on initial convolutional layer
        :param use_bias: if True, use bias in all convolutional layers
        :param normalize: if True, normalize input audio
        """

        super().__init__()

        # define forward-pass behaviors
        self.original = original
        self.causal = causal
        self.normalize = normalize
        self.resample = resample

        # store for receptive field & valid length computations
        self.depth = depth
        self.stride_conv = stride_conv
        self.kernel_conv = kernel_conv
        self.stride_glu = stride_glu
        self.kernel_glu = kernel_glu

        assert resample in [1, 2, 4], "Resampling factor must be 1, 2 or 4."

        # construct waveform convolutional encoder and decoder
        encoder_blocks = []
        decoder_blocks = []

        for i in range(depth):
            encoder_blocks.append(
                self._build_encoder_block(
                    level=i,
                    hidden_dim=hidden_dim,
                    growth=growth,
                    stride_conv=stride_conv,
                    kernel_conv=kernel_conv,
                    stride_glu=stride_glu,
                    kernel_glu=kernel_glu,
                    use_relu=original or i,
                    use_bias=use_bias
                )
            )
            decoder_blocks.append(
                self._build_decoder_block(
                    level=depth - i - 1,
                    hidden_dim=hidden_dim,
                    growth=growth,
                    stride_conv=stride_conv,
                    kernel_conv=kernel_conv,
                    stride_glu=stride_glu,
                    kernel_glu=kernel_glu,
                    use_relu=depth - i - 1 > 0,  # omit activation from final decoder layer
                    use_bias=use_bias
                )
            )

        self.encoder = nn.ModuleList(encoder_blocks)
        self.decoder = nn.ModuleList(decoder_blocks)

        # rescale convolutional weights upon initialization
        if rescale:
            self._rescale_conv(rescale)

        # construct recurrent latent bottleneck
        encoder_channels = int(growth * hidden_dim * (2 ** (depth - 1)))
        self.rnn = nn.LSTM(
            input_size=encoder_channels,
            hidden_size=encoder_channels,
            num_layers=2,
            bidirectional=not causal,
            bias=use_bias
        )

        # only apply linear projection for non-causal bidirectional LSTM
        if not causal:
            self.linear = nn.Linear(
                2*encoder_channels,
                encoder_channels,
                bias=use_bias
            )
        else:
            self.linear = nn.Identity()

    def _rescale_conv(self, reference: float):
        """
        Rescale all convolutional and transpose-convolutional weights
        and biases to reference scale.
        """
        for module in self.modules():
            if isinstance(module, (nn.Conv1d, nn.ConvTranspose1d)):

                std = module.weight.std().detach()
                scale = (std / reference)**0.5
                module.weight.data /= scale
                if module.bias is not None:
                    module.bias.data /= scale

    @staticmethod
    def _build_encoder_block(level: int,
                             hidden_dim: int,
                             growth: float,
                             stride_conv: int,
                             kernel_conv: int,
                             stride_glu: int,
                             kernel_glu: int,
                             use_relu: bool,
                             use_bias: bool) -> nn.Module:

        in_channels = 1 if not level else int(hidden_dim * growth * (2**(level - 1)))
        out_channels = int(hidden_dim * growth * (2 ** level))

        conv = nn.Conv1d(
            in_channels,
            out_channels,
            kernel_conv,
            stride=stride_conv,
            bias=use_bias
        )
        relu = nn.ReLU() if use_relu else nn.Identity()
        conv_glu = nn.Conv1d(
            out_channels,
            2*out_channels,
            kernel_glu,
            stride=stride_glu,
            padding=kernel_glu//2,
            bias=use_bias
        )
        glu = nn.GLU(dim=1)

        return nn.Sequential(conv, relu, conv_glu, glu)

    @staticmethod
    def _build_decoder_block(level: int,
                             hidden_dim: int,
                             growth: float,
                             stride_conv: int,
                             kernel_conv: int,
                             stride_glu: int,
                             kernel_glu: int,
                             use_relu: bool,
                             use_bias: bool) -> nn.Module:

        in_channels = int(hidden_dim * growth * (2 ** level))
        out_channels = 1 if not level else int(hidden_dim * growth * (2**(level - 1)))

        deconv_glu = nn.Conv1d(
            in_channels,
            2*in_channels,
            kernel_glu,
            stride=stride_glu,
            padding=kernel_glu//2,
            bias=use_bias
        )
        glu = nn.GLU(dim=1)
        deconv = nn.ConvTranspose1d(
            in_channels,
            out_channels,
            kernel_conv,
            stride=stride_conv,
            bias=use_bias
        )
        relu = nn.ReLU() if use_relu else nn.Identity()

        return nn.Sequential(deconv_glu, glu, deconv, relu)

    @property
    def total_stride(self):
        return (self.stride_conv * self.stride_glu) ** self.depth // self.resample

    def valid_length(self, length):
        """
        Return the nearest valid input length to the model such that there are
        no time steps "left over" in a convolution, i.e. for all layers

          input_length - kernel_length % stride_length = 0

        If the input has a valid length, the corresponding decoded signal
        will have exactly the same length.
        """

        # compute length through input resampling operation
        length = math.ceil(length * self.resample)

        # compute output length through each encoder layer
        for idx in range(self.depth):
            length = math.ceil((length - self.kernel_conv) / self.stride_conv) + 1
            length = max(length, 1)
            length = math.ceil((length - self.kernel_glu) / self.stride_glu) + 1
            length = max(length, 1)

        # compute output length through each decoder layer, assuming constant
        # convolutional kernel
        for idx in range(self.depth):
            length = (length - 1) * self.stride_conv + self.kernel_conv

        # compute length through output downsampling operation
        length = int(math.ceil(length / self.resample))
        return int(length)

    def encode(self, x: torch.Tensor):
        """
        Given waveform input, obtain encoder output, discarding intermediate
        (skip-connection) outputs
        """

        # require batch, channel dimensions
        assert x.ndim >= 2

        if x.ndim == 2:
            x = x.unsqueeze(1)

        # convert to mono audio
        x = x.mean(dim=1, keepdim=True)

        # normalize
        if self.normalize:
            std = x.std(dim=-1, keepdim=True)
            x = x / (1e-3 + std)

        # zero-pad end of signal to ensure input and output have same length
        length = x.shape[-1]
        x = F.pad(x, (0, self.valid_length(length) - length))

        # upsample input waveform
        if self.resample == 2:
            x = upsample2(x)
        elif self.resample == 4:
            x = upsample2(x)
            x = upsample2(x)

        # pass through encoder layers
        for encode in self.encoder:
            x = encode(x)

        return x

    def bottleneck(self, encoded: torch.Tensor):

        encoded = encoded.permute(2, 0, 1)  # time, batch, channels
        self.rnn.flatten_parameters()

        # per-timestep output, plus final hidden and cell states
        out, (hidden_state, cell_state) = self.rnn(encoded)
        out = self.linear(out)
        out = out.permute(1, 2, 0)

        return out

    def forward(self, x: torch.Tensor, *args, **kwargs):

        # require batch, channel dimensions
        assert x.ndim >= 2

        if x.ndim == 2:
            x = x.unsqueeze(1)

        # convert to mono audio
        x = x.mean(dim=1, keepdim=True)

        # normalize and store standard deviation for output scaling
        if self.normalize:
            std = x.std(dim=-1, keepdim=True)
            x = x / (1e-3 + std)
        else:
            std = 1

        # zero-pad end of signal to ensure input and output have same length
        length = x.shape[-1]
        x = F.pad(x, (0, self.valid_length(length) - length))

        # upsample input waveform
        if self.resample == 2:
            x = upsample2(x)
        elif self.resample == 4:
            x = upsample2(x)
            x = upsample2(x)

        # U-Net architecture: store skip connections from encoder outputs
        skips = []
        for encode in self.encoder:
            x = encode(x)
            skips.append(x)

        # pass through recurrent bottleneck
        x = self.bottleneck(x)

        # U-Net architecture: add skip connections to decoder inputs
        for decode in self.decoder:
            skip = skips.pop(-1)
            x = x + skip[..., :x.shape[-1]]
            x = decode(x)

        # downsample output waveform
        if self.resample == 2:
            x = downsample2(x)
        elif self.resample == 4:
            x = downsample2(x)
            x = downsample2(x)

        # trim to original length
        x = x[..., :length]

        # restore original scale
        return std * x