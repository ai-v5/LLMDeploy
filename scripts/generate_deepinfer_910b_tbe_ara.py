"""Generate the pinned 910B TBE ARA baseline in an isolated empty /workspace."""
# SPDX-License-Identifier: AGPL-3.0-only
import hashlib
import importlib
import json
from pathlib import Path
import sys
import time
import traceback

P = Path('/workspace')
if Path.cwd() != P or not P.is_dir():
    raise RuntimeError('Use a dedicated /workspace as the working directory')
allowed = {'compile.py', 'home', 'tmp'}
if any(entry.name not in allowed or entry.is_symlink() for entry in P.iterdir()):
    raise RuntimeError('Workspace must be new and contain only compile.py, home and tmp')
for name in ('home', 'tmp'):
    entry = P / name
    if entry.exists() and (not entry.is_dir() or any(entry.iterdir())):
        raise RuntimeError('Workspace home and tmp must be empty directories')
entry = P / 'compile.py'
if entry.exists() and (not entry.is_file() or entry.read_bytes() != Path(__file__).read_bytes()):
    raise RuntimeError('Workspace compile.py must match this script')
report = dict(epoch=time.time(), scope='CPU_ONLY_INSTALLED_ARA_SCHEDULER_EXPLICIT_CASE_NOT_NATIVE_BINARY_OR_NUMERICAL_ADMISSION')
try:
    from tbe import tvm, dsl
    from tbe.common.platform import set_current_compile_soc_info
    from tbe.common.context.op_context import OpContext
    from tbe.common.buildcfg import build_config
    from tbe.dsl.base import operation
    from tbe.dsl.unify_schedule import util
    from tbe.dsl.unify_schedule.constants import Pattern, ReducePattern, ReduceSchType

    pins = {
        'tbe.dsl.unify_schedule.vector.reduce.reduce_ara_precision_schedule': '6b16748ed88605a6088160cd7451188c73071c61fe351e6ac3091887a7a24757',
        'tbe.dsl.unify_schedule.vector.reduce.reduce_base_precision_schedule': '6fa516bfda5543ef2cceed272169c3ec1e6b883181b863ce4c4970518ae3b82f',
        'tbe.dsl.unify_schedule.vector.reduce.reduce_tilingcase': '4aa11bc102b1d82fe809fda4958d55a9df4365373eeaffc2e563f96f536af840',
        'tbe.dsl.unify_schedule.vector.reduce.vector_info': 'cca81fedeb2685a368be5a9d8ec9e7c37f2bfcc079dcaaf20d058e0889694f33',
    }
    modules = {}
    for name, digest in pins.items():
        module = importlib.import_module(name)
        assert hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest() == digest
        modules[name.rsplit('.', 1)[-1]] = module
    set_current_compile_soc_info('Ascend910B1', 'VectorCore')
    report['installed_module_sha256'] = pins
    with OpContext('dynamic'):
        # The explicit fixed compute has reduction axis 1. Initialise its true
        # axis metadata before the installed common-info helper, as its source requires.
        operation.add_compile_info_inner('_ori_axis', [1])
        with operation.compute() as compute:
            compute.set_pattern(Pattern.REDUCE)
            compute.set_sub_pattern(ReducePattern.R_0)
            compute.add('_mode', 'const')
            with tvm.target.cce():
                data = tvm.placeholder((1, 32, 128), name='weighted', dtype='float32')
                result = dsl.reduce_sum(data, axis=1, keepdims=False)
                graph = modules['vector_info'].ComputeGraphInfo([result])
                info = modules['reduce_tilingcase'].SingleReduceInfo(graph)
                compute.add('_compute_graph_info', graph)
                compute.add('_single_reduce_info', info)
                modules['reduce_tilingcase'].apply_common_compile_info(graph, info)
                case = modules['reduce_tilingcase'].ReduceTilingCase()
                case.reduce_sch_type = ReduceSchType.ARA_HIGH_PRECISION
                case.block_split_axis_index = 2
                case.block_factor = 8
                case.ub_split_axis_index = 1
                case.ub_factor = 32
                case.ub_factor_2 = 1
                case.multi_core = True
                case.tiling_key = 82100900
                graph.get_maximum_subgraph(graph, info, case)
                outs = [result]
                with operation.schedule() as schedule_context:
                    schedule_context.add('_sch_idx', 0)
                    implementation = modules['reduce_ara_precision_schedule'].ReduceARANormalPrecisionSchedule(graph, info, case, outs)
                    schedule = implementation.do_schedule()
                    schedule.tiling_key = case.tiling_key
                    util.add_sch_additional_entry(schedule, 'original_outs', [result])
                    util.add_sch_additional_entry(schedule, 'real_outs', outs)
                    util.add_sch_additional_entry(schedule, 'context', schedule_context)
                report['rfactor_shape'] = [int(x) for x in implementation.reduce_rf_tensor.shape]
                report['reduce_attrs'] = implementation.ara_reduce_attr
                report['case'] = dict(block_axis=2, block_factor=8, ub_axis=1, ub_factor=32,
                    ub_factor_2=1, tiling_key=case.tiling_key, db=case.db,
                    storage_before=case.tensor_ub_size_before_reduce,
                    storage_after=case.tensor_ub_size_after_reduce)
        with build_config(tbe_debug_level=1):
            dsl.build([[schedule]], dict(name='reduce_installed_ara_r532', tensor_list=[[data, result]],
                print_ir=True, need_build=True))
    generated = P / 'kernel_meta/reduce_installed_ara_r532/reduce_installed_ara_r532_82100900.cce'
    if not generated.is_file() or generated.is_symlink():
        raise RuntimeError('Expected TBE baseline source was not generated')
    report['generated_sha256'] = hashlib.sha256(generated.read_bytes()).hexdigest()
    if report['generated_sha256'] != '888c2eb97f8017e386ffc4a1352f2b451c8c464cff5a2b7bc48716bee6f09eec':
        raise RuntimeError('TBE baseline source checksum mismatch')
    report.update(status='CPU_INSTALLED_ARA_SCHEDULE_CODEGEN_COMPLETE_NOT_NATIVE_IDENTITY_OR_RUNTIME_GO',
        input_shape=[1, 32, 128], axis=1, block_dim_intent=16, columns_each_block=8,
        scope_note='Installed scheduler unchanged. The manually supplied tiling case is motivated by R506 logs; this is not native binary binding, addition-tree identity or numerical validation.')
except BaseException as error:
    report.update(status='CPU_INSTALLED_ARA_SCHEDULE_CODEGEN_FAILED_NO_DEVICE_EXECUTION',
        error=repr(error), traceback=traceback.format_exc())
    raise
finally:
    assert 'torch' not in sys.modules and 'torch_npu' not in sys.modules
    report['torch_imported'] = False
    report['files'] = [dict(path=str(path.relative_to(P)), bytes=path.stat().st_size,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest()) for path in sorted(P.rglob('*'))
        if path.is_file() and not path.is_symlink()]
    with (P / 'result.json').open('x') as stream:
        json.dump(report, stream, indent=2)
    print(json.dumps(dict(status=report['status'], files=len(report['files']), error=report.get('error'))), flush=True)
