// SPDX-License-Identifier: AGPL-3.0-only
// Experimental capture-owned output lifetime; strict device admission pending.
#include <ATen/ATen.h>
#include <torch/library.h>
#include <acl/acl.h>
#include <torch_npu/csrc/core/npu/NPUGuard.h>
#include <torch_npu/csrc/core/npu/NPUStream.h>
#include <torch_npu/csrc/core/npu/NPUCachingAllocator.h>
#include <torch_npu/csrc/core/npu/NPUFormat.h>
#include <torch_npu/csrc/framework/OpCommand.h>
#include <torch_npu/csrc/core/npu/NPUGraphsUtils.h>
#include <array>
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <map>
#include <mutex>
#include <string>
#include <vector>

namespace {
struct Loaded { std::string path; aclrtBinHandle binary; aclrtFuncHandle function; };
std::mutex mutex;
std::map<c10::DeviceIndex, Loaded> loaded;
std::array<std::atomic<int64_t>, 16> gather_calls{};
std::array<std::atomic<int64_t>, 16> captured_calls{};
std::array<std::atomic<int64_t>, 16> owned_output_skips{};

std::vector<int64_t> stats(int64_t index) {
    TORCH_CHECK(index >= 0 && index < 16, "Invalid prefill statistics device");
    return {gather_calls[index].load(), captured_calls[index].load(), owned_output_skips[index].load()};
}


void prepare(const std::string& path, int64_t index) {
    TORCH_CHECK(index >= 0 && index < 16 && !path.empty(), "Invalid prefill kernel path/device");
    const auto device = static_cast<c10::DeviceIndex>(index);
    const c10_npu::NPUGuard guard(device);
    std::lock_guard<std::mutex> lock(mutex);
    const auto old = loaded.find(device);
    if (old != loaded.end()) {
        TORCH_CHECK(old->second.path == path, "Different prefill kernel already loaded");
        return;
    }
    Loaded value{path, nullptr, nullptr};
    TORCH_CHECK(aclrtBinaryLoadFromFile(path.c_str(), nullptr, &value.binary) == ACL_SUCCESS, "Prefill binary load failed");
    const auto status = aclrtBinaryGetFunction(value.binary, "indexer_prefill_gather_r798__kernel0", &value.function);
    if (status != ACL_SUCCESS) {
        aclrtBinaryUnLoad(value.binary);
        TORCH_CHECK(false, "Prefill function lookup failed: ", status);
    }
    loaded.emplace(device, std::move(value));  // Retain captured function until process exit.
}

struct Args {
    std::array<void*, 7> pointers;
    std::array<int64_t, 2> dimensions;
};
static_assert(sizeof(void*) == 8 && offsetof(Args, dimensions) == 56 && sizeof(Args) == 72, "Prefill gather ABI changed");

std::vector<at::Tensor> gather(const at::Tensor& physical, const at::Tensor& rows,
                              const at::Tensor& geometry, const at::Tensor& count,
                              const at::Tensor& logical) {
    TORCH_CHECK(physical.device().type() == c10::DeviceType::PrivateUse1, "Expected NPU physical KV rows");
    TORCH_CHECK(physical.scalar_type() == at::kBFloat16 && physical.dim() == 2 && physical.size(1) == 128 &&
                physical.size(0) > 0 && physical.size(0) <= (int64_t{1} << 40) && physical.is_contiguous() &&
                physical.storage_offset() == 0, "Expected BF16 allocation-base physical[F,128]");
    TORCH_CHECK(rows.scalar_type() == at::kLong && rows.dim() == 2 && rows.size(0) == 32768 &&
                rows.size(1) == 2 && rows.is_contiguous(), "Expected original INT64 load map[32768,2]");
    TORCH_CHECK(geometry.scalar_type() == at::kLong && geometry.dim() == 1 && geometry.size(0) == 5 &&
                geometry.is_contiguous(), "Expected original INT64 geometry[5]");
    TORCH_CHECK(count.scalar_type() == at::kInt && count.numel() == 1 && count.is_contiguous(), "Expected INT32 logical count scalar");
    TORCH_CHECK(logical.scalar_type() == at::kLong && logical.dim() == 1 && logical.is_contiguous() &&
                logical.size(0) <= 131072, "Expected contiguous INT64 selected logical rows through32x4096");
    const c10_npu::NPUGuard guard(physical.device());
    for (const auto* tensor : {&physical, &rows, &geometry, &count, &logical}) {
        TORCH_CHECK(tensor->device() == physical.device(), "Prefill gather inputs must share one NPU");
        TORCH_CHECK(at_npu::native::get_npu_format(*tensor) == ACL_FORMAT_ND, "Expected ND prefill inputs");
        TORCH_CHECK(reinterpret_cast<uintptr_t>(tensor->data_ptr()) % 32 == 0, "Expected aligned prefill input base");
    }
    aclrtFuncHandle function = nullptr;
    {
        std::lock_guard<std::mutex> lock(mutex);
        const auto found = loaded.find(physical.device().index());
        TORCH_CHECK(found != loaded.end(), "Prepare prefill gather before capture");
        function = found->second.function;
    }
    const int64_t P = logical.size(0);
    const auto blocks = static_cast<uint32_t>(P == 0 ? 1 : (P + 15) / 16);
    const bool capture_active = c10_npu::currentStreamCaptureStatusMayInitCtx() == c10_npu::CaptureStatus::Active;
    const auto device_index = physical.device().index();
    gather_calls[device_index].fetch_add(1, std::memory_order_relaxed);
    if (capture_active) captured_calls[device_index].fetch_add(1, std::memory_order_relaxed);
    auto keys = at::empty({P, 128}, physical.options());
    auto flags = at::empty({blocks, 8}, rows.options().dtype(at::kInt));
    const auto npu_stream = c10_npu::getCurrentNPUStream(physical.device().index());
    const auto stream = npu_stream.stream(false);
    for (const auto* tensor : std::array<const at::Tensor*, 7>{&physical, &rows, &geometry, &count, &logical, &keys, &flags}) {
        TORCH_CHECK(at_npu::native::get_npu_format(*tensor) == ACL_FORMAT_ND, "Expected ND prefill output");
        // These outputs were just allocated on this capture stream. Its FIFO
        // and private graph pool own their reuse; recording this same stream
        // would defer every intermediate until capture ends. Inputs can come
        // from another stream and retain the original recordStream contract.
        const bool owned_output = tensor == &keys || tensor == &flags;
        if (capture_active && owned_output) {
            owned_output_skips[device_index].fetch_add(1, std::memory_order_relaxed);
        } else if (tensor->storage().nbytes() != 0) {
            c10_npu::NPUCachingAllocator::recordStream(tensor->storage().data_ptr(), npu_stream);
        }
    }
    Args args{{physical.data_ptr(), rows.data_ptr(), geometry.data_ptr(), count.data_ptr(), logical.data_ptr(), keys.data_ptr(), flags.data_ptr()}, {P, physical.size(0)}};
    at_npu::native::OpCommand command;
    command.Name("DeepInferNativePrefillPagedGather").InputWithoutContiguous(physical)
        .Input(rows).Input(geometry).Input(count).Input(logical).Output(keys).Output(flags);
    command.SetCustomHandler([args, function, stream, blocks]() mutable -> int {
        const auto status = aclrtLaunchKernelWithHostArgs(function, blocks, stream, nullptr, &args, sizeof(args), nullptr, 0);
        TORCH_CHECK(status == ACL_SUCCESS, "Prefill gather launch failed: ", status);
        return status;
    });
    command.Run();
    return {keys, flags};
} // gather
} // namespace

TORCH_LIBRARY(deepinfer_prefill_gather_r827, module) {
    module.def("prepare(str path, int device_index) -> ()");
    module.def("stats(int device_index) -> int[]");
    module.def("gather(Tensor physical, Tensor rows, Tensor geometry, Tensor count, Tensor logical) -> Tensor[]");
}
TORCH_LIBRARY_IMPL(deepinfer_prefill_gather_r827, CompositeExplicitAutograd, module) { module.impl("prepare", &prepare); module.impl("stats", &stats); }
TORCH_LIBRARY_IMPL(deepinfer_prefill_gather_r827, PrivateUse1, module) { module.impl("gather", &gather); }
