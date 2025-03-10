# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

from typing import Optional

import executorch.backends.vulkan.serialization.vulkan_graph_schema as vk_graph_schema

import torch

from executorch.exir.tensor import TensorSpec
from torch._export.utils import get_buffer, get_param, is_buffer, is_param
from torch.export import ExportedProgram
from torch.fx import Node


class VkGraphBuilder:
    def __init__(self, program: ExportedProgram) -> None:
        self.program = program

        self.chain = []
        self.values = []
        self.input_ids = []
        self.output_ids = []
        self.const_tensors = []

        # Mapping from torch.fx.Node to VkValue id
        self.node_to_value_ids = {}

    @staticmethod
    def get_vk_datatype(torch_dtype: torch.dtype) -> vk_graph_schema.VkDataType:
        if torch_dtype == torch.float32:
            return vk_graph_schema.VkDataType.fp32
        else:
            raise AssertionError(f"Invalid dtype for vulkan_preprocess ({torch_dtype})")

    def is_constant(self, node: torch.fx.Node):
        return (
            node.name in self.program.graph_signature.inputs_to_lifted_tensor_constants
        )

    def is_get_attr_node(self, node: torch.fx.Node) -> bool:
        """
        Returns true if the given node is a get attr node for a tensor of the model
        """
        return isinstance(node, torch.fx.Node) and node.op == "get_attr"

    def is_param_node(self, node: torch.fx.Node) -> bool:
        """
        Check if the given node is a parameter within the exported program
        """
        return (
            self.is_get_attr_node(node)
            or is_param(self.program, node)
            or is_buffer(self.program, node)
            or self.is_constant(node)
        )

    def get_constant(self, node: torch.fx.Node) -> Optional[torch.Tensor]:
        """
        Returns the constant associated with the given node in the exported program.
        Returns None if the node is not a constant within the exported program
        """
        if self.is_constant(node):
            constant_name = (
                self.program.graph_signature.inputs_to_lifted_tensor_constants[
                    node.name
                ]
            )
            if constant_name in self.program.constants:
                return self.program.constants[constant_name]
            else:
                return None

        return None

    def get_param_tensor(self, node: torch.fx.Node) -> torch.Tensor:
        tensor = None
        if node is None:
            raise RuntimeError("node is None")
        elif is_param(self.program, node):
            tensor = get_param(self.program, node)
        elif is_buffer(self.program, node):
            tensor = get_buffer(self.program, node)
        elif self.is_constant(node):
            tensor = self.get_constant(node)
        elif self.is_get_attr_node(node):
            # This is a hack to support both lifted and unlifted graph
            try:
                tensor = getattr(node.graph.owning_module, node.target)
            except AttributeError:
                tensor = getattr(self.program.graph_module, node.target)
        else:
            raise RuntimeError(f"unsupported param type, {node.op}.")

        assert tensor is not None
        return tensor

    def maybe_add_constant_tensor(self, node: Node) -> int:
        const_buffer_idx = -1
        if self.is_param_node(node):
            const_buffer_idx = len(self.const_tensors)
            self.const_tensors.append(self.get_param_tensor(node))

        return const_buffer_idx

    def create_single_vk_value(self, node: Node) -> int:
        constant_id = self.maybe_add_constant_tensor(node)

        spec = node.meta.get("spec")
        assert isinstance(spec, TensorSpec)
        new_id = len(self.values)
        if node not in self.node_to_value_ids:
            self.node_to_value_ids[node] = new_id
        else:
            current_ids = self.node_to_value_ids[node]
            if isinstance(current_ids, int):
                current_ids = [current_ids, new_id]
            else:
                current_ids.append(new_id)

        # Negative id indicates that this tensor will have its own dedicated memory.
        mem_obj_id = -1
        if spec.mem_obj_id is not None:
            mem_obj_id = spec.mem_obj_id

        self.values.append(
            vk_graph_schema.VkValue(
                value=vk_graph_schema.VkTensor(
                    datatype=self.get_vk_datatype(spec.dtype),
                    dims=spec.shape,
                    constant_id=constant_id,
                    mem_obj_id=mem_obj_id,
                )
            )
        )
        return new_id

    def create_vk_values_for(self, node: Node):
        spec = node.meta.get("spec")
        if isinstance(spec, TensorSpec):
            return self.create_single_vk_value(node)
        else:
            raise RuntimeError(
                "Creating values for nodes with collection types is not supported yet."
            )

    def process_placeholder_node(self, node: Node) -> None:
        ids = self.create_vk_values_for(node)
        if not self.is_param_node(node):
            if isinstance(ids, int):
                self.input_ids.append(ids)
            else:
                self.input_ids += ids

    def process_call_function_node(self, node) -> None:
        args = []
        # Add input nodes
        for inp_node in node.all_input_nodes:
            if inp_node not in self.node_to_value_ids:
                raise AssertionError(
                    "Cannot find input to current node in node_to_value_ids. This means "
                    "this node is being serialized before its input which is not allowed."
                )
            args.append(self.node_to_value_ids[inp_node])
        # Add output node
        args.append(self.create_vk_values_for(node))

        self.chain.append(
            vk_graph_schema.OperatorCall(
                name=node.target.__name__,
                args=args,
            ),
        )

    def process_getattr_node(self, node: Node) -> None:
        self.create_vk_values_for(node)

    def process_output_node(self, node: Node) -> None:
        if node.all_input_nodes[0] not in self.node_to_value_ids:
            raise AssertionError(
                "Cannot find input to output node in node_to_value_ids. This means the "
                "output node is being serialized before its corresponding internal node "
                "which is not allowed."
            )
        self.output_ids.append(self.node_to_value_ids[node.all_input_nodes[0]])

    def process_node(self, node: Node) -> None:
        if node.op == "placeholder":
            self.process_placeholder_node(node)
        elif node.op == "call_function":
            self.process_call_function_node(node)
        elif node.op == "get_attr":
            self.process_getattr_node(node)
        elif node.op == "output":
            self.process_output_node(node)
        else:
            raise AssertionError(f"Unsupported node op: {node.op}")

    def build_graph(self) -> vk_graph_schema.VkGraph:
        for node in self.program.graph_module.graph.nodes:
            self.process_node(node)

        return vk_graph_schema.VkGraph(
            version="0",
            chain=self.chain,
            values=self.values,
            input_ids=self.input_ids,
            output_ids=self.output_ids,
            constants=[],
            shaders=[],
        )
