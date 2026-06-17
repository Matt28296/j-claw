export async function fetchAgents() {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 5000);
  try {
    const response = await fetch('/api/agents', {
      signal: controller.signal,
      method: 'GET',
      headers: {'Content-Type': 'application/json'}
    });
    clearTimeout(timeoutId);
    if (!response.ok) {
      throw new Error(response.statusText);
    }
    return await response.json();
  } catch (error) {
    clearTimeout(timeoutId);
    if (error.name === 'AbortError') {
      throw new Error('Request timed out');
    }
    if (error.name === 'TypeError') {
      throw new Error('Network error: ' + (error.message || 'fetch failed'));
    }
    throw error;
  }
}

export async function fetchGit() {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 5000);
  try {
    const response = await fetch('/api/git', {
      signal: controller.signal,
      method: 'GET',
      headers: {'Content-Type': 'application/json'}
    });
    clearTimeout(timeoutId);
    if (!response.ok) {
      throw new Error(response.statusText);
    }
    return await response.json();
  } catch (error) {
    clearTimeout(timeoutId);
    if (error.name === 'AbortError') {
      throw new Error('Request timed out');
    }
    if (error.name === 'TypeError') {
      throw new Error('Network error: ' + (error.message || 'fetch failed'));
    }
    throw error;
  }
}

export async function fetchControlStatus() {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 5000);
  try {
    const response = await fetch('/api/control-status', {
      signal: controller.signal,
      method: 'GET',
      headers: {'Content-Type': 'application/json'}
    });
    clearTimeout(timeoutId);
    if (!response.ok) {
      throw new Error(response.statusText);
    }
    return await response.json();
  } catch (error) {
    clearTimeout(timeoutId);
    if (error.name === 'AbortError') {
      throw new Error('Request timed out');
    }
    if (error.name === 'TypeError') {
      throw new Error('Network error: ' + (error.message || 'fetch failed'));
    }
    throw error;
  }
}

export async function fetchTranscript(id) {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 5000);
  const url = '/api/transcript?id=' + encodeURIComponent(id);
  try {
    const response = await fetch(url, {
      signal: controller.signal,
      method: 'GET',
      headers: {'Content-Type': 'application/json'}
    });
    clearTimeout(timeoutId);
    if (!response.ok) {
      throw new Error(response.statusText);
    }
    return await response.json();
  } catch (error) {
    clearTimeout(timeoutId);
    if (error.name === 'AbortError') {
      throw new Error('Request timed out');
    }
    if (error.name === 'TypeError') {
      throw new Error('Network error: ' + (error.message || 'fetch failed'));
    }
    throw error;
  }
}

// postCancel is an alias for cancelAgent kept for main.js compatibility.
export async function postCancel(id) {
  return cancelAgent(id);
}

export async function cancelAgent(id) {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 5000);
  try {
    const response = await fetch('/api/cancel', {
      signal: controller.signal,
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ id: id })
    });
    clearTimeout(timeoutId);
    if (!response.ok) {
      throw new Error(response.statusText);
    }
    return await response.json();
  } catch (error) {
    clearTimeout(timeoutId);
    if (error.name === 'AbortError') {
      throw new Error('Request timed out');
    }
    if (error.name === 'TypeError') {
      throw new Error('Network error: ' + (error.message || 'fetch failed'));
    }
    throw error;
  }
}
