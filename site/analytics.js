(() => {
  const key = 'llmdeploy-analytics-consent';
  const measurementId = 'G-JPBPYQTK7E';
  const banner = document.getElementById('analytics-consent');
  const warning = document.getElementById('analytics-storage-warning');
  let channel = null;
  try { channel = new BroadcastChannel(key); } catch (_) { /* Storage events are the fallback. */ }
  let choice = null;
  try {
    choice = localStorage.getItem(key);
    // Check that storage can actually change before trusting saved consent.
    const probe = key + '-probe';
    localStorage.setItem(probe, 'test');
    if (localStorage.getItem(probe) !== 'test') throw new Error('Storage unavailable');
    localStorage.removeItem(probe);
  } catch (_) { choice = null; }

  function loadAnalytics() {
    if (document.getElementById('llmdeploy-ga4')) return;
    window.dataLayer = window.dataLayer || [];
    function gtag() { window.dataLayer.push(arguments); }
    gtag('js', new Date());
    gtag('config', measurementId);
    const script = document.createElement('script');
    script.id = 'llmdeploy-ga4';
    script.async = true;
    script.src = 'https://www.googletagmanager.com/gtag/js?id=' + measurementId;
    document.head.appendChild(script);
  }

  function save(value) {
    let saved = false;
    try { localStorage.setItem(key, value); saved = localStorage.getItem(key) === value; } catch (_) { /* Storage unavailable. */ }
    if (value === 'yes') {
      banner.hidden = true;
      window['ga-disable-' + measurementId] = false;
      loadAnalytics();
    } else {
      window['ga-disable-' + measurementId] = true;
      if (!saved) {
        try { localStorage.removeItem(key); saved = localStorage.getItem(key) === null; } catch (_) { /* Report failure below. */ }
      }
      if (channel) channel.postMessage({ revoked: true, saved });
      // Only reload when the rejection is known to be stored.
      if (!saved) {
        banner.hidden = false;
        warning.hidden = false;
      } else if (document.getElementById('llmdeploy-ga4')) location.reload();
      else banner.hidden = true;
    }
  }

  if (channel) channel.onmessage = (event) => {
    if (!event.data || !event.data.revoked) return;
    window['ga-disable-' + measurementId] = true;
    if (event.data.saved) {
      if (document.getElementById('llmdeploy-ga4')) location.reload();
    } else {
      banner.hidden = false;
      warning.hidden = false;
    }
  };
  window.addEventListener('storage', (event) => {
    if ((event.key === key && event.newValue !== 'yes') || event.key === null) {
      window['ga-disable-' + measurementId] = true;
      if (document.getElementById('llmdeploy-ga4')) location.reload();
    }
  });
  document.getElementById('analytics-accept').addEventListener('click', () => save('yes'));
  document.getElementById('analytics-reject').addEventListener('click', () => save('no'));
  document.getElementById('analytics-settings').addEventListener('click', () => { banner.hidden = false; });
  if (choice === 'yes') loadAnalytics();
  else if (choice !== 'no') banner.hidden = false;
})();
