"""Task-local admission receipts for actual resident experts before API ready."""
import json,os,time,traceback
from pathlib import Path
from recipe_api.backend import worker

def resident_worker(rank,config,jobs,events,cancel,python_path):
    # The original worker establishes visibility before any torch/NPU import.
    import _config
    original=_config.create_llm
    root=Path('/workspace/receipts')
    run=os.environ['DEEPINFER_CONTROL_RUN_ID']
    if not run.replace('-','').isalnum():raise ValueError('Invalid task run name')
    record={'rank':rank,'run':run,'epoch':time.time(),'scope':'REAL_API_RESIDENT_POINTER_DIAGNOSTIC_NOT_GLOBAL_H2D_PROFILE','projection_calls':0}
    restores=[]
    def create(*args,**kwargs):
        llm=original(*args,**kwargs)
        import torch
        from deepinfer.npu_ops.moe910b import ModelSlimMoE
        from deepinfer.npu_ops.w8a8 import W8A8Linear
        model=llm.llm_engine.driver_worker.model_runner.model
        moes=[m for m in model.modules() if isinstance(m,ModelSlimMoE)]
        projections=[v for m in moes for e in [*m.routed.experts.values(),m.shared] for v in (e.w1,e.w2,e.w3)]
        assert len(moes)==43 and sum(len(m.routed.experts) for m in moes)==984 and len(projections)==3081
        for v in projections:
            assert not v.offload and v._resident is not None and not hasattr(v,'weight_cache')
            assert v.weight.device.type==v.scale.device.type=='npu'
        pointers={id(v):(v.weight.data_ptr(),v.scale.data_ptr()) for v in projections}
        quantized=W8A8Linear.quantized
        def checked(v,*args,**kwargs):
            if id(v) in pointers:
                assert not v.offload and v._resident is not None
                assert pointers[id(v)]==(v.weight.data_ptr(),v.scale.data_ptr())
                record['projection_calls']+=1
            return quantized(v,*args,**kwargs)
        W8A8Linear.quantized=checked;restores.append(lambda:setattr(W8A8Linear,'quantized',quantized))
        record.update(status='READY_RESIDENT_EXPERTS',moe_layers=43,routed_experts=984,shared_experts=43,projections=3081,
            expert_weight_scale_bytes=sum(v.weight.numel()*v.weight.element_size()+v.scale.numel()*v.scale.element_size() for v in projections),
            allocated=torch.npu.memory_allocated(),reserved=torch.npu.memory_reserved(),free_total=list(torch.npu.mem_get_info()),config=config.__dict__)
        with (root/f'{run}-resident-rank{rank}.json').open('x') as f:json.dump(record,f,indent=2)
        from deepinfer._C.engine import RequestOutput
        from runtime_audit import install
        torch.npu.reset_peak_memory_stats()
        def memory():
            return {'allocated':torch.npu.memory_allocated(),'reserved':torch.npu.memory_reserved(),
                'peak_allocated':torch.npu.max_memory_allocated(),'peak_reserved':torch.npu.max_memory_reserved()}
        restores.append(install(llm.llm_engine,RequestOutput,memory,record,root/f'{run}-runtime-rank{rank}.json'))
        return llm
    _config.create_llm=create
    try:worker(rank,config,jobs,events,cancel,python_path)
    except BaseException:
        record['wrapper_error']=traceback.format_exc();raise
    finally:
        _config.create_llm=original
        for f in restores:f()
        record['exit_epoch']=time.time()
        with (root/f'{run}-resident-exit-rank{rank}.json').open('x') as f:json.dump(record,f,indent=2)
