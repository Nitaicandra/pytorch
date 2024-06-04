import torch
import operator
import copy
import logging
from collections import defaultdict, OrderedDict
from torch.distributed._composable.fsdp import _fsdp_collectives


def _flatten_arg_list(args):
    flat_args = []
    for arg in args:
        if isinstance(arg, (list, tuple)):
            flat_args.extend(_flatten_arg_list(arg))
        else:
            flat_args.append(arg)
    return flat_args


def _collect_primal_inputs_used(node_list):
    primal_inputs_tensor_only = [x for x in list(filter(torch._functorch.partitioners._is_primal, node_list)) if isinstance(x.meta.get('val', None), torch.Tensor)]
    primal_inputs_used = set()
    primal_set_info_dict = {}
    for i, n in enumerate(node_list):
        if n.target == torch.ops.aten.set_.source_Tensor and n.args[0] in primal_inputs_tensor_only:
            primal_input = n.args[0]
            set_node = n
            primal_set_info_dict[primal_input] = set_node
            primal_inputs_used.add(primal_input)
    return primal_inputs_used, primal_set_info_dict


def _collect_view_to_as_strided_users(node_list):
    view_to_as_strided_users = defaultdict(list)
    for i, n in enumerate(node_list):
        if n.target == torch.ops.aten.as_strided.default and n.args[0].target == torch.ops.aten.view.default:
            view_to_as_strided_users[n.args[0]].append(n)
    return view_to_as_strided_users


def _propagate_node_meta(from_node, to_node):
    for k, v in from_node.meta.items():
        to_node.meta[k] = v


def reinplace_primal_set_from_allgather_output(mod):
    """
    auto_functionalized = torch._higher_order_ops.auto_functionalize.auto_functionalized(torch.ops.fsdp.split_with_sizes_copy.default, all_gather_output = view_5, all_gather_input_split_sizes = [131072, 256, 131072, 256], dim = 1, out = [view_1, view_2, view_3, view_4]);  view_5 = view_1 = view_2 = view_3 = view_4 = None
    getitem_3 = auto_functionalized[1];  auto_functionalized = None
    getitem_4: "f32[2, 131072]" = getitem_3[0]
    view_6: "f32[262144]" = torch.ops.aten.view.default(getitem_4, [262144]);  getitem_4 = None
    as_strided_5: "f32[512, 512]" = torch.ops.aten.as_strided.default(view_6, [512, 512], [512, 1], 0)
    (... uses as_strided_5)
    as_strided_8: "f32[512, 512]" = torch.ops.aten.as_strided.default(view_6, [512, 512], [512, 1], 0);  view_6 = None
    (... not using as_strided_8)
    set_: "f32[512, 512]" = torch.ops.aten.set_.source_Tensor(primals_6, as_strided_8);  primals_6 = as_strided_8 = None
    (end of graph)

    ->

    auto_functionalized = torch._higher_order_ops.auto_functionalize.auto_functionalized(torch.ops.fsdp.split_with_sizes_copy.default, all_gather_output = view_5, all_gather_input_split_sizes = [131072, 256, 131072, 256], dim = 1, out = [view_1, view_2, view_3, view_4]);  view_5 = view_1 = view_2 = view_3 = view_4 = None
    getitem_3 = auto_functionalized[1];  auto_functionalized = None
    getitem_4: "f32[2, 131072]" = getitem_3[0]
    view_6: "f32[262144]" = torch.ops.aten.view.default(getitem_4, [262144]);  getitem_4 = None
    as_strided_X: "f32[512, 512]" = torch.ops.aten.as_strided.default(view_6, [512, 512], [512, 1], 0);  view_6 = None
    set_: "f32[512, 512]" = torch.ops.aten.set_.source_Tensor(primals_6, as_strided_X);  primals_6 = as_strided_8 = None
    (... uses primals_6 instead of as_strided_5)
    ...
    (end of graph)
    """

    node_list = list(mod.graph.nodes)
    primal_inputs_used, primal_set_info_dict = _collect_primal_inputs_used(node_list)
    view_to_as_strided_users = _collect_view_to_as_strided_users(node_list)

    as_strided_to_primal = {}
    for set_node in primal_set_info_dict.values():
        if set_node.args[1].target == torch.ops.aten.as_strided.default:
            as_strided_to_primal[set_node.args[1]] = set_node.args[0]

    for as_strided_node in as_strided_to_primal:
        assert as_strided_node.args[0].target == torch.ops.aten.view.default
        primal_node = as_strided_to_primal[as_strided_node]
        view_node = as_strided_node.args[0]
        set_node = primal_set_info_dict[primal_node]
        for other_as_strided_node in view_to_as_strided_users[view_node]:
            if other_as_strided_node != as_strided_node:
                assert other_as_strided_node.args == as_strided_node.args
        with mod.graph.inserting_after(view_node):
            new_as_strided_node = mod.graph.call_function(as_strided_node.target, as_strided_node.args, as_strided_node.kwargs)
            _propagate_node_meta(as_strided_node, new_as_strided_node)
        with mod.graph.inserting_after(new_as_strided_node):
            new_set_node = mod.graph.call_function(set_node.target, (primal_node, new_as_strided_node), set_node.kwargs)
            _propagate_node_meta(set_node, new_set_node)
        mod.graph.erase_node(set_node)
        mod.graph.erase_node(as_strided_node)
        for other_as_strided_node in view_to_as_strided_users[view_node]:
            if other_as_strided_node != as_strided_node:
                other_as_strided_node.replace_all_uses_with(primal_node)
                mod.graph.erase_node(other_as_strided_node)
    mod.graph.lint()
    mod.recompile()


def move_primal_set_to_end_of_fwd_graph(mod):
    """
    auto_functionalized = torch._higher_order_ops.auto_functionalize.auto_functionalized(torch.ops.fsdp.split_with_sizes_copy.default, all_gather_output = view_5, all_gather_input_split_sizes = [131072, 256, 131072, 256], dim = 1, out = [view_1, view_2, view_3, view_4]);  view_5 = view_1 = view_2 = view_3 = view_4 = None
    getitem_3 = auto_functionalized[1];  auto_functionalized = None
    getitem_6: "f32[2, 131072]" = getitem_3[2]

    # File: /data/users/willfeng/pytorch/torch/distributed/_composable/fsdp/_fsdp_collectives.py:193 in foreach_all_gather_copy_out, code: torch.ops.fsdp.split_with_sizes_copy(
    view_10: "f32[262144]" = torch.ops.aten.view.default(getitem_6, [262144]);  getitem_6 = None

    as_strided_default_2 = torch.ops.aten.as_strided.default(view_10, [512, 512], [512, 1], 0);  view_10 = None
    set__source_tensor_2 = torch.ops.aten.set_.source_Tensor(primals_8, as_strided_default_2);  as_strided_default_2 = None
    ... (uses primals_8)
    return [..., primals_8, ...]

    ->

    auto_functionalized = torch._higher_order_ops.auto_functionalize.auto_functionalized(torch.ops.fsdp.split_with_sizes_copy.default, all_gather_output = view_5, all_gather_input_split_sizes = [131072, 256, 131072, 256], dim = 1, out = [view_1, view_2, view_3, view_4]);  view_5 = view_1 = view_2 = view_3 = view_4 = None
    getitem_3 = auto_functionalized[1];  auto_functionalized = None
    getitem_6: "f32[2, 131072]" = getitem_3[2]

    # File: /data/users/willfeng/pytorch/torch/distributed/_composable/fsdp/_fsdp_collectives.py:193 in foreach_all_gather_copy_out, code: torch.ops.fsdp.split_with_sizes_copy(
    view_10: "f32[262144]" = torch.ops.aten.view.default(getitem_6, [262144]);  getitem_6 = None

    as_strided_default_2 = torch.ops.aten.as_strided.default(view_10, [512, 512], [512, 1], 0);  view_10 = None
    ... (uses as_strided_default_2 instead of primals_8)
    set__source_tensor_2 = torch.ops.aten.set_.source_Tensor(primals_8, as_strided_default_2);  as_strided_default_2 = None
    return [..., primals_8, ...]

    """
    node_list = list(mod.graph.nodes)
    node_to_idx = {n: i for i, n in enumerate(node_list)}
    return_op = None
    for node in node_list:
        if node.target == "output":
            return_op = node
            break
    _, primal_set_info_dict = _collect_primal_inputs_used(node_list)
    for primal_node in primal_set_info_dict:
        set_node = primal_set_info_dict[primal_node]
        as_strided_node = set_node.args[1]
        as_strided_node_idx = node_to_idx[as_strided_node]
        mod.graph.erase_node(set_node)
        # Replace primals_X node usage with as_strided_Y node for all nodes between as_strided_Y node and return op.
        primal_node.replace_all_uses_with(
            as_strided_node,
            propagate_meta=False,
            delete_user_cb=lambda node: node_to_idx[node] > as_strided_node_idx and node_to_idx[node] < node_to_idx[return_op]
        )
        with mod.graph.inserting_before(return_op):
            new_set_node = mod.graph.call_function(set_node.target, (primal_node, as_strided_node), set_node.kwargs)
        _propagate_node_meta(set_node, new_set_node)
    mod.graph.lint()
    mod.recompile()


def replace_noop_consecutive_permutes_with_original_input_if_first_permute_out_has_no_other_use(mod):
    """
    # NOTE: we only handle len(permute_dims) = 2 case for now.

    permute_3: "f32[12340, 12340]" = torch.ops.aten.permute.default(getitem_106, [1, 0])
    permute_4: "f32[12340, 12340]" = torch.ops.aten.permute.default(permute_3, [1, 0]);  permute_3 = None

    ->

    getitem_106
    """
    node_list = list(mod.graph.nodes)
    for i, n in enumerate(node_list):
        if n.target is torch.ops.aten.permute.default and len(n.args[1]) == 2:
            permute_dims = n.args[1]
            first_permute_node = n
            second_permute_node = None
            first_permute_output_has_other_use = False
            # First check that the first permute output has no other use
            for j, node in enumerate(node_list[i+1:]):
                if first_permute_node in _flatten_arg_list(node.args):
                    if node.target is not torch.ops.aten.permute.default:
                        first_permute_output_has_other_use = True
                    else:
                        if node.args[1] == permute_dims:
                            # if permute_dims also match, we know these two consecutive permutes lead to a no-op.
                            second_permute_node = node
                        else:
                            first_permute_output_has_other_use = True
            if second_permute_node is not None and not first_permute_output_has_other_use:
                second_permute_node.replace_all_uses_with(first_permute_node.args[0])
                mod.graph.erase_node(second_permute_node)
                mod.graph.erase_node(first_permute_node)
    mod.graph.lint()
    mod.recompile()


def replace_noop_consecutive_transpose_with_original_input_if_first_transpose_out_has_no_other_use(mod):
    """
    t_13: "f32[512, 1024]" = torch.ops.aten.t.default(view_73);  view_73 = None
    t_14: "f32[1024, 512]" = torch.ops.aten.t.default(t_13);  t_13 = None

    ->

    view_73
    """
    node_list = list(mod.graph.nodes)
    for i, n in enumerate(node_list):
        if n.target is torch.ops.aten.t.default:
            first_transpose_node = n
            second_transpose_node = None
            first_transpose_output_has_other_use = False
            # First check that the first transpose output has no other use
            for j, node in enumerate(node_list[i+1:]):
                if first_transpose_node in _flatten_arg_list(node.args):
                    if node.target is not torch.ops.aten.t.default:
                        first_transpose_output_has_other_use = True
                    else:
                        if node.args[0] == first_transpose_node:
                            second_transpose_node = node
                        else:
                            first_transpose_output_has_other_use = True
            if second_transpose_node is not None and not first_transpose_output_has_other_use:
                second_transpose_node.replace_all_uses_with(first_transpose_node.args[0])
                mod.graph.erase_node(second_transpose_node)
                mod.graph.erase_node(first_transpose_node)
    mod.graph.lint()
    mod.recompile()


def remove_unnecessary_views(mod):
    """
    X: "f32[512, 1024]" = ...
    view_1: "f32[512, 1024]" = torch.ops.aten.view.default(X, [512, 1024])
    (... uses view_1)

    ->

    (... uses X)
    """
    node_list = list(mod.graph.nodes)
    for i, n in enumerate(node_list):
        if n.target is torch.ops.aten.view.default:
            view_node = n
            view_input = view_node.args[0]
            if list(view_input.meta.get("tensor_meta").shape) == view_node.args[1]:
                view_node.replace_all_uses_with(view_input)
                mod.graph.erase_node(view_node)
    mod.graph.lint()
    mod.recompile()
