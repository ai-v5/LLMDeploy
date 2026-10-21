// Runs the actual installed adapter and native counter; HTTP is a local fixture.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import http from 'node:http';
import { pathToFileURL } from 'node:url';
import { countPayload } from '../guard.mjs';

const modules = process.env.DEEPINFER_RUNTIME_MODULES;
const patched = process.env.DEEPINFER_PATCHED_DIR;
const command = process.env.DEEPINFER_COUNTER;
const tokenizer = process.env.DEEPINFER_TOKENIZER;
const enabled = modules && patched && command && tokenizer;

for (const [provider, model] of [['deepinfer', 'deepseek-v4.1-flash-1017'], ['local950dt', 'ds-v4.1-flash-1017']]) {
  test(`real adapter preflight: ${provider}, reported overflow never reaches HTTP`, { skip: !enabled, timeout: 90000 }, async () => {
    const load = file => import(pathToFileURL(path.join(modules, file)).href);
    const { createUserMessage, createSystemMessage } = await load('@deepseek-ai/dsh-llm/lib/index.js');
    const module = await import(pathToFileURL(path.join(patched, 'llm-pi-ai.mjs')).href);
    const received = [];
    const server = http.createServer(async (request, response) => {
      let body = '';
      for await (const chunk of request) body += chunk;
      received.push(JSON.parse(body));
      response.writeHead(200, { 'Content-Type': 'text/event-stream' });
      response.end('data: ' + JSON.stringify({ id: 'fixture', object: 'chat.completion.chunk', choices: [{ index: 0, delta: { content: 'OK' }, finish_reason: 'stop' }] }) + '\n\ndata: [DONE]\n\n');
    });
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    let adapter;
    const ctx = { fiber: { entry: { options: { id: 'budget-fixture' } } }, inject() {}, on() {},
      get: name => name === 'credentials' ? { async resolve() { return { value: 'fixture-only' }; }, async readRecord() {} } : undefined,
      logger: { warn() {}, error() {} }, llm: { registerConfigurableProviders() { return { replace() {} }; },
        registerModelDiscovery() {}, registerAdapter(_config, value) { adapter = value; return { replace() {} }; } } };
    module.apply(ctx, module.Config({ providers: { [provider]: { api: 'openai-completions', apiKeyEnv: 'FIXTURE',
      baseURL: `http://127.0.0.1:${server.address().port}/v1`, models: [{ id: model, contextWindow: 1048576, maxTokens: 32768, input: ['text'] }] } } }));
    const run = async repeats => {
      const messages = [createSystemMessage('Budget validation.'), createUserMessage({ content: [{ type: 'text', text: ' a'.repeat(repeats) }], source: { kind: 'user' } })];
      const before = JSON.stringify(messages);
      const chunks = [];
      for await (const chunk of adapter.stream({ provider, model, messages, maxTokens: 32768, signal: AbortSignal.timeout(80000) })) chunks.push(chunk);
      assert.equal(JSON.stringify(messages), before, 'admission must not truncate input');
      return chunks.at(-1);
    };
    try {
      await run(100);
      assert.equal(received.length, 1);
      const payload = received.pop();
      const overhead = await countPayload(payload, { command, tokenizer }) - 100;
      for (const target of [1025090, 999424]) {
        const finish = await run(target - overhead);
        assert.equal(received.length, 0, `${provider} sent an oversized request`);
        assert.equal(finish.reason?.failure?.code, 'CONTEXT_WINDOW_EXCEEDED');
      }
      await run(999423 - overhead);
      assert.equal(received.length, 1);
      assert.equal(received[0].max_tokens ?? received[0].max_completion_tokens, 32768);
      assert.equal(await countPayload(received[0], { command, tokenizer }), 999423);
      console.log(JSON.stringify({ provider, overflow: 1025090, safetyBoundary: 999424, accepted: 999423, contextWindow: 1048576, completion: 32768 }));
    } finally {
      server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
    }
  });
}
