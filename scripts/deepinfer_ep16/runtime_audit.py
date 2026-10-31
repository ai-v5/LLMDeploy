"""Read native output counters before their existing consumer releases pointers.

No imports of Torch or accelerator initialization at module import time.
Counters subtract the mandatory golden token from native draft/accept fields.
"""
import hashlib,json,time

def install(engine,output_type,memory,record,path):
    originals={name:getattr(engine,name) for name in ('step','flush_inflight_reqs')}
    record['runtime_audit']={'steps':0,'requests':{},'memory_scope':'Allocator values sampled after engine calls; peaks since audit installation, not driver/global H2D proof'}
    audit=record['runtime_audit']
    def publish():
        record['updated_epoch']=time.time();audit['memory']=memory()
        temporary=path.with_suffix('.tmp')
        temporary.write_text(json.dumps(record,indent=2));temporary.replace(path)
    def wrap(original):
        def call(*args,**kwargs):
            started=time.perf_counter()
            pointers=original(*args,**kwargs)
            audit['steps']+=1;audit['last_engine_call_seconds']=time.perf_counter()-started
            for pointer in pointers:
                identifier=output_type.get_request_id(pointer)
                key=hashlib.sha256(identifier.encode()).hexdigest()[:24]
                row=audit['requests'].setdefault(key,{'outputs':0,'draft_tokens':0,'accepted_draft_tokens':0,'golden_outputs':0,'finished':False})
                draft=output_type.get_draft_tok(pointer);accepted=output_type.get_accept_tok(pointer)
                assert draft>=0 and accepted>=0 and accepted<=draft,(draft,accepted)
                row['outputs']+=1
                if draft>0:
                    row['golden_outputs']+=1;row['draft_tokens']+=draft-1;row['accepted_draft_tokens']+=accepted-1
                    assert accepted>=1,(draft,accepted)
                else:assert accepted==0,(draft,accepted)
                row['finished']=row['finished'] or output_type.get_finished(pointer)
            if len(pointers)>0 or audit['steps']%8==0:publish()
            return pointers
        return call
    for name,original in originals.items():setattr(engine,name,wrap(original))
    publish()
    def restore():
        for name,original in originals.items():setattr(engine,name,original)
    return restore
