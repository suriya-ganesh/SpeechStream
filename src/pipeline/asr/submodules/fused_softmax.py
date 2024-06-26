import torch

from pipeline.asr.submodules.megatron_utils import ApexGuardDefaults

try:
    from apex.transformer.functional.fused_softmax import FusedScaleMaskSoftmax

    HAVE_APEX = True

except (ImportError, ModuleNotFoundError):
    HAVE_APEX = False

if HAVE_APEX:

    class MatchedScaleMaskSoftmax(FusedScaleMaskSoftmax):
        """
        fused operation: scaling + mask + softmax
        match the behavior of fused softmax and torch softmax.
        This is a workaround for https://github.com/NVIDIA/apex/issues/1493.

        Arguments:
            input_in_fp16: flag to indicate if input in fp16 data format.
            input_in_bf16: flag to indicate if input in bf16 data format.
            attn_mask_type: attention mask type (pad or causal)
            scaled_masked_softmax_fusion: flag to indicate user want to use softmax fusion
            mask_func: mask function to be applied.
            softmax_in_fp32: if true, softmax in performed at fp32 precision.
            scale: scaling factor used in input tensor scaling.
        """

        def forward_torch_softmax(self, input, mask):
            if self.input_in_float16 and self.softmax_in_fp32:
                input = input.float()

            if self.scale is not None:
                input = input * self.scale
            mask_output = self.mask_func(input, mask) if mask is not None else input
            probs = torch.nn.Softmax(dim=-1)(mask_output)
            if mask is not None:
                all_k_masked = mask.all(axis=-1)
                zero_attention_mask = (1.0 - all_k_masked.type(probs.type()))[:, :, :, None]
                probs = probs * zero_attention_mask

            if self.input_in_float16 and self.softmax_in_fp32:
                if self.input_in_fp16:
                    probs = probs.half()
                else:
                    probs = probs.bfloat16()
            return probs


else:

    class MatchedScaleMaskSoftmax(ApexGuardDefaults):
        def __init__(self):
            super().__init__()
            print(
                "Apex was not found. ColumnLinear will not work. Please see the NeMo README for installation instructions: https://github.com/NVIDIA/NeMo#megatron-gpt."
            )
