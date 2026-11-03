// Extend the existing budget sidecar for the validated Desktop runtime.
const fs = require('node:fs');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { createRequire } = require('node:module');
const { pathToFileURL } = require('node:url');
const { prepare, PINNED } = require('./prepare.cjs');

const DSH_VERSION = '0.2.1-rc.5';
const UNDICI_VERSION = '8.11.2';
const UNDICI_ENTRY_SHA256 = '93fc85c00a59b58f410c901e5477d56770de3816bfbe183e751624aeb4a5e6a5';
const sha = value => createHash('sha256').update(value).digest('hex');

/** Generate a new, version-checked sidecar; existing installations are untouched. */
function prepareStream({ runtimeModules, output, counter, tokenizer, guard }) {
  if (fs.existsSync(output)) throw new Error('Output already exists');
  for (const file of [counter, tokenizer, guard ?? path.join(__dirname, 'guard.mjs')]) {
    if (typeof file !== 'string' || !fs.existsSync(file) || !fs.statSync(file).isFile()) {
      throw new Error('DeepInfer counter, tokenizer or guard is unavailable');
    }
  }
  const adapter = path.join(runtimeModules, '@deepseek-ai/dsh-llm-pi-ai/lib/index.js');
  const adapterPackage = path.join(runtimeModules, '@deepseek-ai/dsh-llm-pi-ai/package.json');
  const compactionPackage = path.join(runtimeModules, '@deepseek-ai/dsh-compaction-basic/package.json');
  const adapterVersion = JSON.parse(fs.readFileSync(adapterPackage)).version;
  const compactionVersion = JSON.parse(fs.readFileSync(compactionPackage)).version;
  if (adapterVersion !== DSH_VERSION || compactionVersion !== DSH_VERSION) {
    throw new Error('Unsupported DSH runtime version');
  }
  for (const [kind, expected] of Object.entries(PINNED)) {
    const source = path.join(runtimeModules, `@deepseek-ai/dsh-${kind}/lib/index.js`);
    if (sha(fs.readFileSync(source)) !== expected) throw new Error('Unsupported runtime revision: ' + kind);
  }
  const undiciEntry = createRequire(adapter).resolve('undici');
  const relative = path.relative(runtimeModules, undiciEntry);
  if (relative.startsWith('..' + path.sep) || path.isAbsolute(relative)) {
    throw new Error('Undici is outside the DSH runtime');
  }
  const undiciPackage = path.join(path.dirname(undiciEntry), 'package.json');
  if (JSON.parse(fs.readFileSync(undiciPackage)).version !== UNDICI_VERSION ||
      sha(fs.readFileSync(undiciEntry)) !== UNDICI_ENTRY_SHA256) {
    throw new Error('Unsupported DSH transport revision');
  }
  try {
    fs.mkdirSync(output);
  } catch (error) {
    if (error.code === 'EEXIST') throw new Error('Output already exists');
    throw error;
  }
  const manifest = prepare({ runtimeModules, output, counter, tokenizer, ...(guard ? { guard } : {}) });
  const transportPath = path.join(output, 'stream-transport.mjs');
  const transport = fs.readFileSync(path.join(__dirname, 'stream-transport.mjs'));
  fs.writeFileSync(transportPath, transport, { flag: 'wx' });

  const adapterPath = path.join(output, 'llm-pi-ai.mjs');
  let source = fs.readFileSync(adapterPath, 'utf8');
  const anchor = '...profileOptions(profile, reasoning, apiKey),';
  if (source.split(anchor).length !== 2) throw new Error('Runtime stream patch anchor mismatch');
  source = source.replace(anchor, `${anchor}
                    ...isBudgetTarget(options.provider) && isLoopbackHttp(model.baseUrl)
                      ? { fetch: deepinferStreamFetch(profile.streamIdleTimeoutMs) } : {},`);
  const header = `import * as streamUndici from ${JSON.stringify(pathToFileURL(undiciEntry).href)};
import { createStreamTransport, isLoopbackHttp } from ${JSON.stringify(pathToFileURL(transportPath).href)};
import { readFileSync as readStreamRuntime } from 'node:fs';
import { createHash as streamRuntimeHash } from 'node:crypto';
if (JSON.parse(readStreamRuntime(${JSON.stringify(adapterPackage)}, 'utf8')).version !== ${JSON.stringify(DSH_VERSION)} ||
    JSON.parse(readStreamRuntime(${JSON.stringify(compactionPackage)}, 'utf8')).version !== ${JSON.stringify(DSH_VERSION)} ||
    JSON.parse(readStreamRuntime(${JSON.stringify(undiciPackage)}, 'utf8')).version !== ${JSON.stringify(UNDICI_VERSION)} ||
    streamRuntimeHash('sha256').update(readStreamRuntime(${JSON.stringify(undiciEntry)})).digest('hex') !== ${JSON.stringify(UNDICI_ENTRY_SHA256)}) {
  throw new Error('DeepInfer stream transport requires revalidation after a DSH runtime update');
}
const streamTransports = new Map();
function deepinferStreamFetch(timeout) {
  const timeoutMs = Math.ceil(timeout);
  let transport = streamTransports.get(timeoutMs);
  if (!transport) {
    transport = createStreamTransport(streamUndici, timeoutMs);
    streamTransports.set(timeoutMs, transport);
  }
  return transport.fetch;
}
`;
  source = header + source;
  fs.writeFileSync(adapterPath, source);
  manifest.runtimeVersion = DSH_VERSION;
  manifest.streamTransport = { undiciVersion: UNDICI_VERSION, undiciEntrySha256: UNDICI_ENTRY_SHA256, helperSha256: sha(transport) };
  manifest.files['llm-pi-ai'].patchedSha256 = sha(source);
  fs.writeFileSync(path.join(output, 'manifest.json'), JSON.stringify(manifest, null, 2));
  return manifest;
}

module.exports = { prepareStream };
if (require.main === module) {
  const [runtimeModules, output, counter, tokenizer, guard] = process.argv.slice(2);
  prepareStream({ runtimeModules, output, counter, tokenizer, guard });
  console.log('DeepInfer stream sidecar prepared; profile activation is separate.');
}
