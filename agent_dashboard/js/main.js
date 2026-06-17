import initRain from './matrixRain.js';
import { fetchAgents, fetchGit, fetchTranscript, postCancel } from './apiClient.js';
import { renderHeader, renderAgents, renderSessionTotals, renderGit, renderTranscript, renderEmptyState, renderUnreachable } from './domRenderer.js';

const appState = {
  agents: [],
  totals: {},
  scope: null,
  git: null,
  unreachable: false
};

function getElement(id) {
  return document.getElementById(id);
}

function updateHeader() {
  renderHeader({
    clock: new Date().toLocaleTimeString(),
    agents: appState.agents,
    agentCount: appState.agents.length,
    scope: appState.scope,
    git: appState.git,
    unreachable: appState.unreachable
  });
}

async function pollAgents() {
  try {
    const result = await fetchAgents();
    appState.agents = Array.isArray(result)
      ? result
      : (result && Array.isArray(result.agents) ? result.agents : []);
    appState.totals = (result && result.totals) ? result.totals : {};
    appState.scope = (result && result.scope) ? result.scope : null;
    appState.unreachable = false;
    renderAgents(appState.agents, getElement('agent-list'));
    renderSessionTotals(appState.totals, getElement('session-totals'));
    renderEmptyState(appState.agents.length === 0, getElement('empty-state'));
    renderUnreachable(false, getElement('unreachable-banner'));
    updateHeader();
  } catch (error) {
    appState.unreachable = true;
    renderUnreachable(true, getElement('unreachable-banner'));
    updateHeader();
  }
}

async function pollGit() {
  try {
    appState.git = await fetchGit();
    renderGit(appState.git, getElement('git-panel'));
  } catch (error) {
    renderGit(appState.git, getElement('git-panel'));
  }
}

function stringifyCancelResult(result) {
  if (typeof result === 'string') {
    return result;
  }
  try {
    return JSON.stringify(result);
  } catch (error) {
    return 'Cancel request completed';
  }
}

document.addEventListener('DOMContentLoaded', function () {
  initRain(getElement('matrix-canvas'));

  pollAgents();
  pollGit();
  updateHeader();

  setInterval(pollAgents, 3000);
  setInterval(pollGit, 30000);
  setInterval(function () {
    updateHeader();
  }, 1000);

  const agentList = getElement('agent-list');
  if (agentList) {
    agentList.addEventListener('click', async function (event) {
      const cancelButton = event.target.closest('.cancel-btn');
      const card = event.target.closest('.agent-card');

      if (cancelButton) {
        event.stopPropagation();
        const id = cancelButton.dataset.agentId;
        const resultEl = getElement('cancel-result');
        try {
          const result = await postCancel(id);
          if (resultEl) {
            resultEl.textContent = stringifyCancelResult(result);
          }
          pollAgents();
        } catch (error) {
          if (resultEl) {
            resultEl.textContent = 'Cancel failed: ' + (error && error.message ? error.message : 'request failed');
          }
        }
        return;
      }

      if (!card) {
        return;
      }

      const drawer = getElement('transcript-drawer');
      if (drawer) {
        drawer.classList.add('open');
      }

      try {
        const result = await fetchTranscript(card.dataset.agentId);
        const lines = Array.isArray(result) ? result : (result && Array.isArray(result.lines) ? result.lines : []);
        renderTranscript(lines, getElement('transcript-log'));
      } catch (error) {
        renderTranscript(['Unable to load transcript: ' + (error && error.message ? error.message : 'request failed')], getElement('transcript-log'));
      }
    });
  }

  const closeDrawer = getElement('close-drawer');
  if (closeDrawer) {
    closeDrawer.addEventListener('click', function () {
      const drawer = getElement('transcript-drawer');
      if (drawer) {
        drawer.classList.remove('open');
      }
    });
  }
});
