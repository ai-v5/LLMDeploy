# This is the historical serving overlay, not a clean CANN/native build.
# Import the audited native/API parent before building; it is not a registry tag.
ARG BASE_IMAGE=sha256:d43af9a352c2668624d8fdd53e9f9101ead4486f1ef1e125e8bca743accbe3ce
FROM ${BASE_IMAGE}
RUN /opt/deepinfer-api-venv/bin/python -c "import hashlib,pathlib; p=pathlib.Path('/opt/deepinfer-build/source/deepinfer/_so/ascend-aarch64/_C.cpython-312-aarch64-linux-gnu.so'); assert hashlib.sha256(p.read_bytes()).hexdigest()=='1d2da40023fae852577e719bc7494ea7effe391f25b0adf3dbe0ddb80fc927d7'"
COPY source/deepinfer/ve/prepare_tensor_input_vl.py /opt/deepinfer-build/source/deepinfer/ve/prepare_tensor_input_vl.py
COPY source/examples/recipe_api/http.py /opt/deepinfer-build/source/examples/recipe_api/http.py
COPY source/examples/serve_recipe.py /opt/deepinfer-build/source/examples/serve_recipe.py
COPY LICENSE.DeepInfer /opt/llmdeploy/licenses/LICENSE.DeepInfer
LABEL org.opencontainers.image.source="https://github.com/ai-v5/deepinfer_ascend"
LABEL org.opencontainers.image.revision="07e5002c08bce137ab2a86a5a1fc8bb652729ad6"
# The LLMDeploy renderer supplies the explicit service command and port.
