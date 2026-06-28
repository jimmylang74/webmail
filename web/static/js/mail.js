/* ===== Mail Module - Main email interface ===== */

let currentState = {
  currentEmailId: null,
};
let allServers = [];
let contextMenuTarget = null; // {type: 'imp'|'sender', id: int, serverId: int|null}

// Tree state preservation
let expandedNodeIds = new Set();
let selectedEmailNodeId = null;

function saveTreeState() {
  expandedNodeIds.clear();
  document.querySelectorAll('.tree-children:not(.collapsed)').forEach(container => {
    const wrapper = container.parentElement;
    if (wrapper && wrapper.dataset.nodeId) {
      expandedNodeIds.add(wrapper.dataset.nodeId);
    }
  });
}

function restoreTreeState() {
  // Restore expanded nodes
  expandedNodeIds.forEach(id => {
    const wrapper = document.querySelector(`[data-node-id="${CSS.escape(id)}"]`);
    if (wrapper) {
      const childrenContainer = wrapper.querySelector('.tree-children');
      const toggle = wrapper.querySelector('.tree-toggle');
      if (childrenContainer) {
        childrenContainer.classList.remove('collapsed');
        if (toggle) toggle.classList.remove('collapsed');
      }
    }
  });
  // Restore selected email highlight
  if (selectedEmailNodeId) {
    const wrapper = document.querySelector(`[data-node-id="${CSS.escape(selectedEmailNodeId)}"]`);
    if (wrapper) {
      const main = wrapper.querySelector('.tree-item');
      if (main) main.classList.add('active');
    }
  }
}

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
    startServerStatusBar();
  }
});

// ===== Tree Menu =====
async function loadTree() {
  saveTreeState();
  try {
    const data = await api('/api/mailbox/tree');
    const container = document.getElementById('treeMenu');
    container.innerHTML = '';
    data.folders.forEach(folder => {
      const folderEl = createTreeItem(folder, 0);
      container.appendChild(folderEl);
    });
    restoreTreeState();
  } catch (err) {
    console.error('Failed to load tree:', err);
  }
}

function createTreeItem(node, depth) {
  const item = document.createElement('div');
  item.dataset.nodeId = node.id;

  const isImpGroup = node.imp_group_id != null && !node.sender_group_id;
  const isSenderGroup = node.sender_group_id != null;
  const isEmail = node.type === 'email';
  const hasChildren = node.children && node.children.length > 0;

  // Main item row
  const main = document.createElement('div');
  main.className = 'tree-item';
  if (isEmail) main.classList.add('email-item-tree');
  if (isEmail && !node.is_read) main.classList.add('unread');

  // Importance color class
  if (node.imp_group_id && !node.type) {
    const impNames = {Ad: 'imp-Ad', Normal: 'imp-Normal', Important: 'imp-Important'};
    Object.values(impNames).forEach(c => main.classList.remove(c));
    if (impNames[node.name]) {
      main.classList.add(impNames[node.name]);
    }
  }

  // Toggle arrow (for collapsible nodes)
  if (hasChildren && !isEmail) {
    const toggle = document.createElement('span');
    toggle.className = 'tree-toggle collapsed';
    toggle.innerHTML = '&#9660;';
    main.appendChild(toggle);
  } else {
    const spacer = document.createElement('span');
    spacer.style.width = '16px';
    spacer.style.display = 'inline-block';
    main.appendChild(spacer);
  }

  // Icon
  if (!isEmail) {
    const icon = document.createElement('span');
    icon.className = 'tree-icon';
    if (isImpGroup) {
      const impIcon = {Ad: '&#128683;', Normal: '&#128236;', Important: '&#9888;'};
      icon.innerHTML = impIcon[node.name] || '&#9873;';
    } else if (isSenderGroup) {
      icon.innerHTML = '&#128100;';
    } else {
      icon.innerHTML = getFolderIcon(node.icon || 'folder');
    }
    main.appendChild(icon);
  }

  // Name
  const nameSpan = document.createElement('span');
  nameSpan.className = 'tree-name';
  if (isEmail) {
    const senderDisplay = node.sender_group_name || node.sender_name || node.sender || '';
    nameSpan.textContent = senderDisplay ? senderDisplay + ' - ' + (node.name || '') : (node.name || '');
    nameSpan.title = node.name || '';
  } else {
    nameSpan.textContent = node.sender_group_id ? node.name : __(node.name);
    if (node.email) nameSpan.title = node.email;
  }
  main.appendChild(nameSpan);

  // Count badge (for groups, not emails)
  if (!isEmail && (node.count || node.unread)) {
    const count = document.createElement('span');
    count.className = 'tree-count';
    const totalCount = node.count || 0;
    const unreadCount = node.unread || 0;
    if (unreadCount > 0) {
      count.textContent = `${unreadCount}/${totalCount}`;
      count.classList.add('tree-count-unread');
    } else if (totalCount > 0) {
      count.textContent = String(totalCount);
    }
    main.appendChild(count);
  }

  // Date for email items in tree
  if (isEmail && node.received_date) {
    const dateSpan = document.createElement('span');
    dateSpan.className = 'email-date-tree';
    try {
      dateSpan.textContent = new Date(node.received_date).toLocaleDateString();
    } catch (_) {}
    main.appendChild(dateSpan);
  }

	  // ---- Click handler ----
	  main.addEventListener('click', (e) => {
	    e.stopPropagation();
	    hideContextMenu();
	    if (isEmail) {
	      // Click on email → show detail view
	      selectedEmailNodeId = node.id;
	      document.querySelectorAll('.tree-item.active').forEach(el => el.classList.remove('active'));
	      main.classList.add('active');
	      openEmail(node.email_id);
	    } else if (isSenderGroup && main.classList.contains('active')) {
	      // Already selected sender group → enter inline edit mode
	      // Only trigger if clicking the name area, not the toggle/icon/count
	      const nameSpan = main.querySelector('.tree-name');
	      if (nameSpan && (e.target === nameSpan || nameSpan.contains(e.target))) {
	        startInlineEdit(main, node);
	        return;
	      }
	      // Fall through to toggle collapse for other areas (toggle arrow, icon, etc.)
	      const childrenContainer = item.querySelector('.tree-children');
	      if (childrenContainer) {
	        childrenContainer.classList.toggle('collapsed');
	        const toggleEl = main.querySelector('.tree-toggle');
	        if (toggleEl) toggleEl.classList.toggle('collapsed');
	      }
	      return;
	    } else if (hasChildren) {
	      // Click on group with children → select + toggle collapse
	      if (isSenderGroup) {
	        document.querySelectorAll('.tree-item.active').forEach(el => el.classList.remove('active'));
	        main.classList.add('active');
	      } else if (isImpGroup) {
	        // Also allow importance groups to be selectable for consistency
	        document.querySelectorAll('.tree-item.active').forEach(el => el.classList.remove('active'));
	        main.classList.add('active');
	      }
	      const childrenContainer = item.querySelector('.tree-children');
	      if (childrenContainer) {
	        childrenContainer.classList.toggle('collapsed');
	        const toggleEl = main.querySelector('.tree-toggle');
	        if (toggleEl) toggleEl.classList.toggle('collapsed');
	      }
	      showEmptyState();
	    } else if (node.id === 'inbox' || node.id === 'outbox' || node.id === 'drafts' || node.id === 'deleted') {
	      // Click on root folder with no children → show empty state
	      // (folders with children are handled by the hasChildren branch above)
	      showEmptyState();
	    } else if (node.id) {
	      // Any other leaf node
	      showEmptyState();
	    }
	  });

  // ---- Right-click handler (context menu) ----
  main.addEventListener('contextmenu', (e) => {
    e.preventDefault();
    e.stopPropagation();
    if (isEmail) {
      contextMenuTarget = {type: 'email', id: node.email_id, impGroupId: node.imp_group_id, senderGroupId: node.sender_group_id, serverId: node.server_id || null, isRead: !!node.is_read};
      showContextMenu(e.clientX, e.clientY);
    } else if (isImpGroup) {
      contextMenuTarget = {type: 'imp', id: node.imp_group_id, serverId: node.server_id || null};
      showContextMenu(e.clientX, e.clientY);
    } else if (isSenderGroup) {
      contextMenuTarget = {type: 'sender', id: node.sender_group_id, impGroupId: node.imp_group_id, serverId: node.server_id || null};
      showContextMenu(e.clientX, e.clientY);
    } else if (node.id === 'inbox') {
      contextMenuTarget = {type: 'folder', id: 'inbox'};
      showContextMenu(e.clientX, e.clientY);
    }
  });

  // ---- Drag and Drop ----
  if (isEmail || isSenderGroup) {
    main.draggable = true;
    main.addEventListener('dragstart', (e) => {
      e.stopPropagation();
      if (isEmail) {
        e.dataTransfer.setData('text/plain', JSON.stringify({
          type: 'email',
          emailId: node.email_id,
          impGroupId: node.imp_group_id,
        }));
      } else if (isSenderGroup) {
        e.dataTransfer.setData('text/plain', JSON.stringify({
          type: 'sender',
          senderGroupId: node.sender_group_id,
          impGroupId: node.imp_group_id,
        }));
      }
      e.dataTransfer.effectAllowed = 'move';
      main.classList.add('dragging');
    });
    main.addEventListener('dragend', () => {
      main.classList.remove('dragging');
    });
  }

  if (isImpGroup) {
    main.addEventListener('dragover', (e) => {
      e.preventDefault();
      e.stopPropagation();
      e.dataTransfer.dropEffect = 'move';
      main.classList.add('drag-over');
    });
    main.addEventListener('dragleave', (e) => {
      e.stopPropagation();
      main.classList.remove('drag-over');
    });
    main.addEventListener('drop', (e) => {
      e.preventDefault();
      e.stopPropagation();
      main.classList.remove('drag-over');
      const raw = e.dataTransfer.getData('text/plain');
      if (!raw) return;
      try {
        const data = JSON.parse(raw);
        const impGroupId = parseInt(node.imp_group_id);
        if (data.type === 'email' && data.emailId) {
          moveEmailToImportance(data.emailId, impGroupId);
        } else if (data.type === 'sender' && data.senderGroupId) {
          moveSenderGroupImportance(data.senderGroupId, impGroupId);
        }
      } catch (_) {}
    });
  }

  item.appendChild(main);

  // Children
  if (hasChildren) {
    const childrenContainer = document.createElement('div');
    childrenContainer.className = 'tree-children collapsed';
    node.children.forEach(child => {
      const childEl = createTreeItem(child, depth + 1);
      childrenContainer.appendChild(childEl);
    });
    item.appendChild(childrenContainer);
  }

  return item;
}

function startInlineEdit(main, node) {
  // Replace the .tree-name span with an input for inline editing
  const nameSpan = main.querySelector('.tree-name');
  if (!nameSpan) return;
  const currentName = nameSpan.textContent;

  const input = document.createElement('input');
  input.type = 'text';
  input.value = currentName;
  input.className = 'tree-name-input';
  input.setAttribute('aria-label', 'Edit sender group name');
  nameSpan.replaceWith(input);
  input.focus();
  input.select();

  function finishEdit(save) {
    if (save) {
      const newName = input.value.trim();
      if (newName && newName !== currentName) {
        // Save via API, optimistic update the name
        const origName = nameSpan.textContent;
        nameSpan.textContent = newName;
        api(`/api/sender-groups/${node.sender_group_id}`, {
          method: 'PUT',
          body: { group_name: newName },
        }).catch(() => {
          nameSpan.textContent = origName;
        });
      }
    }
    input.replaceWith(nameSpan);
  }

  input.addEventListener('blur', () => finishEdit(true));
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); input.blur(); }
    if (e.key === 'Escape') { finishEdit(false); }
  });
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

// ===== Empty State =====
function showEmptyState() {
  document.getElementById('emailDetailView').style.display = 'none';
  document.getElementById('composeView').style.display = 'none';
  document.getElementById('emptyState').style.display = 'flex';
  currentState.currentEmailId = null;
  // Clear email highlight
  document.querySelectorAll('.tree-item.active').forEach(el => el.classList.remove('active'));
  selectedEmailNodeId = null;
}

// ===== Email Detail =====
async function openEmail(emailId) {
  try {
    const data = await api(`/api/emails/${emailId}`);
    const email = data.email;
    currentState.currentEmailId = emailId;

    document.getElementById('composeView').style.display = 'none';
    document.getElementById('emptyState').style.display = 'none';
    const detailView = document.getElementById('emailDetailView');
    detailView.style.display = 'flex';

    const detailEl = document.getElementById('emailDetail');
    const date = email.received_date ? new Date(email.received_date).toLocaleString() : '';
    const senderDisplay = email.sender_name || email.sender;
    const hasHtml = !!(email.body_html && email.body_html.trim());
    const textContent = email.body_text || __('(No content)');

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
      <div class="email-detail-body-wrapper"></div>
    `;

    const bodyWrapper = detailEl.querySelector('.email-detail-body-wrapper');
    if (hasHtml) {
      const iframe = document.createElement('iframe');
      iframe.className = 'email-detail-body-html';
      iframe.sandbox = 'allow-same-origin';

      // Inject CSS override so email HTML fills available width.
      // HTML emails typically ship with max-width:600px or body margins
      // that cause content to occupy only a narrow strip in our full-width iframe.
      const fullWidthCss = `
        <style>
          body, table, td, div, p, span, .container, .wrapper, .email-body, .content {
            max-width: none !important;
            width: 100% !important;
          }
          body { margin: 0 !important; padding: 16px !important; box-sizing: border-box !important; }
          img { max-width: 100% !important; height: auto !important; }
          table { max-width: 100% !important; }
        </style>
      `;
      let htmlContent = email.body_html;
      if (/<\/head>/i.test(htmlContent)) {
        htmlContent = htmlContent.replace(/<\/head>/i, fullWidthCss + '</head>');
      } else if (/<html[\s>]/i.test(htmlContent)) {
        htmlContent = htmlContent.replace(/(<html[\s>])/i, '$1<head>' + fullWidthCss + '</head>');
      } else if (/<body[\s>]/i.test(htmlContent)) {
        htmlContent = '<head>' + fullWidthCss + '</head>' + htmlContent;
      } else {
        htmlContent = '<!DOCTYPE html><html><head>' + fullWidthCss + '</head><body>' + htmlContent + '</body></html>';
      }
      iframe.srcdoc = htmlContent;
      bodyWrapper.appendChild(iframe);

      // Poll-based iframe height auto-adjustment.
      // The `load` event fires before layout settles and before images fetch,
      // so scrollHeight is unreliable then. Polling every 100ms for 6s ensures
      // we catch the final rendered height regardless of image load timing.
      const pollInterval = setInterval(() => {
        try {
          const doc = iframe.contentDocument || iframe.contentWindow.document;
          if (!doc || !doc.body) return;
          const h = doc.documentElement.scrollHeight;
          if (h > 0 && h !== parseInt(iframe.style.height)) {
            iframe.style.height = h + 'px';
          }
        } catch (_) {}
      }, 100);
      setTimeout(() => clearInterval(pollInterval), 6000);
    } else {
      const bodyDiv = document.createElement('div');
      bodyDiv.className = 'email-detail-body';
      bodyDiv.textContent = textContent;
      bodyWrapper.appendChild(bodyDiv);
    }

    // Refresh tree to update unread counts (preserves expanded/selected state)
    loadTree();
  } catch (err) {
    alert(__('Failed to load email: {0}', err.message));
  }
}

async function deleteCurrentEmail() {
  if (!currentState.currentEmailId) return;
  if (!(await showDialog({ title: __('Delete Email'), message: __('Move this email to trash?') }))) return;

  try {
    await api(`/api/emails/${currentState.currentEmailId}/move`, {
      method: 'POST',
      body: { folder: 'deleted' },
    });
    showEmptyState();
    loadTree();
  } catch (err) {
    alert(__('Failed to delete: {0}', err.message));
  }
}

function forwardEmail() {
  if (!currentState.currentEmailId) return;
  showCompose(__('Forward from email #{0}', currentState.currentEmailId));
}

// ===== Context Menu =====
let cachedImpGroups = null;

async function getImpGroups() {
  if (cachedImpGroups) return cachedImpGroups;
  try {
    const data = await api('/api/groups/importance');
    cachedImpGroups = data.groups || [];
  } catch (_) {
    cachedImpGroups = [];
  }
  return cachedImpGroups;
}

function invalidateImpGroupCache() {
  cachedImpGroups = null;
}

function showContextMenu(x, y) {
  document.getElementById('contextMenu').classList.remove('show');
  const menu = document.getElementById('contextMenu');
  const target = contextMenuTarget;
  if (!target) return;

  // Build move-to submenu
  const moveSubmenu = document.getElementById('ctxMoveSubmenu');
  moveSubmenu.innerHTML = '';

  getImpGroups().then(groups => {
    groups.forEach(g => {
      // Skip current group for email and sender (no point moving to where it already is)
      if ((target.type === 'email' || target.type === 'sender') && target.impGroupId === g.id) return;

      const item = document.createElement('div');
      item.className = 'context-menu-item submenu-item';
      item.textContent = g.name;
      const impGroupId = g.id;
      item.addEventListener('click', (e) => {
        e.stopPropagation();
        const savedTarget = contextMenuTarget;
        hideContextMenu();
        if (savedTarget) moveTargetToImportance(impGroupId, savedTarget);
      });
      moveSubmenu.appendChild(item);
    });
  });

	  const markReadItem = document.getElementById('ctxMarkRead');
	  const markUnreadItem = document.getElementById('ctxMarkUnread');
	  const editNameItem = document.getElementById('ctxEditName');
	  const deleteItem = document.getElementById('ctxDeleteAll');
	  const divider1 = document.getElementById('ctxDivider1');
	  const divider2 = document.getElementById('ctxDivider2');
	  const moveSection = document.getElementById('ctxMoveSection');
	  if (target.type === 'email') {
	    markReadItem.style.display = 'none';
	    markUnreadItem.style.display = 'flex';
	    editNameItem.style.display = 'none';
	    divider1.style.display = 'block';
	    moveSection.style.display = 'block';
	    divider2.style.display = 'none';
	    deleteItem.style.display = 'none';
	  } else if (target.type === 'folder') {
	    markReadItem.style.display = 'flex';
	    markUnreadItem.style.display = 'flex';
	    editNameItem.style.display = 'none';
	    divider1.style.display = 'none';
	    moveSection.style.display = 'none';
	    divider2.style.display = 'none';
	    deleteItem.style.display = 'none';
	  } else if (target.type === 'sender') {
	    markReadItem.style.display = 'flex';
	    markUnreadItem.style.display = 'flex';
	    editNameItem.style.display = 'flex';
	    divider1.style.display = 'block';
	    moveSection.style.display = 'block';
	    divider2.style.display = 'block';
	    deleteItem.style.display = 'flex';
	  } else {
	    markReadItem.style.display = 'flex';
	    markUnreadItem.style.display = 'flex';
	    editNameItem.style.display = 'none';
	    divider1.style.display = 'block';
	    moveSection.style.display = 'block';
	    divider2.style.display = 'block';
	    deleteItem.style.display = 'flex';
	  }

  menu.style.left = x + 'px';
  menu.style.top = y + 'px';
  menu.classList.add('show');
}

async function moveTargetToImportance(impGroupId, target) {
  target = target || contextMenuTarget;
  if (!target) return;

  try {
    if (target.type === 'email') {
      await api(`/api/emails/${target.id}/move-importance`, {
        method: 'POST',
        body: { importance_group_id: impGroupId },
      });
    } else if (target.type === 'sender') {
      await api(`/api/sender-groups/${target.id}`, {
        method: 'PUT',
        body: { importance_group_id: impGroupId },
      });
    }
    loadTree();
  } catch (err) {
    alert(__('Failed to move: {0}', err.message));
  }
}

async function moveEmailToImportance(emailId, impGroupId) {
  try {
    await api(`/api/emails/${emailId}/move-importance`, {
      method: 'POST',
      body: { importance_group_id: impGroupId },
    });
    loadTree();
  } catch (err) {
    alert(__('Failed to move email: {0}', err.message));
  }
}

async function moveSenderGroupImportance(senderGroupId, impGroupId) {
  try {
    await api(`/api/sender-groups/${senderGroupId}`, {
      method: 'PUT',
      body: { importance_group_id: impGroupId },
    });
    loadTree();
  } catch (err) {
    alert(__('Failed to move group: {0}', err.message));
  }
}

function hideContextMenu() {
  document.getElementById('contextMenu').classList.remove('show');
  contextMenuTarget = null;
}

async function contextEditName() {
  const target = contextMenuTarget;
  hideContextMenu();
  if (!target || target.type !== 'sender') return;

  const result = await showDialog({ title: __('Edit Name') });
  if (result === null) return;
  const trimmed = result.trim();
  if (!trimmed) return;

  try {
    await api(`/api/sender-groups/${target.id}`, {
      method: 'PUT',
      body: { group_name: trimmed },
    });
    loadTree();
  } catch (err) {
    alert(__('Failed to update: {0}', err.message));
  }
}

async function contextDeleteAll() {
  const target = contextMenuTarget;
  hideContextMenu();
  if (!target) return;
  if (!(await showDialog({ title: __('Delete All'), message: __('Delete ALL emails in this group?') }))) return;

  try {
    if (target.type === 'sender') {
      await api(`/api/emails/group/${target.id}`, { method: 'DELETE' });
    } else if (target.type === 'imp') {
      await api(`/api/emails/group/importance/${target.id}`, { method: 'DELETE' });
    }
    showEmptyState();
    loadTree();
  } catch (err) {
    alert(__('Failed: {0}', err.message));
  }
}

async function markGroupAsRead() {
  const target = contextMenuTarget;
  hideContextMenu();
  if (!target) return;

  try {
    if (target.type === 'folder') {
      await api('/api/emails/mark-read', {
        method: 'POST',
        body: { scope: 'folder' },
      });
    } else if (target.type === 'imp') {
      await api('/api/emails/mark-read', {
        method: 'POST',
        body: { scope: 'imp', imp_group_id: target.id },
      });
    } else if (target.type === 'sender') {
      await api('/api/emails/mark-read', {
        method: 'POST',
        body: { scope: 'sender', sender_group_id: target.id },
      });
    }
    loadTree();
  } catch (err) {
    alert(__('Failed to mark as read: {0}', err.message));
  }
}

async function markGroupAsUnread() {
  const target = contextMenuTarget;
  hideContextMenu();
  if (!target) return;

  try {
    const body = { read: false };
    if (target.type === 'folder') {
      body.scope = 'folder';
    } else if (target.type === 'imp') {
      body.scope = 'imp';
      body.imp_group_id = target.id;
    } else if (target.type === 'sender') {
      body.scope = 'sender';
      body.sender_group_id = target.id;
    } else if (target.type === 'email') {
      body.scope = 'email';
      body.email_id = target.id;
    }
    await api('/api/emails/mark-read', {
      method: 'POST',
      body,
    });
    loadTree();
  } catch (err) {
    alert(__('Failed to mark as unread: {0}', err.message));
  }
}

// Close context menu on click anywhere else
document.addEventListener('click', (e) => {
  if (!e.target.closest('.context-menu')) {
    hideContextMenu();
  }
});

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
  document.getElementById('emptyState').style.display = 'none';
  document.getElementById('emailDetailView').style.display = 'none';
  const composeView = document.getElementById('composeView');
  composeView.style.display = 'flex';
  composeView.style.flex = '1';
  document.getElementById('composeTo').value = '';
  document.getElementById('composeSubject').value = forwardText ? __('Fwd: {0}', forwardText) : '';
  document.getElementById('composeBody').value = '';
  document.getElementById('composeStatus').textContent = '';
}

function closeCompose() {
  const composeView = document.getElementById('composeView');
  composeView.style.display = 'none';
  composeView.style.flex = '';
  showEmptyState();
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
      loadTree();
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
    loadTree();
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
    statusEl.textContent = __('Fetch started...');
    startFetchProgressPolling();
  } catch (err) {
    statusEl.textContent = __('Fetch failed');
  }
}

function formatCountdown(seconds) {
  if (seconds <= 0) return __('Due');
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  if (m > 0) {
    return __('{0}m {1}s', m, s < 10 ? '0' + s : s);
  }
  return __('{0}s', s);
}

let serverStatusData = { servers: [] };
let serverStatusTimer = null;
let serverStatusRefreshing = false;

let fetchProgressData = {};
let fetchProgressTimer = null;
let fetchProgressCount = 0;

async function refreshFetchProgress() {
  try {
    const data = await api('/api/fetch-progress');
    fetchProgressData = data.servers || {};
    const entries = Object.values(fetchProgressData);

    renderServerStatusBar();

    // Only consider completion when there's actual progress data
    // (IMAP connection may take 10-30s before progress starts)
    if (entries.length === 0) {
      fetchProgressCount = 0;
      return;
    }

    const hasActive = entries.some(p => p.status === 'fetching');
    if (!hasActive) {
      fetchProgressCount++;
      if (fetchProgressCount > 3) {  // ~6 seconds after last active
        stopFetchProgressPolling();
        loadTree();
      }
    } else {
      fetchProgressCount = 0;
    }
  } catch (_) {
    stopFetchProgressPolling();
  }
}

function startFetchProgressPolling() {
  stopFetchProgressPolling();
  fetchProgressCount = 0;
  fetchProgressTimer = setInterval(refreshFetchProgress, 2000);
  refreshFetchProgress();
}

function stopFetchProgressPolling() {
  if (fetchProgressTimer) {
    clearInterval(fetchProgressTimer);
    fetchProgressTimer = null;
  }
  // Keep final progress data for a bit so UI shows done state
  const hasFinal = Object.values(fetchProgressData).some(
    p => p.status === 'done' || p.status === 'error'
  );
  if (!hasFinal) {
    fetchProgressData = {};
    renderServerStatusBar();
  }
}

async function refreshServerStatusBar() {
  if (serverStatusRefreshing) return;
  serverStatusRefreshing = true;
  try {
    const data = await api('/api/next-fetch');
    serverStatusData = data;
    renderServerStatusBar();
  } catch (_) {
  } finally {
    serverStatusRefreshing = false;
  }
}

function renderServerStatusBar() {
  const container = document.getElementById('serverStatusBar');
  if (!container) return;
  container.innerHTML = '';

  const servers = serverStatusData.servers || [];
  if (servers.length === 0) {
    container.style.display = 'none';
    return;
  }
  container.style.display = 'flex';

  servers.forEach(srv => {
    const row = document.createElement('div');
    row.className = 'server-status-row';

    const progress = fetchProgressData[srv.id];
    const isFetching = progress && progress.status === 'fetching';
    const isDone = progress && (progress.status === 'done' || progress.status === 'error');

    if (isFetching || isDone) {
      // Show progress bar
      const total = progress.total || 0;
      const current = progress.current || 0;
      const pct = total > 0 ? Math.round((current / total) * 100) : 0;

      const statusText = isDone
        ? (progress.status === 'done' ? __('Done') : __('Failed'))
        : __('Fetching {0}/{1}', current, total);

      row.innerHTML = `
        <span class="server-status-name">${escHtml(srv.server_name)}</span>
        <button class="btn btn-sm btn-outline" onclick="composeForServer(${srv.id})">${__('Compose')}</button>
        <button class="btn btn-sm btn-outline" onclick="fetchOneServer(${srv.id})" ${isFetching ? 'disabled' : ''}>${__('Fetch')}</button>
        <div class="fetch-progress-wrap">
          <div class="fetch-progress-bar">
            <div class="fetch-progress-fill" style="width:${pct}%"></div>
          </div>
          <span class="fetch-progress-text">${statusText}</span>
        </div>
      `;
    } else {
      // Normal status display
      let modeText = '';
      if (srv.mode === 'imap_idle') {
        modeText = __('IMAP IDLE auto fetch');
      } else if (srv.mode === 'auto') {
        const seconds = Math.max(0, srv.seconds_until || 0);
        modeText = __('Auto fetch (countdown {0} min): {1}', srv.interval_minutes || 0, formatCountdown(seconds));
      } else {
        modeText = __('Manual');
      }

      row.innerHTML = `
        <span class="server-status-name">${escHtml(srv.server_name)}</span>
        <button class="btn btn-sm btn-outline" onclick="composeForServer(${srv.id})">${__('Compose')}</button>
        <button class="btn btn-sm btn-outline" onclick="fetchOneServer(${srv.id})">${__('Fetch')}</button>
        <span class="server-status-mode">${modeText}</span>
      `;
    }
    container.appendChild(row);
  });
}

function updateServerStatusCountdown() {
  const container = document.getElementById('serverStatusBar');
  if (!container) return;

  const rows = container.querySelectorAll('.server-status-row');
  const servers = serverStatusData.servers || [];
  rows.forEach((row, idx) => {
    const srv = servers[idx];
    if (!srv || srv.mode !== 'auto') return;
    const modeEl = row.querySelector('.server-status-mode');
    if (!modeEl) return;
    const seconds = Math.max(0, (srv.seconds_until || 0) - 1);
    srv.seconds_until = seconds;
    modeEl.textContent = __('Auto fetch (countdown {0} min): {1}', srv.interval_minutes || 0, formatCountdown(seconds));
  });
}

function startServerStatusBar() {
  refreshServerStatusBar();
  setInterval(refreshServerStatusBar, 30000);
  if (!serverStatusTimer) {
    serverStatusTimer = setInterval(updateServerStatusCountdown, 1000);
  }
}

function composeForServer(serverId) {
  showCompose(null);
  const select = document.getElementById('composeServer');
  if (select) select.value = String(serverId);
}

async function fetchOneServer(serverId) {
  try {
    await api(`/api/servers/${serverId}/fetch`, { method: 'POST' });
    startFetchProgressPolling();
  } catch (err) {
    alert(__('Failed: {0}', err.message));
  }
}

// ===== Dropdown =====
document.addEventListener('click', function(e) {
  const dropdown = document.getElementById('userDropdown');
  const menu = document.getElementById('dropdownMenu');
  if (dropdown && !dropdown.contains(e.target)) {
    menu.classList.remove('show');
  }
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

// ===== Splitter: Vertical (sidebar / content) =====
(function initSplitterV() {
  const splitter = document.getElementById('splitterV');
  const sidebar = document.getElementById('sidebar');
  let dragging = false;

  splitter.addEventListener('mousedown', (e) => {
    dragging = true;
    splitter.classList.add('active');
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    e.preventDefault();
  });

  document.addEventListener('mousemove', (e) => {
    if (!dragging) return;
    let w = e.clientX;
    w = Math.max(160, Math.min(500, w));
    sidebar.style.width = w + 'px';
  });

  document.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false;
    splitter.classList.remove('active');
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
    try { localStorage.setItem('mail_sidebar_width', sidebar.style.width); } catch (_) {}
  });

  try {
    const saved = localStorage.getItem('mail_sidebar_width');
    if (saved) sidebar.style.width = saved;
  } catch (_) {}
})();

// ===== Helper =====
function escHtml(str) {
  if (!str) return '';
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}