// js/domRenderer.js — ES module. All DOM mutations use createElement; never innerHTML.

export function renderHeader(appState) {
  if (!appState) appState = {};
  const sessEl = document.getElementById('session-scope');
  if (sessEl) {
    let sid = '—';
    if (appState.scope && appState.scope.session_id) {
      sid = appState.scope.session_id;
    } else if (appState.session_id) {
      sid = appState.session_id;
    }
    sessEl.textContent = sid;
  }
  const countEl = document.getElementById('agent-count');
  if (countEl) {
    let cnt = '—';
    if (appState.agentCount != null) {
      cnt = appState.agentCount;
    } else if (appState.agents && Array.isArray(appState.agents)) {
      cnt = appState.agents.length;
    }
    countEl.textContent = cnt + ' agents';
  }
  const clockEl = document.getElementById('clock');
  if (clockEl) {
    clockEl.textContent = appState.clock || '—';
  }
}

function _fmtTokens(tokens) {
  // tokens: {input_tokens?, output_tokens?, total_tokens?, tokens?}
  if (!tokens || typeof tokens !== 'object') return null;
  const total = tokens.total_tokens || tokens.tokens;
  const inp = tokens.input_tokens;
  const out = tokens.output_tokens;
  if (total != null) {
    return 'tokens: ' + total.toLocaleString() +
      (inp != null && out != null ? ' (in ' + inp.toLocaleString() + ' / out ' + out.toLocaleString() + ')' : '');
  }
  if (inp != null || out != null) {
    return 'tokens: in ' + (inp || 0).toLocaleString() + ' / out ' + (out || 0).toLocaleString();
  }
  return null;
}

export function renderAgents(agents, container) {
  if (!container) return;
  while (container.firstChild) {
    container.removeChild(container.firstChild);
  }
  if (!Array.isArray(agents)) {
    agents = [];
  }
  for (let i = 0; i < agents.length; i++) {
    const agent = agents[i] || {};
    const card = document.createElement('div');
    const statusClass = agent.zombie ? 'zombie' : (agent.status || 'unknown');
    card.className = 'agent-card agent-card--' + statusClass;
    if (agent.id != null) {
      card.dataset.agentId = String(agent.id);
    }

    // Kind badge
    const kindSpan = document.createElement('span');
    kindSpan.className = 'agent-card__kind';
    kindSpan.textContent = (agent.kind || 'agent').toUpperCase();
    card.appendChild(kindSpan);

    // Zombie badge
    if (agent.zombie) {
      const zombieBadge = document.createElement('span');
      zombieBadge.className = 'badge-zombie';
      zombieBadge.textContent = 'ZOMBIE';
      card.appendChild(zombieBadge);
    }

    // Title row with running pulse
    const titleRow = document.createElement('div');
    if (agent.status === 'running' && !agent.zombie) {
      const pulse = document.createElement('span');
      pulse.className = 'agent-pulse';
      titleRow.appendChild(pulse);
    }
    const titleSpan = document.createElement('span');
    titleSpan.className = 'agent-card__title';
    titleSpan.textContent = agent.title || 'Untitled';
    titleRow.appendChild(titleSpan);
    card.appendChild(titleRow);

    // Summary (truncated server-side to 200 chars)
    if (agent.summary) {
      const summarySpan = document.createElement('span');
      summarySpan.className = 'agent-card__meta';
      summarySpan.textContent = agent.summary;
      card.appendChild(summarySpan);
    }

    // Status + reason
    const statusLine = document.createElement('span');
    statusLine.className = 'agent-card__status';
    statusLine.textContent = (agent.status || 'unknown').toUpperCase();
    card.appendChild(statusLine);

    const sr = (agent.statusReason != null) ? agent.statusReason : agent.status_reason;
    if (sr) {
      const reasonSpan = document.createElement('span');
      reasonSpan.className = 'agent-card__meta';
      const stVal = (agent.staleness_seconds != null) ? agent.staleness_seconds : agent.staleness;
      reasonSpan.textContent = sr + (stVal != null ? ' · ' + stVal + 's stale' : '');
      card.appendChild(reasonSpan);
    }

    // Token readout (Task 3 UI)
    const tokLabel = _fmtTokens(agent.tokens);
    if (tokLabel) {
      const tokSpan = document.createElement('span');
      tokSpan.className = 'agent-card__tokens';
      tokSpan.textContent = tokLabel;
      card.appendChild(tokSpan);
    }

    // Cancel button
    let hasCancel = false;
    if (agent.actions && Array.isArray(agent.actions)) {
      hasCancel = agent.actions.includes('cancel');
    }
    if (hasCancel) {
      const btn = document.createElement('button');
      btn.className = 'cancel-btn';
      btn.dataset.agentId = (agent.id != null) ? String(agent.id) : '';
      btn.textContent = 'CANCEL';
      card.appendChild(btn);
    }

    container.appendChild(card);
  }
}

export function renderSessionTotals(totals, container) {
  // totals: {"claude-sonnet-4-5": {input_tokens:N, output_tokens:N, total_tokens:N}, ...}
  if (!container) return;
  while (container.firstChild) {
    container.removeChild(container.firstChild);
  }
  if (!totals || typeof totals !== 'object' || Object.keys(totals).length === 0) {
    container.hidden = true;
    return;
  }
  container.hidden = false;
  const h = document.createElement('h3');
  h.textContent = 'Session Token Totals';
  container.appendChild(h);
  for (const [model, counts] of Object.entries(totals)) {
    const row = document.createElement('div');
    row.className = 'token-row';
    const modelEl = document.createElement('span');
    modelEl.className = 'token-model';
    modelEl.textContent = model;
    row.appendChild(modelEl);
    const countsEl = document.createElement('span');
    countsEl.className = 'token-counts';
    const label = _fmtTokens(counts);
    countsEl.textContent = label || JSON.stringify(counts);
    row.appendChild(countsEl);
    container.appendChild(row);
  }
}

export function renderGit(git, container) {
  if (!container) return;
  while (container.firstChild) {
    container.removeChild(container.firstChild);
  }
  if (!git) {
    const msg = document.createElement('div');
    msg.textContent = 'gh-unavailable';
    container.appendChild(msg);
    return;
  }
  const h = document.createElement('h3');
  h.style.cssText = 'margin:0 0 0.5rem 0; font-size:0.75rem; text-transform:uppercase; letter-spacing:0.1em; color:var(--color-green)';
  h.textContent = 'Git State';
  container.appendChild(h);

  const b = document.createElement('div');
  b.textContent = 'Branch: ' + (git.branch || '—');
  container.appendChild(b);

  const d = document.createElement('div');
  d.textContent = 'Dirty files: ' + (git.dirty != null ? git.dirty : '—');
  container.appendChild(d);

  const a = document.createElement('div');
  a.textContent = 'Ahead of upstream: ' + (git.ahead != null ? git.ahead : '—');
  container.appendChild(a);

  if (git.prs && Array.isArray(git.prs) && git.prs.length > 0) {
    const prHead = document.createElement('div');
    prHead.style.marginTop = '0.5rem';
    prHead.textContent = 'Open PRs:';
    container.appendChild(prHead);
    for (let i = 0; i < git.prs.length; i++) {
      const pr = git.prs[i] || {};
      const pEl = document.createElement('div');
      pEl.style.cssText = 'font-size:0.8rem; color:var(--color-muted); padding-left:0.5rem';
      const num = (pr.number != null) ? ('#' + pr.number + ' ') : '';
      pEl.textContent = num + (pr.title || pr.headRefName || '');
      container.appendChild(pEl);
    }
  }
  if (git.gh_ok === false) {
    const note = document.createElement('div');
    note.style.cssText = 'font-size:0.75rem; color:var(--color-amber); margin-top:0.5rem';
    note.textContent = 'gh unavailable';
    container.appendChild(note);
  }
}

export function renderTranscript(lines, container) {
  if (!container) return;
  while (container.firstChild) {
    container.removeChild(container.firstChild);
  }
  let displayLines = Array.isArray(lines) ? lines : [];
  const max = 2000;
  if (displayLines.length > max) {
    displayLines = displayLines.slice(displayLines.length - max);
  }
  for (let i = 0; i < displayLines.length; i++) {
    const pre = document.createElement('pre');
    pre.style.margin = '0';
    pre.textContent = (displayLines[i] != null) ? String(displayLines[i]) : '';
    container.appendChild(pre);
  }
  container.scrollTop = container.scrollHeight;
}

export function renderEmptyState(show, container) {
  if (!container) return;
  container.hidden = !show;
}

export function renderUnreachable(show, el) {
  if (!el) return;
  el.hidden = !show;
}
