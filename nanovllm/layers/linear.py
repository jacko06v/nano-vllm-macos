import torch
from torch import nn
import torch.nn.functional as F


def divide(numerator, denominator):
    assert numerator % denominator == 0
    return numerator // denominator


class LinearBase(nn.Module):

    def __init__(
        self,
        input_size: int,
        output_size: int,
        tp_dim: int | None = None,
    ):
        super().__init__()
        self.input_size = input_size
        self.output_size = output_size
        self.tp_dim = tp_dim
        
        # macOS always uses single device - no tensor parallelism
        self.tp_rank = 0
        self.tp_size = 1

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        raise NotImplementedError


class ReplicatedLinear(LinearBase):

    def __init__(
        self,
        input_size: int,
        output_size: int,
        bias: bool = False,
    ):
        super().__init__(input_size, output_size)
        self.weight = nn.Parameter(torch.empty(self.output_size, self.input_size))
        self.weight.weight_loader = self.weight_loader
        if bias:
            self.bias = nn.Parameter(torch.empty(self.output_size))
            self.bias.weight_loader = self.weight_loader
        else:
            self.register_parameter("bias", None)

    def weight_loader(self, param: nn.Parameter, loaded_weight: torch.Tensor):
        param.data.copy_(loaded_weight)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return F.linear(x, self.weight, self.bias)


class ColumnParallelLinear(LinearBase):

    def __init__(
        self,
        input_size: int,
        output_size: int,
        bias: bool = False,
    ):
        super().__init__(input_size, output_size, 0)
        self.input_size_per_partition = input_size
        
        # For single device, don't partition
        if self.tp_size == 1:
            self.output_size_per_partition = output_size
        else:
            self.output_size_per_partition = divide(output_size, self.tp_size)

        self.weight = nn.Parameter(torch.empty(self.output_size_per_partition, self.input_size))
        self.weight.weight_loader = self.weight_loader
        if bias:
            self.bias = nn.Parameter(torch.empty(self.output_size_per_partition))
            self.bias.weight_loader = self.weight_loader
        else:
            self.register_parameter("bias", None)

    def weight_loader(self, param: nn.Parameter, loaded_weight: torch.Tensor):
        if self.tp_size == 1:
            # No partitioning - load full weight
            param.data.copy_(loaded_weight)
        else:
            # Partitioned loading
            param_data = param.data
            shard_size = param_data.size(self.tp_dim)
            start_idx = self.tp_rank * shard_size
            loaded_weight = loaded_weight.narrow(self.tp_dim, start_idx, shard_size)
            param_data.copy_(loaded_weight)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return F.linear(x, self.weight, self.bias)


class MergedColumnParallelLinear(ColumnParallelLinear):

    def __init__(
        self,
        input_size: int,
        output_sizes: list[int],
        bias: bool = False,
    ):
        self.output_sizes = output_sizes
        super().__init__(input_size, sum(output_sizes), bias=bias)

    def weight_loader(self, param: nn.Parameter, loaded_weight: torch.Tensor, loaded_shard_id: int):
        # macOS single device - no partitioning
        shard_offset = sum(self.output_sizes[:loaded_shard_id])
        shard_size = self.output_sizes[loaded_shard_id]
        param_data = param.data.narrow(self.tp_dim, shard_offset, shard_size)
        param_data.copy_(loaded_weight)


class QKVParallelLinear(ColumnParallelLinear):

    def __init__(
        self,
        hidden_size: int,
        head_size: int,
        total_num_heads: int,
        total_num_kv_heads: int | None = None,
        bias: bool = False,
    ):
        self.head_size = head_size
        self.total_num_heads = total_num_heads
        self.total_num_kv_heads = total_num_kv_heads or total_num_heads
        
        # macOS single device - no tensor parallelism
        self.num_heads = self.total_num_heads
        self.num_kv_heads = self.total_num_kv_heads
            
        input_size = hidden_size
        output_size = (self.total_num_heads + 2 * self.total_num_kv_heads) * self.head_size
        super().__init__(input_size, output_size, bias)

    def weight_loader(self, param: nn.Parameter, loaded_weight: torch.Tensor, loaded_shard_id: str):
        assert loaded_shard_id in ["q", "k", "v"]
        
        if self.tp_size == 1:
            # No partitioning - calculate offsets for full weight
            if loaded_shard_id == "q":
                shard_size = self.total_num_heads * self.head_size
                shard_offset = 0
            elif loaded_shard_id == "k":
                shard_size = self.total_num_kv_heads * self.head_size
                shard_offset = self.total_num_heads * self.head_size
            else:  # "v"
                shard_size = self.total_num_kv_heads * self.head_size
                shard_offset = self.total_num_heads * self.head_size + self.total_num_kv_heads * self.head_size
            
            param_data = param.data.narrow(self.tp_dim, shard_offset, shard_size)
            param_data.copy_(loaded_weight)
        else:
            # Partitioned loading
            param_data = param.data
            if loaded_shard_id == "q":
                shard_size = self.num_heads * self.head_size
                shard_offset = 0
            elif loaded_shard_id == "k":
                shard_size = self.num_kv_heads * self.head_size
                shard_offset = self.num_heads * self.head_size
            else:  # "v"
                shard_size = self.num_kv_heads * self.head_size
                shard_offset = self.num_heads * self.head_size + self.num_kv_heads * self.head_size
            param_data = param_data.narrow(self.tp_dim, shard_offset, shard_size)
            loaded_weight = loaded_weight.chunk(self.tp_size, self.tp_dim)[self.tp_rank]
            param_data.copy_(loaded_weight)


class RowParallelLinear(LinearBase):

    def __init__(
        self,
        input_size: int,
        output_size: int,
        bias: bool = False,
    ):
        super().__init__(input_size, output_size, 1)
        
        # For single device, don't partition
        if self.tp_size == 1:
            self.input_size_per_partition = input_size
        else:
            self.input_size_per_partition = divide(input_size, self.tp_size)
            
        self.output_size_per_partition = output_size

        self.weight = nn.Parameter(torch.empty(self.output_size, self.input_size_per_partition))
        self.weight.weight_loader = self.weight_loader
        if bias:
            self.bias = nn.Parameter(torch.empty(self.output_size))
            self.bias.weight_loader = self.weight_loader
        else:
            self.register_parameter("bias", None)

    def weight_loader(self, param: nn.Parameter, loaded_weight: torch.Tensor):
        if self.tp_size == 1:
            # No partitioning - load full weight
            param.data.copy_(loaded_weight)
        else:
            # Partitioned loading
            param_data = param.data
            shard_size = param_data.size(self.tp_dim)
            start_idx = self.tp_rank * shard_size
            loaded_weight = loaded_weight.narrow(self.tp_dim, start_idx, shard_size)
            param_data.copy_(loaded_weight)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # On macOS single device, no need for distributed reduction
        y = F.linear(x, self.weight, self.bias)
        return y
