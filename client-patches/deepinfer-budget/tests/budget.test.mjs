import { test } from 'node:test';
import assert from 'node:assert/strict';
import { admitPayload, countPayload } from '../guard.mjs';

const payload = { model: 'DeepSeek-V4.1-Flash', messages: [{ role: 'user', content: 'hello' }], max_tokens: 32768 };
const settings = { contextWindow: 1048576 };

test('334-token overflow triggers compaction without changing output or input', async () => {
  const before = structuredClone(payload);
  await assert.rejects(admitPayload(payload, { ...settings, count: async () => 1016142 }), /context_length_exceeded/);
  assert.deepEqual(payload, before);
});

test('hard context boundary and one token over are both protected by safety headroom', async () => {
  for (const input of [1015808, 1015809]) {
    await assert.rejects(admitPayload(payload, { ...settings, count: async () => input }), /compaction required/);
  }
});

test('triggers at exactly the safety boundary, accepts one below it', async () => {
  await admitPayload(payload, { ...settings, count: async () => 999423 });
  await assert.rejects(admitPayload(payload, { ...settings, count: async () => 999424 }), /compaction required/);
});

test('output is reserved once and the effective wire field is used', async () => {
  let measured;
  const alternate = { ...payload, max_tokens: undefined, max_completion_tokens: 32768 };
  await admitPayload(alternate, { ...settings, count: async () => 999000, observe: value => { measured = value; } });
  assert.equal(measured.inputBudget, 1048576 - 32768 - 16384);
  assert.equal(alternate.max_completion_tokens, 32768);
});

test('a still oversized replacement is rejected again without an internal resend', async () => {
  let counts = 0;
  await assert.rejects(admitPayload(payload, { ...settings, count: async () => { counts++; return 1000000; } }));
  assert.equal(counts, 1);
});

test('counting failure stops admission without leaking parser input', async () => {
  await assert.rejects(countPayload(payload, { command: 'nonexistent-deepinfer-counter', tokenizer: 'fixture' }), /counter unavailable/);
});

const counter = process.env.DEEPINFER_COUNTER;
const tokenizer = process.env.DEEPINFER_TOKENIZER;
test('native recipe counts system, tools, Unicode and template overhead', { skip: !counter || !tokenizer }, async () => {
  const count = body => countPayload(body, { command: counter, tokenizer });
  const base = await count(payload);
  const system = await count({ ...payload, messages: [{ role: 'system', content: '必须遵守系统指令。'.repeat(100) }, ...payload.messages] });
  const developer = await count({ ...payload, messages: [{ role: 'developer', content: '必须遵守系统指令。'.repeat(100) }, ...payload.messages] });
  const tools = await count({ ...payload, tools: [{ type: 'function', function: { name: 'lookup', description: '工具定义开销'.repeat(100), parameters: { type: 'object', properties: { query: { type: 'string' } } } } }] });
  assert.ok(base > 1, 'template overhead must be included');
  assert.ok(system > base + 100);
  assert.equal(developer, system, 'match server developer-to-system normalization');
  assert.ok(tools > base + 100);
  const images = await count({ ...payload, messages: [{ role: 'user', content: [
    { type: 'text', text: 'hello' }, { type: 'image_url', image_url: { url: 'data:image/png;base64,fixture' } },
  ] }] });
  assert.ok(images >= base + 1024, 'reserve the server recipe image cap rather than counting only the placeholder');
  console.log(JSON.stringify({ nativeCounts: { base, system, developer, tools } }));
});
