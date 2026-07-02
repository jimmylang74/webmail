/* ===== Mail Module - Main email interface ===== */

let currentState = {
  currentEmailId: null,
  currentSenderGroupId: null,
  currentSenderGroupImpGroupId: null,
  currentImpGroupId: null,
  currentFolder: 'inbox',
  currentFolderId: null,
};
let allServers = [];
let contextMenuTarget = null;
let sortByTime = false;

// Tree state preservation
let expandedNodeIds = new Set();
let selectedEmailNodeId = null;
let currentDraftId = null;

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
      const pref = await api('/api/preferences/sort-by-time');
      sortByTime = pref.sort_by_time;
      updateDisplayModeBtn(pref.sort_by_time);
    } catch (_) {}
    try {
      const gbs = await api('/api/preferences/group-by-server');
      document.getElementById('groupByServer').checked = gbs.group_by_server;
    } catch (_) {}
    loadTree();
    loadServersForCompose();
    startServerStatusBar();

    document.addEventListener('keydown', async (e) => {
      if (e.key !== 'Delete') return;
      const tag = e.target?.tagName?.toLowerCase();
      if (tag === 'input' || tag === 'textarea') return;

      const folder = currentState.currentFolder || 'inbox';

      if (currentState.currentEmailId) {
        e.preventDefault();
        if (folder === 'deleted') {
          if (!(await showDialog({ title: __('Empty Deleted'), message: __('Delete this email permanently?') + ' ' + __('This will permanently delete the email from the database.') }))) return;
          try {
            const result = await api(`/api/emails/${currentState.currentEmailId}`, { method: 'DELETE' });
            if (!result.success) { alert(__('Failed')); return; }
            showEmptyState();
            loadTree();
          } catch (err) {
            alert(__('Failed: {0}', err.message));
          }
        } else {
          deleteCurrentEmail();
        }
      } else if (currentState.currentImpGroupId) {
        e.preventDefault();
        if (deleteProgressTimer) {
          await showDialog({ title: __('Delete All'), message: __('Previous deletion still in progress. Please wait for it to complete before deleting again.') });
          return;
        }
        const serverChecked = document.getElementById('deleteServerCopy').checked;
        let msg = __('Delete ALL emails in this group?');
        if (serverChecked) {
          msg += '\n\n' + __('Also delete from server') + ': ' + __('This will permanently delete the server copies as well.');
        }
        if (!(await showDialog({ title: __('Delete All'), message: msg }))) return;
        try {
          if (serverChecked) {
            const resp = await api(`/api/emails/group/importance/${currentState.currentImpGroupId}/delete-progress?delete_from_server=1`, { method: 'POST' });
            if (resp.server_delete_total > 0) {
              showDeleteProgressDialog(resp.server_delete_total);
              startDeleteProgressPolling(resp.task_id, resp.server_delete_total);
            } else {
              showEmptyState();
              loadTree();
            }
          } else {
            await api(`/api/emails/group/importance/${currentState.currentImpGroupId}`, { method: 'DELETE' });
            showEmptyState();
            loadTree();
          }
        } catch (err) {
          alert(__('Failed: {0}', err.message));
        }
      } else if (currentState.currentSenderGroupId) {
        e.preventDefault();
        if (folder !== 'deleted' && deleteProgressTimer) {
          await showDialog({ title: __('Delete All'), message: __('Previous deletion still in progress. Please wait for it to complete before deleting again.') });
          return;
        }
        if (folder === 'deleted') {
          if (!(await showDialog({ title: __('Empty Deleted'), message: __('Permanently delete all emails in Deleted folder?') }))) return;
          try {
            const result = await api(`/api/emails/deleted-group/${currentState.currentSenderGroupId}`, {
              method: 'POST',
              body: { action: 'clear' },
            });
            if (!result.success) { alert(__('Failed')); return; }
            showEmptyState();
            loadTree();
          } catch (err) {
            alert(__('Failed: {0}', err.message));
          }
        } else {
          const activeSenderEl = document.querySelector('.tree-item.active[data-sender-group-id]');
          const activeImpId = activeSenderEl ? activeSenderEl.dataset.senderGroupImpGroupId : currentState.currentSenderGroupImpGroupId;

          const isDrafts = folder === 'drafts';
          const serverChecked = document.getElementById('deleteServerCopy').checked;
          let msg = __('Delete ALL emails in this group?');
          if (!isDrafts && serverChecked) {
            msg += '\n\n' + __('Also delete from server') + ': ' + __('This will permanently delete the server copies as well.');
          }
          if (!(await showDialog({ title: __('Delete All'), message: msg }))) return;
          try {
            const impParam = activeImpId ? `&imp_group_id=${activeImpId}` : '';
            if (!isDrafts && serverChecked) {
              const resp = await api(`/api/emails/group/${currentState.currentSenderGroupId}/delete-progress?delete_from_server=1${impParam}`, { method: 'POST' });
              if (resp.server_delete_total > 0) {
                showDeleteProgressDialog(resp.server_delete_total);
                startDeleteProgressPolling(resp.task_id, resp.server_delete_total);
              } else {
                showEmptyState();
                loadTree();
              }
            } else {
              await api(`/api/emails/group/${currentState.currentSenderGroupId}?delete_from_server=0${impParam}`, { method: 'DELETE' });
              showEmptyState();
              loadTree();
            }
          } catch (err) {
            alert(__('Failed: {0}', err.message));
          }
        }
      }
    });
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
  const isCustomFolder = !!node.is_custom_folder;
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
	      currentState.currentFolder = node.folder || 'inbox';
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
	      showEmptyState();
	      const childrenContainer = item.querySelector('.tree-children');
	      if (childrenContainer) {
	        childrenContainer.classList.toggle('collapsed');
	        const toggleEl = main.querySelector('.tree-toggle');
	        if (toggleEl) toggleEl.classList.toggle('collapsed');
	      }
		      if (isSenderGroup) {
		        clearDeleteProgressStatus();
		        main.classList.add('active');
		        main.dataset.senderGroupId = node.sender_group_id;
		        main.dataset.senderGroupImpGroupId = node.imp_group_id != null ? String(node.imp_group_id) : '';
		        currentState.currentSenderGroupId = node.sender_group_id;
		        currentState.currentSenderGroupImpGroupId = node.imp_group_id;
		        currentState.currentFolder = node.folder || 'inbox';
		      } else if (isImpGroup) {
	        main.classList.add('active');
	        currentState.currentImpGroupId = node.imp_group_id;
	        currentState.currentFolder = 'inbox';
	      }
    } else if (node.is_custom_folder) {
      currentState.currentFolderId = node.folder_id;
      currentState.currentFolder = node.name;
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
      contextMenuTarget = {type: 'email', id: node.email_id, folder: node.folder, folderId: node.folder_id, impGroupId: node.imp_group_id, senderGroupId: node.sender_group_id, serverId: node.server_id || null, isRead: !!node.is_read};
      showContextMenu(e.clientX, e.clientY);
    } else if (isImpGroup) {
      contextMenuTarget = {type: 'imp', id: node.imp_group_id, serverId: node.server_id || null, isSystem: !!node.is_system};
      showContextMenu(e.clientX, e.clientY);
    } else if (isSenderGroup) {
      contextMenuTarget = {type: 'sender', id: node.sender_group_id, folder: node.folder, impGroupId: node.imp_group_id, serverId: node.server_id || null};
      showContextMenu(e.clientX, e.clientY);
    } else if (node.id === 'inbox') {
      contextMenuTarget = {type: 'folder', id: 'inbox'};
      showContextMenu(e.clientX, e.clientY);
    } else if (node.server_id) {
      // Server-grouped inbox (when "Group by server" is checked)
      contextMenuTarget = {type: 'folder', id: 'inbox', serverId: node.server_id};
      showContextMenu(e.clientX, e.clientY);
    } else if (node.id === 'deleted') {
      contextMenuTarget = {type: 'folder', id: 'deleted'};
      showContextMenu(e.clientX, e.clientY);
    } else if (node.is_custom_folder) {
      contextMenuTarget = {type: 'custom_folder', id: node.id, folder_id: node.folder_id, name: node.name};
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
          folderId: node.folder_id,
          impGroupId: node.imp_group_id,
        }));
      } else if (isSenderGroup) {
        e.dataTransfer.setData('text/plain', JSON.stringify({
          type: 'sender',
          senderGroupId: node.sender_group_id,
          serverId: node.server_id,
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
          moveSenderGroupImportance(data.senderGroupId, impGroupId, data.serverId);
        }
      } catch (_) {}
    });
  }

  if (isCustomFolder) {
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
    main.addEventListener('drop', async (e) => {
      e.preventDefault();
      e.stopPropagation();
      main.classList.remove('drag-over');
      const raw = e.dataTransfer.getData('text/plain');
      if (!raw) return;
      try {
        const data = JSON.parse(raw);
        const folderId = node.folder_id;
        if (data.type === 'email' && data.emailId) {
          await api(`/api/emails/${data.emailId}/move`, {
            method: 'POST',
            body: { folder_id: folderId },
          });
          loadTree();
        }
      } catch (err) {
        alert(__('Failed to move: {0}', err.message));
      }
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
  currentState.currentSenderGroupId = null;
  currentState.currentSenderGroupImpGroupId = null;
  currentState.currentImpGroupId = null;
  currentState.currentFolder = 'inbox';
  // Clear email highlight
  document.querySelectorAll('.tree-item.active').forEach(el => el.classList.remove('active'));
  selectedEmailNodeId = null;
}

// ===== Email Detail =====
async function openEmail(emailId) {
  clearDeleteProgressStatus();
  try {
    const data = await api(`/api/emails/${emailId}`);
    const email = data.email;
    currentState.currentEmailId = emailId;
    currentState.currentSenderGroupId = null;
    currentState.currentSenderGroupImpGroupId = null;
    currentState.currentImpGroupId = null;

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

    const deleteBtn = document.getElementById('deleteBtn');
    const restoreBtn = document.getElementById('restoreBtn');
    const editDraftBtn = document.getElementById('editDraftBtn');
    if (email.folder === 'deleted') {
      if (deleteBtn) deleteBtn.style.display = 'none';
      if (restoreBtn) restoreBtn.style.display = '';
      if (editDraftBtn) editDraftBtn.style.display = 'none';
    } else {
      if (deleteBtn) deleteBtn.style.display = '';
      if (restoreBtn) restoreBtn.style.display = 'none';
      if (editDraftBtn) editDraftBtn.style.display = (email.folder === 'drafts') ? '' : 'none';
    }
  } catch (err) {
    alert(__('Failed to load email: {0}', err.message));
  }
}

async function editDraft() {
  if (!currentState.currentEmailId) return;
  try {
    const data = await api(`/api/emails/${currentState.currentEmailId}`);
    const email = data.email;
    if (email.folder !== 'drafts') return;
    // Ensure server list is loaded before selecting
    await (serversLoaded || Promise.resolve());
    showCompose(email.subject, email.server_id, email.body_text, email.recipients, email.id, email.body_html);
  } catch (_) {}
}

async function deleteCurrentEmail() {
  if (!currentState.currentEmailId) return;

  const deleteServerChecked = document.getElementById('deleteServerCopy').checked;
  let dialogMsg = __('Move this email to trash?');
  if (deleteServerChecked) {
    dialogMsg += '\n\n' + __('Also delete from server') + ': ' + __('This will permanently delete the server copies as well.');
  }
  if (!(await showDialog({ title: __('Delete Email'), message: dialogMsg }))) return;

  try {
    const body = { folder: 'deleted' };
    const deleteServerCopy = document.getElementById('deleteServerCopy');
    if (deleteServerCopy && deleteServerCopy.checked) {
      body.delete_from_server = true;
    }
    const result = await api(`/api/emails/${currentState.currentEmailId}/move`, {
      method: 'POST',
      body,
    });
    const deleteResult = result && result.delete_from_server;
    if (deleteResult && !deleteResult.success) {
      console.warn('Server delete failed:', deleteResult.error);
    }
    showEmptyState();
    loadTree();
  } catch (err) {
    alert(__('Failed to delete: {0}', err.message));
  }
}

async function contextDeleteEmail() {
  const target = contextMenuTarget;
  hideContextMenu();
  if (!target || target.type !== 'email') return;

  const deleteServerChecked = document.getElementById('deleteServerCopy').checked;
  let dialogMsg = __('Move this email to trash?');
  if (deleteServerChecked) {
    dialogMsg += '\n\n' + __('Also delete from server') + ': ' + __('This will permanently delete the server copies as well.');
  }
  if (!(await showDialog({ title: __('Delete'), message: dialogMsg }))) return;

  try {
    const body = { folder: 'deleted' };
    const deleteServerCopy = document.getElementById('deleteServerCopy');
    if (deleteServerCopy && deleteServerCopy.checked) {
      body.delete_from_server = true;
    }
    const result = await api(`/api/emails/${target.id}/move`, {
      method: 'POST',
      body,
    });
    const deleteResult = result && result.delete_from_server;
    if (deleteResult && !deleteResult.success) {
      console.warn('Server delete failed:', deleteResult.error);
    }
    showEmptyState();
    loadTree();
  } catch (err) {
    alert(__('Failed to delete: {0}', err.message));
  }
}

async function restoreCurrentEmail() {
  if (!currentState.currentEmailId) return;
  if (!(await showDialog({ title: __('Restore Email'), message: __('Move this email back to its original folder?') }))) return;

  try {
    const result = await api(`/api/emails/${currentState.currentEmailId}/restore`, {
      method: 'POST',
    });
    if (result.success) {
      showEmptyState();
      loadTree();
    }
  } catch (err) {
    alert(__('Failed to restore: {0}', err.message));
  }
}

async function forwardEmail() {
  if (!currentState.currentEmailId) return;
  // Ensure server list is loaded before we try badge matching
  await (serversLoaded || Promise.resolve());
  let serverId = null;
  let fwdSubject = '';
  let fwdBody = '';
  let fwdHtml = '';
  try {
    const data = await api(`/api/emails/${currentState.currentEmailId}`);
    const email = data.email;
    if (email) {
      // Use server_id if available
      if (email.server_id) {
        serverId = email.server_id;
      }
      // Fallback: server_id is null (e.g. server was deleted, FK set null).
      // Match by server_badge text, or if only one SMTP server exists, use it.
      if (!serverId && allServers.length) {
        const smtpServers = allServers.filter(s => s.outgoing_server);
        if (smtpServers.length === 1) {
          serverId = smtpServers[0].id;
        } else if (email.server_badge) {
          const match = smtpServers.find(s =>
            s.server_name === email.server_badge
            || (s.server_name && email.server_badge && s.server_name.includes(email.server_badge))
            || (s.server_name && email.server_badge && email.server_badge.includes(s.server_name))
          );
          if (match) serverId = match.id;
        }
      }
      fwdSubject = email.subject || '';
      const date = email.received_date ? new Date(email.received_date).toLocaleString() : '';
      const textContent = email.body_text || '';
      const htmlContent = email.body_html || '';

      // Plain text forward body
      fwdBody = '---------- Forwarded email ----------\n'
              + 'From: ' + (email.sender || '') + '\n'
              + 'Subject: ' + (email.subject || '') + '\n'
              + (date ? 'Date: ' + date + '\n' : '')
              + '\n'
              + (textContent || __('(No content)'));

      // HTML forward body (include original HTML formatting)
      if (htmlContent.trim()) {
        fwdHtml = '<div style="padding-bottom:8px;margin-bottom:8px;border-bottom:1px solid #ccc;color:#666;font-size:12px;">'
                + '<strong>---------- Forwarded email ----------</strong><br>'
                + '<b>From:</b> ' + escHtml(email.sender || '') + '<br>'
                + '<b>Subject:</b> ' + escHtml(email.subject || '') + '<br>'
                + (date ? '<b>Date:</b> ' + escHtml(date) + '<br>' : '')
                + '</div>'
                + htmlContent;
      }
    }
  } catch (_) {}
  showCompose(fwdSubject, serverId, fwdBody, null, null, fwdHtml);
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

function _hideAllContextItems() {
  const ids = ['ctxMarkRead', 'ctxMarkUnread', 'ctxEditName', 'ctxAddContact',
    'ctxDivider1', 'ctxMoveSection', 'ctxDivider2', 'ctxDeleteAll', 'ctxDeleteEmail',
    'ctxDeletedDivider1', 'ctxDeletedRestore', 'ctxDeletedClear',
    'ctxRenameFolder', 'ctxDeleteFolder', 'ctxAddFolder',
    'ctxAddImpGroup', 'ctxRenameImpGroup', 'ctxDeleteImpGroup'];
  ids.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.style.display = 'none';
  });
}

function showContextMenu(x, y) {
  document.getElementById('contextMenu').classList.remove('show');
  const menu = document.getElementById('contextMenu');
  const target = contextMenuTarget;
  if (!target) return;
  _hideAllContextItems();

  const moveSubmenu = document.getElementById('ctxMoveSubmenu');
  moveSubmenu.innerHTML = '';

  const isDeleted = target.folder === 'deleted' || target.id === 'deleted';

  if (!isDeleted) {
    const inCustomFolder = (target.type === 'email' && target.folderId) ||
                           (target.type === 'sender' && target.folder && target.folder !== 'inbox' && target.folder !== 'deleted');

    if (inCustomFolder) {
      const item = document.createElement('div');
      item.className = 'context-menu-item submenu-item';
      item.textContent = 'Inbox';
      item.addEventListener('click', async (e) => {
        e.stopPropagation();
        const savedTarget = contextMenuTarget;
        hideContextMenu();
        if (!savedTarget) return;
        try {
          if (savedTarget.type === 'email' && savedTarget.id) {
            await api(`/api/emails/${savedTarget.id}/move`, {
              method: 'POST',
              body: { folder: 'inbox' },
            });
          } else if (savedTarget.type === 'sender' && savedTarget.id) {
            await api(`/api/emails/group/${savedTarget.id}/move`, {
              method: 'POST',
              body: { folder: 'inbox' },
            });
          }
          loadTree();
        } catch (err) {
          alert(__('Failed to move: {0}', err.message));
        }
      });
      moveSubmenu.appendChild(item);
    } else {
      // In time-sorted mode, don't show importance groups in Move-to for individual emails
      if (!(target.type === 'email' && sortByTime)) {
        getImpGroups().then(groups => {
          groups.forEach(g => {
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
      }

      api('/api/folders').then(data => {
        const folders = data.folders || [];
        folders.forEach(f => {
          if (f.is_system) return;
          const item = document.createElement('div');
          item.className = 'context-menu-item submenu-item';
          item.textContent = f.name;
          const folderId = f.id;
          item.addEventListener('click', async (e) => {
            e.stopPropagation();
            const savedTarget = contextMenuTarget;
            hideContextMenu();
            if (!savedTarget) return;
            try {
              if (savedTarget.type === 'email' && savedTarget.id) {
                await api(`/api/emails/${savedTarget.id}/move`, {
                  method: 'POST',
                  body: { folder_id: folderId },
                });
              } else if (savedTarget.type === 'sender' && savedTarget.id) {
                await api(`/api/emails/group/${savedTarget.id}/move`, {
                  method: 'POST',
                  body: { folder_id: folderId },
                });
              }
              loadTree();
            } catch (err) {
              alert(__('Failed to move: {0}', err.message));
            }
          });
          moveSubmenu.appendChild(item);
        });
      });
    }
  }

  const markReadItem = document.getElementById('ctxMarkRead');
  const markUnreadItem = document.getElementById('ctxMarkUnread');
  const editNameItem = document.getElementById('ctxEditName');
  const deleteGroupItem = document.getElementById('ctxDeleteAll');
  const deleteEmailItem = document.getElementById('ctxDeleteEmail');
  const divider1 = document.getElementById('ctxDivider1');
  const divider2 = document.getElementById('ctxDivider2');
  const moveSection = document.getElementById('ctxMoveSection');
  const deletedClear = document.getElementById('ctxDeletedClear');
  const deletedRestore = document.getElementById('ctxDeletedRestore');
  const deletedDivider1 = document.getElementById('ctxDeletedDivider1');

  if (isDeleted) {
    if (target.id === 'deleted' && target.type === 'folder') {
      deletedClear.style.display = 'flex';
    } else if (target.type === 'sender') {
      deletedRestore.style.display = 'flex';
      deletedClear.style.display = 'flex';
      deletedDivider1.style.display = 'block';
    } else if (target.type === 'email') {
      deletedRestore.style.display = 'flex';
      deletedClear.style.display = 'flex';
      deletedDivider1.style.display = 'block';
    }
  } else if (target.type === 'email') {
    markReadItem.style.display = 'none';
    markUnreadItem.style.display = 'flex';
    editNameItem.style.display = 'none';
    divider1.style.display = 'block';
    moveSection.style.display = 'block';
    divider2.style.display = 'block';
    deleteEmailItem.style.display = 'flex';
    deleteGroupItem.style.display = 'none';
  } else if (target.type === 'folder') {
    markReadItem.style.display = 'flex';
    markUnreadItem.style.display = 'flex';
    editNameItem.style.display = 'none';
    divider1.style.display = 'block';
    moveSection.style.display = 'none';
    divider2.style.display = 'none';
    deleteGroupItem.style.display = 'none';
    deleteEmailItem.style.display = 'none';
    const addImpGroupItem = document.getElementById('ctxAddImpGroup');
    if (addImpGroupItem && (target.id === 'inbox' || target.serverId)) {
      addImpGroupItem.style.display = 'flex';
    }
  } else if (target.type === 'custom_folder') {
    markReadItem.style.display = 'none';
    markUnreadItem.style.display = 'none';
    editNameItem.style.display = 'none';
    divider1.style.display = 'none';
    moveSection.style.display = 'none';
    divider2.style.display = 'block';
    deleteGroupItem.style.display = 'none';
    deleteEmailItem.style.display = 'none';
    const renameItem = document.getElementById('ctxRenameFolder');
    const deleteItem = document.getElementById('ctxDeleteFolder');
    if (renameItem) renameItem.style.display = 'flex';
    if (deleteItem) deleteItem.style.display = 'flex';
  } else if (target.type === 'imp') {
    markReadItem.style.display = 'flex';
    markUnreadItem.style.display = 'flex';
    editNameItem.style.display = 'none';
    divider1.style.display = 'none';
    moveSection.style.display = 'none';
    divider2.style.display = 'none';
    deleteGroupItem.style.display = 'none';
    deleteEmailItem.style.display = 'none';
    if (!target.isSystem) {
      const renameImpItem = document.getElementById('ctxRenameImpGroup');
      const deleteImpItem = document.getElementById('ctxDeleteImpGroup');
      if (renameImpItem) renameImpItem.style.display = 'flex';
      if (deleteImpItem) deleteImpItem.style.display = 'flex';
    }
  } else if (target.type === 'blank_area') {
    const addFolderItem = document.getElementById('ctxAddFolder');
    const addImpGroupItem = document.getElementById('ctxAddImpGroup');
    if (addFolderItem) addFolderItem.style.display = 'flex';
    if (addImpGroupItem) addImpGroupItem.style.display = 'flex';
  } else if (target.type === 'sender') {
    markReadItem.style.display = 'flex';
    markUnreadItem.style.display = 'flex';
    editNameItem.style.display = 'flex';
    divider1.style.display = 'block';
    moveSection.style.display = 'block';
    divider2.style.display = 'block';
    deleteGroupItem.style.display = 'flex';
    deleteEmailItem.style.display = 'none';
  } else {
    markReadItem.style.display = 'flex';
    markUnreadItem.style.display = 'flex';
    editNameItem.style.display = 'none';
    divider1.style.display = 'block';
    moveSection.style.display = 'block';
    divider2.style.display = 'block';
    deleteGroupItem.style.display = 'flex';
    deleteEmailItem.style.display = 'none';
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
      const body = { importance_group_id: impGroupId };
      if (target.serverId) body.server_id = target.serverId;
      await api(`/api/sender-groups/${target.id}`, {
        method: 'PUT',
        body,
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

async function moveSenderGroupImportance(senderGroupId, impGroupId, serverId) {
  try {
    const body = { importance_group_id: impGroupId };
    if (serverId) body.server_id = serverId;
    await api(`/api/sender-groups/${senderGroupId}`, {
      method: 'PUT',
      body,
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

async function contextDeletedRestore() {
  const target = contextMenuTarget;
  hideContextMenu();
  if (!target) return;

  try {
    if (target.type === 'email' && target.id) {
      const result = await api(`/api/emails/${target.id}/restore`, { method: 'POST' });
      if (!result.success) { alert(__('Failed to restore')); return; }
    } else if (target.type === 'sender' && target.id) {
      const result = await api(`/api/emails/deleted-group/${target.id}`, {
        method: 'POST',
        body: { action: 'restore' },
      });
      if (!result.success) { alert(__('Failed to restore')); return; }
    }
    showEmptyState();
    loadTree();
  } catch (err) {
    alert(__('Failed to restore: {0}', err.message));
  }
}

async function contextDeletedClear() {
  const target = contextMenuTarget;
  hideContextMenu();
  if (!target) return;

  let title = __('Empty Deleted');
  let msg = __('Permanently delete all emails in Deleted folder?');
  if (target.type === 'sender') {
    msg = __('Delete ALL emails in this group?');
  } else if (target.type === 'email') {
    msg = __('Delete this email permanently?');
  }
  const deleteServerChecked = document.getElementById('deleteServerCopy').checked;
  if (deleteServerChecked) {
    msg += '\n\n' + __('This will permanently delete the server copies as well.');
    if (!(await showDialog({ title, message: msg }))) return;
  } else {
    if (!(await showDialog({ title, message: msg }))) return;
  }

  try {
    if (target.type === 'folder' && target.id === 'deleted') {
      const result = await api('/api/emails/clear-deleted', { method: 'POST' });
      if (!result.success) { alert(__('Failed')); return; }
    } else if (target.type === 'sender' && target.id) {
      const result = await api(`/api/emails/deleted-group/${target.id}`, {
        method: 'POST',
        body: { action: 'clear' },
      });
      if (!result.success) { alert(__('Failed')); return; }
    } else if (target.type === 'email' && target.id) {
      const result = await api(`/api/emails/${target.id}`, { method: 'DELETE' });
      if (!result.success) { alert(__('Failed')); return; }
    }
    showEmptyState();
    loadTree();
  } catch (err) {
    alert(__('Failed: {0}', err.message));
  }
}

async function contextDeleteAll() {
  const target = contextMenuTarget;
  hideContextMenu();
  if (!target) return;
  if (!(await showDialog({ title: __('Delete All'), message: __('Delete ALL emails in this group?') }))) return;

  try {
    if (target.type === 'sender') {
      const impParam = target.impGroupId ? `?imp_group_id=${target.impGroupId}` : '';
      await api(`/api/emails/group/${target.id}${impParam}`, { method: 'DELETE' });
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
        const body = { scope: 'folder' };
        if (target.serverId) body.server_id = target.serverId;
	      await api('/api/emails/mark-read', {
	        method: 'POST',
	        body,
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
        if (target.serverId) body.server_id = target.serverId;
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

// ---- Right-click on blank tree area → "Add Folder" ----
document.getElementById('treeMenu').addEventListener('contextmenu', (e) => {
  if (e.target === document.getElementById('treeMenu') || e.target.closest('.tree-menu')) {
    const clickedItem = e.target.closest('.tree-item');
    if (!clickedItem) {
      e.preventDefault();
      e.stopPropagation();
      contextMenuTarget = {type: 'blank_area'};
      showContextMenu(e.clientX, e.clientY);
    }
  }
});

// ---- Folder CRUD context menu handlers ----
async function contextAddFolder() {
  hideContextMenu();
  const result = await showDialog({ title: __('New Folder') });
  if (result === null) return;
  const trimmed = result.trim();
  if (!trimmed) return;

  try {
    await api('/api/folders', {
      method: 'POST',
      body: { name: trimmed },
    });
    loadTree();
  } catch (err) {
    alert(__('Failed to create folder: {0}', err.message));
  }
}

async function contextRenameFolder() {
  const target = contextMenuTarget;
  hideContextMenu();
  if (!target || target.type !== 'custom_folder') return;

  const currentName = target.name || '';
  const result = await showDialog({ title: __('Rename Folder') });
  if (result === null) return;
  const trimmed = result.trim();
  if (!trimmed) return;

  try {
    await api(`/api/folders/${target.folder_id}`, {
      method: 'PUT',
      body: { name: trimmed },
    });
    loadTree();
  } catch (err) {
    alert(__('Failed to rename folder: {0}', err.message));
  }
}

async function contextDeleteFolder() {
  const target = contextMenuTarget;
  hideContextMenu();
  if (!target || target.type !== 'custom_folder') return;

  try {
    await api(`/api/folders/${target.folder_id}`, {
      method: 'DELETE',
    });
    loadTree();
  } catch (err) {
    alert(err.message);
  }
}

async function contextAddImpGroup() {
  hideContextMenu();
  const result = await showDialog({ title: __('New Group') });
  if (result === null) return;
  const trimmed = result.trim();
  if (!trimmed) return;

  try {
    await api('/api/groups/importance', {
      method: 'POST',
      body: { name: trimmed },
    });
    invalidateImpGroupCache();
    loadTree();
  } catch (err) {
    alert(__('Failed to create group: {0}', err.message));
  }
}

async function contextRenameImpGroup() {
  const target = contextMenuTarget;
  hideContextMenu();
  if (!target || target.type !== 'imp' || target.isSystem) return;

  const result = await showDialog({ title: __('Rename Group') });
  if (result === null) return;
  const trimmed = result.trim();
  if (!trimmed) return;

  try {
    await api(`/api/groups/importance/${target.id}`, {
      method: 'PUT',
      body: { name: trimmed },
    });
    invalidateImpGroupCache();
    loadTree();
  } catch (err) {
    alert(__('Failed to rename group: {0}', err.message));
  }
}

async function contextDeleteImpGroup() {
  const target = contextMenuTarget;
  hideContextMenu();
  if (!target || target.type !== 'imp' || target.isSystem) return;

  try {
    await api(`/api/groups/importance/${target.id}`, {
      method: 'DELETE',
    });
    invalidateImpGroupCache();
    loadTree();
  } catch (err) {
    alert(err.message);
  }
}

// ===== HTML Editor =====
let editorMode = 'richtext'; // 'richtext' | 'plain'

function execFormat(cmd) {
  const editor = document.getElementById('richEditor');
  editor.focus();
  if (cmd === 'createLink') {
    const url = prompt(__('Enter link URL:'), 'https://');
    if (url) document.execCommand('createLink', false, url);
  } else {
    document.execCommand(cmd, false, null);
  }
}

function setEditorMode(mode) {
  const richEditor = document.getElementById('richEditor');
  const textarea = document.getElementById('composeBody');
  const toolbar = document.getElementById('editorToolbar');
  const btnRich = document.getElementById('editorModeRich');
  const btnPlain = document.getElementById('editorModePlain');

  if (mode === 'plain') {
    // Sync rich editor → plain text
    textarea.value = richEditor.innerText || '';
    richEditor.style.display = 'none';
    textarea.style.display = 'block';
    toolbar.style.display = 'none';
    btnPlain.classList.add('editor-btn-active');
    btnRich.classList.remove('editor-btn-active');
    editorMode = 'plain';
  } else {
    // Sync plain text → rich editor
    const txt = textarea.value;
    if (txt && !richEditor.innerHTML.trim()) {
      richEditor.innerHTML = escHtml(txt).replace(/\n/g, '<br>');
    }
    richEditor.style.display = 'block';
    textarea.style.display = 'none';
    toolbar.style.display = 'flex';
    btnRich.classList.add('editor-btn-active');
    btnPlain.classList.remove('editor-btn-active');
    editorMode = 'richtext';
  }
}

function getBodyText() {
  if (editorMode === 'richtext') {
    return document.getElementById('richEditor').innerText || '';
  }
  return document.getElementById('composeBody').value || '';
}

function getBodyHtml() {
  if (editorMode === 'richtext') {
    return document.getElementById('richEditor').innerHTML || '';
  }
  return '';
}

function setEditorContent(bodyText, bodyHtml) {
  const richEditor = document.getElementById('richEditor');
  const textarea = document.getElementById('composeBody');
  const hasHtml = bodyHtml && bodyHtml.trim();

  if (hasHtml) {
    // Has HTML content — show in rich text mode
    richEditor.innerHTML = bodyHtml;
    textarea.value = bodyText || richEditor.innerText || '';
    if (editorMode === 'plain') setEditorMode('richtext');
  } else if (bodyText) {
    // Plain text only — show in rich text mode with <br> conversion
    richEditor.innerHTML = escHtml(bodyText).replace(/\n/g, '<br>');
    textarea.value = bodyText;
    if (editorMode === 'plain') setEditorMode('richtext');
  } else {
    // Empty — reset
    richEditor.innerHTML = '';
    textarea.value = '';
    if (editorMode !== 'richtext') setEditorMode('richtext');
  }
}

// ===== Compose =====
let serversLoaded = null; // Promise tracking when server list is populated

async function loadServersForCompose() {
  const promise = (async () => {
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
  })();
  serversLoaded = promise;
  return promise;
}

function composeNew() {
  showCompose(null);
}

function showCompose(subject, preselectServerId, bodyText, recipients, draftId, bodyHtml) {
  document.getElementById('emptyState').style.display = 'none';
  document.getElementById('emailDetailView').style.display = 'none';
  const composeView = document.getElementById('composeView');
  composeView.style.display = 'flex';
  composeView.style.flex = '1';
  document.getElementById('composeTo').value = recipients || '';
  document.getElementById('composeSubject').value = (subject || '');
  setEditorContent(bodyText || '', bodyHtml || '');
  document.getElementById('composeStatus').textContent = '';
  currentDraftId = draftId || null;
  if (preselectServerId) {
    // Ensure server dropdown is populated before selecting
    (serversLoaded || Promise.resolve()).then(() => {
      const select = document.getElementById('composeServer');
      if (select) select.value = String(preselectServerId);
    });
  }
}

function closeCompose() {
  const composeView = document.getElementById('composeView');
  composeView.style.display = 'none';
  composeView.style.flex = '';
  currentDraftId = null;
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
        body_text: getBodyText(),
        body_html: getBodyHtml(),
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
    const body = {
      server_id: parseInt(document.getElementById('composeServer').value) || null,
      to: document.getElementById('composeTo').value.trim(),
      subject: document.getElementById('composeSubject').value.trim(),
      body_text: getBodyText(),
      body_html: getBodyHtml(),
    };
    if (currentDraftId) {
      body.draft_id = currentDraftId;
    }
    const data = await api('/api/drafts', {
      method: 'POST',
      body,
    });
    if (data.draft_id) {
      currentDraftId = data.draft_id;
    }
    document.getElementById('composeStatus').textContent = __('Draft saved!');
    document.getElementById('composeStatus').className = 'compose-status text-success';
    loadTree();
  } catch (err) {
    document.getElementById('composeStatus').textContent = __('Error saving draft');
    document.getElementById('composeStatus').className = 'compose-status text-danger';
  }
}

// ===== Display Mode =====
function updateDisplayModeBtn(sortByTime) {
  const btn = document.getElementById('displayModeBtn');
  if (btn) {
    btn.textContent = sortByTime ? __('按分组显示邮件') : __('按时间排序显示邮件');
  }
}

async function toggleDisplayMode() {
  try {
    const result = await api('/api/preferences/sort-by-time', { method: 'POST' });
    sortByTime = result.sort_by_time;
    updateDisplayModeBtn(result.sort_by_time);
    currentState.serverId = null;
    currentState.currentImpGroupId = null;
    currentState.currentSenderGroupId = null;
    loadTree();
  } catch (_) {}
}

// ===== Group by Server =====
async function toggleGroupByServer(checked) {
  try {
    await api('/api/preferences/group-by-server', { method: 'POST' });
    currentState.serverId = null;
    loadTree();
  } catch (_) {}
}

// ===== Fetch =====

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

    const hasActive = entries.some(p => p.status === 'fetching' || p.status === 'downloading' || p.status === 'classifying');
    if (!hasActive) {
      fetchProgressCount++;
      if (fetchProgressCount > 3) {
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

// ===== Delete Progress (batch delete from server) =====
let deleteProgressTaskId = null;
let deleteProgressTimer = null;
let deleteProgressTotal = 0;
let _lastTreeRefresh = 0;

async function refreshDeleteProgress() {
  if (!deleteProgressTaskId) return;
  try {
    const data = await api(`/api/delete-progress/${deleteProgressTaskId}`);
    if (data.status === 'not_found') {
      stopDeleteProgressPolling();
      return;
    }

    const total = data.total || 1;
    const current = data.current || 0;
    deleteProgressTotal = total;

    updateDeleteProgressUI(current, total);

    if (data.status === 'running') {
      const now = Date.now();
      if (now - _lastTreeRefresh > 1000) {
        _lastTreeRefresh = now;
        loadTree();
      }
    }

    if (data.status === 'done' || data.status === 'partial') {
      stopDeleteProgressPolling();
      hideDeleteProgressDialog();
      showDeleteProgressToast(data.status, data.error);
      showEmptyState();
      loadTree();
    } else if (data.status === 'error') {
      stopDeleteProgressPolling();
      hideDeleteProgressDialog();
      showDeleteProgressToast('error', data.error);
      showEmptyState();
      loadTree();
    }
  } catch (_) {
    stopDeleteProgressPolling();
  }
}

function startDeleteProgressPolling(taskId, total) {
  stopDeleteProgressPolling();
  deleteProgressTaskId = taskId;
  deleteProgressTotal = total;
  _lastTreeRefresh = 0;
  deleteProgressTimer = setInterval(refreshDeleteProgress, 500);
  refreshDeleteProgress();
}

function stopDeleteProgressPolling() {
  if (deleteProgressTimer) {
    clearInterval(deleteProgressTimer);
    deleteProgressTimer = null;
  }
  deleteProgressTaskId = null;
}

function updateDeleteProgressUI(current, total) {
  const pct = total > 0 ? Math.round((current / total) * 100) : 0;

  const statusEl = document.getElementById('deleteProgressStatus');
  const textEl = document.getElementById('deleteProgressText');
  if (statusEl && textEl) {
    statusEl.style.display = 'inline-flex';
    textEl.textContent = `${__('{0}/{1}', current, total)}`;
  }

  const fillEl = document.getElementById('deleteProgressFill');
  const countEl = document.getElementById('deleteProgressCount');
  const pctEl = document.getElementById('deleteProgressPct');
  const msgEl = document.getElementById('deleteProgressMsg');
  if (fillEl) fillEl.style.width = pct + '%';
  if (countEl) countEl.textContent = `${current} / ${total}`;
  if (pctEl) pctEl.textContent = pct + '%';
  if (msgEl) msgEl.textContent = __('Deleting from server...');
}

function showDeleteProgressDialog(total) {
  const dialog = document.getElementById('deleteProgressDialog');
  if (!dialog) return;
  dialog.style.display = 'flex';

  const fillEl = document.getElementById('deleteProgressFill');
  const countEl = document.getElementById('deleteProgressCount');
  const pctEl = document.getElementById('deleteProgressPct');
  const msgEl = document.getElementById('deleteProgressMsg');
  if (fillEl) fillEl.style.width = '0%';
  if (countEl) countEl.textContent = `0 / ${total}`;
  if (pctEl) pctEl.textContent = '0%';
  if (msgEl) msgEl.textContent = __('Preparing...');
}

function hideDeleteProgressDialog() {
  const dialog = document.getElementById('deleteProgressDialog');
  if (dialog) dialog.style.display = 'none';
}

function showDeleteProgressToast(status, errorMsg) {
  const statusEl = document.getElementById('deleteProgressStatus');
  if (!statusEl) return;

  if (status === 'done') {
    statusEl.innerHTML = `<span class="delete-progress-label" style="color:var(--success)">${__('Deletion complete')}</span>`;
  } else if (status === 'partial') {
    const msg = errorMsg ? __('Deletion complete with errors: {0}', errorMsg) : __('Deletion complete with errors');
    statusEl.innerHTML = `<span class="delete-progress-label" style="color:var(--warning)">${msg}</span>`;
  } else {
    const msg = errorMsg ? __('Deletion failed: {0}', errorMsg) : __('Deletion failed');
    statusEl.innerHTML = `<span class="delete-progress-label" style="color:var(--danger)">${msg}</span>`;
  }
  statusEl.style.display = 'inline-flex';
}

function clearDeleteProgressStatus() {
  const statusEl = document.getElementById('deleteProgressStatus');
  if (statusEl) {
    statusEl.style.display = 'none';
  }
}

async function refreshServerStatusBar() {
  if (serverStatusRefreshing) return;
  serverStatusRefreshing = true;
  try {
    const data = await api('/api/next-fetch');
    serverStatusData = data;
    renderServerStatusBar();

    // If not already polling for fetch progress, check whether the
    // background scheduler has started an auto-fetch (after countdown
    // reaches 0 or via IMAP IDLE) and begin polling so the progress bar
    // appears without requiring a manual "Fetch" click.
    if (!fetchProgressTimer) {
      try {
        const fpData = await api('/api/fetch-progress');
        const entries = Object.values(fpData.servers || {});
        if (entries.some(p => p.status === 'fetching' || p.status === 'downloading')) {
          startFetchProgressPolling();
        }
      } catch (_) {}
    }
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
    const isDownloading = progress && progress.status === 'downloading';
    const isClassifying = progress && progress.status === 'classifying';
    const isDone = progress && (progress.status === 'done' || progress.status === 'error');

    let modeText = '';
    if (srv.mode === 'imap_idle') {
      modeText = __('IMAP IDLE auto fetch');
    } else if (srv.mode === 'auto') {
      const seconds = Math.max(0, srv.seconds_until || 0);
      modeText = __('Auto fetch (countdown {0} min): {1}', srv.interval_minutes || 0, formatCountdown(seconds));
    } else {
      modeText = __('Manual');
    }

    if (isFetching) {
      const total = progress.total || 0;
      const current = progress.current || 0;
      const pct = total > 0 ? Math.round((current / total) * 100) : 0;

      row.innerHTML = `
        <span class="server-status-name">${escHtml(srv.server_name)}</span>
        <button class="btn btn-sm btn-outline" onclick="composeForServer(${srv.id})">${__('Compose')}</button>
        <button class="btn btn-sm btn-outline" onclick="fetchOneServer(${srv.id})" disabled>${__('Fetch')}</button>
        <button class="btn btn-sm btn-outline" onclick="downloadAllServer(${srv.id})" disabled>${__('Refresh Server Emails')}</button>
        <span class="server-status-mode">${modeText}</span>
        <div class="fetch-progress-wrap">
          <div class="fetch-progress-bar">
            <div class="fetch-progress-fill" style="width:${pct}%"></div>
          </div>
          <span class="fetch-progress-text">${__('Fetching {0}/{1}', current, total)}</span>
        </div>
      `;
    } else if (isDownloading) {
      const total = progress.total || 0;
      const current = progress.current || 0;
      const pct = total > 0 ? Math.round((current / total) * 100) : 0;

      row.innerHTML = `
        <span class="server-status-name">${escHtml(srv.server_name)}</span>
        <button class="btn btn-sm btn-outline" onclick="composeForServer(${srv.id})">${__('Compose')}</button>
        <button class="btn btn-sm btn-outline" onclick="fetchOneServer(${srv.id})" disabled>${__('Fetch')}</button>
        <button class="btn btn-sm btn-outline" onclick="downloadAllServer(${srv.id})" disabled>${__('Refresh Server Emails')}</button>
        <span class="server-status-mode">${modeText}</span>
        <div class="fetch-progress-wrap">
          <div class="fetch-progress-bar">
            <div class="fetch-progress-fill download-all-fill" style="width:${pct}%"></div>
          </div>
          <span class="fetch-progress-text">${__('Downloading {0}/{1}', current, total)}</span>
        </div>
      `;
    } else if (isClassifying) {
      row.innerHTML = `
        <span class="server-status-name">${escHtml(srv.server_name)}</span>
        <button class="btn btn-sm btn-outline" onclick="composeForServer(${srv.id})">${__('Compose')}</button>
        <button class="btn btn-sm btn-outline" onclick="fetchOneServer(${srv.id})" disabled>${__('Fetch')}</button>
        <button class="btn btn-sm btn-outline" onclick="downloadAllServer(${srv.id})" disabled>${__('Refresh Server Emails')}</button>
        <span class="server-status-mode">${modeText}</span>
        <div class="fetch-progress-wrap">
          <div class="fetch-progress-bar">
            <div class="fetch-progress-fill classifying-fill" style="width:100%"></div>
          </div>
          <span class="fetch-progress-text">${__('Classifying...')}</span>
        </div>
      `;
    } else {
      row.innerHTML = `
        <span class="server-status-name">${escHtml(srv.server_name)}</span>
        <button class="btn btn-sm btn-outline" onclick="composeForServer(${srv.id})">${__('Compose')}</button>
        <button class="btn btn-sm btn-outline" onclick="fetchOneServer(${srv.id})">${__('Fetch')}</button>
        <button class="btn btn-sm btn-outline" onclick="downloadAllServer(${srv.id})">${__('Refresh Server Emails')}</button>
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

async function downloadAllServer(serverId) {
  try {
    await api(`/api/servers/${serverId}/download-all`, { method: 'POST' });
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

// ===== Contacts Panel =====
let contactPanelVisible = false;

// Load contact panel state from localStorage
try {
  contactPanelVisible = localStorage.getItem('contact_panel_visible') === 'true';
} catch (_) {}

async function loadContacts() {
  try {
    const [contactsData, groupsData] = await Promise.all([
      api('/api/contacts'),
      api('/api/contact-groups'),
    ]);
    renderContactTree(contactsData.contacts || [], groupsData.contact_groups || []);
  } catch (err) {
    console.error('Failed to load contacts:', err);
  }
}

function renderContactTree(contacts, groups) {
  const container = document.getElementById('contactTree');
  container.innerHTML = '';

  // Favorites section
  const favContacts = contacts.filter(c => c.is_favorite);
  if (favContacts.length > 0) {
    const section = document.createElement('div');
    const header = document.createElement('div');
    header.className = 'contact-tree-group-header';
    header.textContent = __('Favorites');
    section.appendChild(header);

    favContacts.forEach(c => {
      section.appendChild(createContactTreeItem(c, true));
    });
    container.appendChild(section);
  }

  // Grouped contacts
  groups.forEach(g => {
    const groupContacts = contacts.filter(c => c.group_ids && c.group_ids.includes(g.id));
    if (groupContacts.length === 0) return;

    const groupWrapper = document.createElement('div');
    const groupHeader = document.createElement('div');
    groupHeader.className = 'contact-tree-item';
    groupHeader.style.fontWeight = '600';

    const toggle = document.createElement('span');
    toggle.className = 'contact-tree-toggle collapsed';
    toggle.innerHTML = '&#9660;';
    groupHeader.appendChild(toggle);

    const icon = document.createElement('span');
    icon.className = 'contact-tree-icon';
    icon.innerHTML = '&#128193;';
    groupHeader.appendChild(icon);

    const nameSpan = document.createElement('span');
    nameSpan.className = 'contact-tree-name';
    nameSpan.textContent = g.name;
    groupHeader.appendChild(nameSpan);

    const countSpan = document.createElement('span');
    countSpan.className = 'contact-tree-count';
    countSpan.textContent = groupContacts.length;
    groupHeader.appendChild(countSpan);

    groupHeader.addEventListener('click', (e) => {
      const children = groupWrapper.querySelector('.contact-tree-children');
      const toggler = groupHeader.querySelector('.contact-tree-toggle');
      if (children) {
        children.classList.toggle('collapsed');
        if (toggler) toggler.classList.toggle('collapsed');
      }
    });

    groupHeader.addEventListener('contextmenu', (e) => {
      e.preventDefault();
      e.stopPropagation();
      showContactContextMenu(e, {type: 'group', id: g.id, name: g.name});
    });

    groupWrapper.appendChild(groupHeader);

    const childrenContainer = document.createElement('div');
    childrenContainer.className = 'contact-tree-children collapsed';
    groupContacts.forEach(c => {
      childrenContainer.appendChild(createContactTreeItem(c, false));
    });
    groupWrapper.appendChild(childrenContainer);
    container.appendChild(groupWrapper);
  });

  // Ungrouped contacts (not favorite, not in any group)
  const ungrouped = contacts.filter(c => !c.is_favorite && (!c.group_ids || c.group_ids.length === 0));
  if (ungrouped.length > 0) {
    const section = document.createElement('div');
    const header = document.createElement('div');
    header.className = 'contact-tree-group-header';
    header.textContent = __('Other Contacts');
    section.appendChild(header);

    ungrouped.forEach(c => {
      section.appendChild(createContactTreeItem(c, false));
    });
    container.appendChild(section);
  }

  // Empty state
  if (contacts.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'contact-tree-group-header';
    empty.textContent = __('No contacts yet');
    empty.style.padding = '20px 12px';
    empty.style.textAlign = 'center';
    empty.style.opacity = '0.5';
    container.appendChild(empty);
  }
}

function createContactTreeItem(contact, isFav) {
  const item = document.createElement('div');
  item.className = 'contact-tree-item';
  if (isFav) item.classList.add('contact-fav');

  const icon = document.createElement('span');
  icon.className = 'contact-tree-icon';
  if (isFav) {
    icon.innerHTML = '&#11088;';
  } else {
    icon.innerHTML = '&#128100;';
  }
  item.appendChild(icon);

  const nameSpan = document.createElement('span');
  nameSpan.className = 'contact-tree-name';
  nameSpan.textContent = contact.name;
  nameSpan.title = contact.email;
  item.appendChild(nameSpan);

  item.addEventListener('click', () => {
    document.querySelectorAll('#contactTree .contact-tree-item.selected').forEach(el => el.classList.remove('selected'));
    item.classList.add('selected');
  });

  item.addEventListener('dblclick', () => {
    showCompose(null, contact.default_server_id);
    const toField = document.getElementById('composeTo');
    if (toField) {
      toField.value = contact.email;
    }
  });

  item.addEventListener('contextmenu', (e) => {
    e.preventDefault();
    e.stopPropagation();
    showContactContextMenu(e, {type: 'contact', id: contact.id, name: contact.name, email: contact.email});
  });

  return item;
}

function toggleContactPanel() {
  contactPanelVisible = !contactPanelVisible;
  const panel = document.getElementById('contactPanel');
  const splitter = document.getElementById('splitterVC');
  if (contactPanelVisible) {
    panel.classList.remove('collapsed');
    splitter.style.display = 'block';
    loadContacts();
  } else {
    panel.classList.add('collapsed');
    splitter.style.display = 'none';
  }
  try {
    localStorage.setItem('contact_panel_visible', contactPanelVisible);
  } catch (_) {}
}

// ===== Contact Panel Splitter =====
(function initSplitterVC() {
  const splitter = document.getElementById('splitterVC');
  const panel = document.getElementById('contactPanel');
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
    const sidebar = document.getElementById('sidebar');
    const sidebarW = sidebar.offsetWidth;
    let w = e.clientX - sidebarW;
    w = Math.max(120, Math.min(350, w));
    panel.style.width = w + 'px';
  });

  document.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false;
    splitter.classList.remove('active');
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
  });
})();

// Initialize contact panel visibility on page load
(function initContactPanel() {
  if (contactPanelVisible) {
    const panel = document.getElementById('contactPanel');
    const splitter = document.getElementById('splitterVC');
    if (panel) panel.classList.remove('collapsed');
    if (splitter) splitter.style.display = 'block';
    loadContacts();
  }
  // Empty-space right-click on contact panel
  const panel = document.getElementById('contactPanel');
  if (panel) {
    panel.addEventListener('contextmenu', (e) => {
      // Only trigger if clicking the panel background or #contactTree empty area
      if (e.target === panel || e.target === document.getElementById('contactTree') || e.target.closest('.contact-panel-tree')) {
        e.preventDefault();
        showContactContextMenu(e, null);
      }
    });
  }
})();

// ===== Contact Context Menu =====
let contactCtxTarget = null;

function showContactContextMenu(e, target) {
  hideContextMenu();
  hideContactContextMenu();
  contactCtxTarget = target;
  const menu = document.getElementById('contactCtxMenu');
  if (!menu) return;
  const addGroup = document.getElementById('ctxCtcAddGroup');
  const addContact = document.getElementById('ctxCtcAddContact');
  const groupRename = document.getElementById('ctxCtcGroupRename');
  const groupDelete = document.getElementById('ctxCtcGroupDelete');
  const locateGroup = document.getElementById('ctxCtcLocateGroup');
  const sendEmail = document.getElementById('ctxCtcSendEmail');
  const editContactEl = document.getElementById('ctxCtcEditContact');
  const deleteContactEl = document.getElementById('ctxCtcDeleteContact');
  const div1 = document.getElementById('ctxCtcDiv1');
  const div2 = document.getElementById('ctxCtcDiv2');
  [addGroup, addContact, groupRename, groupDelete, locateGroup, sendEmail, editContactEl, deleteContactEl, div1, div2].forEach(el => { if (el) el.style.display = 'none'; });
  if (!target) {
    if (addGroup) addGroup.style.display = 'flex';
    if (addContact) addContact.style.display = 'flex';
  } else if (target.type === 'group') {
    if (groupRename) groupRename.style.display = 'flex';
    if (groupDelete) groupDelete.style.display = 'flex';
  } else if (target.type === 'contact') {
    if (locateGroup) locateGroup.style.display = 'flex';
    if (sendEmail) sendEmail.style.display = 'flex';
    if (editContactEl) editContactEl.style.display = 'flex';
    if (deleteContactEl) deleteContactEl.style.display = 'flex';
    if (div2) div2.style.display = 'block';
  }
  menu.style.left = e.clientX + 'px';
  menu.style.top = e.clientY + 'px';
  menu.classList.add('show');
}

function hideContactContextMenu() {
  const menu = document.getElementById('contactCtxMenu');
  if (menu) menu.classList.remove('show');
  contactCtxTarget = null;
}

function ctcCtxAddGroup() {
  hideContactContextMenu();
  contactsAddGroup();
}
function ctcCtxAddContact() {
  hideContactContextMenu();
  contactsAddContact();
}
function ctcCtxGroupRename() {
  const target = contactCtxTarget;
  hideContactContextMenu();
  if (!target) return;
  document.getElementById('contactGroupEditTitle').textContent = __('Edit Group');
  document.getElementById('cgeName').value = target.name;
  document.getElementById('contactGroupEditForm').dataset.groupId = target.id;
  document.getElementById('cgeDeleteBtn').style.display = '';
  document.getElementById('contactGroupEditModal').style.display = 'flex';
}
async function ctcCtxGroupDelete() {
  const target = contactCtxTarget;
  hideContactContextMenu();
  if (!target) return;
  if (!(await showDialog({ title: __('Delete Group'), message: __('Delete this group?') }))) return;
  try {
    await api('/api/contact-groups/' + target.id, { method: 'DELETE' });
    loadContacts();
    if (document.getElementById('manageContactsModal').style.display === 'flex') refreshContactsMgr();
  } catch (err) {
    alert(__('Error: {0}', err.message));
  }
}
function ctcCtxSendEmail() {
  const target = contactCtxTarget;
  hideContactContextMenu();
  if (!target) return;
  showCompose(null);
  const toField = document.getElementById('composeTo');
  if (toField) toField.value = target.email;
}
async function ctcCtxLocateGroup() {
  const target = contactCtxTarget;
  hideContactContextMenu();
  if (!target || !target.email) return;

  const atIdx = target.email.indexOf('@');
  if (atIdx < 0) { alert(__('Invalid email address')); return; }
  const domain = target.email.slice(atIdx + 1).toLowerCase();
  if (!domain) { alert(__('Invalid email address')); return; }

  try {
    const data = await api('/api/mailbox/tree');

    let targetNodeId = null;
    let targetImpGroupId = null;
    let targetSenderGroupId = null;
    let expandIds = [];

    (function searchNodes(nodes, ancestors) {
      for (const node of nodes) {
        if (node.sender_group_id && node.email) {
          const m = node.email.match(/@([\w.-]+)/);
          const nodeDomain = m ? m[1].toLowerCase() : '';
          if (nodeDomain === domain) {
            targetNodeId = node.id;
            targetImpGroupId = node.imp_group_id;
            targetSenderGroupId = node.sender_group_id;
            expandIds = ancestors;
            return true;
          }
        }
        if (node.children && node.children.length > 0) {
          if (searchNodes(node.children, [...ancestors, node.id])) return true;
        }
      }
      return false;
    })(data.folders, []);

    if (!targetNodeId || !targetSenderGroupId) {
      alert(__('No sender group found matching domain: {0}', domain));
      return;
    }

    const container = document.getElementById('treeMenu');
    container.innerHTML = '';
    data.folders.forEach(folder => {
      container.appendChild(createTreeItem(folder, 0));
    });

    document.querySelectorAll('.tree-item.active').forEach(el => el.classList.remove('active'));

    expandIds.forEach(id => {
      const w = document.querySelector(`[data-node-id="${CSS.escape(id)}"]`);
      if (w) {
        const ch = w.querySelector('.tree-children');
        const tg = w.querySelector('.tree-toggle');
        if (ch) ch.classList.remove('collapsed');
        if (tg) tg.classList.remove('collapsed');
      }
    });

    const targetEl = document.querySelector(`[data-node-id="${CSS.escape(targetNodeId)}"]`);
    if (targetEl) {
      const ch = targetEl.querySelector('.tree-children');
      const tg = targetEl.querySelector('.tree-toggle');
      if (ch) ch.classList.remove('collapsed');
      if (tg) tg.classList.remove('collapsed');
      showEmptyState();

      currentState.currentSenderGroupId = targetSenderGroupId;
      currentState.currentImpGroupId = targetImpGroupId;
      currentState.currentFolder = 'inbox';

      // Re-apply active class (cleared by showEmptyState)
      const renewedEl = document.querySelector(`[data-node-id="${CSS.escape(targetNodeId)}"]`);
      if (renewedEl) {
        const mainItem = renewedEl.querySelector('.tree-item');
        if (mainItem) mainItem.classList.add('active');
      }

      targetEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
  } catch (err) {
    console.error('Failed to locate group:', err);
    alert(__('Failed to locate sender group'));
  }
}
function ctcCtxEditContact() {
  const target = contactCtxTarget;
  hideContactContextMenu();
  if (!target) return;
  showManageContacts();
  setTimeout(() => contactsEditContact(target.id), 300);
}
async function ctcCtxDeleteContact() {
  const target = contactCtxTarget;
  hideContactContextMenu();
  if (!target) return;
  if (!(await showDialog({ title: __('Delete Contact'), message: __('Delete this contact?') }))) return;
  try {
    await api('/api/contacts/' + target.id, { method: 'DELETE' });
    loadContacts();
    if (document.getElementById('manageContactsModal').style.display === 'flex') refreshContactsMgr();
  } catch (err) {
    alert(__('Error: {0}', err.message));
  }
}

// Close contact context menu on click elsewhere
document.addEventListener('click', (e) => {
  if (!e.target.closest('#contactCtxMenu')) {
    hideContactContextMenu();
  }
});

// ===== Manage Contacts Modal =====
let contactsDataCache = { contacts: [], groups: [] };
let selectedGroupId = null;

async function showManageContacts() {
  document.getElementById('manageContactsModal').style.display = 'flex';
  await refreshContactsMgr();
}

function closeManageContacts() {
  document.getElementById('manageContactsModal').style.display = 'none';
  loadContacts(); // Refresh the contact panel
}

async function refreshContactsMgr() {
  try {
    const [contactsData, groupsData] = await Promise.all([
      api('/api/contacts'),
      api('/api/contact-groups'),
    ]);
    contactsDataCache = {
      contacts: contactsData.contacts || [],
      groups: groupsData.contact_groups || [],
    };
    renderContactGroups();
    renderContactList();
  } catch (err) {
    console.error('Failed to load contacts data:', err);
  }
}

function renderContactGroups() {
  const list = document.getElementById('contactsGroupList');
  list.innerHTML = '';

  // "All" item
  const allItem = document.createElement('div');
  allItem.className = 'contacts-mgr-group-item' + (selectedGroupId === null ? ' active' : '');
  allItem.textContent = __('All Contacts');
  allItem.addEventListener('click', () => {
    selectedGroupId = null;
    renderContactGroups();
    renderContactList();
  });
  list.appendChild(allItem);

  contactsDataCache.groups.forEach(g => {
    const item = document.createElement('div');
    item.className = 'contacts-mgr-group-item' + (selectedGroupId === g.id ? ' active' : '');
    const nameSpan = document.createElement('span');
    nameSpan.textContent = g.name;
    item.appendChild(nameSpan);

    const countSpan = document.createElement('span');
    countSpan.className = 'group-count';
    countSpan.textContent = g.contact_count || 0;
    item.appendChild(countSpan);

    item.addEventListener('click', () => {
      selectedGroupId = g.id;
      renderContactGroups();
      renderContactList();
    });

    item.addEventListener('dblclick', () => {
      document.getElementById('contactGroupEditTitle').textContent = __('Edit Group');
      document.getElementById('cgeName').value = g.name;
      document.getElementById('contactGroupEditForm').dataset.groupId = g.id;
      document.getElementById('cgeDeleteBtn').style.display = '';
      document.getElementById('contactGroupEditModal').style.display = 'flex';
    });

    list.appendChild(item);
  });
}

function renderContactList() {
  const container = document.getElementById('contactsMgrList');
  container.innerHTML = '';

  let contacts = contactsDataCache.contacts;
  if (selectedGroupId !== null) {
    contacts = contacts.filter(c => (c.group_ids && c.group_ids.includes(selectedGroupId)) || c.contact_group_id === selectedGroupId);
  }

  if (contacts.length === 0) {
    container.innerHTML = '<div class="contacts-mgr-group-header" style="padding:20px;text-align:center;opacity:0.5;">' + __('No contacts') + '</div>';
    return;
  }

  contacts.forEach(c => {
    const el = document.createElement('div');
    el.className = 'contacts-mgr-contact';

    if (c.is_favorite) {
      const star = document.createElement('span');
      star.className = 'contact-fav-star';
      star.textContent = '\u2605';
      el.appendChild(star);
    }

    const nameEl = document.createElement('span');
    nameEl.className = 'contact-name';
    nameEl.textContent = c.name;
    el.appendChild(nameEl);

    const emailEl = document.createElement('span');
    emailEl.className = 'contact-email';
    emailEl.textContent = '<' + c.email + '>';
    el.appendChild(emailEl);

    // Show group badges (may be multiple)
    const gids = c.group_ids || [];
    if (c.contact_group_id && !gids.includes(c.contact_group_id)) gids.push(c.contact_group_id);
    const groupNames = contactsDataCache.groups.filter(g => gids.includes(g.id)).map(g => g.name);
    groupNames.forEach(gname => {
      const badge = document.createElement('span');
      badge.className = 'contact-group-badge';
      badge.textContent = gname;
      el.appendChild(badge);
    });

    el.addEventListener('click', () => contactsEditContact(c.id));
    container.appendChild(el);
  });

  document.getElementById('contactsMgrStatus').textContent = contacts.length + ' ' + __('contacts');
}

async function contactsAddContact() {
  await loadContactEditFormData();
  document.getElementById('contactEditTitle').textContent = __('Add Contact');
  document.getElementById('ceName').value = '';
  document.getElementById('ceEmail').value = '';
  document.getElementById('cePhone').value = '';
  document.getElementById('ceServer').value = '';
  document.getElementById('ceFavorite').checked = false;
  document.getElementById('ceNotes').value = '';
  document.getElementById('ceMessage').textContent = '';
  document.getElementById('ceDeleteBtn').style.display = 'none';
  delete document.getElementById('contactEditForm').dataset.contactId;
  // Uncheck all group checkboxes
  document.querySelectorAll('#ceGroups input[type="checkbox"]').forEach(cb => cb.checked = false);
  document.querySelectorAll('#ceGroups label').forEach(lb => lb.classList.remove('checked'));
  document.getElementById('contactEditModal').style.display = 'flex';
}

async function contactsEditContact(contactId) {
  await loadContactEditFormData();
  const contact = contactsDataCache.contacts.find(c => c.id === contactId);
  if (!contact) return;

  document.getElementById('contactEditTitle').textContent = __('Edit Contact');
  document.getElementById('ceName').value = contact.name || '';
  document.getElementById('ceEmail').value = contact.email || '';
  document.getElementById('cePhone').value = contact.phone || '';
  document.getElementById('ceServer').value = contact.default_server_id || '';
  document.getElementById('ceFavorite').checked = !!contact.is_favorite;
  document.getElementById('ceNotes').value = contact.notes || '';
  document.getElementById('ceMessage').textContent = '';
  document.getElementById('ceDeleteBtn').style.display = '';
  document.getElementById('contactEditForm').dataset.contactId = contactId;

  // Check the contact's group checkboxes
  const gids = contact.group_ids || [];
  if (contact.contact_group_id && !gids.includes(contact.contact_group_id)) gids.push(contact.contact_group_id);
  document.querySelectorAll('#ceGroups input[type="checkbox"]').forEach(cb => {
    cb.checked = gids.includes(parseInt(cb.value));
    cb.closest('label').classList.toggle('checked', cb.checked);
  });

  document.getElementById('contactEditModal').style.display = 'flex';
}

async function loadContactEditFormData() {
  try {
    const [groupsData, serversData] = await Promise.all([
      api('/api/contact-groups'),
      api('/api/servers'),
    ]);
    // Render group checkboxes for multi-select
    const container = document.getElementById('ceGroups');
    container.innerHTML = '';
    (groupsData.contact_groups || []).forEach(g => {
      const label = document.createElement('label');
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.value = g.id;
      cb.addEventListener('change', () => {
        label.classList.toggle('checked', cb.checked);
      });
      label.appendChild(cb);
      label.appendChild(document.createTextNode(' ' + escHtml(g.name)));
      container.appendChild(label);
    });

    const serverSelect = document.getElementById('ceServer');
    serverSelect.innerHTML = '<option value="">(' + __('None') + ')</option>';
    (serversData.servers || []).forEach(s => {
      serverSelect.innerHTML += '<option value="' + s.id + '">' + escHtml(s.server_name) + '</option>';
    });
  } catch (err) {
    console.error('Failed to load form data:', err);
  }
}

function closeContactEdit() {
  document.getElementById('contactEditModal').style.display = 'none';
}

async function contactsSaveContact(e) {
  e.preventDefault();
  const contactId = document.getElementById('contactEditForm').dataset.contactId;
  const name = document.getElementById('ceName').value.trim();
  const email = document.getElementById('ceEmail').value.trim();
  const phone = document.getElementById('cePhone').value.trim();
  const defaultServerId = document.getElementById('ceServer').value;
  const isFavorite = document.getElementById('ceFavorite').checked ? 1 : 0;
  const notes = document.getElementById('ceNotes').value.trim();
  const msgDiv = document.getElementById('ceMessage');

  // Gather checked group IDs
  const groupIds = [];
  document.querySelectorAll('#ceGroups input[type="checkbox"]:checked').forEach(cb => {
    groupIds.push(parseInt(cb.value));
  });

  if (!name || !email) {
    msgDiv.textContent = __('Name and email are required');
    msgDiv.className = 'form-message form-error';
    return;
  }

  try {
    const body = {
      name, email, phone,
      contact_group_id: groupIds.length > 0 ? groupIds[0] : null,
      group_ids: groupIds,
      default_server_id: defaultServerId || null,
      is_favorite: isFavorite,
      notes,
    };
    if (contactId) {
      await api('/api/contacts/' + contactId, {
        method: 'PUT',
        body,
      });
    } else {
      await api('/api/contacts', {
        method: 'POST',
        body,
      });
    }
    msgDiv.textContent = __('Saved!');
    msgDiv.className = 'form-message form-success';
    closeContactEdit();
    await refreshContactsMgr();
    loadContacts();
  } catch (err) {
    msgDiv.textContent = __('Error: {0}', err.message);
    msgDiv.className = 'form-message form-error';
  }
}

async function contactsDeleteContact() {
  const contactId = document.getElementById('contactEditForm').dataset.contactId;
  if (!contactId) return;
  if (!(await showDialog({ title: __('Delete Contact'), message: __('Delete this contact?') }))) return;

  try {
    await api('/api/contacts/' + contactId, { method: 'DELETE' });
    closeContactEdit();
    await refreshContactsMgr();
    loadContacts();
  } catch (err) {
    document.getElementById('ceMessage').textContent = __('Error: {0}', err.message);
    document.getElementById('ceMessage').className = 'form-message form-error';
  }
}

// Contact group management
function contactsAddGroup() {
  document.getElementById('contactGroupEditTitle').textContent = __('Add Group');
  document.getElementById('cgeName').value = '';
  document.getElementById('cgeMessage').textContent = '';
  document.getElementById('cgeDeleteBtn').style.display = 'none';
  delete document.getElementById('contactGroupEditForm').dataset.groupId;
  document.getElementById('contactGroupEditModal').style.display = 'flex';
}

function closeContactGroupEdit() {
  document.getElementById('contactGroupEditModal').style.display = 'none';
}

async function contactsSaveGroup(e) {
  e.preventDefault();
  const groupId = document.getElementById('contactGroupEditForm').dataset.groupId;
  const name = document.getElementById('cgeName').value.trim();
  const msgDiv = document.getElementById('cgeMessage');

  if (!name) {
    msgDiv.textContent = __('Group name is required');
    msgDiv.className = 'form-message form-error';
    return;
  }

  try {
    if (groupId) {
      await api('/api/contact-groups/' + groupId, {
        method: 'PUT',
        body: { name },
      });
    } else {
      await api('/api/contact-groups', {
        method: 'POST',
        body: { name },
      });
    }
    msgDiv.textContent = __('Saved!');
    msgDiv.className = 'form-message form-success';
    closeContactGroupEdit();
    await refreshContactsMgr();
    loadContacts();
  } catch (err) {
    msgDiv.textContent = __('Error: {0}', err.message);
    msgDiv.className = 'form-message form-error';
  }
}

async function contactsDeleteGroup() {
  const groupId = document.getElementById('contactGroupEditForm').dataset.groupId;
  if (!groupId) return;
  if (!(await showDialog({ title: __('Delete Group'), message: __('Delete this group? Contacts will be ungrouped.') }))) return;

  try {
    await api('/api/contact-groups/' + groupId, { method: 'DELETE' });
    closeContactGroupEdit();
    await refreshContactsMgr();
    loadContacts();
  } catch (err) {
    document.getElementById('cgeMessage').textContent = __('Error: {0}', err.message);
    document.getElementById('cgeMessage').className = 'form-message form-error';
  }
}

// ===== Context Menu: Add Sender to Contacts =====
async function contextAddSenderToContact() {
  const target = contextMenuTarget;
  hideContextMenu();
  if (!target) return;

  let senderName = '';
  let senderEmail = '';

  try {
    if (target.type === 'email' && target.id) {
      const data = await api('/api/emails/' + target.id);
      const email = data.email;
      if (email) {
        senderName = email.sender_name || '';
        senderEmail = email.sender || '';
      }
    }
  } catch (_) {}

  if (!senderEmail) {
    alert(__('Could not determine sender email'));
    return;
  }

  try {
    const result = await api('/api/contacts/add-from-email', {
      method: 'POST',
      body: { name: senderName || senderEmail, email: senderEmail },
    });
    if (result.already_exists) {
      alert(__('Contact already exists'));
    } else {
      alert(__('Contact added!'));
    }
    loadContacts();
  } catch (err) {
    alert(__('Failed to add contact: {0}', err.message));
  }
}

// Context menu visibility for contact-related items
const origShowContextMenu = showContextMenu;
showContextMenu = function(x, y) {
  origShowContextMenu(x, y);
  const addContactItem = document.getElementById('ctxAddContact');
  if (addContactItem) {
    addContactItem.style.display = (contextMenuTarget && contextMenuTarget.type === 'email') ? 'flex' : 'none';
  }
};

// ===== Helper =====
function escHtml(str) {
  if (!str) return '';
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}