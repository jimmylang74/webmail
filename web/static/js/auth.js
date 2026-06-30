/* ===== Auth Module ===== */

// ===== Dialog =====
function showDialog(options) {
  // Show a modal dialog. Supports two modes:
  // 1) Confirm mode (options.message): shows a message paragraph, resolves true|null
  // 2) Input mode (default): shows a text input, resolves with the value|null
  return new Promise((resolve) => {
    const existing = document.getElementById('__dialog');
    if (existing) existing.remove();

    const overlay = document.createElement('div');
    overlay.id = '__dialog';
    overlay.className = 'modal';
    overlay.style.display = 'flex';

    const content = document.createElement('div');
    content.className = 'modal-content modal-sm';
    overlay.appendChild(content);

    const header = document.createElement('div');
    header.className = 'modal-header';
    const h3 = document.createElement('h3');
    h3.textContent = options.title || '';
    header.appendChild(h3);
    content.appendChild(header);

    const body = document.createElement('div');
    body.className = 'modal-body';

    let input = null;
    if (options.message) {
      // Confirm mode
      const p = document.createElement('p');
      p.style.whiteSpace = 'pre-wrap';
      p.textContent = options.message;
      body.appendChild(p);
    } else {
      // Input mode
      const group = document.createElement('div');
      group.className = 'form-group';
      input = document.createElement('input');
      input.type = 'text';
      input.value = options.value || '';
      if (options.placeholder) input.placeholder = options.placeholder;
      group.appendChild(input);
      body.appendChild(group);
    }
    content.appendChild(body);

    const footer = document.createElement('div');
    footer.className = 'modal-footer';
    const cancelBtn = document.createElement('button');
    cancelBtn.type = 'button';
    cancelBtn.className = 'btn';
    cancelBtn.textContent = __('Cancel');
    const okBtn = document.createElement('button');
    okBtn.type = 'button';
    okBtn.className = 'btn btn-primary';
    okBtn.textContent = __('OK');
    footer.appendChild(cancelBtn);
    footer.appendChild(okBtn);
    content.appendChild(footer);

    document.body.appendChild(overlay);

    function close(val) {
      overlay.remove();
      resolve(val);
    }

    okBtn.onclick = () => close(options.message ? true : input.value);
    cancelBtn.onclick = () => close(null);

    if (input) {
      input.focus();
      input.select();
      input.onkeydown = (e) => {
        if (e.key === 'Enter') close(input.value);
        if (e.key === 'Escape') close(null);
      };
    } else {
      // Confirm mode — close on Escape
      overlay.onkeydown = (e) => { if (e.key === 'Escape') close(null); };
    }
  });
}

async function api(url, options = {}) {
  const config = {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  };
  if (config.body && typeof config.body === 'object') {
    config.body = JSON.stringify(config.body);
  }
  try {
    const resp = await fetch(url, config);
    const data = await resp.json();
    if (!resp.ok && data.error) throw new Error(data.error);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return data;
  } catch (err) {
    if (err.message.includes('Failed to fetch')) {
      throw new Error(__('Connection error. Please check if the server is running.'));
    }
    throw err;
  }
}

async function checkSession() {
  try {
    const data = await api('/api/session');
    const expected = sessionStorage.getItem('expectedUser');
    if (expected && data.username !== expected) {
      // Session cookie was overwritten by another tab/window (different user).
      window.location.href = '/login';
      return null;
    }
    // Track the user for this tab so we can detect cross-tab session takeover.
    sessionStorage.setItem('expectedUser', data.username);

    const display = document.getElementById('userDisplay') || document.getElementById('adminUserDisplay') || document.getElementById('configUserDisplay');
    if (display) display.textContent = data.username + (data.role === 'admin' ? __(' (Admin)') : '');
    const adminLink = document.getElementById('adminLink');
    if (adminLink) adminLink.style.display = data.role === 'admin' ? '' : 'none';
    const adminLinkDropdown = document.getElementById('adminLinkDropdown');
    if (adminLinkDropdown) adminLinkDropdown.style.display = data.role === 'admin' ? '' : 'none';
    return data;
  } catch {
    window.location.href = '/login';
    return null;
  }
}

function logout() {
  // Clear the per-tab user expectation so a re-login in this tab won't
  // trigger the cross-tab session takeover guard.
  sessionStorage.removeItem('expectedUser');
  api('/api/logout', { method: 'POST' }).finally(() => {
    window.location.href = '/login';
  });
}

// Login form
document.addEventListener('DOMContentLoaded', () => {
  const form = document.getElementById('loginForm');
  if (!form) return;

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const username = document.getElementById('username').value.trim();
    const password = document.getElementById('password').value;
    const errorDiv = document.getElementById('loginError');
    const btn = document.getElementById('loginBtn');

    errorDiv.style.display = 'none';
    btn.disabled = true;
    btn.textContent = __('Signing in...');

    try {
      const data = await api('/api/login', {
        method: 'POST',
        body: { username, password },
      });
      if (data.success) {
        window.location.href = data.role === 'admin' ? '/mail' : '/mail';
      }
    } catch (err) {
      errorDiv.textContent = err.message || __('Login failed');
      errorDiv.style.display = 'block';
    } finally {
      btn.disabled = false;
      btn.textContent = __('Sign In');
    }
  });
});
