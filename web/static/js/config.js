/* ===== Config Module - Server & Forward Rule Management ===== */

let allImportanceGroups = [];
let allSenderGroups = [];

document.addEventListener('DOMContentLoaded', async () => {
  await checkSession();
  await loadServers();
  await loadForwardRules();
  await loadSenderGroups();
});

// ---- Servers ----

async function loadServers() {
  try {
    const data = await api('/api/servers');
    const container = document.getElementById('serverList');
    container.innerHTML = '';

    if (data.servers.length === 0) {
      container.innerHTML = '<p class="text-muted text-center" style="padding:20px;">' + __('No email servers configured yet.') + '</p>';
      return;
    }

    data.servers.forEach(srv => {
      const card = document.createElement('div');
      card.className = 'server-card';
      const proto = srv.incoming_protocol || 'POP3';
      const lastFetch = srv.last_fetch_at ? new Date(srv.last_fetch_at).toLocaleString() : __('Never');
      const fetchInterval = srv.fetch_interval_minutes || 0;
      let intervalLabel = '';
      if (srv.use_imap_idle) {
        intervalLabel = ' &middot; ' + __('IMAP IDLE');
      } else if (fetchInterval > 0) {
        intervalLabel = ' &middot; ' + __('Auto every {0} min', fetchInterval);
      }
      card.innerHTML = `
        <div class="server-info">
          <div class="server-name">${escHtml(srv.server_name)}</div>
          <div class="server-detail">
            ${escHtml(srv.username)} &middot; ${proto} ${srv.incoming_server}${srv.outgoing_server ? ' &middot; ' + __('SMTP enabled') : ''}
            <br>${__('Last fetch: {0}', lastFetch)}${intervalLabel}
          </div>
        </div>
        <div class="server-actions">
          <button class="btn btn-sm btn-outline" onclick="editServer(${srv.id})">&#9998;</button>
          <button class="btn btn-sm btn-outline" onclick="testServer(${srv.id})" title="${__('Test Server Connection')}">&#9881;</button>
          <button class="btn btn-sm btn-outline" onclick="fetchServer(${srv.id})">&#8635;</button>
          <button class="btn btn-sm btn-danger" onclick="deleteServer(${srv.id})">&#128465;</button>
        </div>
      `;
      container.appendChild(card);
    });
  } catch (err) {
    document.getElementById('serverList').innerHTML = '<p class="text-danger">' + __('Error: {0}', err.message) + '</p>';
  }
}

function showAddServer() {
  document.getElementById('serverModalTitle').textContent = __('Add Email Server');
  document.getElementById('serverForm').reset();
  document.getElementById('serverId').value = '';
  document.getElementById('serverError').textContent = '';
  document.getElementById('imapIdleGroup').style.display = 'none';
  document.getElementById('useImapIdle').checked = false;
  document.getElementById('fetchInterval').disabled = false;
  document.getElementById('checkImapIdleBtn').style.display = 'none';
  document.getElementById('checkImapIdleMsg').style.display = 'none';
  document.getElementById('serverModal').style.display = 'flex';
}

function closeServerModal() {
  document.getElementById('serverModal').style.display = 'none';
}

async function editServer(id) {
  try {
    const data = await api('/api/servers');
    const srv = data.servers.find(s => s.id === id);
    if (!srv) return;

    document.getElementById('serverModalTitle').textContent = __('Edit Email Server');
    document.getElementById('serverId').value = srv.id;
    document.getElementById('serverName').value = srv.server_name || '';
    document.getElementById('incomingProtocol').value = srv.incoming_protocol || 'POP3';
    document.getElementById('incomingServer').value = srv.incoming_server || '';
    document.getElementById('incomingPort').value = srv.incoming_port || '';
    document.getElementById('outgoingServer').value = srv.outgoing_server || '';
    document.getElementById('outgoingPort').value = srv.outgoing_port || '';
    document.getElementById('emailUsername').value = srv.username || '';
    document.getElementById('emailPassword').value = srv.password || '';
    document.getElementById('useSsl').value = srv.use_ssl ? '1' : '0';
    document.getElementById('deleteAfterDownload').checked = !!srv.delete_after_download;
    document.getElementById('fetchInterval').value = srv.fetch_interval_minutes || 0;

    const isImap = (srv.incoming_protocol || 'POP3').toUpperCase() === 'IMAP';
    const idleSupported = !!srv.imap_idle_supported;
    document.getElementById('imapIdleGroup').style.display = (isImap && idleSupported) ? 'block' : 'none';
    document.getElementById('useImapIdle').checked = !!srv.use_imap_idle;
    document.getElementById('fetchInterval').disabled = !!srv.use_imap_idle;
    document.getElementById('checkImapIdleBtn').style.display = isImap ? 'inline-block' : 'none';
    document.getElementById('checkImapIdleMsg').style.display = 'none';

    document.getElementById('serverError').textContent = '';
    document.getElementById('serverModal').style.display = 'flex';
  } catch (err) {
    alert(__('Failed to load server: {0}', err.message));
  }
}

async function saveServer(e) {
  e.preventDefault();
  const id = document.getElementById('serverId').value;
  const data = {
    server_name: document.getElementById('serverName').value.trim(),
    incoming_protocol: document.getElementById('incomingProtocol').value,
    incoming_server: document.getElementById('incomingServer').value.trim(),
    incoming_port: parseInt(document.getElementById('incomingPort').value) || null,
    outgoing_server: document.getElementById('outgoingServer').value.trim(),
    outgoing_port: parseInt(document.getElementById('outgoingPort').value) || null,
    username: document.getElementById('emailUsername').value.trim(),
    password: document.getElementById('emailPassword').value,
    use_ssl: document.getElementById('useSsl').value === '1',
    delete_after_download: document.getElementById('deleteAfterDownload').checked,
    fetch_interval_minutes: parseInt(document.getElementById('fetchInterval').value) || 0,
    use_imap_idle: document.getElementById('useImapIdle').checked,
  };

  try {
    if (id) {
      await api(`/api/servers/${id}`, { method: 'PUT', body: data });
    } else {
      await api('/api/servers', { method: 'POST', body: data });
    }
    closeServerModal();
    await loadServers();
  } catch (err) {
    document.getElementById('serverError').textContent = err.message;
  }
}

function getServerFormData() {
  return {
    incoming_protocol: document.getElementById('incomingProtocol').value,
    incoming_server: document.getElementById('incomingServer').value.trim(),
    incoming_port: parseInt(document.getElementById('incomingPort').value) || null,
    use_ssl: document.getElementById('useSsl').value === '1',
    username: document.getElementById('emailUsername').value.trim(),
    password: document.getElementById('emailPassword').value,
  };
}

function toggleImapIdle(checked) {
  document.getElementById('fetchInterval').disabled = checked;
  if (checked) {
    document.getElementById('fetchInterval').value = '0';
  }
}

function updateImapIdleVisibility() {
  const proto = document.getElementById('incomingProtocol').value.toUpperCase();
  const group = document.getElementById('imapIdleGroup');
  const btn = document.getElementById('checkImapIdleBtn');
  const msg = document.getElementById('checkImapIdleMsg');

  if (proto !== 'IMAP') {
    group.style.display = 'none';
    btn.style.display = 'none';
    msg.style.display = 'none';
    document.getElementById('useImapIdle').checked = false;
    document.getElementById('fetchInterval').disabled = false;
    return;
  }

  btn.style.display = 'inline-block';
}

async function checkImapIdleFromButton() {
  const server = document.getElementById('incomingServer').value.trim();
  const btn = document.getElementById('checkImapIdleBtn');
  const msg = document.getElementById('checkImapIdleMsg');
  const group = document.getElementById('imapIdleGroup');

  if (!server) {
    msg.textContent = __('Incoming server required');
    msg.className = 'form-message form-error';
    msg.style.display = 'inline';
    return;
  }

  btn.disabled = true;
  btn.textContent = __('Checking...');
  msg.style.display = 'none';

  const serverId = document.getElementById('serverId').value;
  const cfg = getServerFormData();
  const url = serverId ? `/api/servers/${serverId}/idle-supported` : '/api/servers/check-idle';

  try {
    const result = await api(url, { method: 'POST', body: cfg });
    if (result.success && result.idle_supported) {
      group.style.display = 'block';
      msg.textContent = __('IMAP IDLE supported');
      msg.className = 'form-message form-success';
    } else {
      group.style.display = 'none';
      document.getElementById('useImapIdle').checked = false;
      document.getElementById('fetchInterval').disabled = false;
      msg.textContent = __('IMAP IDLE not supported');
      msg.className = 'form-message form-error';
    }
  } catch (err) {
    group.style.display = 'none';
    document.getElementById('useImapIdle').checked = false;
    document.getElementById('fetchInterval').disabled = false;
    msg.textContent = __('Check failed: {0}', err.message);
    msg.className = 'form-message form-error';
  } finally {
    btn.disabled = false;
    btn.textContent = __('Check IMAP IDLE support');
    msg.style.display = 'inline';
  }
}

async function testServer(id) {
  const btn = event && event.target ? event.target : document.querySelector(`button[onclick*="testServer(${id})"]`);
  if (btn) { btn.disabled = true; btn.textContent = '...'; }

  try {
    const result = await api(`/api/servers/${id}/test`, { method: 'POST' });

    let html = '';
    const incoming = result.incoming || {};
    const outgoing = result.outgoing || {};

    html += `<div class="test-result-item ${incoming.success ? 'test-ok' : 'test-fail'}">
      <span class="test-icon">${incoming.success ? '&#10003;' : '&#10007;'}</span>
      <div class="test-result-text">
        <strong>${__('Incoming Server')}</strong>
        <p>${escHtml(incoming.message || '')}</p>
      </div>
    </div>`;

    html += `<div class="test-result-item ${outgoing.success ? 'test-ok' : 'test-fail'}">
      <span class="test-icon">${outgoing.success ? '&#10003;' : '&#10007;'}</span>
      <div class="test-result-text">
        <strong>${__('Outgoing (SMTP)')}</strong>
        <p>${escHtml(outgoing.message || '')}</p>
      </div>
    </div>`;

    document.getElementById('testResultContent').innerHTML = html;
    document.getElementById('testResultModal').style.display = 'flex';
  } catch (err) {
    document.getElementById('testResultContent').innerHTML =
      `<div class="test-result-item test-fail">
        <span class="test-icon">&#10007;</span>
        <div class="test-result-text">
          <strong>${__('Error')}</strong>
          <p>${escHtml(err.message)}</p>
        </div>
      </div>`;
    document.getElementById('testResultModal').style.display = 'flex';
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = '&#9881;'; }
  }
}

async function deleteServer(id) {
  if (!confirm(__('Delete this email server configuration?'))) return;
  try {
    await api(`/api/servers/${id}`, { method: 'DELETE' });
    await loadServers();
  } catch (err) {
    alert(__('Failed to delete server: {0}', err.message));
  }
}

async function fetchServer(id) {
  try {
    await api(`/api/servers/${id}/fetch`, { method: 'POST' });
    alert(__('Fetch started. Check Inbox after a moment.'));
  } catch (err) {
    alert(__('Failed: {0}', err.message));
  }
}

// ---- Forward Rules ----

async function loadForwardRules() {
  try {
    const data = await api('/api/forward-rules');
    const container = document.getElementById('forwardRulesList');
    container.innerHTML = '';

    if (data.rules.length === 0) {
      container.innerHTML = '<p class="text-muted text-center" style="padding:20px;">' + __('No forward rules configured.') + '</p>';
      return;
    }

    data.rules.forEach(rule => {
      const card = document.createElement('div');
      card.className = 'rule-card';
      const target = rule.importance_name || rule.sender_group_name || __('All');
      card.innerHTML = `
        <div class="rule-info">
          <div class="rule-name">
            ${rule.enabled ? '&#9989;' : '&#10060;'} ${__('Forward')} <strong>${escHtml(target)}</strong>
            &rarr; <strong>${escHtml(rule.forward_to)}</strong>
          </div>
          <div class="rule-detail">
            ${rule.importance_name ? __('Importance: {0}', rule.importance_name) : ''}
            ${rule.sender_group_name ? __('Sender: {0}', rule.sender_group_name) : ''}
          </div>
        </div>
        <div class="rule-actions">
          <button class="btn btn-sm btn-outline" onclick="toggleForwardRule(${rule.id}, ${rule.enabled ? 0 : 1})">
            ${rule.enabled ? __('Disable') : __('Enable')}
          </button>
          <button class="btn btn-sm btn-danger" onclick="deleteForwardRule(${rule.id})">&#128465;</button>
        </div>
      `;
      container.appendChild(card);
    });
  } catch (err) {
    document.getElementById('forwardRulesList').innerHTML = '<p class="text-danger">' + __('Error: {0}', err.message) + '</p>';
  }
}

async function showAddForwardRule() {
  document.getElementById('forwardModalTitle').textContent = __('Add Forward Rule');
  document.getElementById('forwardRuleId').value = '';
  document.getElementById('forwardForm').reset();
  document.getElementById('forwardError').textContent = '';

  // Load groups
  try {
    const igData = await api('/api/groups/importance');
    allImportanceGroups = igData.groups || [];
    const sgData = await api('/api/sender-groups');
    allSenderGroups = sgData.sender_groups || [];

    const impSelect = document.getElementById('forwardImportanceGroup');
    impSelect.innerHTML = '<option value="">' + __('-- Any Importance --') + '</option>';
    allImportanceGroups.forEach(g => {
      impSelect.innerHTML += `<option value="${g.id}">${escHtml(g.name)}</option>`;
    });

    const sgSelect = document.getElementById('forwardSenderGroup');
    sgSelect.innerHTML = '<option value="">' + __('-- Any Sender --') + '</option>';
    allSenderGroups.forEach(g => {
      sgSelect.innerHTML += `<option value="${g.id}">${escHtml(g.group_name)} (${escHtml(g.sender_email)})</option>`;
    });

    document.getElementById('forwardModal').style.display = 'flex';
  } catch (err) {
    alert(__('Failed to load groups: {0}', err.message));
  }
}

function closeForwardModal() {
  document.getElementById('forwardModal').style.display = 'none';
}

async function saveForwardRule(e) {
  e.preventDefault();
  const id = document.getElementById('forwardRuleId').value;
  const data = {
    forward_to: document.getElementById('forwardTo').value.trim(),
    importance_group_id: parseInt(document.getElementById('forwardImportanceGroup').value) || null,
    sender_group_id: parseInt(document.getElementById('forwardSenderGroup').value) || null,
  };

  if (!data.forward_to) {
    document.getElementById('forwardError').textContent = __('Forward email required');
    return;
  }
  if (!data.importance_group_id && !data.sender_group_id) {
    document.getElementById('forwardError').textContent = __('Select at least one group');
    return;
  }

  try {
    if (id) {
      await api(`/api/forward-rules/${id}`, { method: 'PUT', body: data });
    } else {
      await api('/api/forward-rules', { method: 'POST', body: data });
    }
    closeForwardModal();
    await loadForwardRules();
  } catch (err) {
    document.getElementById('forwardError').textContent = err.message;
  }
}

async function toggleForwardRule(id, enabled) {
  try {
    await api(`/api/forward-rules/${id}`, { method: 'PUT', body: { enabled } });
    await loadForwardRules();
  } catch (err) {
    alert(__('Failed: {0}', err.message));
  }
}

async function deleteForwardRule(id) {
  if (!confirm(__('Delete this forward rule?'))) return;
  try {
    await api(`/api/forward-rules/${id}`, { method: 'DELETE' });
    await loadForwardRules();
  } catch (err) {
    alert(__('Failed to delete rule: {0}', err.message));
  }
}

// ---- Sender Groups ----

async function loadSenderGroups() {
  try {
    const igData = await api('/api/groups/importance');
    allImportanceGroups = igData.groups || [];

    const sgData = await api('/api/sender-groups');
    allSenderGroups = sgData.sender_groups || [];
    const container = document.getElementById('senderGroupsList');
    container.innerHTML = '';

    if (allSenderGroups.length === 0) {
      container.innerHTML = '<p class="text-muted text-center" style="padding:20px;">' + __('No sender groups yet. Fetch some emails first.') + '</p>';
      return;
    }

    allSenderGroups.forEach(g => {
      const card = document.createElement('div');
      card.className = 'sender-card';
      card.innerHTML = `
        <div class="sender-info">
          <strong>${escHtml(g.group_name)}</strong>
          <span class="text-muted" style="font-size:0.8rem;"> &lt;${escHtml(g.sender_email)}&gt;</span>
          ${g.is_auto_classified ? '<span class="text-muted" style="font-size:0.75rem;">' + __('(auto)') + '</span>' : ''}
        </div>
        <div class="sender-actions">
          <select class="importance-select" onchange="updateSenderGroup(${g.id}, this.value)">
            <option value="">${__('-- Set Importance --')}</option>
            ${allImportanceGroups.map(ig =>
              `<option value="${ig.id}" ${g.importance_group_id === ig.id ? 'selected' : ''}>${escHtml(ig.name)}</option>`
            ).join('')}
          </select>
        </div>
      `;
      container.appendChild(card);
    });
  } catch (err) {
    document.getElementById('senderGroupsList').innerHTML = '<p class="text-danger">' + __('Error: {0}', err.message) + '</p>';
  }
}

async function updateSenderGroup(groupId, importanceGroupId) {
  try {
    await api(`/api/sender-groups/${groupId}`, {
      method: 'PUT',
      body: { importance_group_id: importanceGroupId ? parseInt(importanceGroupId) : null },
    });
    await loadSenderGroups();
  } catch (err) {
    alert(__('Failed to update: {0}', err.message));
  }
}

async function autoClassifySenders() {
  try {
    await api('/api/sender-groups/auto-classify', { method: 'POST' });
    await loadSenderGroups();
    alert(__('Auto-classification complete!'));
  } catch (err) {
    alert(__('Failed: {0}', err.message));
  }
}

// ---- Helpers ----

function escHtml(str) {
  if (!str) return '';
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

document.getElementById('incomingProtocol').addEventListener('change', updateImapIdleVisibility);

// Close modals on overlay click
document.addEventListener('click', (e) => {
  if (e.target.classList.contains('modal')) {
    e.target.style.display = 'none';
  }
});
