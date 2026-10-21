// Opt-in composition test: runs the shipped DSH SDK profile against a local HTTP fixture.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import http from 'node:http';
import { pathToFileURL } from 'node:url';
const modules = process.env.DEEPINFER_RUNTIME_MODULES;
const sdk = process.env.DEEPINFER_SDK_CLIENT;
const patched = process.env.DEEPINFER_PATCHED_DIR;
const testRoot = process.env.DEEPINFER_TEST_ROOT;
const enabled = modules && sdk && patched && testRoot;

for (const [provider, model] of [['deepinfer', 'deepseek-v4.1-flash-1017'], ['local950dt', 'ds-v4.1-flash-1017']]) {
for (const scenario of ['recover', 'still-overflow', 'summary-overflow']) {
  test(`real DSH loop: ${provider} ${scenario}, bounded retry and paired tools`, { skip: !enabled, timeout: 90000 }, async () => {
    const { DeepSeekHarness } = await import(pathToFileURL(sdk).href);
    const yaml = (await import(pathToFileURL(path.join(modules, 'js-yaml/index.js')).href)).default;
    const work = fs.mkdtempSync(path.join(testRoot, scenario + '-'));
    const calls = [];
    const server = http.createServer(async (request, response) => {
      let bytes = '';
      for await (const chunk of request) bytes += chunk;
      const payload = JSON.parse(bytes);
      const summary = JSON.stringify(payload.messages.at(-1)).includes('acting as a compaction engine');
      calls.push({ summary, payload });
      const conversationCount = calls.filter(call => !call.summary).length;
      if ((!summary && (conversationCount === 1 || scenario === 'still-overflow'))
        || (summary && scenario === 'summary-overflow')) {
        response.writeHead(400, { 'Content-Type': 'application/json' });
        response.end(JSON.stringify({ error: { type: 'invalid_request_error', message: 'maximum context length is 1048576; messages=1016142, completion=32768; total=1048910' } }));
        return;
      }
      response.writeHead(200, { 'Content-Type': 'text/event-stream' });
      const content = summary ? 'RECOVERY CHECKPOINT' : 'DSH_RECOVERED';
      response.end('data: ' + JSON.stringify({ id: 'fixture', object: 'chat.completion.chunk', choices: [{ index: 0, delta: { content }, finish_reason: null }] })
        + '\n\ndata: ' + JSON.stringify({ id: 'fixture', object: 'chat.completion.chunk', choices: [{ index: 0, delta: {}, finish_reason: 'stop' }] }) + '\n\ndata: [DONE]\n\n');
    });
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const fixture = path.join(work, 'seed.mjs');
    fs.writeFileSync(fixture, `import { createUserMessage, createAssistantMessage, createToolResultMessage } from ${JSON.stringify(pathToFileURL(path.join(modules, '@deepseek-ai/dsh-llm/lib/index.js')).href)};
export const name='budget-recovery-fixture'; export const inject=['agents'];
export function apply(ctx){ctx.on('agent/created',({agent})=>{
for(let i=0;i<8;i++){
agent.session.append('user/message',createUserMessage({content:[{type:'text',text:'OLD_HISTORY_'+i+' archive '.repeat(500)}],source:{kind:'user'}}),{surfaceOp:'append'});
if(i===3||i===6){const id='fixture-call-'+i;
agent.session.append('step/start',{turn:0,step:i});
agent.session.append('assistant/message',{turn:0,step:i,message:createAssistantMessage({content:[{type:'tool-call',id,name:'lookup',arguments:'{}'}],source:{provider:${JSON.stringify(provider)},model:${JSON.stringify(model)}}}),stream:[]},{surfaceOp:'append'});
agent.session.append('tool/result',{turn:0,step:i,message:createToolResultMessage({callId:id,content:[{type:'text',text:'paired result '.repeat(100)}],isError:false})},{surfaceOp:'append'});
agent.session.append('step/end',{turn:0,step:i});
}}});}`);
    const patch = [
      { id: 'llm-deepseek', disabled: true },
      { id: 'compaction-basic', disabled: true },
      { insert: [
        { id: 'llm-pi-ai-budget', name: path.join(patched, 'llm-pi-ai.mjs'), config: { providers: { [provider]: {
          api: 'openai-completions', apiKeyEnv: 'BUDGET_FIXTURE_KEY', baseURL: `http://127.0.0.1:${server.address().port}/v1`,
          models: [{ id: model, contextWindow: 1048576, maxTokens: 32768, input: ['text'] }],
        } } } },
        { id: 'compaction-budget', name: path.join(patched, 'compaction-basic.mjs'), config: { modelPolicies: [
          { provider, model, headroomTokens: 16384, maxTokens: 32768, maxOverflowRetries: 1 },
        ] } },
        { id: 'fixture', name: fixture },
      ] },
    ];
    const overlay = path.join(work, 'overlay.yml');
    fs.writeFileSync(overlay, yaml.dump(patch));
    const harness = new DeepSeekHarness({ dshBin: path.join(modules, '@deepseek-ai/dsh/lib/bin.js'), profile: 'sdk-minimal',
      patches: [overlay], dshHome: path.join(work, 'home'), processCwd: work, env: { ...process.env, BUDGET_FIXTURE_KEY: 'fixture-only' },
      provider, model, initializeTimeoutMs: 30000, requestTimeoutMs: 60000 });
    try {
      const result = await harness.run('Reply DSH_RECOVERED without tools.');
      const conversation = calls.filter(call => !call.summary);
      assert.equal(calls.filter(call => call.summary).length, 1, JSON.stringify(result.events.filter(event => event.type === 'turn/end' || event.type === 'compaction/end').map(event => event.data)));
      assert.equal(conversation.length, scenario === 'summary-overflow' ? 1 : 2);
      for (const { payload } of calls) {
        assert.equal(payload.max_tokens ?? payload.max_completion_tokens, 32768);
        const pending = new Set();
        for (const message of payload.messages) {
          for (const call of message.tool_calls ?? []) pending.add(call.id);
          if (message.role === 'tool') assert.ok(pending.delete(message.tool_call_id), 'tool result must have its call');
        }
        assert.equal(pending.size, 0, 'compaction must keep whole tool pairs');
      }
      if (scenario !== 'summary-overflow') {
        assert.notDeepEqual(conversation[0].payload.messages, conversation[1].payload.messages);
        assert.ok(JSON.stringify(conversation[1].payload.messages).includes('RECOVERY CHECKPOINT'));
      }
      const summary = calls.find(call => call.summary);
      assert.ok(JSON.stringify(summary.payload.messages).length < JSON.stringify(conversation[0].payload.messages).length,
        'overflow recovery must summarize a bounded prefix rather than replaying all oversized history');
      assert.equal(result.finalResponse, scenario === 'recover' ? 'DSH_RECOVERED' : '');
      const reason = result.events.findLast(event => event.type === 'turn/end').data.reason;
      assert.equal(reason.kind, scenario === 'recover' ? 'completed' : 'error');
      console.log(JSON.stringify({ provider, scenario, conversationRequests: conversation.length, summaryRequests: 1, final: reason.kind }));
    } finally {
      await harness.close(); server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
    }
  });
}
}
