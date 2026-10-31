# Third-party notices

LLMDeploy original scripts, documentation, and original modifications are licensed under **AGPL-3.0-only**, as provided in the root `LICENSE`. This does not replace the licenses or copyright notices applicable to upstream code portions.

The following unified diffs retain portions of upstream **Apache License 2.0** source code:

| Patch | Upstream source | Pinned commit |
| --- | --- | --- |
| `patches/model-runner-eager-fallback.patch` | [vLLM](https://github.com/vllm-project/vllm), `vllm/v1/worker/gpu/model_runner.py` | `db9527a46873454610df6dbedf79a36d6bf1a7f6` |
| `patches/decoder-prefill-no-trimming.patch` | [vLLM Ascend](https://github.com/vllm-project/vllm-ascend), `vllm_ascend/models/deepseek_v41/decoder_replay_layers.py` | `f978348e9045fb045a4281d189339ef2ca1f1a45` |
| `patches/deepinfer/a2-910b-modelslim-w8a8.patch` | [DeepInfer Ascend fork](https://github.com/ai-v5/deepinfer_ascend), native 910B ModelSlim adapters and dispatch sites | `07e5002c08bce137ab2a86a5a1fc8bb652729ad6` |
| `patches/deepinfer/a2-910b-ep16-resident.patch` | [DeepInfer Ascend fork](https://github.com/ai-v5/deepinfer_ascend), incremental EP16/resident-expert and bounded operator changes after the base 910B diff | `07e5002c08bce137ab2a86a5a1fc8bb652729ad6` |
| `patches/deepinfer/a2-910b-w4a8-source.patch` | DeepInfer Ascend fork, W4A8 source changes on the two-patch baseline | `07e5002c08bce137ab2a86a5a1fc8bb652729ad6` |

The two vLLM source files carry the SPDX copyright notice "Copyright contributors to the vLLM project". Existing upstream copyright, SPDX, license and attribution notices remain in the source files when these patches are applied. The root project license does not remove those requirements. The Apache-2.0 license text is reproduced in [licenses/Apache-2.0.txt](licenses/Apache-2.0.txt).

LLMDeploy does not distribute the vLLM repositories, CANN, drivers, DSL binary dependencies, container images or model weights. Those dependencies remain subject to their respective upstream licenses and terms; this repository's license does not grant rights to those separate products.

The DeepInfer recipe references the [DeepInfer Ascend fork](https://github.com/ai-v5/deepinfer_ascend) (Apache-2.0) and [deepseek-recipe](https://github.com/deepseek-ai/deepseek-recipe) (MIT). Source preparation reads pinned public Git objects and preserves the DeepInfer license in the generated, ignored build context. Complete repositories, vendor dependencies, and built binaries are not vendored here; the listed 910B diffs include upstream source context. Preserve each upstream license and copyright notice when obtaining or redistributing them. Original 910B adapters and modifications in those diffs use the root AGPL-3.0-only license; retain that license with the patches when applying or redistributing them. This does not replace the licenses of upstream portions.

`scripts/deepinfer_ep16/serve_recipe_ep16.py` adapts the pinned fork's `examples/serve_recipe.py` for a sixteen-worker pool. Its upstream portions remain subject to Apache-2.0, whose text is included above; original control-plane code and modifications use AGPL-3.0-only. Preserve the upstream repository's license and applicable copyright notices along with this notice when redistributing these source portions. The EP16 recipe locks existing runtime artifacts but does not distribute their binaries or change their licenses.
