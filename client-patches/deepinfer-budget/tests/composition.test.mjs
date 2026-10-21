// Load the complete adapter layout through Cordis, including the original entry.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import http from 'node:http';
import { pathToFileURL } from 'node:url';
const modules = process.env.DEEPINFER_RUNTIME_MODULES;
const sdk = process.env.DEEPINFER_SDK_CLIENT;
const patched = process.env.DEEPINFER_PATCHED_DIR;
const root = process.env.DEEPINFER_TEST_ROOT;
const enabled = modules && sdk && patched && root;
const split = process.env.DEEPINFER_TEST_SPLIT_LAYOUT === '1';

for (const [provider, model] of [['deepinfer', 'deepseek-v4.1-flash-1017'], ['local950dt', 'ds-v4.1-flash-1017'], ['other', 'other-model']]) {
  test(`complete Cordis composition routes ${provider} through one adapter`, { skip: !enabled, timeout: 60000 }, async () => {
    const { DeepSeekHarness } = await import(pathToFileURL(sdk).href);
    const yaml = (await import(pathToFileURL(path.join(modules, 'js-yaml/index.js')).href)).default;
    const work = fs.mkdtempSync(path.join(root, 'composition-'));
    const calls = [];
    const server = http.createServer(async (request, response) => {
      let body = '';
      for await (const chunk of request) body += chunk;
      calls.push(JSON.parse(body));
      response.writeHead(200, { 'Content-Type': 'text/event-stream' });
      response.end('data: ' + JSON.stringify({ id: 'fixture', object: 'chat.completion.chunk', choices: [{ index: 0, delta: { content: 'COMPOSITION_OK' }, finish_reason: 'stop' }] }) + '\n\ndata: [DONE]\n\n');
    });
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const profile = id => ({ api: 'openai-completions', apiKeyEnv: 'COMPOSITION_FIXTURE_KEY',
      baseURL: `http://127.0.0.1:${server.address().port}/v1`, models: [{ id, contextWindow: 1048576, maxTokens: 32768, input: ['text'] }] });
    const targets = { deepinfer: profile('deepseek-v4.1-flash-1017'), local950dt: profile('ds-v4.1-flash-1017') };
    const other = { other: profile('other-model') };
    const overlay = path.join(work, 'overlay.yml');
    fs.writeFileSync(overlay, yaml.dump([{ id: 'llm-deepseek', disabled: true }, { insert: [
      { id: 'llm-pi-ai-original', name: '@deepseek-ai/dsh-llm-pi-ai', disabled: !split, config: { providers: other } },
      { id: 'llm-pi-ai-budget', name: path.join(patched, 'llm-pi-ai.mjs'), config: { providers: split ? targets : { ...other, ...targets } } },
    ] }]));
    const harness = new DeepSeekHarness({ dshBin: path.join(modules, '@deepseek-ai/dsh/lib/bin.js'), profile: 'sdk-minimal',
      patches: [overlay], dshHome: path.join(work, 'home'), processCwd: work,
      env: { ...process.env, COMPOSITION_FIXTURE_KEY: 'fixture-only' }, provider, model, initializeTimeoutMs: 20000, requestTimeoutMs: 30000 });
    try {
      const result = await harness.run('Reply COMPOSITION_OK without tools.');
      assert.equal(result.finalResponse, 'COMPOSITION_OK');
      assert.equal(result.events.findLast(e => e.type === 'turn/end').data.reason.kind, 'completed');
      assert.equal(calls.length, 1);
      assert.equal(calls[0].max_tokens ?? calls[0].max_completion_tokens, 32768);
      console.log(JSON.stringify({ provider, singleAdapter: !split, routed: true, completion: 32768 }));
    } finally {
      await harness.close(); server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
    }
  });
}
