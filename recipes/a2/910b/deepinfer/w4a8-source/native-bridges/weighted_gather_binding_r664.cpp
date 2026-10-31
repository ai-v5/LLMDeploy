// SPDX-License-Identifier: AGPL-3.0-only
// Task-local bridge; CPU compile and runtime/numerical admission are separate gates.
#include <ATen/ATen.h>
#include <torch/library.h>
#include <acl/acl.h>
#include <torch_npu/csrc/core/npu/NPUGuard.h>
#include <torch_npu/csrc/core/npu/NPUStream.h>
#include <torch_npu/csrc/core/npu/NPUCachingAllocator.h>
#include <torch_npu/csrc/core/npu/NPUFormat.h>
#include <torch_npu/csrc/framework/OpCommand.h>
#include <array>
#include <cstdint>
#include <limits>
#include <map>
#include <mutex>
#include <string>
#include <tuple>

namespace {
struct Loaded {
    std::string path;
    aclrtBinHandle binary = nullptr;
    aclrtFuncHandle function = nullptr;
};
std::mutex load_mutex;
// Captured graphs can retain functions. Handles live until process exit.
std::array<std::map<c10::DeviceIndex, Loaded>, 2> loaded;

void prepare_impl(const std::string& path, int64_t device_index, bool singleton) {
    TORCH_CHECK(device_index >= 0 && device_index < 16, "Invalid task NPU device index");
    TORCH_CHECK(!path.empty(), "Expected a pinned kernel path");
    const auto device = static_cast<c10::DeviceIndex>(device_index);
    const c10_npu::NPUGuard guard(device);
    std::lock_guard<std::mutex> lock(load_mutex);
    auto& functions = loaded[singleton ? 1 : 0];
    const auto existing = functions.find(device);
    if (existing != functions.end()) {
        TORCH_CHECK(existing->second.path == path, "Different task kernel already loaded");
        return;
    }
    Loaded value;
    value.path = path;
    const auto status = aclrtBinaryLoadFromFile(path.c_str(), nullptr, &value.binary);
    TORCH_CHECK(status == ACL_SUCCESS, "Task kernel load failed: ", status);
    const char* symbol = singleton ? "weighted_gather1_r664__kernel0" : "weighted_gather128_r664__kernel0";
    const auto function_status = aclrtBinaryGetFunction(value.binary, symbol, &value.function);
    if (function_status != ACL_SUCCESS) {
        aclrtBinaryUnLoad(value.binary);
        TORCH_CHECK(false, "Task kernel function lookup failed: ", function_status);
    }
    functions.emplace(device, std::move(value));
}

void prepare(const std::string& path, int64_t device) { prepare_impl(path, device, false); }
void prepare1(const std::string& path, int64_t device) { prepare_impl(path, device, true); }

std::tuple<at::Tensor, at::Tensor> sum_impl(const at::Tensor& weighted,
                                          const at::Tensor& weights, const at::Tensor& columns, const at::Tensor& lookup, int64_t candidate_width, bool singleton) {
    TORCH_CHECK(weighted.device().type() == c10::DeviceType::PrivateUse1,
        "Expected NPU packed scores");
    TORCH_CHECK(lookup.device() == weighted.device(), "Expected same-device lookup");
    TORCH_CHECK(weighted.scalar_type() == at::kFloat && weighted.dim() == 2 &&
        weighted.size(1) == 32 && weighted.is_contiguous(), "Expected contiguous FP32 weighted[P,32]");
    TORCH_CHECK(lookup.scalar_type() == at::kLong && lookup.dim() == 2 &&
        lookup.size(1) == 128 && lookup.is_contiguous(), "Expected contiguous int64 lookup[G,128]");
    TORCH_CHECK(weights.device() == weighted.device() && columns.device() == weighted.device(), "All inputs must share the same NPU");
    TORCH_CHECK(weights.scalar_type() == at::kFloat && weights.dim() == 2 && weights.size(0) > 0 &&
        weights.size(1) == 32 && weights.is_contiguous(), "Expected FP32 query weights[Q,32]");
    TORCH_CHECK(columns.scalar_type() == at::kLong && columns.dim() == 1 &&
        columns.size(0) == weighted.size(0) && columns.is_contiguous(), "Expected int64 packed source columns[P]");
    TORCH_CHECK(candidate_width > 0 && candidate_width <= std::numeric_limits<int64_t>::max() / weights.size(0),
        "Invalid width or overflowing query-column domain");
    const auto groups = lookup.size(0);
    TORCH_CHECK(groups % weights.size(0) == 0, "Each query needs the same number of run groups");
    const auto packed_rows = weighted.size(0);
    TORCH_CHECK(groups > 0 && groups <= 1023, "Task group capacity must be in [1,1023]");
    TORCH_CHECK(packed_rows >= 0 && packed_rows <= std::numeric_limits<int64_t>::max() / 128,
        "Packed address arithmetic would overflow");
    const c10_npu::NPUGuard guard(weighted.device());
    for (const auto* tensor : {&weighted, &weights, &columns, &lookup}) {
        TORCH_CHECK(at_npu::native::get_npu_format(*tensor) == ACL_FORMAT_ND,
            "Expected ND storage for raw addressing");
        TORCH_CHECK(reinterpret_cast<uintptr_t>(tensor->data_ptr()) % 32 == 0,
            "Expected 32-byte aligned task DMA input");
    }
    aclrtFuncHandle function = nullptr;
    {
        std::lock_guard<std::mutex> lock(load_mutex);
        const auto& functions = loaded[singleton ? 1 : 0];
        const auto found = functions.find(weighted.device().index());
        TORCH_CHECK(found != functions.end(), "Call prepare before warmup or capture");
        function = found->second.function;
    }
    const auto blocks = static_cast<uint32_t>(groups * (singleton ? 1 : 16));
    // C1 padding stays zero; the kernel writes its first score. Errors have private DMA slots.
    auto output = at::zeros({groups, singleton ? 8 : 128}, weighted.options());
    auto errors = at::zeros({static_cast<int64_t>(blocks), 8}, lookup.options().dtype(at::kInt));
    // Actual P is generated from host shape metadata, never accepted as an unchecked caller tensor.
    auto tiling = at::full({4}, packed_rows, lookup.options());
    tiling.select(0, 1).fill_(weights.size(0));
    tiling.select(0, 2).fill_(candidate_width);
    tiling.select(0, 3).fill_(groups);
    for (const auto* tensor : {&output, &errors, &tiling}) {
        TORCH_CHECK(at_npu::native::get_npu_format(*tensor) == ACL_FORMAT_ND,
            "Expected ND output/tiling storage");
    }
    const auto npu_stream = c10_npu::getCurrentNPUStream(weighted.device().index());
    const auto stream = npu_stream.stream(false);
    const std::array<const at::Tensor*, 7> allocations{&weighted, &weights, &columns, &lookup, &output, &errors, &tiling};
    for (const auto* tensor : allocations) {
        c10_npu::NPUCachingAllocator::recordStream(tensor->storage().data_ptr(), npu_stream);
    }
    at_npu::native::OpCommand command;
    command.Name(singleton ? "DeepInferTaskWeightedGather1" : "DeepInferTaskWeightedGather128")
        .Input(weighted).Input(weights).Input(columns).Input(lookup).Input(tiling).Output(output).Output(errors);
    void* weighted_ptr = weighted.data_ptr();
    void* weights_ptr = weights.data_ptr();
    void* columns_ptr = columns.data_ptr();
    void* lookup_ptr = lookup.data_ptr();
    void* output_ptr = output.data_ptr();
    void* errors_ptr = errors.data_ptr();
    void* tiling_ptr = tiling.data_ptr();
    // No Tensor ownership in queued closures: destruction under enqueue lock can reenter it.
    command.SetCustomHandler([weighted_ptr, weights_ptr, columns_ptr, lookup_ptr, output_ptr, errors_ptr, tiling_ptr,
                              function, stream, blocks]() -> int {
        std::array<void*, 7> args{weighted_ptr, weights_ptr, columns_ptr, lookup_ptr, output_ptr, errors_ptr, tiling_ptr};
        const auto status = aclrtLaunchKernelWithHostArgs(function, blocks, stream,
            nullptr, args.data(), sizeof(args), nullptr, 0);
        TORCH_CHECK(status == ACL_SUCCESS, "Task gather kernel launch failed: ", status);
        return status;
    });
    command.Run();
    return std::make_tuple(output, errors);
}

std::tuple<at::Tensor, at::Tensor> sum128(const at::Tensor& weighted, const at::Tensor& weights, const at::Tensor& columns, const at::Tensor& lookup, int64_t candidate_width) {
    return sum_impl(weighted, weights, columns, lookup, candidate_width, false);
}
std::tuple<at::Tensor, at::Tensor> sum1(const at::Tensor& weighted, const at::Tensor& weights, const at::Tensor& columns, const at::Tensor& lookup, int64_t candidate_width) {
    return sum_impl(weighted, weights, columns, lookup, candidate_width, true);
}
} // namespace

TORCH_LIBRARY(deepinfer_weighted_gather_r664, module) {
    module.def("prepare(str path, int device_index) -> ()");
    module.def("prepare1(str path, int device_index) -> ()");
    module.def("sum128(Tensor dots, Tensor weights, Tensor columns, Tensor lookup, int candidate_width) -> (Tensor, Tensor)");
    module.def("sum1(Tensor dots, Tensor weights, Tensor columns, Tensor lookup, int candidate_width) -> (Tensor, Tensor)");
}
TORCH_LIBRARY_IMPL(deepinfer_weighted_gather_r664, CompositeExplicitAutograd, module) {
    module.impl("prepare", &prepare);
    module.impl("prepare1", &prepare1);
}
TORCH_LIBRARY_IMPL(deepinfer_weighted_gather_r664, PrivateUse1, module) {
    module.impl("sum128", &sum128);
    module.impl("sum1", &sum1);
}
