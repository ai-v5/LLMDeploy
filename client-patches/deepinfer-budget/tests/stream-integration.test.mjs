// Exercises the generated adapter against a local SSE fixture, never a model.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const modules = process.env.DEEPINFER_RUNTIME_MODULES;
const patched = process.env.DEEPINFER_PATCHED_DIR;

test('the local DeepInfer transport survives a shorter global body timeout',
  { skip: !modules || !patched, timeout: 15000 }, async () => {
    const load = file => import(pathToFileURL(path.join(modules, file)).href);
    const llm = await load('@deepseek-ai/dsh-llm/lib/index.js');
    const undici = await load('undici/index.js');
    const adapterModule = await import(pathToFileURL(path.join(patched, 'llm-pi-ai.mjs')).href);
    const previousDispatcher = undici.getGlobalDispatcher();
    const shortDispatcher = new undici.Agent({ headersTimeout: 5000, bodyTimeout: 400 });
    undici.setGlobalDispatcher(shortDispatcher);

    const payloads = [];
    let cancelOnResponse;
    const server = http.createServer(async (request, response) => {
      const body = [];
      for await (const part of request) body.push(part);
      payloads.push(JSON.parse(Buffer.concat(body).toString('utf8')));
      response.writeHead(200, { 'Content-Type': 'text/event-stream' });
      const chunk = { id: 'fixture', object: 'chat.completion.chunk', created: 1, model: 'fixture-model',
        choices: [{ index: 0, delta: { role: 'assistant' }, finish_reason: null }] };
      response.write('data: ' + JSON.stringify(chunk) + '\n\n');
      if (cancelOnResponse) setTimeout(() => cancelOnResponse.abort(), 100);
      const timer = setTimeout(() => {
        chunk.choices[0].delta = { content: 'ok' };
        response.write('data: ' + JSON.stringify(chunk) + '\n\n');
        chunk.choices[0].delta = {};
        chunk.choices[0].finish_reason = 'stop';
        response.end('data: ' + JSON.stringify(chunk) + '\n\ndata: [DONE]\n\n');
      }, 1400);
      response.on('close', () => clearTimeout(timer));
    });
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    let adapter;
    const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/aWQAAAAASUVORK5CYII=', 'base64');
    const attachments = { imageHostPath() {}, async readImageRequest(ref) {
      return { variantId: 'sha256:' + 'b'.repeat(64), attachment: ref, data: png, mediaType: 'image/png',
        bytes: png.length, width: 1, height: 1, depth: 'uchar', space: 'srgb', hasAlpha: true };
    } };
    const ctx = { fiber: { entry: { options: { id: 'stream-fixture' } } }, inject() {}, on() {},
      get: name => name === 'credentials' ? { async resolve() { return { value: 'fixture-only' }; }, async readRecord() {} }
        : name === 'attachments' ? attachments : undefined,
      logger: { warn() {}, error() {} }, llm: { registerConfigurableProviders() { return { replace() {} }; },
        registerModelDiscovery() {}, registerAdapter(_config, value) { adapter = value; return { replace() {} }; } } };
    const profile = { api: 'openai-completions', apiKeyEnv: 'FIXTURE',
      baseURL: `http://127.0.0.1:${server.address().port}/v1`, streamIdleTimeoutMs: 3000, timeoutMs: 5000,
      models: [{ id: 'fixture-model', contextWindow: 32768, maxTokens: 128, input: ['text', 'image'] }] };
    const extraProvider = process.env.DEEPINFER_TEST_EXTRA_PROVIDER;
    const providers = { local950dt: profile, unrelated: profile, ...(extraProvider ? { [extraProvider]: profile } : {}) };
    adapterModule.apply(ctx, adapterModule.Config({ providers }));
    const run = async (provider, messages = [llm.createUserMessage({ content: [{ type: 'text', text: 'hello' }], source: { kind: 'user' } })], signal = AbortSignal.timeout(6000)) => {
      const chunks = [];
      for await (const chunk of adapter.stream({ provider, model: 'fixture-model', messages, maxTokens: 32,
        signal })) chunks.push(chunk);
      return chunks.find(chunk => chunk.type === 'finish')?.reason;
    };
    try {
      assert.equal((await run('unrelated')).failure?.code, 'TRANSPORT');
      assert.equal((await run('local950dt')).kind, 'stop');
      if (extraProvider) assert.equal((await run(extraProvider)).kind, 'stop');
      assert.equal(undici.getGlobalDispatcher(), shortDispatcher);
      const ref = { attachmentId: 'sha256:' + 'a'.repeat(64), mediaType: 'image/png', bytes: png.length, width: 1, height: 1 };
      const messages = [llm.createAssistantMessage({ content: [{ type: 'tool-call', id: 'fixture-call', name: 'inspect', arguments: '{}' }],
        source: { provider: 'local950dt', model: 'fixture-model' } }),
      llm.createToolResultMessage({ callId: 'fixture-call', content: [{ type: 'text', text: 'screenshot' }, { type: 'image', attachment: ref }], isError: false })];
      assert.equal((await run('local950dt', messages)).kind, 'stop');
      const images = payloads.at(-1).messages.flatMap(message => Array.isArray(message.content) ? message.content : [])
        .filter(block => block.type === 'image_url');
      assert.equal(images.length, 1);
      assert.ok(images[0].image_url.url.startsWith('data:image/png;base64,'));
      cancelOnResponse = new AbortController();
      const cancelled = await run('local950dt', undefined, cancelOnResponse.signal);
      assert.equal(cancelled.kind, 'aborted');
      assert.equal(cancelled.failure?.code, 'ABORTED');
    } finally {
      server.closeAllConnections();
      await new Promise(resolve => server.close(resolve));
      undici.setGlobalDispatcher(previousDispatcher);
      await shortDispatcher.close();
    }
  });
