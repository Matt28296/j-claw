// Shared fetch wrapper: a 5s AbortController timeout, ok-check, and friendly
// AbortError/TypeError mapping, defined ONCE. Every exported endpoint routes through it so the
// timeout, error mapping, and headers can never drift between calls. clearTimeout lives in
// `finally`, so it fires on every path (success, !ok, abort, network error) — no per-branch leak.
async function _request(url, { method = 'GET', body = null } = {}) {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 5000);
  try {
    const opts = {
      signal: controller.signal,
      method,
      headers: { 'Content-Type': 'application/json' },
    };
    if (body != null) opts.body = JSON.stringify(body);
    const response = await fetch(url, opts);
    if (!response.ok) {
      throw new Error(response.statusText);
    }
    return await response.json();
  } catch (error) {
    if (error.name === 'AbortError') {
      throw new Error('Request timed out');
    }
    if (error.name === 'TypeError') {
      throw new Error('Network error: ' + (error.message || 'fetch failed'));
    }
    throw error;
  } finally {
    clearTimeout(timeoutId);
  }
}

export function fetchAgents() {
  return _request('/api/agents');
}

export function fetchGit() {
  return _request('/api/git');
}

export function fetchControlStatus() {
  return _request('/api/control-status');
}

export function fetchTranscript(id) {
  return _request('/api/transcript?id=' + encodeURIComponent(id));
}

export function cancelAgent(id) {
  return _request('/api/cancel', { method: 'POST', body: { id } });
}

// postCancel is an alias for cancelAgent kept for main.js compatibility.
export function postCancel(id) {
  return cancelAgent(id);
}
