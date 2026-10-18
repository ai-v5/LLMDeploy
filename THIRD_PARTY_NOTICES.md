# Third-party notices

LLMDeploy original scripts, documentation, and original modifications are licensed under **AGPL-3.0-only**, as provided in the root `LICENSE`. This does not replace the licenses or copyright notices applicable to upstream code portions.

The following unified diffs retain portions of upstream **Apache License 2.0** source code:

| Patch | Upstream source | Pinned commit |
| --- | --- | --- |
| `patches/model-runner-eager-fallback.patch` | [vLLM](https://github.com/vllm-project/vllm), `vllm/v1/worker/gpu/model_runner.py` | `db9527a46873454610df6dbedf79a36d6bf1a7f6` |
| `patches/decoder-prefill-no-trimming.patch` | [vLLM Ascend](https://github.com/vllm-project/vllm-ascend), `vllm_ascend/models/deepseek_v41/decoder_replay_layers.py` | `f978348e9045fb045a4281d189339ef2ca1f1a45` |

Both corresponding source files carry the SPDX copyright notice "Copyright contributors to the vLLM project". Existing upstream copyright, SPDX, license and attribution notices remain in the source files when these patches are applied. The root project license does not remove those requirements. The Apache-2.0 license text is reproduced in [licenses/Apache-2.0.txt](licenses/Apache-2.0.txt).

LLMDeploy does not distribute the vLLM repositories, CANN, drivers, DSL binary dependencies, container images or model weights. Those dependencies remain subject to their respective upstream licenses and terms; this repository's license does not grant rights to those separate products.
