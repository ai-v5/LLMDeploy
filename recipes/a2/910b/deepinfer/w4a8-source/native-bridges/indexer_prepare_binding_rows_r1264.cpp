// SPDX-License-Identifier: AGPL-3.0-only
// Task-local raw paged preparation. CPU registration is not device admission.
#include <ATen/ATen.h>
#include <torch/library.h>
#include <acl/acl.h>
#include <torch_npu/csrc/core/npu/NPUGuard.h>
#include <torch_npu/csrc/core/npu/NPUStream.h>
#include <torch_npu/csrc/core/npu/NPUCachingAllocator.h>
#include <torch_npu/csrc/core/npu/NPUFormat.h>
#include <torch_npu/csrc/framework/OpCommand.h>
#include <array>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <map>
#include <mutex>
#include <string>
#include <vector>

namespace {
struct Loaded { std::string path; aclrtBinHandle binary; aclrtFuncHandle function; };
std::mutex mutex;
// Functions used by captured graphs remain loaded until process exit.
std::array<std::map<c10::DeviceIndex, Loaded>, 3> loaded;

void prepare_impl(const std::string& path, int64_t index, int kind) {
    TORCH_CHECK(index >= 0 && index < 16 && !path.empty(), "Invalid kernel path/device");
    const auto device = static_cast<c10::DeviceIndex>(index);
    const c10_npu::NPUGuard guard(device);
    std::lock_guard<std::mutex> lock(mutex);
    auto& functions = loaded[kind];
    const auto previous = functions.find(device);
    if (previous != functions.end()) {
        TORCH_CHECK(previous->second.path == path, "Different task kernel already loaded");
        return;
    }
    Loaded value{path, nullptr, nullptr};
    TORCH_CHECK(aclrtBinaryLoadFromFile(path.c_str(), nullptr, &value.binary) == ACL_SUCCESS, "Kernel binary load failed");
    const auto status = aclrtBinaryGetFunction(value.binary,
        kind == 0 ? "indexer_schedule_rows_r1264__kernel0" : (kind == 1 ? "indexer_gather_r722__kernel0" : "indexer_select_r760__kernel0"), &value.function);
    if (status != ACL_SUCCESS) {
        aclrtBinaryUnLoad(value.binary);
        TORCH_CHECK(false, "Kernel function lookup failed: ", status);
    }
    functions.emplace(device, std::move(value));
}
void prepare_schedule(const std::string& path, int64_t index) { prepare_impl(path, index, 0); }
void prepare_gather(const std::string& path, int64_t index) { prepare_impl(path, index, 1); }
void prepare_select(const std::string& path, int64_t index) { prepare_impl(path, index, 2); }

struct ScheduleArgs {
    std::array<void*, 11> pointers;
    std::array<int64_t, 11> dimensions;
};
struct GatherArgs {
    std::array<void*, 4> pointers;
    int64_t packed_capacity;
};
static_assert(sizeof(void*) == 8, "This task kernel ABI is aarch64 only");
static_assert(offsetof(ScheduleArgs, dimensions) == 88 && sizeof(ScheduleArgs) == 176, "Schedule kernel ABI layout changed");
static_assert(offsetof(GatherArgs, packed_capacity) == 32 && sizeof(GatherArgs) == 40, "Gather kernel ABI layout changed");

std::vector<at::Tensor> run(const at::Tensor& cache, const at::Tensor& blocks,
                          const at::Tensor& lengths, const at::Tensor& tables,
                          const at::Tensor& active, int64_t block_size,
                          int64_t run_capacity, int64_t packed_capacity) {
    TORCH_CHECK(cache.device().type() == c10::DeviceType::PrivateUse1, "Expected NPU cache");
    TORCH_CHECK(cache.scalar_type() == at::kBFloat16 && cache.dim() == 3 && cache.size(0) > 0 &&
                (cache.size(1) == 64 || cache.size(1) == 128) && cache.size(2) == 128 &&
                cache.stride(2) == 1 && cache.stride(1) == 128 && cache.stride(0) >= cache.size(1) * 128,
                "Expected actual nonoverlapping BF16 paged rows of128");
    TORCH_CHECK(blocks.scalar_type() == at::kInt && blocks.dim() == 2 && blocks.is_contiguous(), "Expected contiguous INT32 candidates");
    const int64_t Q = blocks.size(0), C = blocks.size(1);
    TORCH_CHECK(Q >= 1 && Q <= 6 && C >= 1 && C <= 4096, "Task query/candidate capacity exceeded");
    TORCH_CHECK(lengths.scalar_type() == at::kInt && lengths.dim() == 1 && lengths.size(0) == Q && lengths.is_contiguous(), "Expected INT32 lengths[Q]");
    TORCH_CHECK(tables.scalar_type() == at::kInt && tables.dim() == 2 && tables.size(0) == Q && tables.size(1) <= 4096 && tables.is_contiguous(), "Expected INT32 tables[Q,T<=4096]");
    TORCH_CHECK(active.scalar_type() == at::kBool && active.dim() == 1 && active.size(0) == Q && active.is_contiguous(), "Expected bool active[Q]");
    TORCH_CHECK(block_size == 8 || block_size == 16, "Expected8/16token blocks");
    TORCH_CHECK(run_capacity > 0 && run_capacity <= C && Q * run_capacity <= 1023, "Grouped capacity must fit1023 real groups");
    // Existing candidate callers reserve Q*host_bound+19, which can exceed
    // Q*candidate_width. Retain that static padding contract through32K.
    TORCH_CHECK(packed_capacity > 0 && packed_capacity <= Q * 32768 + 32, "Invalid bounded packed capacity");
    TORCH_CHECK(cache.storage_offset() >= 0 && cache.storage_offset() % 128 == 0, "Expected row-aligned actual storage offset");
    const auto storage_bytes = cache.storage().nbytes();
    TORCH_CHECK(storage_bytes <= static_cast<size_t>(std::numeric_limits<int64_t>::max()), "Storage byte count overflow");
    const int64_t elements = static_cast<int64_t>(storage_bytes / 2);
    // Bound every intermediate product before constructing signed64 device addresses.
    const int64_t tail = (cache.size(1) - 1) * 128 + 128;
    TORCH_CHECK(elements >= cache.storage_offset() && elements - cache.storage_offset() >= tail,
                "Paged view exceeds backing storage");
    TORCH_CHECK(cache.size(0) - 1 <= (elements - cache.storage_offset() - tail) / cache.stride(0),
                "Paged view extent exceeds backing storage");
    for (const auto* t : {&cache, &blocks, &lengths, &tables, &active}) {
        TORCH_CHECK(t->device() == cache.device(), "All native inputs must share one NPU");
        TORCH_CHECK(at_npu::native::get_npu_format(*t) == ACL_FORMAT_ND, "Expected ND raw addressing");
        TORCH_CHECK(reinterpret_cast<uintptr_t>(t->data_ptr()) % 32 == 0, "Expected32byte aligned base input");
    }
    const c10_npu::NPUGuard guard(cache.device());
    std::array<aclrtFuncHandle, 2> functions;
    {
        std::lock_guard<std::mutex> lock(mutex);
        for (int kind = 0; kind < 2; ++kind) {
            const auto it = loaded[kind].find(cache.device().index());
            TORCH_CHECK(it != loaded[kind].end(), "Prepare both task functions before capture");
            functions[kind] = it->second.function;
        }
    }
    const auto ints = blocks.options().dtype(at::kLong);
    auto keys = at::empty({packed_capacity, 128}, cache.options().dtype(at::kFloat));
    auto counts = at::empty({Q * run_capacity + 1}, ints);
    // Private output domains are zeroed once; only valid compact prefixes/runs are written.
    auto columns = at::zeros({packed_capacity}, ints);
    auto offsets = at::zeros({packed_capacity}, ints);
    auto lookup = at::zeros({Q * run_capacity, 128}, ints);
    auto errors = at::empty({Q}, active.options());
    auto meta = at::empty({4}, ints);
    auto restore = at::zeros({Q, C * block_size}, ints);
    for (const auto* t : {&keys, &counts, &columns, &offsets, &lookup, &errors, &meta, &restore}) {
        TORCH_CHECK(at_npu::native::get_npu_format(*t) == ACL_FORMAT_ND, "Expected ND task outputs");
    }
    const auto npu_stream = c10_npu::getCurrentNPUStream(cache.device().index());
    const auto stream = npu_stream.stream(false);
    for (const auto* t : std::array<const at::Tensor*, 13>{&cache, &blocks, &lengths, &tables, &active, &keys, &counts, &columns, &offsets, &lookup, &errors, &meta, &restore}) {
        c10_npu::NPUCachingAllocator::recordStream(t->storage().data_ptr(), npu_stream);
    }
    ScheduleArgs schedule{{blocks.data_ptr(), lengths.data_ptr(), tables.data_ptr(), active.data_ptr(),
                           counts.data_ptr(), columns.data_ptr(), offsets.data_ptr(), lookup.data_ptr(), errors.data_ptr(), meta.data_ptr(), restore.data_ptr()},
                          {Q, C, tables.size(1), run_capacity, packed_capacity, cache.size(1), block_size,
                           cache.stride(0), cache.storage_offset(), elements, cache.size(0)}};
    // Offsets include storage_offset, so use the allocation base, never view.data_ptr().
    GatherArgs gather{{cache.storage().data_ptr().get(), offsets.data_ptr(), meta.data_ptr(), keys.data_ptr()}, packed_capacity};
    const auto schedule_blocks = static_cast<uint32_t>(Q);
    const auto gather_blocks = static_cast<uint32_t>((packed_capacity + 15) / 16);
    at_npu::native::OpCommand command;
    // The pinned API explicitly avoids allocating a compact copy of the strided KV view.
    command.Name("DeepInferNativePagedPrepare").InputWithoutContiguous(cache)
        .Input(blocks).Input(lengths).Input(tables).Input(active)
        .Output(keys).Output(counts).Output(columns).Output(offsets).Output(lookup).Output(errors).Output(meta).Output(restore);
    // All captures are POD/raw pointers. Never destroy a Tensor under the task queue lock.
    command.SetCustomHandler([schedule, gather, functions, stream, gather_blocks, schedule_blocks]() mutable -> int {
        auto status = aclrtLaunchKernelWithHostArgs(functions[0], schedule_blocks, stream, nullptr, &schedule, sizeof(schedule), nullptr, 0);
        TORCH_CHECK(status == ACL_SUCCESS, "Native schedule launch failed: ", status);
        status = aclrtLaunchKernelWithHostArgs(functions[1], gather_blocks, stream, nullptr, &gather, sizeof(gather), nullptr, 0);
        TORCH_CHECK(status == ACL_SUCCESS, "Native paged gather launch failed: ", status);
        return status;
    });
    command.Run();
    return {keys, counts, columns, offsets, lookup, errors, meta, restore};
}
// Task-local selection ABI. No floating arithmetic before the explicit cast.
struct SelectArgs {
    std::array<void*, 8> pointers;
    std::array<int64_t, 3> dimensions;
};
static_assert(offsetof(SelectArgs, dimensions) == 64 && sizeof(SelectArgs) == 88, "Selection ABI changed");

std::vector<at::Tensor> select_scores(const at::Tensor& native, const at::Tensor& singleton,
                                    const at::Tensor& lengths, const at::Tensor& restore,
                                    const at::Tensor& row_errors) {
    TORCH_CHECK(native.device().type() == c10::DeviceType::PrivateUse1, "Expected NPU reduced scores");
    TORCH_CHECK(native.scalar_type() == at::kFloat && native.dim() == 2 && native.size(1) == 128 && native.is_contiguous(), "Expected FP32 native[G,128]");
    const int64_t G = native.size(0);
    TORCH_CHECK(G > 0 && G <= 1023, "Invalid native group capacity");
    TORCH_CHECK(singleton.scalar_type() == at::kFloat && singleton.dim() == 2 && singleton.size(0) == G && singleton.size(1) == 8 && singleton.is_contiguous(), "Expected FP32 singleton[G,8]");
    TORCH_CHECK(lengths.scalar_type() == at::kLong && lengths.dim() == 1 && lengths.size(0) == G + 1 && lengths.is_contiguous(), "Expected original INT64 counts[G+1]");
    TORCH_CHECK(row_errors.scalar_type() == at::kBool && row_errors.dim() == 1 && row_errors.is_contiguous(), "Expected bool row errors[Q]");
    const int64_t Q = row_errors.size(0);
    TORCH_CHECK(Q > 0 && Q <= 6 && G % Q == 0, "Expected private Q1..6 uniform group capacity");
    TORCH_CHECK(restore.scalar_type() == at::kLong && restore.dim() == 2 && restore.size(0) == Q && restore.is_contiguous(), "Expected contiguous INT64 restore[Q,width]");
    const int64_t width = restore.size(1);
    TORCH_CHECK(width > 0 && width <= 65536 && width % 8 == 0, "Expected8-column native block domain through65536");
    for (const auto* tensor : {&native, &singleton, &lengths, &restore, &row_errors}) {
        TORCH_CHECK(tensor->device() == native.device(), "Selection inputs must share one NPU");
        TORCH_CHECK(at_npu::native::get_npu_format(*tensor) == ACL_FORMAT_ND && reinterpret_cast<uintptr_t>(tensor->data_ptr()) % 32 == 0, "Expected aligned ND selection inputs");
    }
    const c10_npu::NPUGuard guard(native.device());
    aclrtFuncHandle function = nullptr;
    {
        std::lock_guard<std::mutex> lock(mutex);
        const auto found = loaded[2].find(native.device().index());
        TORCH_CHECK(found != loaded[2].end(), "Prepare selection before capture");
        function = found->second.function;
    }
    auto scores = at::empty({Q, width}, native.options());
    auto bf16 = at::empty({Q, width}, native.options().dtype(at::kBFloat16));
    // A private32-byte flag slot for each8-column half-tile preserves row ownership
    // even when a16-column tile straddles two rows of width8 modulo16.
    auto flags = at::empty({Q, width / 8, 8}, lengths.options().dtype(at::kInt));
    const auto npu_stream = c10_npu::getCurrentNPUStream(native.device().index());
    const auto stream = npu_stream.stream(false);
    for (const auto* tensor : std::array<const at::Tensor*, 8>{&native, &singleton, &lengths, &restore, &row_errors, &scores, &bf16, &flags}) {
        TORCH_CHECK(at_npu::native::get_npu_format(*tensor) == ACL_FORMAT_ND, "Expected ND selection outputs");
        c10_npu::NPUCachingAllocator::recordStream(tensor->storage().data_ptr(), npu_stream);
    }
    SelectArgs args{{native.data_ptr(), singleton.data_ptr(), lengths.data_ptr(), restore.data_ptr(), row_errors.data_ptr(), scores.data_ptr(), bf16.data_ptr(), flags.data_ptr()}, {Q, width, G}};
    const auto blocks = static_cast<uint32_t>((Q * width + 15) / 16);
    at_npu::native::OpCommand command;
    command.Name("DeepInferNativeScoreSelection").Input(native).Input(singleton).Input(lengths).Input(restore).Input(row_errors)
        .Output(scores).Output(bf16).Output(flags);
    command.SetCustomHandler([args, function, stream, blocks]() mutable -> int {
        const auto status = aclrtLaunchKernelWithHostArgs(function, blocks, stream, nullptr, &args, sizeof(args), nullptr, 0);
        TORCH_CHECK(status == ACL_SUCCESS, "Native score selection launch failed: ", status);
        return status;
    });
    command.Run();
    return {scores, bf16, flags};
}

} // namespace

TORCH_LIBRARY(deepinfer_indexer_prepare_rows_r1264, module) {
    module.def("prepare_schedule(str path, int device_index) -> ()");
    module.def("prepare_gather(str path, int device_index) -> ()");
    module.def("prepare_select(str path, int device_index) -> ()");
    module.def("select_scores(Tensor native, Tensor singleton, Tensor lengths, Tensor restore, Tensor row_errors) -> Tensor[]");
    module.def("run(Tensor cache, Tensor blocks, Tensor lengths, Tensor tables, Tensor active, int block_size, int run_capacity, int packed_capacity) -> Tensor[]");
}
TORCH_LIBRARY_IMPL(deepinfer_indexer_prepare_rows_r1264, CompositeExplicitAutograd, module) {
    module.impl("prepare_schedule", &prepare_schedule);
    module.impl("prepare_gather", &prepare_gather);
    module.impl("prepare_select", &prepare_select);
}
TORCH_LIBRARY_IMPL(deepinfer_indexer_prepare_rows_r1264, PrivateUse1, module) {
    module.impl("run", &run);
    module.impl("select_scores", &select_scores);
}
