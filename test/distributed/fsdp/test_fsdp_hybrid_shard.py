# Owner(s): ["oncall: distributed"]

from copy import deepcopy
import functools
import sys
from collections import namedtuple
from contextlib import suppress
import contextlib

import torch
import torch.distributed as dist
import torch.nn as nn
from torch.distributed.fsdp import FlatParameter
from torch.distributed.fsdp import FullyShardedDataParallel as FSDP
from torch.distributed.fsdp import ShardingStrategy, CPUOffload
from torch.distributed.fsdp.wrap import (
    always_wrap_policy,
    transformer_auto_wrap_policy,
)
from torch.nn import TransformerDecoderLayer, TransformerEncoderLayer
from torch.testing._internal.common_distributed import skip_if_lt_x_gpu
from torch.testing._internal.common_fsdp import (
    CUDAInitMode,
    FSDPInitMode,
    FSDPTest,
    NestedWrappedModule,
    TransformerWithSharedParams,
    _assert_module_states,
)
from torch.testing._internal.common_utils import (
    TEST_WITH_DEV_DBG_ASAN,
    instantiate_parametrized_tests,
    parametrize,
    run_tests,
)

from torch.distributed.distributed_c10d import _rank_not_in_group

if not dist.is_available():
    print("Distributed not available, skipping tests", file=sys.stderr)
    sys.exit(0)

if TEST_WITH_DEV_DBG_ASAN:
    print(
        "Skip dev-asan as torch + multiprocessing spawn have known issues",
        file=sys.stderr,
    )
    sys.exit(0)

@contextlib.contextmanager
def patch_reduce_scatter(new_reduce_scatter, full_precision_param_dtype):
    """
    Patches dist._reduce_scatter_base with a new reduce_scatter_base and
    restores upon exiting. Used for validation of mixed precision
    """
    orig_reduce_scatter = dist._reduce_scatter_base
    dist._reduce_scatter_base = new_reduce_scatter
    global _CURRENT_FULL_PRECISION_PARAM_DTYPE
    _CURRENT_FULL_PRECISION_PARAM_DTYPE = full_precision_param_dtype
    try:
        yield
    finally:
        dist._reduce_scatter_base = orig_reduce_scatter
        _CURRENT_FULL_PRECISION_PARAM_DTYPE = None

@contextlib.contextmanager
def patch_allreduce(new_allreduce):
    """
    Patches dist.all_reduce with a new_allreduce and
    restores upon exiting.
    """
    orig_ar = dist.all_reduce
    dist.all_reduce = new_allreduce
    try:
        yield
    finally:
        dist.all_reduce = orig_ar

class TestFSDPHybridShard(FSDPTest):
    @property
    def world_size(self):
        return 2

    @property
    def process_group(self):
        return dist.distributed_c10d._get_default_group()

    @skip_if_lt_x_gpu(2)
    def test_fsdp_hybrid_shard_basic_setup(self):
        """Tests that ``auto_wrap_policy`` propagates ``device_id`` to all
        nested FSDP instances."""
        auto_wrap_policy = functools.partial(
            transformer_auto_wrap_policy,
            transformer_layer_cls={TransformerEncoderLayer, TransformerDecoderLayer},
        )
        fsdp_kwargs = {
            "auto_wrap_policy": auto_wrap_policy,
            "device_id": torch.cuda.current_device(),
            "sharding_strategy": ShardingStrategy.HYBRID_SHARD,
        }
        fsdp_model = TransformerWithSharedParams.init(
            self.process_group,
            FSDPInitMode.RECURSIVE,
            CUDAInitMode.CUDA_BEFORE,
            fsdp_kwargs,
        )

        for fsdp_module in FullyShardedDataParallel.fsdp_modules(fsdp_model):
            self.assertEqual(ShardingStrategy.HYBRID_SHARD, fsdp_module.sharding_strategy)
            # Note that since test env is only 1 node, inter_node_pg should only contain
            # 1 rank.
            inter_node_pg = fsdp_module._inter_node_pg
            my_rank = dist.get_rank(self.process_group)
            other_ranks = [i for i in range(dist.get_world_size(self.process_group)) if i != my_rank]
            self.assertFalse(_rank_not_in_group(my_rank, inter_node_pg))
            for r in other_ranks:
                self.assertTrue(_rank_not_in_group(r, inter_node_pg))

        orig_ar = dist.all_reduce
        def patched_allreduce(*args, **kwargs):
            print(" -- patched --")
            return orig_ar(*args, **kwargs)

        with patch_allreduce(patched_allreduce):
            inp = fsdp_model.get_input()
            out = fsdp_model(inp)
            loss = fsdp_model.get_loss(inp, out)
            loss.backward()



instantiate_parametrized_tests(TestFSDPHybridShard)

if __name__ == "__main__":
    run_tests()
