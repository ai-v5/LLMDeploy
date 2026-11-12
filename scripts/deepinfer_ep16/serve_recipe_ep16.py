"""Serve the official recipe protocols using resident native DeepInfer workers."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _exit_failed_service(failed: bool) -> None:
    if failed:
        # Native runtimes can leave non-daemon threads after worker failure.
        # Serving and pool cleanup have finished; hand the failure to the
        # process supervisor without waiting for interpreter finalizers.
        import os

        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, help="complete local checkpoint directory")
    parser.add_argument("--served-model-name", default="deepseek-v4.1-flash-1017")
    parser.add_argument("--model-alias", action="append", default=[], help="additional accepted model IDs for this checkpoint")
    parser.add_argument("--worker-python", default=sys.executable, help="Python with the built DeepInfer dependencies")
    parser.add_argument("--port", type=int, default=28334)
    parser.add_argument("--max-model-len", type=int, default=1048576)
    parser.add_argument("--max-output-tokens", type=int, default=32768)
    parser.add_argument("--max-images-per-request", type=int, default=999)
    parser.add_argument("--max-num-batched-tokens", type=int, default=2048)
    parser.add_argument("--max-num-seqs", type=int, default=4)
    parser.add_argument("--gpu-memory-utilization", type=float, default=0.95)
    parser.add_argument("--prefill-any-interval", type=int, default=1)
    parser.add_argument("--eplb-interval", type=int, default=256)
    parser.add_argument("--eplb-after-prefill", action="store_true")
    parser.add_argument(
        "--dspark",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="confidence-scheduled variable-length verification; --no-dspark keeps fixed-length MTP drafting",
    )
    parser.add_argument(
        "--warmup-workload", help="local JSON containing chat-completion request groups; admission waits until all passes complete"
    )
    args = parser.parse_args()

    import asyncio
    import json
    import logging
    from contextlib import asynccontextmanager
    from dataclasses import replace

    import uvicorn
    from deepseek_recipe import ConversionOptions, DeepseekV41Encoding, ModelVersion, SpecialToken, SpecialTokens, Tokenizer
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse, PlainTextResponse
    from recipe_api.backend import EngineConfig, EnginePool
    from recipe_api.http import PreparedRequest, RequestError, create_app, prepare_request
    from recipe_api.prefix_cache import prefix_digests
    from recipe_api.warmup import warmup_requests
    from starlette.concurrency import run_in_threadpool

    model = Path(args.model).resolve()
    tokenizer = Tokenizer.from_file(str(model / "tokenizer.json"))
    special_tokens = SpecialTokens(image="<｜deepseek_image｜>")
    served_names = list(dict.fromkeys([args.served_model_name, *args.model_alias]))
    encoding = DeepseekV41Encoding().with_special_tokens(special_tokens).with_tokenizer(tokenizer)
    if args.prefill_any_interval < 1 or args.eplb_interval < 0:
        parser.error("prefill interval must be positive and EPLB interval nonnegative")
    if args.max_model_len < 1 or args.max_num_batched_tokens < 1 or args.max_output_tokens < 1 or args.max_images_per_request < 1 or args.max_num_seqs < 1:
        parser.error("context, image, sequence and token limits must be positive")
    config = EngineConfig(
        ep=16,
        dp=16,
        model=str(model),
        max_model_len=args.max_model_len,
        gpu_memory_utilization=args.gpu_memory_utilization,
        max_num_batched_tokens=args.max_num_batched_tokens,
        max_num_seqs=args.max_num_seqs,
        max_output_tokens=args.max_output_tokens,
        dspark=args.dspark,
        prefill_any_interval=args.prefill_any_interval,
        eplb_interval=args.eplb_interval,
        eplb_after_prefill=args.eplb_after_prefill,
    )
    pool = EnginePool(config, args.worker_python)
    serving_ready = asyncio.Event()
    startup = {"phase": "starting"}

    def prepare_backend(request: PreparedRequest, *, internal: bool = False) -> dict:
        if not pool.available():
            raise RequestError("DeepInfer workers are unavailable", 503)
        if not internal and not serving_ready.is_set():
            raise RequestError("DeepInfer is warming up; retry when health is ready", 503)
        converted = request.conversation_request
        token_ids = encoding.encode(converted.conversation)
        prompt_len = len(token_ids) + request.image_token_adjustment
        max_tokens = converted.resolve_max_tokens(prompt_len, config.max_model_len)
        if max_tokens > config.max_output_tokens:
            raise RequestError("output exceeds the configured maximum output tokens")
        if prompt_len + max_tokens > config.max_model_len:
            raise RequestError("prompt and output exceed the configured context limit")
        options = request.inference_options
        so_args = None
        constraint = options.output_constraint
        if constraint is not None:
            if constraint.kind == "tool_call_required":
                raise RequestError("required tool-call constrained decoding is not supported by this adapter")
            if constraint.kind in ("json", "regex"):
                so_args = {"schema_type": constraint.kind, "schema": constraint.schema or "{}"}
                thinking = converted.parsing_options.thinking_initial_stage
                if thinking is not None and thinking.starts_in_reasoning():
                    so_args["trigger_token_ids"] = tokenizer.encode(SpecialToken.ThinkingEnd.default_str())
        return {
            "affinity": request.affinity,
            "affinity_strong": request.affinity_strong,
            "prefix_digests": prefix_digests(token_ids, config.max_num_batched_tokens // pool.block_size * pool.block_size)
            if not request.images and so_args is None
            else {},
            "token_ids": token_ids,
            "logprobs": options.logprobs,
            "sampling": {
                "temperature": options.temperature,
                "top_p": options.top_p,
                "max_tokens": max_tokens,
                "ignore_eos": request.ignore_eos,
                "top_logprobs": options.top_logprobs or 0,
                "seq_seed": options.seed,
                "so_args": so_args,
            },
            "multi_modal_data": {
                "image_bytes": [image.data for image in request.images],
                "image_sizes": [(image.width, image.height) for image in request.images],
            }
            if request.images
            else None,
        }

    options = (
        ConversionOptions()
        .with_special_tokens(special_tokens)
        .with_model_versions_by_name(
            {
                **dict.fromkeys(served_names, ModelVersion.DeepseekV41Flash),
                "DeepSeek-V4.1-Flash": ModelVersion.DeepseekV41Flash,
                "deepseek-flash": ModelVersion.DeepseekV41Flash,
            }
        )
    )
    app = create_app(
        pool.infer, tokenizer=tokenizer, prepare_backend=prepare_backend, options=options, default_max_tokens=config.max_output_tokens,
        max_images_per_request=args.max_images_per_request,
    )

    async def warmup() -> None:
        try:
            startup["phase"] = "warming"

            async def prepare(body: dict) -> PreparedRequest:
                prepared = await run_in_threadpool(
                    prepare_request,
                    "chat_completions",
                    json.dumps(body).encode(),
                    options=options,
                    default_max_tokens=config.max_output_tokens,
                    max_images_per_request=args.max_images_per_request,
                )
                native = await run_in_threadpool(prepare_backend, prepared, internal=True)
                return replace(prepared, backend_input=native)

            def report_warmup(value: dict) -> None:
                startup["warmup"] = value

            async with asyncio.timeout(1800):
                startup["warmup"] = await warmup_requests(args.warmup_workload, prepare, pool.infer, progress=report_warmup)
            startup["phase"] = "ready"
            serving_ready.set()
            logging.getLogger("uvicorn.error").info(
                "DeepInfer warmup complete: %d requests in %.3fs", startup["warmup"]["requests"], startup["warmup"]["seconds"]
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            startup["failed"] = True
            startup["phase"] = "warmup_failed"
            # Do not log request bodies or exception values containing input.
            logging.getLogger("uvicorn.error").error(
                "DeepInfer startup warmup failed (%s); external admission remains closed", type(error).__name__
            )
            server.should_exit = True

    async def watch_workers() -> None:
        while True:
            await asyncio.sleep(1)
            if not pool.available():
                startup["failed"] = True
                serving_ready.clear()
                startup["phase"] = "worker_failed"
                logging.getLogger("uvicorn.error").error("DeepInfer worker failed; exiting for service-manager recovery")
                server.should_exit = True
                return

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        task = None
        watcher = None
        try:
            # Linux parent-death signals follow the thread that spawned a child.
            # Start on the service's main thread, whose lifetime owns the workers.
            pool.start()
            watcher = asyncio.create_task(watch_workers())
            if args.warmup_workload:
                task = asyncio.create_task(warmup())
            else:
                startup["phase"] = "ready"
                serving_ready.set()
            yield
        finally:
            serving_ready.clear()
            if watcher is not None:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await pool.aclose()

    app.router.lifespan_context = lifespan

    @app.get("/health")
    async def health() -> JSONResponse:
        ready = pool.available() and serving_ready.is_set()
        return JSONResponse(
            {
                "status": "ready" if ready else startup["phase"],
                "engine": "deepinfer",
                "workers": config.dp,
                "warmup_requests": startup.get("warmup", {}).get("requests", 0),
                "warmup_total_requests": startup.get("warmup", {}).get("total_requests", 0),
            },
            status_code=200 if ready else 503,
        )

    @app.get("/v1/models")
    async def models() -> dict:
        return {
            "object": "list",
            "data": [
                {
                    "id": name,
                    "object": "model",
                    "owned_by": "deepseek",
                    "max_model_len": config.max_model_len,
                    "max_output_tokens": config.max_output_tokens,
                    "max_images_per_request": args.max_images_per_request,
                    "dspark": config.dspark,
                }
                for name in served_names
            ],
        }

    @app.get("/metrics")
    async def metrics() -> PlainTextResponse:
        # Running counts an admitted native request until completion/abort ACK.
        values = pool.statistics()
        values["admission_ready"] = int(pool.available() and serving_ready.is_set())
        return PlainTextResponse(
            "".join(
                f"# TYPE deepinfer:{key} {'counter' if key.endswith('_total') else 'gauge'}\ndeepinfer:{key} {value}\n"
                for key, value in values.items()
            )
        )

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port, timeout_graceful_shutdown=30))
    server.run()
    _exit_failed_service(bool(startup.get("failed")))


if __name__ == "__main__":
    main()
