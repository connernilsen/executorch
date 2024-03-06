# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

# pyre-unsafe

import unittest

import torch
import torchvision.models as models
from executorch.backends.xnnpack.test.tester import Tester
from executorch.backends.xnnpack.test.tester.tester import Quantize
from torchvision.models.mobilenetv2 import MobileNet_V2_Weights


class TestMobileNetV2(unittest.TestCase):
    mv2 = models.mobilenetv2.mobilenet_v2(weights=MobileNet_V2_Weights)
    mv2 = mv2.eval()
    model_inputs = (torch.ones(1, 3, 224, 224),)

    all_operators = {
        "executorch_exir_dialects_edge__ops_aten__native_batch_norm_legit_no_training_default",
        "executorch_exir_dialects_edge__ops_aten_add_Tensor",
        "executorch_exir_dialects_edge__ops_aten_permute_copy_default",
        "executorch_exir_dialects_edge__ops_aten_addmm_default",
        "executorch_exir_dialects_edge__ops_aten_mean_dim",
        "executorch_exir_dialects_edge__ops_aten_hardtanh_default",
        "executorch_exir_dialects_edge__ops_aten_convolution_default",
    }

    def test_fp32_mv2(self):

        (
            Tester(self.mv2, self.model_inputs)
            .export()
            .to_edge()
            .check(list(self.all_operators))
            .partition()
            .check(["torch.ops.higher_order.executorch_call_delegate"])
            .check_not(list(self.all_operators))
            .to_executorch()
            .serialize()
            .run_method()
            .compare_outputs()
        )

    def test_qs8_mv2(self):
        # Quantization fuses away batchnorm, so it is no longer in the graph
        ops_after_quantization = self.all_operators - {
            "executorch_exir_dialects_edge__ops_aten__native_batch_norm_legit_no_training_default",
        }

        (
            Tester(self.mv2, self.model_inputs)
            .quantize(Quantize(calibrate=False))
            .export()
            .to_edge()
            .check(list(ops_after_quantization))
            .partition()
            .check(["torch.ops.higher_order.executorch_call_delegate"])
            .check_not(list(ops_after_quantization))
            .to_executorch()
            .serialize()
            .run_method()
            .compare_outputs()
        )
