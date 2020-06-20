import math
import torch
import torch.distributed as dist
from torch._utils import _flatten_dense_tensors, _unflatten_dense_tensors
from collections import defaultdict

from deepspeed.pt.zero_utils import _initialize_parameter_parallel_groups
from deepspeed.pt.log_utils import log_dist, logger
from deepspeed.pt.loss_scaler import LossScaler, DynamicLossScaler
from deepspeed.pt.deepspeed_utils import get_grad_norm, CheckOverflow


def flatten_dense_tensors_sub_partition_aligned_(tensor_list,
                                                dp,
                                                max_elements_per_comm,
                                                pg):
    num_elements = sum(t.numel() for t in tensor_list)
    log_dist("Total number of elements in model: {}, max elements per com: {}".format(
        num_elements,
        max_elements_per_comm),
        ranks=[0])

    # Compute aligned partition size based on parameter count
    aligned_param_partition_size = math.ceil(num_elements / dp)

    # Compute aligned partition size based on communication size
    aligned_comm_partition_size = int(max_elements_per_comm // dp)



def flatten_dense_tensors_sub_partition_aligned(tensor_list,
                                                dp,
                                                max_elements_per_comm,
                                                pg):
    num_elements = 0
    for tensor in tensor_list:
        num_elements = num_elements + tensor.numel()

    log_dist("Total number of elements in model: {}, max elements per com: {}".format(
        num_elements,
        max_elements_per_comm),
             ranks=[0])

    max_elements_per_comm = min(max_elements_per_comm, num_elements)
    sub_partition_size = int(max_elements_per_comm // dp)

    alignment = sub_partition_size

    # if alignment == 0:
    #     # number of elements not divisible by dp, outside range and small model must pad with zeroes
    #     pad_tensor = torch.zeros(max_elements_per_comm,
    #                              device=tensor_list[0].device,
    #                              dtype=tensor_list[0].dtype)
    #     return _flatten_dense_tensors(pad_tensor)

    remaining = int(num_elements % alignment)

    # ensure we have equal sized sub-partitions
    elements_to_add = 0
    if remaining:
        elements_to_add = alignment - remaining
        # adding padded tensor later after we check comm alignment
        log_dist("adding pad tensor for alignment, {} + {}->{}".format(
            num_elements,
            elements_to_add,
            num_elements + elements_to_add),
                 ranks=[0])
        #num_elements = num_elements + elements_to_add
    else:
        padded_tensor_list = tensor_list

    num_partitions = int((num_elements + elements_to_add) // sub_partition_size)
    assert (num_elements + elements_to_add) % sub_partition_size == 0, "num elements should be " \
                                                                       "aligned by sub partition " \
                                                                       "size"
    num_comm_intervals = int(num_partitions // dp)
    partition_remaining = int(num_partitions % dp)
    log_dist("num_comm_intervals={}, partition_remaining={}".format(
        num_comm_intervals,
        partition_remaining),
             ranks=[0])
    if partition_remaining != 0:
        log_dist("adding pad tensor and/or extra sub partition", ranks=[0])
        # add pad tensor for alignment of comm interval, this overrules previous possibly sub-partition alignment
        num_comm_intervals += 1
        aligned_comm_elements = num_comm_intervals * sub_partition_size * dp
        elements_to_add = aligned_comm_elements - num_elements

        pad_tensor = torch.zeros(elements_to_add,
                                 device=tensor_list[0].device,
                                 dtype=tensor_list[0].dtype)
        padded_tensor_list = tensor_list + [pad_tensor]
        log_dist("adding pad tensor and/or extra sub partition, {} + {}->{}".format(
            num_elements,
            elements_to_add,
            num_elements + elements_to_add),
                 ranks=[0])
        num_elements += elements_to_add
    elif elements_to_add > 0:
        # add pad tensor for just alignment of sub-partition
        pad_tensor = torch.zeros(elements_to_add,
                                 device=tensor_list[0].device,
                                 dtype=tensor_list[0].dtype)
        padded_tensor_list = tensor_list + [pad_tensor]
        num_elements += elements_to_add

    if pg is None or dist.get_rank(group=pg) == 0:
        logger.info("Number of Elements (w. padding) is %s", num_elements)

    padded_num_elems = 0
    for p in padded_tensor_list:
        padded_num_elems += p.numel()
    assert num_elements == padded_num_elems, "{} != {}, rank={}".format(num_elements, padded_num_elems, dist.get_rank())

    return _flatten_dense_tensors(padded_tensor_list)


def _single_range_check(current_index, start_index, end_index, tensor_size):
    offset = 0
    if (current_index >= start_index) and (current_index < end_index):
        # Fully inside bounds
        return True, offset
    elif (start_index > current_index) and (start_index < (current_index + tensor_size)):
        # Partially contained, compute offset
        offset = start_index - current_index
        return True, offset
    else:
        return False, offset


def _range_check(current_index, element_intervals, tensor_size):
    results = []
    for comm_idx, interval in enumerate(element_intervals):
        start_index, end_index = interval
        contained, offset = _single_range_check(current_index, start_index, end_index, tensor_size)
        if contained:
            results.append((contained, offset, comm_idx))
    if len(results) == 0:
        return [(False, 0, -1)]
    return results


class FP16_DeepSpeedZeroOptimizer_Stage1(object):
    """
    FP16_DeepSpeedZeroOptimizer_Stage1 designed to reduce the memory footprint
    required for training large deep learning models.

    For more details please see ZeRO: Memory Optimization Towards Training A Trillion Parameter Models
    https://arxiv.org/abs/1910.02054

    This version aligns with stage-1 in the paper above.
    """
    def __init__(self,
                 init_optimizer,
                 static_loss_scale=1.0,
                 dynamic_loss_scale=False,
                 dynamic_loss_args=None,
                 verbose=True,
                 dp_process_group=None,
                 partition_size=None,
                 mpu=None,
                 all_gather_partitions=True,
                 allgather_size=500000000,
                 clip_grad=0.0,
                 max_elements_per_comm=5e8):

        if dp_process_group is not None and partition_size is not None:
            raise ValueError("Cannot specify both dp_process_group "
                             "and partition size")

        if dp_process_group is None:
            dp_process_group = _initialize_parameter_parallel_groups(partition_size)

        if not torch.cuda.is_available:
            raise SystemError("Cannot use fp16 without CUDA.")
        self.optimizer = init_optimizer

        self.verbose = verbose
        self.dp_process_group = dp_process_group

        # TODO: automatically turn off if #params > some_limit
        self.all_gather_partitions = all_gather_partitions
        self.allgather_size = allgather_size

        self.max_elements_per_comm = max_elements_per_comm
        logger.info("max_elements_per_comm={}".format(max_elements_per_comm))

        # param flattened by groups
        self.fp16_groups = []
        self.fp16_groups_flat = []

        # Setup bookkeeping data structures depending on partitioning type

        # parallel_sub_partitioned_fp16_groups[group-idx] -> [comm-ids] -> [rank-ids]
        self.parallel_sub_partitioned_fp16_groups = []
        # same underlying data as above but viewed as: [groups] -> [rank-ids] -> [comm-ids]
        self.parallel_comm_sub_partitioned_fp16_groups = []

        # 32-bit sub-partitions of the parallel partitioned parameters
        # that this process will update
        self.local_sub_partitions_of_fp32_groups = []

        # param partition info

        # parameters in each group that will not be updated by this process directly
        self.params_not_local = []

        # parameters that will be updated by this process directly
        self.params_in_rank_sub_partitions = []

        # parameter offsets for parameters in sub-partitions. Parameter
        # boundaries may not align with sub-partition boundaries
        # so we need to keep track of the offsets
        self.params_in_rank_sub_partitions_offsets = []

        # number of elements per sub-partition in each group
        self.sub_partition_sizes = []

        # number of communication intervals for each group
        self.num_comm_intervals_per_group = []

        local_rank = dist.get_rank(group=self.dp_process_group)

        # loop to deal with groups
        for i, param_group in enumerate(self.optimizer.param_groups):
            # push this group to list before modify
            self.fp16_groups.append(param_group['params'])

            # flattens all tensors into single 1d tensor aligned with sub-partition size for later dividing
            # RS: create aligned sub-partitions
            self.fp16_groups_flat.append(
                flatten_dense_tensors_sub_partition_aligned(
                    tensor_list=self.fp16_groups[i],
                    dp=dist.get_world_size(group=self.dp_process_group),
                    max_elements_per_comm=self.max_elements_per_comm,
                    pg=self.dp_process_group))

            # TODO: I don't think this does anything?
            # set model fp16 weight to slices of flattened buffer
            updated_params = _unflatten_dense_tensors(self.fp16_groups_flat[i],
                                                      self.fp16_groups[i])
            for p, q in zip(self.fp16_groups[i], updated_params):
                p.data = q.data

            # divide the flat weights into near equal partition equal to the data parallel degree
            # each process will compute on a different part of the partition
            # RS: split into two layer list -> [comm-id] -> [sub-partitions per rank]
            comm_partitions, dp_sub_partitions, element_intervals, sub_partition_size, num_comm_intervals = \
                self.get_data_parallel_sub_partitions(
                    tensor=self.fp16_groups_flat[i],
                    max_elements_per_comm=self.max_elements_per_comm,
                    world_size=dist.get_world_size(
                        group=self.dp_process_group),
                    dp_process_group=self.dp_process_group
                )
            self.parallel_comm_sub_partitioned_fp16_groups.append(
                comm_partitions)  # comm -> rank
            self.parallel_sub_partitioned_fp16_groups.append(
                dp_sub_partitions)  # rank -> comm
            self.sub_partition_sizes.append(sub_partition_size)
            self.num_comm_intervals_per_group.append(num_comm_intervals)
            # data_parallel_partitions = self.get_data_parallel_partitions(self.fp16_groups_flat[i])
            # self.parallel_partitioned_fp16_groups.append(data_parallel_partitions)

            # a partition of the fp32 master weights that will be updated by this process
            # RS: store/detach/cast our local sub-partitions
            local_sub_partitions = []
            for sub_partition in self.parallel_sub_partitioned_fp16_groups[i][
                    local_rank]:
                fp32_sub_partition = sub_partition.clone().float().detach()
                fp32_sub_partition.requires_grad = True
                local_sub_partitions.append(fp32_sub_partition)
            self.local_sub_partitions_of_fp32_groups.append(local_sub_partitions)

            # modify optimizer of have flat master weight
            # self.single_partition_of_fp32_groups[i].requires_grad = True # keep this in case internal optimizer uses it
            param_group['params'] = self.local_sub_partitions_of_fp32_groups[i]

            # RS: divide up the sub-partitions and keep track of offsets for each param
            # partition_size = len(self.fp16_groups_flat[i]) / dist.get_world_size(group=self.dp_process_group)
            params_in_rank_sub_partition, params_in_rank_sub_partitions_offsets, \
            params_not_local = self.get_all_sub_partition_info(
                tensor_list=self.fp16_groups[i],
                all_element_intervals=element_intervals,
                local_rank=local_rank,
                world_size=dist.get_world_size(group=self.dp_process_group)
            )

            self.params_in_rank_sub_partitions.append(params_in_rank_sub_partition)
            self.params_not_local.append(params_not_local)
            self.params_in_rank_sub_partitions_offsets.append(
                params_in_rank_sub_partitions_offsets)

        # we may have a way of fusing dynamic scale. Do not support for now
        if dynamic_loss_scale:
            if dynamic_loss_args is None:
                self.loss_scaler = DynamicLossScaler()
            else:
                self.loss_scaler = DynamicLossScaler(**dynamic_loss_args)

            self.dynamic_loss_scale = True

        else:
            self.dynamic_loss_scale = False
            self.loss_scaler = LossScaler(scale=static_loss_scale)
            self.cur_iter = 0

        self.mpu = mpu
        self.clip_grad = clip_grad

        self.overflow = False
        self.overflow_checker = CheckOverflow(self.fp16_groups,
                                              mpu=self.mpu,
                                              zero_reduce_scatter=True)

    @staticmethod
    def get_data_parallel_sub_partitions(tensor,
                                         max_elements_per_comm,
                                         world_size,
                                         dp_process_group=None):
        total_num_elements = tensor.numel()

        # if total elements is less than our max, revert to splitting into dp partitions
        max_elements_per_comm = min(total_num_elements, max_elements_per_comm)
        sub_partition_size = int(max_elements_per_comm // world_size)

        # Ensure partition alignment was done correctly
        num_sub_partitions = int(total_num_elements // sub_partition_size)
        assert total_num_elements % sub_partition_size == 0, "{} % {} != 0".format(total_num_elements, sub_partition_size)

        # Ensure comm interval alignment was done correctly.
        num_comm_intervals = int(num_sub_partitions // world_size)
        assert num_sub_partitions % world_size == 0, "{} % {} != 0".format(num_sub_partitions, world_size)

        if not dist.is_initialized() or dist.get_rank(group=dp_process_group) == 0:
            logger.info("**** partition info:")
            logger.info("\t total_num_elements=%s", total_num_elements)
            logger.info("\t world_size=%s", world_size)
            logger.info("\t max_elements_per_comm=%s", max_elements_per_comm)
            logger.info("\t sub_partition_size=%s", sub_partition_size)
            logger.info("\t num_sub_partitions=%s", num_sub_partitions)
            logger.info("\t num_comm_intervals=%s", num_comm_intervals)
            logger.info("****")

        # [comm_id] -> [rank]
        comm_partitions = []
        for _ in range(num_comm_intervals):
            comm_partitions.append([])

        start = 0
        comm_id = 0
        element_intervals = defaultdict(
            list)  # [rank] -> [(start,end), (start,end), ...]
        for idx in range(num_sub_partitions):
            rank_id = idx % world_size
            sub_partition = tensor.narrow(0, start, sub_partition_size).detach()
            element_intervals[rank_id].append((start, start + sub_partition_size))
            comm_partitions[comm_id].append(sub_partition)
            start = start + sub_partition_size
            if rank_id == (world_size - 1):
                comm_id += 1

        # [rank] -> [comm_id]
        sub_partitions = []
        for _ in range(world_size):
            sub_partitions.append([])
        for comm_id, partitions in enumerate(comm_partitions):
            for rank_id, partition in enumerate(partitions):
                sub_partitions[rank_id].append(partition)

        return comm_partitions, sub_partitions, element_intervals, sub_partition_size, num_comm_intervals

    @staticmethod
    def get_all_sub_partition_info(tensor_list,
                                   all_element_intervals,
                                   local_rank,
                                   world_size):
        params_not_local = []

        # [rank] -> [comm-id] -> [param/offset]
        params_in_rank_sub_partition = []
        params_in_rank_sub_partitions_offsets = []

        for rank in range(world_size):
            params_in_local_sub_partition = []
            local_sub_partition_offsets = []
            comm_tensor_list = []
            comm_offset_list = []
            current_index = 0
            prev_comm_idx = 0
            for iii, tensor in enumerate(tensor_list):
                tensor_size = tensor.numel()
                #if local_rank == 0:
                #    # logger.info("rank={}, current_index={}, tensor_size={}, tensor-idx={}".format(rank,
                #        current_index, tensor_size, iii))
                results_list = _range_check(current_index,
                                            all_element_intervals[rank],
                                            tensor_size)
                for contained, offset, comm_idx in results_list:
                    #if local_rank == 0:
                    #    logger.info("rank={}, contained={}, offset={}, comm_idx={}".format(rank, contained,
                    #        offset, comm_idx))
                    if contained:
                        if prev_comm_idx != comm_idx:
                            params_in_local_sub_partition.append(comm_tensor_list)
                            comm_tensor_list = []
                            local_sub_partition_offsets.append(comm_offset_list)
                            comm_offset_list = []
                        comm_tensor_list.append(tensor)
                        comm_offset_list.append(offset)
                        prev_comm_idx = comm_idx
                    elif rank == local_rank:
                        params_not_local.append(tensor)

                current_index = current_index + tensor_size

            #assert len(comm_tensor_list) > 0
            #assert len(comm_offset_list) > 0
            params_in_local_sub_partition.append(comm_tensor_list)
            local_sub_partition_offsets.append(comm_offset_list)

            params_in_rank_sub_partition.append(params_in_local_sub_partition)
            params_in_rank_sub_partitions_offsets.append(local_sub_partition_offsets)

        return params_in_rank_sub_partition, params_in_rank_sub_partitions_offsets, params_not_local

    @staticmethod
    def get_flat_sub_partitions(comm_tensor_list,
                                comm_param_offsets,
                                sub_partition_size,
                                dtype,
                                num_comm_intervals=None,
                                default_device=None,
                                return_partition_params=False):
        partition_params = []
        final_param_offsets = []
        flat_sub_partitions = []
        for tensor_list, param_offsets in zip(comm_tensor_list, comm_param_offsets):
            flat_tensor_list = []
            current_size = 0
            my_offsets = []
            my_params = []

            if dtype is None:
                dtype = tensor_list[0].dtype

            for i, tensor in enumerate(tensor_list):
                if tensor.grad is None:
                    tensor.grad = torch.zeros(tensor.size(),
                                              dtype=tensor.dtype,
                                              device=tensor.device)
                param = tensor
                tensor = tensor.grad
                num_elements = tensor.numel()
                tensor_offset = 0

                #we need to offset to get to the right element
                if i == 0 and param_offsets[i] > 0:
                    tensor_offset = param_offsets[i]
                    num_elements = num_elements - tensor_offset

                # We don't need all elements of the tensor if this tensor is
                # larger than we have space for in our curr sub-partition
                if num_elements > (sub_partition_size - current_size):
                    num_elements = sub_partition_size - current_size

                #we need a narrow view of the tensor based on the tensor offset and number of elements that
                #we need from this tensor
                if tensor_offset > 0 or num_elements < tensor.numel():
                    flat_tensor_list.append(tensor.contiguous().view(-1).narrow(
                        0,
                        int(tensor_offset),
                        int(num_elements)).to(dtype))
                else:
                    flat_tensor_list.append(tensor.to(dtype))
                my_params.append(param)

                #remember offset into partition and #elems for this tensor
                my_offsets.append((current_size, num_elements))

                current_size = current_size + num_elements

            #this means its the last partition and does not align with the dp boundary. We need to pad before flattening
            if current_size < sub_partition_size:
                my_offsets.append((None, None))
                my_params.append(None)
                if len(tensor_list) == 0:
                    assert default_device != None
                    flat_tensor_list.append(
                        torch.zeros(int(sub_partition_size - current_size),
                                    dtype=dtype,
                                    device=default_device))
                else:
                    flat_tensor_list.append(
                        torch.zeros(int(sub_partition_size - current_size),
                                    dtype=dtype,
                                    device=tensor_list[0].device))
            partition_params.append(my_params)  #flat_tensor_list)
            final_param_offsets.append(my_offsets)
            assert len(flat_tensor_list) == len(my_offsets), "{} {}".format(len(flat_tensor_list), len(my_offsets))
            flat_sub_partitions.append(_flatten_dense_tensors(flat_tensor_list))
        if num_comm_intervals is not None and len(
                flat_sub_partitions) < num_comm_intervals:
            # logger.info("padding w. sub partitions to ensure uniform communication")
            device = flat_sub_partitions[0].device
            for _ in range(num_comm_intervals - len(flat_sub_partitions)):
                flat_sub_partitions.append(
                    torch.zeros(int(sub_partition_size),
                                dtype=dtype,
                                device=device))
                partition_params.append([None])
                final_param_offsets.append([(None, None)])

        if return_partition_params:
            assert len(flat_sub_partitions) == len(partition_params)
            assert len(partition_params) == len(final_param_offsets), "{} {}".format(len(partition_params), len(final_param_offsets))
            return flat_sub_partitions, partition_params, final_param_offsets
        return flat_sub_partitions

    def zero_grad(self, set_grads_to_None=True):
        """
        Zero FP16 parameter grads.
        """
        # FP32 grad should never exist.
        # For speed, set model fp16 grad to None by default
        for group in self.fp16_groups:
            for p in group:
                if set_grads_to_None:
                    p.grad = None
                else:
                    if p.grad is not None:
                        p.grad.detach_()
                        p.grad.zero_()

    def free_grad_in_param_list(self, param_list):
        for p in param_list:
            if isinstance(p, list):
                for _p in p:
                    _p.grad = None
            else:
                p.grad = None

    def reduce_scatter_gradients(self,
                                 postscale_gradients,
                                 gradient_predivide_factor,
                                 gradient_average):
        world_size = dist.get_world_size(group=self.dp_process_group)
        local_rank = dist.get_rank(group=self.dp_process_group)

        for i, group in enumerate(self.fp16_groups):
            partition_param_map = {}
            param_partition_map = {}
            my_params = set()

            # [rank] -> [comm] -> partition
            num_comm_intervals = self.num_comm_intervals_per_group[i]
            all_sub_partitions = []
            for rank in range(world_size):
                # gsp is list of partitions indexed by comm_idx
                #FIXME: currently hardcoding fp16, should infer dtype
                grad_sub_partitions, partition_params, param_offsets = self.get_flat_sub_partitions(
                    comm_tensor_list=self.params_in_rank_sub_partitions[i][rank],
                    comm_param_offsets=self.params_in_rank_sub_partitions_offsets[i][rank],
                    sub_partition_size=self.sub_partition_sizes[i],
                    dtype=torch.half, #self.params_in_rank_sub_partitions[i][rank][0][0].dtype,
                    num_comm_intervals=self.num_comm_intervals_per_group[i],
                    default_device='cuda', #self.params_in_rank_sub_partitions[i][rank][0][0].device,
                    return_partition_params=True)
                all_sub_partitions.append(grad_sub_partitions)

                # create map from partition -> params in that partition
                for comm_idx, part in enumerate(grad_sub_partitions):
                    partition_param_map[part] = (partition_params[comm_idx],
                                                 param_offsets[comm_idx])

                for comm_idx, params in enumerate(partition_params):
                    for pidx, p in enumerate(params):
                        # store the parameters we care about locally
                        if rank == local_rank:
                            my_params.add(p)
                        # map from param -> partitions
                        if p in param_partition_map:
                            param_partition_map[p].append(grad_sub_partitions[comm_idx])
                        else:
                            param_partition_map[p] = [grad_sub_partitions[comm_idx]]

                assert len(grad_sub_partitions) == num_comm_intervals

            if not postscale_gradients:
                raise NotImplementedError("pre-scale_gradients is not implemented")

            all_comm_partitions = []
            for comm_idx in range(num_comm_intervals):
                single_comm_all_partitions = []
                for rank in range(world_size):
                    single_comm_all_partitions.append(all_sub_partitions[rank][comm_idx])
                dist.reduce_scatter(output=single_comm_all_partitions[local_rank],
                                    input_list=single_comm_all_partitions,
                                    group=self.dp_process_group)

                if gradient_average:
                    for partition in single_comm_all_partitions:
                        partition.mul_(gradient_predivide_factor / world_size)

                all_comm_partitions.append(single_comm_all_partitions)

            for p in my_params:
                partitions = param_partition_map[p]
                parts = []
                for part in partitions:
                    params, offsets = partition_param_map[part]
                    found = False
                    for p_idx, _p in enumerate(params):
                        if p.__hash__() == _p.__hash__():
                            found = True
                            if offsets[p_idx][0] is not None:
                                my_part = part.narrow(0,
                                                      offsets[p_idx][0],
                                                      offsets[p_idx][1])
                                parts.append(my_part)
                    assert found
                if p is not None:
                    updated_grad = _unflatten_dense_tensors(torch.cat(parts), [p])
                    p.grad.copy_(updated_grad[0])

    def step(self, closure=None):
        # First compute norm for all group so we know if there is overflow

        self.overflow = self.overflow_checker.check()

        prev_scale = self.loss_scale
        self._update_scale(self.overflow)
        if self.overflow:
            self.zero_grad()
            if self.verbose:
                logger.info("[deepspeed] OVERFLOW! Skipping step. Attempted loss "
                            "scale: {}, reducing to {}".format(
                                prev_scale,
                                self.loss_scale))
            return self.overflow

        norm_groups = []
        local_sub_partitions_grad_groups = []

        partition_id = dist.get_rank(group=self.dp_process_group)
        for i, group in enumerate(self.fp16_groups):

            #TODO RS: update get grad norm to support sub partitions
            norm_groups.append(get_grad_norm(group, mpu=self.mpu))

            #RS: update free grads w.r.t. sub partitions
            #free gradients for all the parameters that are not updated by this process
            self.free_grad_in_param_list(self.params_not_local[i])

            #create flat gradients for parameters updated by this process
            #tensor_list, first_offset, partition_size, dtype
            #single_grad_partition = self.get_flat_partition(
            #    tensor_list=self.params_in_partition[i],
            #    first_offset=self.first_offset[i],
            #    partition_size=self.partition_size[i],
            #    dtype=self.single_partition_of_fp32_groups[i].dtype
            #)

            #TODO RS: can we safely use dtype of the first sub-partition? i think so
            local_grad_sub_partitions = self.get_flat_sub_partitions(
                comm_tensor_list=self.params_in_rank_sub_partitions[i][partition_id],
                comm_param_offsets=self.params_in_rank_sub_partitions_offsets[i]
                [partition_id],
                sub_partition_size=self.sub_partition_sizes[i],
                dtype=self.local_sub_partitions_of_fp32_groups[i][0].dtype,
                num_comm_intervals=self.num_comm_intervals_per_group[i],
                default_device=self.local_sub_partitions_of_fp32_groups[i][0].device)

            #RS: update all our local params with sub-partition grads
            #logger. info("self.local_sub_partitions_of_fp32_groups[i]={}, local_grad_sub_partitions={}".format(len(self.local_sub_partitions_of_fp32_groups[i]), len(local_grad_sub_partitions)))
            for idx, sub_partition_param in enumerate(self.local_sub_partitions_of_fp32_groups[i]):
                sub_partition_param.grad = local_grad_sub_partitions[idx]
            #self.single_partition_of_fp32_groups[i].grad = single_grad_partition

            #RS: update free grads for sub-partitions
            #release all the gradient since we have already created a necessary copy in dp_grad_partition
            self.free_grad_in_param_list(
                self.params_in_rank_sub_partitions[i][partition_id])

            local_sub_partitions_grad_groups.append(local_grad_sub_partitions)

        #RS: update unscale/clip with sub partitions
        self.unscale_and_clip_grads(local_sub_partitions_grad_groups, norm_groups)

        self.optimizer.step()

        #RS: clear our sub partition grads
        #get rid of the fp32 gradients. Not needed anymore
        for group in self.local_sub_partitions_of_fp32_groups:
            for idx, sub_partition_param in enumerate(group):
                sub_partition_param.grad = None
            #group.grad = None

        #NOTE RS: removed norm_groups outer loop from original code, i don't think it's needed
        #RS: copy all sub-partition fp32 data to fp16 sub partitions
        # copy fp32 param data to fp16 partitions w.r.t. our local rank
        for fp16_all_sub_partitions, fp32_local_sub_partitions in zip(self.parallel_sub_partitioned_fp16_groups, self.local_sub_partitions_of_fp32_groups):
            for local_sub_partition_param_fp16, local_sub_partition_param_fp32 in zip(fp16_all_sub_partitions[partition_id], fp32_local_sub_partitions):
                local_sub_partition_param_fp16.data.copy_(
                    local_sub_partition_param_fp32.data)

        #RS: all_gather/broadcast sub-partitions in separate comm calls
        #gather the updated weights from everyone
        for fp16_all_sub_partitions in self.parallel_comm_sub_partitioned_fp16_groups:
            for comm_id, sub_partitions in enumerate(fp16_all_sub_partitions):
                dist.all_gather(sub_partitions,
                                sub_partitions[partition_id],
                                group=self.dp_process_group)

        # TODO: we probably don't need this? just to be safe
        for i in range(len(norm_groups)):
            updated_params = _unflatten_dense_tensors(self.fp16_groups_flat[i],
                                                      self.fp16_groups[i])
            for p, q in zip(self.fp16_groups[i], updated_params):
                p.data = q.data

        return self.overflow

    def unscale_and_clip_grads(self, grad_groups_flat, norm_groups):
        total_norm = 0.0
        for norm in norm_groups:
            total_norm += norm**2.0
        total_norm = math.sqrt(total_norm)

        # compute combined scale factor for this group
        combined_scale = self.loss_scale
        if self.clip_grad > 0.:
            # norm is in fact norm*scale
            clip = ((total_norm / self.loss_scale) + 1e-6) / self.clip_grad
            if clip > 1:
                combined_scale = clip * self.loss_scale

        for grad in grad_groups_flat:
            if isinstance(grad, list):
                sub_partitions = grad
                for g in sub_partitions:
                    g.data.mul_(1. / combined_scale)
            else:
                grad.data.mul_(1. / combined_scale)

    def backward(self, loss, retain_graph=False):
        self.loss_scaler.backward(loss.float(), retain_graph=retain_graph)

    def _update_scale(self, has_overflow=False):
        self.loss_scaler.update_scale(has_overflow)

    # Promote state so it can be retrieved or set via "fp16_optimizer_instance.state"
    def _get_state(self):
        return self.optimizer.state

    def _set_state(self, value):
        self.optimizer.state = value

    state = property(_get_state, _set_state)

    # Promote param_groups so it can be retrieved or set via "fp16_optimizer_instance.param_groups"
    # (for example, to adjust the learning rate)
    def _get_param_groups(self):
        return self.optimizer.param_groups

    def _set_param_groups(self, value):
        self.optimizer.param_groups = value

    param_groups = property(_get_param_groups, _set_param_groups)

    # Promote loss scale so it can be retrieved or set via "fp16_optimizer_instance.loss_scale"
    def _get_loss_scale(self):
        return self.loss_scaler.loss_scale

    def _set_loss_scale(self, value):
        self.loss_scaler.cur_scale = value

    loss_scale = property(_get_loss_scale, _set_loss_scale)
    cur_scale = property(_get_loss_scale, _set_loss_scale)

    def state_dict(self):
        """
        Returns a dict containing the current state of this :class:`FP16_Optimizer` instance.
        This dict contains attributes of :class:`FP16_Optimizer`, as well as the state_dict
        of the contained Pytorch optimizer.
        Example::
            checkpoint = {}
            checkpoint['model'] = model.state_dict()
            checkpoint['optimizer'] = optimizer.state_dict()
            torch.save(checkpoint, "saved.pth")
        """
        state_dict = {}
        state_dict['loss_scaler'] = self.loss_scaler
        state_dict['dynamic_loss_scale'] = self.dynamic_loss_scale
        state_dict['overflow'] = self.overflow
        state_dict['optimizer_state_dict'] = self.optimizer.state_dict()
        state_dict[
            'local_sub_partitions_of_fp32_groups'] = self.local_sub_partitions_of_fp32_groups
        return state_dict

    # Refresh the fp32 master params from the fp16 copies.
    def refresh_fp32_params(self):
        partition_id = dist.get_rank(group=self.dp_process_group)
        for fp16_all_sub_partitions, fp32_local_sub_partitions in zip(self.parallel_sub_partitioned_fp16_groups, self.local_sub_partitions_of_fp32_groups):
            for local_sub_partition_param_fp16, local_sub_partition_param_fp32 in zip(fp16_all_sub_partitions[partition_id], fp32_local_sub_partitions):
                local_sub_partition_param_fp32.data.copy_(
                    local_sub_partition_param_fp16.data)

    def load_state_dict(self, state_dict, load_optimizer_states=True):
        """
        Loads a state_dict created by an earlier call to state_dict().
        If ``fp16_optimizer_instance`` was constructed from some ``init_optimizer``,
        whose parameters in turn came from ``model``, it is expected that the user
        will call ``model.load_state_dict()`` before
        ``fp16_optimizer_instance.load_state_dict()`` is called.
        Example::
            model = torch.nn.Linear(D_in, D_out).cuda().half()
            optimizer = torch.optim.SGD(model.parameters(), lr=1e-3)
            optimizer = FP16_Optimizer(optimizer, static_loss_scale = 128.0)
            ...
            checkpoint = torch.load("saved.pth")
            model.load_state_dict(checkpoint['model'])
            optimizer.load_state_dict(checkpoint['optimizer'])
        """
        # I think it should actually be ok to reload the optimizer before the model.
        self.loss_scaler = state_dict['loss_scaler']
        self.dynamic_loss_scale = state_dict['dynamic_loss_scale']
        self.overflow = state_dict['overflow']
        if load_optimizer_states:
            self.optimizer.load_state_dict(state_dict['optimizer_state_dict'])

        for curr_group, saved_group in zip(self.local_sub_partitions_of_fp32_groups, state_dict['local_sub_partitions_of_fp32_groups']):
            for curr_param, saved_param in zip(curr_group, saved_group):
                curr_param.data.copy_(saved_param.data)
