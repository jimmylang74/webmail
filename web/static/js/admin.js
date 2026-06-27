/* ===== Admin Module ===== */

document.addEventListener('DOMContentLoaded', async () => {
  await checkSession();
  loadUsers();
});

async function loadUsers() {
  try {
    const data = await api('/api/admin/users');
    const tbody = document.getElementById('userTableBody');
    tbody.innerHTML = '';
    data.users.forEach(user => {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td>${user.id}</td>
        <td><strong>${user.username}</strong></td>
        <td><span class="badge ${user.role === 'admin' ? 'badge-important' : ''}">${user.role}</span></td>
        <td class="text-muted">${user.created_at || '-'}</td>
        <td>
          ${user.role !== 'admin'
            ? `<button class="btn btn-sm btn-danger" onclick="deleteUser(${user.id}, '${user.username}')">${__('Delete')}</button>`
            : '<span class="text-muted">-</span>'}
        </td>
      `;
      tbody.appendChild(tr);
    });
  } catch (err) {
    alert(__('Failed to load users: {0}', err.message));
  }
}

function showAddUser() {
  document.getElementById('addUserModal').style.display = 'flex';
  document.getElementById('newUsername').value = '';
  document.getElementById('newUserPassword').value = '';
  document.getElementById('addUserError').textContent = '';
  document.getElementById('newUsername').focus();
}

function closeAddUser() {
  document.getElementById('addUserModal').style.display = 'none';
}

async function addUser(e) {
  e.preventDefault();
  const username = document.getElementById('newUsername').value.trim();
  const password = document.getElementById('newUserPassword').value;
  const errorDiv = document.getElementById('addUserError');

  try {
    await api('/api/admin/users', {
      method: 'POST',
      body: { username, password },
    });
    closeAddUser();
    loadUsers();
  } catch (err) {
    errorDiv.textContent = err.message;
  }
}

async function deleteUser(userId, username) {
  if (!(await showDialog({ title: __('Delete User'), message: __('Delete user "{0}"? This will remove all their data.', username) }))) return;
  try {
    await api(`/api/admin/users/${userId}/delete`, { method: 'POST' });
    loadUsers();
  } catch (err) {
    alert(__('Failed to delete user: {0}', err.message));
  }
}

async function changePassword(e) {
  e.preventDefault();
  const newPwd = document.getElementById('newPassword').value;
  const confirmPwd = document.getElementById('confirmPassword').value;
  const msgDiv = document.getElementById('pwdMessage');

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
  } catch (err) {
    msgDiv.textContent = err.message;
    msgDiv.className = 'form-message form-error';
  }
}

// Close modal on overlay click
document.addEventListener('click', (e) => {
  if (e.target.classList.contains('modal')) {
    e.target.style.display = 'none';
  }
});
