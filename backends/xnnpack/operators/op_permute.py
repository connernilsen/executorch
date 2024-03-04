# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

# pyre-unsafe

from typing import cast, Dict, List

import torch
from executorch.backends.xnnpack.operators.node_visitor import (
    NodeVisitor,
    register_node_visitor,
)
from executorch.backends.xnnpack.serialization.xnnpack_graph_schema import (
    XNNGraph,
    XNNStaticTranspose,
    XNode,
)
from executorch.backends.xnnpack.utils.utils import get_input_node


@register_node_visitor
class PermuteVisitor(NodeVisitor):
    target = "aten.permute_copy.default"

    def __init__(self, *args) -> None:
        super().__init__(*args)

    def define_node(
        self,
        node: torch.fx.Node,
        xnn_graph: XNNGraph,
        vals_to_ids: Dict[torch.fx.Node, int],
        debug_handle: int,
    ) -> None:
        self.define_nodes_tensor_inputs_outputs(node, xnn_graph, vals_to_ids)

        # input
        input_id = vals_to_ids[get_input_node(node, 0)]

        # output
        output_id = vals_to_ids[node]

        # permutation
        permute_order = cast(List[int], node.args[1])

        ser_node = XNode(
            xnode_union=XNNStaticTranspose(
                input_id=input_id,
                num_dims=len(permute_order),
                perm=permute_order,
                output_id=output_id,
                flags=0,
            ),
            debug_handle=debug_handle,
        )
        xnn_graph.xnodes.append(ser_node)
