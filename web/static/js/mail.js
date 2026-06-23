/* ===== Mail Module - Main email interface ===== */

let currentState = {
  folder: 'inbox',
  impGroupId: null,
  senderGroupId: null,
  serverId: null,
  currentEmailId: null,
  page: 1,
  search: '',
};
let allServers = [];

// ===== Init =====
document.addEventListener('DOMContentLoaded', async () => {
  const user = await checkSession();
  if (user) {
    try {
      const pref = await api('/api/preferences/group-by-server');
      document.getElementById('groupByServer').checked = pref.group_by_server;
    } catch (_) {}
    loadTree();
    loadServersForCompose();
  }
});

// ===== Tree Menu =====
async function loadTree() {
  try {
    const data = await api('/api/mailbox/tree');
    const container = document.getElementById('treeMenu');
    container.innerHTML = '';
    data.folders.forEach(folder => {
      const folderEl = createTreeItem(folder);
      container.appendChild(folderEl);
    });
    // Default: select inbox
    selectFolder('inbox');
  } catch (err) {
    console.error('Failed to load tree:', err);
  }
}

function createTreeItem(node) {
  const item = document.createElement('div');

  // Main folder/group item
  const main = document.createElement('div');
  main.className = 'tree-item';
  if (node.imp_group_id) {
    // Look up importance name for coloring
    const impNames = {Ad: 'imp-Ad', Normal: 'imp-Normal', Important: 'imp-Important'};
    Object.values(impNames).forEach(c => main.classList.remove(c));
  }
  if (node.icon === 'inbox') main.classList.add('active');

  const hasChildren = node.children && node.children.length > 0;

  if (hasChildren) {
    const toggle = document.createElement('span');
    toggle.className = 'tree-toggle';
    toggle.innerHTML = '&#9660;';
    main.appendChild(toggle);
  } else {
    const spacer = document.createElement('span');
    spacer.style.width = '16px';
    spacer.style.display = 'inline-block';
    main.appendChild(spacer);
  }

  const icon = document.createElement('span');
  icon.className = 'tree-icon';
  icon.innerHTML = getFolderIcon(node.icon || 'folder');
  main.appendChild(icon);

  const name = document.createElement('span');
  name.className = 'tree-name';
  // Translate folder/importance names, keep sender names as-is
  name.textContent = node.sender_group_id ? node.name : __(node.name);
  if (node.email) name.title = node.email;
  main.appendChild(name);

  const count = document.createElement('span');
  count.className = 'tree-count';
  const totalCount = node.count || 0;
  const unreadStr = node.unread ? __(' new') : '';
  count.textContent = totalCount > 0 ? `${totalCount}${unreadStr}` : '';
  main.appendChild(count);

  // Click handler
  main.addEventListener('click', () => {
    if ((node.name === 'Inbox' || node.server_id) && hasChildren) {
      // Toggle expand
      const childrenContainer = item.querySelector('.tree-children');
      if (childrenContainer) {
        childrenContainer.classList.toggle('collapsed');
        main.querySelector('.tree-toggle')?.classList.toggle('collapsed');
      }
    } else if (node.id === 'inbox') {
      selectFolder('inbox');
    } else if (node.server_id) {
      selectServer(node.server_id);
    } else if (node.id === 'outbox' || node.id === 'drafts' || node.id === 'deleted') {
      selectFolder(node.id);
    } else if (node.sender_group_id) {
      selectSenderGroup(node.sender_group_id, node.imp_group_id, node.server_id);
    } else if (node.imp_group_id) {
      selectImportanceGroup(node.imp_group_id, node.server_id);
    }
  });

  item.appendChild(main);

  // Children
  if (hasChildren) {
    const childrenContainer = document.createElement('div');
    childrenContainer.className = 'tree-children';
    node.children.forEach(child => {
      const childEl = createTreeItem(child);
      childrenContainer.appendChild(childEl);
    });
    item.appendChild(childrenContainer);
  }

  return item;
}

function getFolderIcon(iconName) {
  const icons = {
    'inbox': '&#128229;',
    'send': '&#10148;',
    'file-text': '&#128221;',
    'trash-2': '&#128465;',
    'flag': '&#9873;',
    'user': '&#128100;',
    'folder': '&#128193;',
  };
  return icons[iconName] || '&#128193;';
}

// ===== Selection =====
function selectFolder(folder) {
  currentState = { folder, impGroupId: null, senderGroupId: null, serverId: null, currentEmailId: null, page: 1, search: '' };
  document.getElementById('searchInput').value = '';
  updateActiveTreeItem();
  loadEmails();
}

function selectImportanceGroup(impGroupId, serverId) {
  currentState = { folder: 'inbox', impGroupId, senderGroupId: null, serverId: serverId || null, currentEmailId: null, page: 1, search: '' };
  document.getElementById('searchInput').value = '';
  updateActiveTreeItem();
  loadEmails();
}

function selectSenderGroup(senderGroupId, impGroupId, serverId) {
  currentState = { folder: 'inbox', impGroupId, senderGroupId, serverId: serverId || null, currentEmailId: null, page: 1, search: '' };
  document.getElementById('searchInput').value = '';
  updateActiveTreeItem();
  loadEmails();
}

function selectServer(serverId) {
  currentState = { folder: 'inbox', impGroupId: null, senderGroupId: null, serverId, currentEmailId: null, page: 1, search: '' };
  document.getElementById('searchInput').value = '';
  updateActiveTreeItem();
  loadEmails();
}

function updateActiveTreeItem() {
  document.querySelectorAll('.tree-item').forEach(el => el.classList.remove('active'));
  // The active state is somewhat simplified - we just show the current folder
}

// ===== Email List =====
async function loadEmails() {
  const listView = document.getElementById('emailListView');
  const detailView = document.getElementById('emailDetailView');
  const composeView = document.getElementById('composeView');

  detailView.style.display = 'none';
  composeView.style.display = 'none';
  listView.style.display = 'flex';

  const listEl = document.getElementById('emailList');
  listEl.innerHTML = '<div class="loading">' + __('Loading emails') + '</div>';

  // Update title
  const titleEl = document.getElementById('currentFolderTitle');
  const folderNames = { inbox: __('Inbox'), outbox: __('Outbox'), drafts: __('Drafts'), deleted: __('Deleted') };
  titleEl.textContent = folderNames[currentState.folder] || __('Inbox');

  // Show/hide delete group button
  const deleteGroupBtn = document.getElementById('deleteGroupBtn');
  deleteGroupBtn.style.display = (currentState.senderGroupId || currentState.impGroupId) ? '' : 'none';

  try {
    const params = new URLSearchParams({
      folder: currentState.folder,
      page: currentState.page,
      per_page: 50,
    });
    if (currentState.impGroupId) params.set('imp_group_id', currentState.impGroupId);
    if (currentState.senderGroupId) params.set('sender_group_id', currentState.senderGroupId);
    if (currentState.serverId) params.set('server_id', currentState.serverId);
    if (currentState.search) params.set('search', currentState.search);

    const data = await api(`/api/emails?${params}`);
    document.getElementById('emailCount').textContent = __('{0} messages', data.total);

    listEl.innerHTML = '';
    if (data.emails.length === 0) {
      listEl.innerHTML = '<div class="empty-state"><div class="empty-icon">&#128236;</div><p>' + __('No emails in this folder') + '</p></div>';
      return;
    }

    data.emails.forEach(email => {
      const item = document.createElement('div');
      item.className = `email-item${email.is_read ? '' : ' unread'}`;
      const date = email.received_date ? new Date(email.received_date).toLocaleString() : '';
      const senderDisplay = email.sender_name || email.sender;
      const subjectDisplay = email.subject || __('(No Subject)');
      item.innerHTML = `
        <span class="email-sender" title="${escHtml(email.sender)}">${escHtml(senderDisplay)}</span>
        <span class="email-subject">${escHtml(subjectDisplay)}</span>
        ${email.server_badge ? `<span class="email-badge">${escHtml(email.server_badge)}</span>` : ''}
        <span class="email-date">${date}</span>
      `;
      item.addEventListener('click', () => openEmail(email.id));
      listEl.appendChild(item);
    });
  } catch (err) {
    document.getElementById('emailList').innerHTML = '<div class="empty-state"><p class="text-danger">' + __('Error: {0}', err.message) + '</p></div>';
  }
}

function searchEmails() {
  const search = document.getElementById('searchInput').value.trim();
  currentState.search = search;
  currentState.page = 1;
  loadEmails();
}

// ===== Email Detail =====
async function openEmail(emailId) {
  try {
    const data = await api(`/api/emails/${emailId}`);
    const email = data.email;
    currentState.currentEmailId = emailId;

    document.getElementById('emailListView').style.display = 'none';
    document.getElementById('composeView').style.display = 'none';
    const detailView = document.getElementById('emailDetailView');
    detailView.style.display = 'flex';

    const detailEl = document.getElementById('emailDetail');
    const date = email.received_date ? new Date(email.received_date).toLocaleString() : '';
    const senderDisplay = email.sender_name || email.sender;
    const bodyContent = email.body_text || __('(No content)');

    detailEl.innerHTML = `
      <div class="email-detail-header">
        <h2>${escHtml(email.subject || __('(No Subject)'))}</h2>
        <dl class="email-detail-meta">
          <dt>${__('From:')}</dt><dd>${escHtml(senderDisplay)} &lt;${escHtml(email.sender)}&gt;</dd>
          <dt>${__('To:')}</dt><dd>${escHtml(email.recipients || '')}</dd>
          <dt>${__('Date:')}</dt><dd>${date}</dd>
          ${email.server_badge ? `<dt>${__('Server:')}</dt><dd><span class="email-badge">${escHtml(email.server_badge)}</span></dd>` : ''}
        </dl>
      </div>
      <div class="email-detail-body">${escHtml(bodyContent)}</div>
    `;

    // Refresh tree to update unread counts
    loadTree();
  } catch (err) {
    alert(__('Failed to load email: {0}', err.message));
  }
}

function backToList() {
  document.getElementById('emailDetailView').style.display = 'none';
  document.getElementById('emailListView').style.display = 'flex';
  currentState.currentEmailId = null;
  loadEmails();
}

async function deleteCurrentEmail() {
  if (!currentState.currentEmailId) return;
  if (!confirm(__('Move this email to trash?'))) return;

  try {
    await api(`/api/emails/${currentState.currentEmailId}/move`, {
      method: 'POST',
      body: { folder: 'deleted' },
    });
    backToList();
  } catch (err) {
    alert(__('Failed to delete: {0}', err.message));
  }
}

function forwardEmail() {
  if (!currentState.currentEmailId) return;
  showCompose(__('Forward from email #{0}', currentState.currentEmailId));
}

async function deleteGroup() {
  let msg = __('Delete ALL emails in this group?');
  if (!confirm(msg)) return;

  try {
    if (currentState.senderGroupId) {
      await api(`/api/emails/group/${currentState.senderGroupId}`, { method: 'DELETE' });
    } else if (currentState.impGroupId) {
      await api(`/api/emails/group/importance/${currentState.impGroupId}`, { method: 'DELETE' });
    }
    loadEmails();
    loadTree();
  } catch (err) {
    alert(__('Failed: {0}', err.message));
  }
}

// ===== Compose =====
async function loadServersForCompose() {
  try {
    const data = await api('/api/servers');
    allServers = data.servers || [];
    const select = document.getElementById('composeServer');
    select.innerHTML = '<option value="">' + __('-- Select SMTP Server --') + '</option>';
    allServers.filter(s => s.outgoing_server).forEach(s => {
      select.innerHTML += `<option value="${s.id}">${escHtml(s.server_name)} (${escHtml(s.username)})</option>`;
    });
  } catch (err) {
    console.error('Failed to load servers:', err);
  }
}

function composeNew() {
  showCompose(null);
}

function showCompose(forwardText) {
  document.getElementById('emailListView').style.display = 'none';
  document.getElementById('emailDetailView').style.display = 'none';
  document.getElementById('composeView').style.display = 'flex';
  document.getElementById('composeTo').value = '';
  document.getElementById('composeSubject').value = forwardText ? __('Fwd: {0}', forwardText) : '';
  document.getElementById('composeBody').value = '';
  document.getElementById('composeStatus').textContent = '';
}

function closeCompose() {
  document.getElementById('composeView').style.display = 'none';
  document.getElementById('emailListView').style.display = 'flex';
  loadEmails();
}

async function sendEmail(e) {
  e.preventDefault();
  const btn = e.target.querySelector('.btn-primary');
  const statusEl = document.getElementById('composeStatus');
  btn.disabled = true;
  statusEl.textContent = __('Sending...');

  try {
    const data = await api('/api/compose', {
      method: 'POST',
      body: {
        server_id: parseInt(document.getElementById('composeServer').value),
        to: document.getElementById('composeTo').value.trim(),
        subject: document.getElementById('composeSubject').value.trim(),
        body_text: document.getElementById('composeBody').value,
      },
    });

    if (data.success) {
      statusEl.textContent = __('Sent!');
      statusEl.className = 'compose-status text-success';
      setTimeout(() => closeCompose(), 1500);
    } else {
      statusEl.textContent = __('Error: {0}', data.error || __('Failed to send'));
      statusEl.className = 'compose-status text-danger';
    }
  } catch (err) {
    statusEl.textContent = __('Error: {0}', err.message);
    statusEl.className = 'compose-status text-danger';
  } finally {
    btn.disabled = false;
  }
}

async function saveDraft() {
  try {
    const data = await api('/api/drafts', {
      method: 'POST',
      body: {
        server_id: parseInt(document.getElementById('composeServer').value) || null,
        to: document.getElementById('composeTo').value.trim(),
        subject: document.getElementById('composeSubject').value.trim(),
        body_text: document.getElementById('composeBody').value,
      },
    });
    document.getElementById('composeStatus').textContent = __('Draft saved!');
    document.getElementById('composeStatus').className = 'compose-status text-success';
  } catch (err) {
    document.getElementById('composeStatus').textContent = __('Error saving draft');
    document.getElementById('composeStatus').className = 'compose-status text-danger';
  }
}

// ===== Fetch =====
async function toggleGroupByServer(checked) {
  try {
    await api('/api/preferences/group-by-server', { method: 'POST' });
    currentState.serverId = null;
    loadTree();
  } catch (_) {}
}

async function fetchAll() {
  const statusEl = document.getElementById('fetchStatus');
  statusEl.textContent = __('Fetching...');
  try {
    await api('/api/fetch-all', { method: 'POST' });
    statusEl.textContent = __('Fetch started. Refresh tree shortly.');
    setTimeout(() => {
      loadTree();
      loadEmails();
      statusEl.textContent = '';
    }, 3000);
  } catch (err) {
    statusEl.textContent = __('Fetch failed');
  }
}

// ===== Dropdown =====
document.addEventListener('click', function(e) {
  const dropdown = document.getElementById('userDropdown');
  const menu = document.getElementById('dropdownMenu');
  if (dropdown && !dropdown.contains(e.target)) {
    menu.classList.remove('show');
  }
  // Close modals on overlay click
  if (e.target.classList.contains('modal')) {
    e.target.style.display = 'none';
  }
});

function toggleDropdown(e) {
  e.stopPropagation();
  const menu = document.getElementById('dropdownMenu');
  menu.classList.toggle('show');
}

// ===== Config =====
function openConfig() {
  window.location.href = '/config';
}

// ===== Change Password =====
function showChangePassword() {
  document.getElementById('changePwdModal').style.display = 'flex';
  document.getElementById('cpNewPassword').value = '';
  document.getElementById('cpConfirmPassword').value = '';
  document.getElementById('cpMessage').textContent = '';
  document.getElementById('dropdownMenu').classList.remove('show');
}

function closeChangePassword() {
  document.getElementById('changePwdModal').style.display = 'none';
}

async function changeMyPassword(e) {
  e.preventDefault();
  const newPwd = document.getElementById('cpNewPassword').value;
  const confirmPwd = document.getElementById('cpConfirmPassword').value;
  const msgDiv = document.getElementById('cpMessage');

  if (newPwd !== confirmPwd) {
    msgDiv.textContent = __('Passwords do not match');
    msgDiv.className = 'form-message form-error';
    return;
  }

  try {
    await api('/api/admin/change-password', {
      method: 'POST',
      body: { new_password: newPwd },
    });
    msgDiv.textContent = __('Password updated successfully!');
    msgDiv.className = 'form-message form-success';
    document.getElementById('changePwdForm').reset();
    setTimeout(closeChangePassword, 1500);
  } catch (err) {
    msgDiv.textContent = err.message;
    msgDiv.className = 'form-message form-error';
  }
}

// ===== Helper =====
function escHtml(str) {
  if (!str) return '';
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}
