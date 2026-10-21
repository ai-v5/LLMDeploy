// Generate local patches beside the signed runtime; never modify app.asar.
const fs = require('node:fs');
const path = require('node:path');
const { createRequire } = require('node:module');
const { pathToFileURL } = require('node:url');
const { createHash } = require('node:crypto');

const PINNED = {
  'llm-pi-ai': '99b5c20830a52f69641df8a18a39206079b0c725201958cce2c4a934f1d4f7cd',
  'compaction-basic': 'b7ac52f20a36430cf01a66b3a36f23a48ffdb22708d714dbe1fff541b5df4ec5',
};
const sha = value => createHash('sha256').update(value).digest('hex');
const UPSTREAM_LICENSE = `MIT License

Copyright (c) 2026 DeepSeek

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
`;
function replaceOnce(text, before, after) {
  if (text.split(before).length !== 2) throw new Error('Runtime patch anchor mismatch');
  return text.replace(before, after);
}

/** Generate exact-version adapter and compaction modules, plus a non-secret manifest. */
function prepare({ runtimeModules, output, counter, tokenizer, guard = path.join(__dirname, 'guard.mjs') }) {
  fs.mkdirSync(output, { recursive: true });
  const settings = { command: counter, tokenizer };
  const manifest = { version: 1, runtimeVersion: '0.2.1-rc.2', files: {} };
  const installedGuard = path.join(output, 'budget-guard.mjs');
  const guardBytes = fs.readFileSync(guard);
  fs.writeFileSync(installedGuard, guardBytes, { flag: 'wx' });
  manifest.guardSha256 = sha(guardBytes);
  for (const kind of Object.keys(PINNED)) {
    const original = path.join(runtimeModules, '@deepseek-ai/dsh-' + kind, 'lib/index.js');
    const originalRequire = createRequire(original);
    const bytes = fs.readFileSync(original);
    if (sha(bytes) !== PINNED[kind]) throw new Error('Unsupported runtime revision: ' + kind);
    let text = bytes.toString('utf8');
    if (kind === 'llm-pi-ai') {
      text = replaceOnce(text, '...profileOptions(profile, reasoning, apiKey),', `...profileOptions(profile, reasoning, apiKey),
                    ...options.provider === 'deepinfer' ? { onPayload: async payload => {
                      await admitPayload(payload, {
                        contextWindow: model.contextWindow,
                        safetyTokens: 16384,
                        count: body => countPayload(body, { ...deepinferCounter, signal: options.signal }),
                        observe: facts => console.info('deepinfer-budget', JSON.stringify(facts)),
                      });
                    } } : {},`);
      text = `import { admitPayload, countPayload } from ${JSON.stringify(pathToFileURL(installedGuard).href)};\nconst deepinferCounter = ${JSON.stringify(settings)};\n` + text;
    } else {
      text = replaceOnce(text, 'selectCompactableRange(agent.session, measurement, 0)',
        `selectCompactableRange(agent.session, measurement, target.provider === 'deepinfer' ? Math.floor(measurement.surfaceTokens / 2) : 0)`);
      text = replaceOnce(text, 'if (retries >= policy.maxOverflowRetries) return next();',
        `if (retries >= (target.provider === 'deepinfer' ? Math.min(1, policy.maxOverflowRetries) : policy.maxOverflowRetries)) return next();`);
    }
    // Preserve shared runtime module identities when loading a sidecar copy.
    text = text.replace(/from "([^"\n]+)"/g, (all, spec) => {
      if (/^(node:|file:)/.test(spec)) return all;
      let resolved;
      try { resolved = originalRequire.resolve(spec); }
      catch (error) {
        if (error.code !== 'ERR_PACKAGE_PATH_NOT_EXPORTED') throw error;
        const parts = spec.split('/');
        const packageName = parts.splice(0, spec.startsWith('@') ? 2 : 1).join('/');
        const packageDir = path.join(runtimeModules, packageName);
        const metadata = JSON.parse(fs.readFileSync(path.join(packageDir, 'package.json')));
        const subpath = parts.length ? './' + parts.join('/') : '.';
        const pattern = Object.keys(metadata.exports).find(key => key.endsWith('*') && subpath.startsWith(key.slice(0, -1)));
        let entry = metadata.exports[subpath] ?? metadata.exports[pattern];
        while (entry && typeof entry === 'object') entry = entry.import ?? entry.node ?? entry.default;
        if (typeof entry !== 'string') throw error;
        if (pattern) entry = entry.replace('*', subpath.slice(pattern.length - 1));
        resolved = path.join(packageDir, entry);
      }
      return 'from ' + JSON.stringify(pathToFileURL(resolved).href);
    });
    const filename = path.join(output, kind + '.mjs');
    const header = `// Patched local copy of DeepSeek Harness (MIT); original copyright retained.
import { readFileSync as readRuntimeSource } from 'node:fs';
import { createHash as runtimeHash } from 'node:crypto';
if (runtimeHash('sha256').update(readRuntimeSource(${JSON.stringify(original)})).digest('hex') !== ${JSON.stringify(PINNED[kind])}) {
  throw new Error('DeepInfer client patch requires revalidation after a DSH runtime update');
}
`;
    fs.writeFileSync(filename, header + text, { flag: 'wx' });
    manifest.files[kind] = { originalSha256: sha(bytes), patchedSha256: sha(header + text), filename };
  }
  fs.writeFileSync(path.join(output, 'LICENSE.DeepSeek'), UPSTREAM_LICENSE, { flag: 'wx' });
  fs.writeFileSync(path.join(output, 'manifest.json'), JSON.stringify(manifest, null, 2), { flag: 'wx' });
  return manifest;
}
module.exports = { prepare };
if (require.main === module) {
  const [runtimeModules, output, counter, tokenizer] = process.argv.slice(2);
  prepare({ runtimeModules, output, counter, tokenizer });
  console.log('DeepInfer client patch prepared; profile activation is separate.');
}
