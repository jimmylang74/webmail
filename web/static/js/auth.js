/* ===== Auth Module ===== */

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
