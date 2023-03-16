from __future__ import annotations

import functools

from typing import Callable, Union

import onnx

import torch
import torch._C
import torch._decomp
import torch._dynamo
import torch._ops
import torch.fx

from torch.onnx import _constants

from torch.onnx._internal import _beartype
from torch.onnx._internal.fx import frontend, function_dispatcher, options, passes
from torch.utils import _pytree

# TODO: Separate into individual components.
# TODO: make_fx lose stack info https://github.com/pytorch/pytorch/issues/90276


@_beartype.beartype
def _export(
    module: torch.fx.GraphModule,
    args,
    **kwargs,
) -> Union["onnx.ModelProto", bytes]:
    export_options = options.ExportOptions()
    export_options.update(**kwargs)
    # Apply decomposition table to the input graph.
    # Make sure the feed-in "module" is stateless.
    # Ensure placeholder targets match the original module's signature since
    # We don't want to map forward(x, y, z) to forward(arg0, arg1, arg2).
    decomposed_module = passes.Decompose(
        module, export_options.decomposition_table
    ).run(*args)
    # Run FakeTensorProp on decomposed_module.
    # Symbolic output of the i-th node can be accessed via
    # decomposed_module.graph.nodes[i].meta["val"]
    decomposed_module = passes.ShapeInferenceWithFakeTensor(decomposed_module).run(
        *args
    )

    # We want to pass list of ints and floats to TorchScript graph correctly
    # in _export_fx_to_ts, so we must disable FakeTensorMode. Otherwise, graph may
    # receive FakeTensor and results runtime error. In addition, TorchScript-based
    # ONNX exporter used in _ts_graph_to_onnx_model_in_protobuf is not compatible
    # with FakeTensorMode.
    with torch.utils._mode_utils.no_dispatch():
        onnxscript_graph = passes.export_fx_to_onnxscript(
            decomposed_module, export_options
        )
    # Export TorchScript graph to ONNX ModelProto.
    onnx_model = onnxscript_graph.to_model_proto(export_options.opset_version)

    if export_options.use_binary_format:
        # Return ModelProto in binary format.
        return onnx_model.SerializeToString()
    # Return ModelProto
    return onnx_model


@_beartype.beartype
def export(
    fn: Union[torch.nn.Module, Callable],
    *args,
    use_binary_format: bool = True,
    opset_version: int = _constants.ONNX_DEFAULT_OPSET,
    op_level_debug: bool = False,
) -> Union["onnx.ModelProto", bytes]:
    # Translate callable to FX graph.
    #
    # TODO(wechi): There are several symbolic tracing mechanisms to convert
    # nn.Module to FX graph. We should choose the right one after they are
    # matured.
    fx_frontend = frontend.DynamoExport(tracing_mode="real", aten_graph=True)
    graph_module = fx_frontend.trace(fn, *args)
    # Export FX graph to ONNX ModelProto.
    #
    # Note that ALL kwargs are folded into constants in graph_module, so we don't pass kwargs
    # to _export.
    return _export(
        graph_module,
        args,
        opset_version=opset_version,
        decomposition_table=function_dispatcher._ONNX_FRIENDLY_DECOMPOSITION_TABLE,
        use_binary_format=use_binary_format,
        op_level_debug=op_level_debug,
    )


@_beartype.beartype
def export_after_normalizing_args_and_kwargs(
    fn: Union[torch.nn.Module, Callable],
    *args,
    use_binary_format: bool = True,
    opset_version: int = _constants.ONNX_DEFAULT_OPSET,
    op_level_debug: bool = False,
    **kwargs,
) -> Union["onnx.ModelProto", bytes]:
    """Export an nn.Module or a callable to ONNX.

    This traces the given nn.Module or a callable into FX graph and then
    and exports it to ONNX by calling `_export`. Notice that ONNX does
    not represent keyword arguments, so `args` and `kwargs` are normalized by
    `frontend.FxFrontendUnpackKwargs`.

    Args:
        fn: nn.Module or a callable to be exported to ONNX.
        args: the positional arguments to pass to `fn`.
        use_binary_format: whether to return the ONNX model in binary format.
            If False, `onnx.ModelProto` will be returned. If False, the byte array
            generated by `onnx.ModelProto.SerializeToString` is returned.
        opset_version: the opset version to export the model to. E.g., 14.
        op_level_debug: whether to export the model with op-level validation.
        kwargs: the keyword arguments to pass to `fn`.

    Returns:
        ONNX model in binary format or `onnx.ModelProto`. To select return type,
        use `use_binary_format` argument.
    """

    # FIXME: Rewritten "wrapper" class with 'functools.wraps' to retain signature.
    # However, we should remove this in the effort to retain same input/output signature.
    def wrapper(fn):
        fn_call = fn.forward if isinstance(fn, torch.nn.Module) else fn

        @functools.wraps(fn_call)
        def inner(*args, **kwargs):
            result, _ = _pytree.tree_flatten(fn_call(*args, **kwargs))
            return result

        return inner

    wrapped_fn = wrapper(fn)

    # Translate callable to FX graph.
    #
    # TODO(wechi): There are several symbolic tracing mechanisms to convert
    # nn.Module to FX graph. We should choose the right one after they are
    # matured.
    fx_frontend = frontend.FxFrontendUnpackKwargs(
        frontend.DynamoOptimize(dynamic=False)
    )
    captured_graph, bound_args = fx_frontend.trace(wrapped_fn, *args, **kwargs)

    # Export FX graph to ONNX ModelProto.
    return _export(
        captured_graph,
        # Function optimized by _dynamo doesn't have None in args.
        tuple(arg for arg in bound_args if arg is not None),
        opset_version=opset_version,
        decomposition_table=function_dispatcher._ONNX_FRIENDLY_DECOMPOSITION_TABLE,
        use_binary_format=use_binary_format,
        op_level_debug=op_level_debug,
    )
