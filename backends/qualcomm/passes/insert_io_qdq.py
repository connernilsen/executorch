# Copyright (c) Qualcomm Innovation Center, Inc.
# All rights reserved
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.
from typing import Dict

import torch

from executorch.backends.qualcomm.builders.utils import is_parameter
from executorch.exir.dialects._ops import ops as exir_ops
from executorch.exir.pass_base import ExportPass, PassResult


class InsertIOQDQ(ExportPass):
    """
    For delegated QNN subgraph, no more QDQ operators will appear after
    'fold_qdq pass'.
    This pass will insert quantize nodes right after inputs, dequantize nodes
    right before outputs according to stored quantization encodings.
    """

    q_dq_map = {
        # per tensor
        exir_ops.edge.quantized_decomposed.quantize_per_tensor.default: exir_ops.edge.quantized_decomposed.dequantize_per_tensor.tensor,
        exir_ops.edge.quantized_decomposed.quantize_per_tensor.tensor: exir_ops.edge.quantized_decomposed.dequantize_per_tensor.tensor,
        # per channel
        exir_ops.edge.quantized_decomposed.quantize_per_channel.default: exir_ops.edge.quantized_decomposed.dequantize_per_channel.default,
    }

    def __init__(self, edge_program: torch.export.ExportedProgram):
        super(InsertIOQDQ, self).__init__()
        self.edge_program = edge_program

    def _ceate_args(self, target: torch.fx.node.Target, quant_attrs: Dict):
        ret = []

        arg_schemas = list(target._schema.arguments)[1:]
        for arg_schema in arg_schemas:
            name = arg_schema.name
            value = quant_attrs[name]
            if type(arg_schema.type) == torch.tensor and type(value) in [int, float]:
                value = torch.tensor(value)
            ret.append(value)
        return ret

    def _insert_node(
        self,
        graph_module: torch.fx.GraphModule,
        node: torch.fx.node,
        target: torch.fx.node.Target,
        quant_attrs: Dict = None,
    ) -> torch.fx.node:
        # check if there has a specified quant_attrs
        # if not, use the existent info. from current node
        if quant_attrs is None:
            quant_attrs = node.meta.get("quant_attrs")

        inserted_node = graph_module.graph.create_node(
            "call_function",
            target,
            (node, *self._ceate_args(target, quant_attrs)),
        )
        meta_val = node.meta["val"]
        if target in self.q_dq_map:
            inserted_node.meta["quant_attrs"] = node.meta.pop("quant_attrs")
            meta_val = meta_val.to(quant_attrs["dtype"])

        inserted_node.meta["val"] = meta_val
        return inserted_node

    def _insert_quant_node(
        self,
        graph_module: torch.fx.GraphModule,
        node: torch.fx.node,
        target: torch.fx.node.Target,
        quant_attrs: Dict = None,
    ) -> torch.fx.Node:
        with graph_module.graph.inserting_after(node):
            users = list(node.users.keys())
            inserted_node = self._insert_node(graph_module, node, target, quant_attrs)
            for user in users:
                user.replace_input_with(node, inserted_node)

        return inserted_node

    def _insert_dequant_node(
        self,
        graph_module: torch.fx.GraphModule,
        node: torch.fx.node,
        target: torch.fx.node.Target,
    ) -> None:
        with graph_module.graph.inserting_after(node):
            users = list(node.users.keys())
            inserted_node = self._insert_node(graph_module, node, target)
            for user in users:
                if user.op == "output":
                    user.replace_input_with(node, inserted_node)

    def _insert(self, graph_module: torch.fx.GraphModule) -> torch.fx.GraphModule:
        for n in graph_module.graph.nodes:
            # insert q after input
            if (
                n.op == "placeholder"
                and n.meta.get("quant_attrs")
                and not is_parameter(n, self.edge_program)
            ):
                self._insert_quant_node(
                    graph_module, n, n.meta["quant_attrs"]["encoding"]
                )

            # insert dq before output
            users = list(n.users.keys())
            if n.meta.get("quant_attrs") and any(user.op == "output" for user in users):
                self._insert_dequant_node(
                    graph_module, n, self.q_dq_map[n.meta["quant_attrs"]["encoding"]]
                )

    def call(self, graph_module: torch.fx.GraphModule):
        self._insert(graph_module)
        graph_module.recompile()
        return PassResult(graph_module, True)
