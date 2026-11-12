# SPDX-License-Identifier: AGPL-3.0-only
# PREPARATION_NOT_REBUILT_NOT_NPU. AOT compilation is a separate CPU-only step.
FROM --platform=linux/arm64 quay.io/ascend/vllm-ascend@sha256:10f4e41479604545b74f3684e1a98e9e7116eff6adc72ecaec765d2dbecc7e81

# Use a dedicated, explicitly staged context; never the whole checkout.
COPY cannbot_dsl-0.7.1-cp312-cp312-manylinux_2_28_aarch64.whl /deps/
COPY cannbot_arena_net-0.3.0-cp312-cp312-linux_aarch64.whl /operator/
COPY LICENSE.cannbot /usr/local/share/licenses/cannbot-arena-net/LICENSE
COPY dependencies.json verify_runtime.py /opt/llmdeploy/pd-runtime/
RUN python3 /opt/llmdeploy/pd-runtime/verify_runtime.py --install-only \
    --framework-dir /deps --operator-dir /operator \
    --license-file /usr/local/share/licenses/cannbot-arena-net/LICENSE

ENV HOME=/root TORCH_DEVICE_BACKEND_AUTOLOAD=1 OPERATOR_WHEEL=""
