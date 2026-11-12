import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { createStreamTransport, isLoopbackHttp } from '../stream-transport.mjs';
const { prepareStream } = createRequire(import.meta.url)('../prepare-stream.cjs');

test('an existing output directory is never overwritten', () => {
  assert.throws(() => prepareStream({ output: process.cwd() }), /Output already exists/);
});

test('an unsupported runtime is rejected before creating an output', () => {
  const local = path.resolve('local');
  fs.mkdirSync(local, { recursive: true });
  const root = fs.mkdtempSync(path.join(local, 'stream-runtime-fixture-'));
  const namespace = path.join(root, '@deepseek-ai');
  const adapter = path.join(namespace, 'dsh-llm-pi-ai');
  const compaction = path.join(namespace, 'dsh-compaction-basic');
  const fixture = path.join(root, 'fixture');
  try {
    fs.mkdirSync(adapter, { recursive: true });
    fs.mkdirSync(compaction);
    fs.writeFileSync(path.join(adapter, 'package.json'), '{"version":"unsupported"}');
    fs.writeFileSync(path.join(compaction, 'package.json'), '{"version":"unsupported"}');
    fs.writeFileSync(fixture, 'fixture');
    const output = path.join(root, 'output');
    assert.throws(() => prepareStream({ runtimeModules: root, output, counter: fixture, tokenizer: fixture, guard: fixture }), /Unsupported DSH runtime version/);
    assert.equal(fs.existsSync(output), false);
  } finally {
    fs.unlinkSync(path.join(adapter, 'package.json'));
    fs.unlinkSync(path.join(compaction, 'package.json'));
    fs.unlinkSync(fixture);
    fs.rmdirSync(adapter);
    fs.rmdirSync(compaction);
    fs.rmdirSync(namespace);
    fs.rmdirSync(root);
  }
});

test('only literal loopback HTTP endpoints use the local transport', () => {
  for (const url of ['http://127.0.0.1:12345/v1', 'http://localhost/v1', 'http://[::1]:9000/v1']) {
    assert.equal(isLoopbackHttp(url), true, url);
  }
  for (const url of ['https://example.com/v1', 'http://localhost.example.com/v1', 'http://127.0.0.2/v1', 'not a URL']) {
    assert.equal(isLoopbackHttp(url), false, url);
  }
});

test('invalid timeout cannot silently disable the inactivity limit', () => {
  const undici = { Agent: class {}, fetch() {} };
  for (const timeout of [0, -1, Infinity, 1.5, 2147483648]) {
    assert.throws(() => createStreamTransport(undici, timeout), /Invalid DeepInfer stream transport timeout/);
  }
});

test('request-scoped transport preserves the caller options and reuses its dispatcher', async () => {
  const seen = [];
  const undici = {
    Agent: class {
      constructor(options) { this.options = options; }
      async close() { this.closed = true; }
    },
    fetch: async (input, init) => { seen.push({ input, init }); return 'response'; },
  };
  const transport = createStreamTransport(undici, 900000);
  const controller = new AbortController();
  const init = { method: 'POST', signal: controller.signal, headers: { Accept: 'text/event-stream' } };
  assert.equal(await transport.fetch('http://127.0.0.1/v1', init), 'response');
  await transport.fetch('http://127.0.0.1/v1', init);
  assert.equal(seen[0].init.dispatcher, seen[1].init.dispatcher);
  assert.deepEqual(seen[0].init.dispatcher.options, { headersTimeout: 900000, bodyTimeout: 900000 });
  assert.equal(seen[0].init.signal, controller.signal);
  assert.equal(seen[0].init.headers, init.headers);
  assert.equal(init.dispatcher, undefined);
  await transport.close();
  assert.equal(seen[0].init.dispatcher.closed, true);
});
