"""Functional text/SSE usage check over an existing private loopback tunnel."""
import argparse
import json
import urllib.request
from urllib.parse import urlsplit

def main():
    p=argparse.ArgumentParser();p.add_argument('--base',default='http://127.0.0.1:8900/v1');a=p.parse_args()
    parsed=urlsplit(a.base)
    assert parsed.scheme=='http' and parsed.hostname in ['127.0.0.1','localhost'] and not parsed.username and not parsed.query,'USE_EXISTING_LOOPBACK_TUNNEL'
    op=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with op.open(a.base+'/models',timeout=15) as r:m=json.load(r)['data'][0]
    assert m['id']=='ds-v4.1-flash-1017' and m['max_model_len']==524288
    rows=[]
    prompt='cache-test '+('stable prefix block. '*2048)+' Reply exactly DEPLOY_OK.'
    for stage in ['cold','warm']:
        body={'model':m['id'],'messages':[{'role':'user','content':prompt}],'temperature':0,'max_completion_tokens':32,'stream':True,'stream_options':{'include_usage':True},'chat_template_kwargs':{'thinking':False}}
        req=urllib.request.Request(a.base+'/chat/completions',data=json.dumps(body).encode(),headers={'Content-Type':'application/json','X-data-parallel-rank':'0'})
        usage=None;text='';done=False
        with op.open(req,timeout=300) as r:
            for raw in r:
                line=raw.decode().strip()
                if line=='data: [DONE]':done=True;continue
                if not line.startswith('data: '):continue
                v=json.loads(line[6:]);usage=v.get('usage') or usage
                for c in v.get('choices',[]):text+=c.get('delta',{}).get('content') or ''
        cached=(usage.get('prompt_tokens_details') or {}).get('cached_tokens') if usage else None
        assert done and text.strip()=='DEPLOY_OK' and cached is not None
        if stage=='warm':assert cached>0,'WARM_CACHE_NOT_REPORTED'
        rows.append({'stage':stage,'text_correct':True,'usage_details_present':True,'cache_hit_observed':cached>0})
    print(json.dumps({'state':'PASS_FUNCTIONAL_ONLY','model':m['id'],'checks':rows}))

if __name__=='__main__':main()
