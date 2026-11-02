import shutil
import subprocess
import unittest
from pathlib import Path


class TrendTooltipTests(unittest.TestCase):
    def test_hover_reports_exact_bucket_value_and_preserves_gaps(self):
        node = shutil.which('node')
        if node is None:
            self.skipTest('Node.js is required to validate dashboard interactions')
        script = r'''
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(process.argv[1],'utf8');
function element(tag,attrs={}) {return {tag,attrs,children:[],textContent:'',
  replaceChildren(){this.children=[];},appendChild(x){this.children.push(x);},
  classList:{add(){},remove(){}},
  setAttribute(k,v){this.attrs[k]=v;},removeAttribute(k){delete this.attrs[k];},
  getBoundingClientRect(){return {left:100,top:100,width:800,height:68};}};}
const tooltip={hidden:true,textContent:'',style:{},getBoundingClientRect:()=>({width:180,height:60})};
const elements={'trend-tooltip':tooltip};
for(const id of ['input','output','cache-rate','device-prefill','device-decode']){
  elements[`chart-${id}`]=element('label');elements[`chart-${id}-svg`]=element('svg');
}
const ctx={$:(id)=>elements[id]||null,svgElement:element,setTextIfPresent:()=>{},
  document:{documentElement:{clientWidth:1200,clientHeight:800}},window:{innerWidth:1200,innerHeight:800},
  number:{format:String},integer:{format:String},count:String,speed:(v)=>`${v} tok/s`,percent:(v)=>`${v*100}%`,
  clock:{format:(v)=>`time${v.getTime()/1000}`},dateTime:{format:(v)=>`date${v.getTime()/1000}`}};
function extract(name){const a=source.indexOf(`function ${name}(`),b=source.indexOf('\n}\n',a);
  assert(a>=0&&b>a,`missing ${name}`);return vm.runInNewContext('('+source.slice(a,b+2)+')',ctx);}
ctx.attachTrendTooltip=extract('attachTrendTooltip');
const render=extract('renderPlot'),renderDevice=extract('renderDeviceHistory');
const points=[{start:0,end:600,input:120,known:true,complete:true},
  {start:600,end:1200,input:43,known:true,complete:false},
  {start:1200,end:1800,input:null,known:false,complete:false}];
render({id:'input',field:'input',kind:'tokens',label:'输入'},points);
let svg=elements['chart-input-svg'];
svg.onpointermove({clientX:100,clientY:130});
assert.equal(tooltip.hidden,false);assert.match(tooltip.textContent,/输入.*120 Token/);
assert.match(tooltip.textContent,/date0.*date600/);
points[0].input=121;render({id:'input',field:'input',kind:'tokens',label:'输入'},points);
assert.equal(tooltip.hidden,false);assert.match(tooltip.textContent,/121 Token/); // Refresh while hovered.
svg.onpointermove({clientX:500,clientY:130});assert.match(tooltip.textContent,/至少 43 Token/);
svg.onpointermove({clientX:900,clientY:130});assert.equal(tooltip.hidden,true); // Gap is not zero.
svg.onpointermove({clientX:100,clientY:130});svg.onpointerleave();assert.equal(tooltip.hidden,true);
svg.onfocus();assert.equal(tooltip.hidden,false);assert.match(tooltip.textContent,/121 Token/);
svg.onkeydown({key:'ArrowRight',preventDefault(){}});assert.match(tooltip.textContent,/43 Token/);
points[1].input=null;points[1].known=false;
render({id:'input',field:'input',kind:'tokens',label:'输入'},points);
assert.equal(tooltip.hidden,true); // Focused bucket disappeared during refresh.
render({id:'input',field:'input',kind:'tokens',label:'输入'},points); // Remains missing through another refresh.
svg.onkeydown({key:'ArrowLeft',preventDefault(){}});assert.match(tooltip.textContent,/121 Token/);
svg.onblur();assert.equal(tooltip.hidden,true);
const output=[{start:0,end:600,output:456,known:true,complete:true}];
render({id:'output',field:'output',kind:'tokens',label:'输出'},output);
svg=elements['chart-output-svg'];svg.onpointermove({clientX:100,clientY:130});
assert.match(tooltip.textContent,/输出.*456 Token/);
const hit=[{start:0,end:600,known:true,complete:true,mean_request_hit_rate:.25,request_hit_sum:.5,request_hit_samples:2}];
render({id:'cache-rate',field:'mean_request_hit_rate',kind:'hit',label:'平均请求命中率',samples:'request_hit_samples'},hit);
svg=elements['chart-cache-rate-svg'];svg.onpointermove({clientX:100,clientY:130});
assert.match(tooltip.textContent,/平均请求命中率.*25%.*2 个样本/);
const device=Array.from({length:144},(_,i)=>({start:i*600,end:(i+1)*600,
  prefill_tps:null,prefill_samples:0,prefill_groups:0,decode_tps:null,decode_samples:0,decode_groups:0}));
device[20].decode_tps=200;device[20].decode_samples=1;device[20].decode_groups=1;
device[20].prefill_tps=1200;device[20].prefill_samples=2;device[20].prefill_groups=2;
renderDevice({available:true,history_available:true},{history:{method:'deepinfer-log-step-approx-v1',
  bucket_seconds:600,aggregate:true,group_count:2,points:device}});
svg=elements['chart-device-prefill-svg'];svg.onpointermove({clientX:100+20/143*800,clientY:130});
assert.match(tooltip.textContent,/Prefill.*1200 tok\/s[\s\S]*2\/2 组/);
elements['chart-input-svg'].onfocus(); // Keyboard focus may remain while another chart is hovered.
svg=elements['chart-device-decode-svg'];svg.onpointermove({clientX:100+20/143*800,clientY:130});
assert.match(tooltip.textContent,/Decode.*200 tok\/s[\s\S]*1\/2 组.*部分数据/);
render({id:'input',field:'input',kind:'tokens',label:'输入'},points);
assert.match(tooltip.textContent,/Decode.*200 tok\/s/); // Unrelated refresh cannot steal the tooltip.
svg.onpointermove({clientX:100+21/143*800,clientY:130});assert.equal(tooltip.hidden,true);
renderDevice({available:true,history_available:false},null);
assert.equal(tooltip.hidden,true);assert.equal(svg.onpointermove,null);
// A failed statistics refresh must clear the previous tooltip and handlers.
render({id:'input',field:'input',kind:'tokens',label:'输入'},points);
svg=elements['chart-input-svg'];svg.onpointermove({clientX:100,clientY:130});assert.equal(tooltip.hidden,false);
ctx.$=(id)=>elements[id]||(elements[id]=element('div'));
Object.assign(ctx,{revision:0,pendingRefresh:null,activeExpiry:null,lastSnapshot:null,selected:'24h',
  tables:{requests:{offset:0,size:20,sort:'finished',direction:'desc'},
          sessions:{offset:0,size:20,sort:'last_finished',direction:'desc'}},
  charts:[{id:'input'},{id:'output'},{id:'cache-rate'}],empty:()=>{},updatePager:()=>{},
  AbortController,URLSearchParams,setTimeout:()=>0,clearTimeout:()=>{},fetch:async()=>{throw Error('synthetic failure');}});
const start=source.indexOf('async function refresh('),end=source.indexOf('\n}\n',start);
assert(start>=0&&end>start);
const refresh=vm.runInNewContext('('+source.slice(start,end+2)+')',ctx);
refresh().then(()=>{assert.equal(tooltip.hidden,true);assert.equal(svg.onpointermove,null);
  assert.equal(elements['chart-input'].textContent,'暂无数据');
  renderDevice({available:true,history_available:true},{history:{method:'deepinfer-log-step-approx-v1',
    bucket_seconds:600,aggregate:true,group_count:2,points:device}});
  svg=elements['chart-device-decode-svg'];svg.onpointermove({clientX:100+20/143*800,clientY:130});
  return refresh();
}).then(()=>{assert.equal(tooltip.hidden,false);assert.match(tooltip.textContent,/Decode.*200 tok\/s/);
}).catch((error)=>{console.error(error);process.exitCode=1;});
'''
        source = Path(__file__).resolve().parents[1] / 'deploy_stats/static/app.js'
        result = subprocess.run([node, '-e', script, str(source)], capture_output=True, text=True,
                                encoding='utf-8', timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
