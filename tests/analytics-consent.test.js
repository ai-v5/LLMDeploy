const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('site/analytics.js', 'utf8');

function page(initial, failWrites = false, failRemove = false) {
  let stored = initial;
  const nodes = Object.fromEntries(['analytics-consent', 'analytics-storage-warning', 'analytics-accept', 'analytics-reject', 'analytics-settings'].map(id => [id, { hidden: true, addEventListener: (_, fn) => { nodes[id].click = fn; } }]));
  const listeners = {};
  let reloads = 0;
  const window = { addEventListener: (name, fn) => { listeners[name] = fn; } };
  const localStorage = {
    getItem: key => key.endsWith('-probe') ? 'test' : stored,
    setItem: (key, value) => { if (failWrites) throw Error(); if (!key.endsWith('-probe')) stored = value; },
    removeItem: key => { if (failRemove) throw Error(); if (!key.endsWith('-probe')) stored = null; }
  };
  const document = { getElementById: id => nodes[id], createElement: () => ({}), head: { appendChild: node => { nodes[node.id] = node; } } };
  const channel = { postMessage: message => { channel.lastMessage = message; } };
  function BroadcastChannel() { return channel; }
  vm.runInNewContext(source, { window, document, localStorage, BroadcastChannel, location: { reload: () => { reloads++; } }, Date });
  return { nodes, window, listeners, channel, get stored() { return stored; }, get reloads() { return reloads; } };
}
const fresh = page(null);
assert.equal(fresh.nodes['analytics-consent'].hidden, false);
assert.equal(fresh.nodes['llmdeploy-ga4'], undefined);
fresh.nodes['analytics-accept'].click();
assert.equal(fresh.stored, 'yes');
assert.ok(fresh.nodes['llmdeploy-ga4']);
fresh.nodes['analytics-reject'].click();
assert.equal(fresh.stored, 'no');
assert.equal(fresh.reloads, 1);
const other = page('yes');
other.listeners.storage({ key: 'llmdeploy-analytics-consent', newValue: 'no' });
assert.equal(other.window['ga-disable-G-JPBPYQTK7E'], true);
assert.equal(other.reloads, 1);
const broken = page('yes', true, true);
assert.equal(broken.nodes['llmdeploy-ga4'], undefined);
broken.nodes['analytics-reject'].click();
assert.equal(broken.nodes['analytics-storage-warning'].hidden, false);
assert.equal(broken.reloads, 0);
assert.equal(broken.channel.lastMessage.saved, false);
const receiving = page('yes');
receiving.channel.onmessage({ data: broken.channel.lastMessage });
assert.equal(receiving.window['ga-disable-G-JPBPYQTK7E'], true);
assert.equal(receiving.reloads, 0);
assert.equal(receiving.nodes['analytics-storage-warning'].hidden, false);
console.log('analytics consent checks passed');
