/** Use the dedicated transport only for a local HTTP inference endpoint. */
export function isLoopbackHttp(value) {
  try {
    const url = new URL(value);
    return url.protocol === 'http:' && ['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname);
  } catch {
    return false;
  }
}

/** Keep the HTTP inactivity clocks aligned with the provider's stream watchdog. */
export function createStreamTransport(undici, timeoutMs) {
  if (!Number.isSafeInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 2147483647) {
    throw new Error('Invalid DeepInfer stream transport timeout');
  }
  const dispatcher = new undici.Agent({ headersTimeout: timeoutMs, bodyTimeout: timeoutMs });
  return {
    fetch: (input, init) => undici.fetch(input, { ...init, dispatcher }),
    close: () => dispatcher.close(),
  };
}
