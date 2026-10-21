import { spawn } from 'node:child_process';

/** Deployment aliases sharing the V4.1 recipe and total context contract. */
export function isBudgetTarget(provider) {
  return provider === 'deepinfer' || provider === 'local950dt';
}

/** Count a complete OpenAI payload using the matching recipe and tokenizer. */
export function countPayload(payload, { command, tokenizer, signal }) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, [tokenizer], { windowsHide: true, signal, stdio: ['pipe', 'pipe', 'pipe'] });
    let output = '';
    child.stdout.setEncoding('utf8').on('data', text => { output += text; });
    // Never include request-derived parser errors in logs or exception text.
    child.stderr.resume();
    child.on('error', () => reject(new Error('DeepInfer prompt token counter unavailable')));
    child.on('close', code => {
      const tokens = Number(output.trim());
      if (code !== 0 || !/^\d+\s*$/.test(output) || !Number.isSafeInteger(tokens)) {
        reject(new Error('DeepInfer prompt token counting failed; request was not sent'));
      } else resolve(tokens);
    });
    child.stdin.on('error', () => {}); // Process failure is reported by close/error.
    child.stdin.end(JSON.stringify(payload));
  });
}

/** Admit against the total capacity, reserving the wire output cap exactly once. */
export async function admitPayload(payload, { contextWindow, safetyTokens = 16384, count, observe }) {
  const completion = payload.max_tokens ?? payload.max_completion_tokens;
  if (!Number.isSafeInteger(completion) || completion <= 0) {
    throw new Error('DeepInfer requires an explicit positive output budget');
  }
  const input = await count(payload);
  const inputBudget = contextWindow - completion - safetyTokens;
  observe?.({ input, completion, safetyTokens, contextWindow, inputBudget });
  // Trigger at the safety boundary, before the server's hard boundary.
  if (input >= inputBudget) {
    throw new Error(`context_length_exceeded: DeepInfer prompt=${input}, completion=${completion}, safety=${safetyTokens}, context=${contextWindow}; compaction required`);
  }
}
