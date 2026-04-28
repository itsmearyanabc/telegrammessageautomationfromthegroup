/**
 * ARMEDIAS AI — Production Frontend Controller (Hardened + Nicknames)
 * ──────────────────────────────────────────────────────
 */

const socket = io();
let currentAccounts = [];

// ──────────────────────────────────────────────
// 1. AUTHENTICATION & APP INITIALIZATION
// ──────────────────────────────────────────────

function checkAuth() {
    const token = localStorage.getItem('token');
    if (!token && window.location.pathname !== '/login') {
        window.location.href = '/login';
        return null;
    }
    return token;
}

document.addEventListener('DOMContentLoaded', async () => {
    const token = checkAuth();
    if (token) {
        setLoading(true);
        await forceInitialSync();
        setLoading(false);
        
        socket.on('status_update', (data) => {
            currentAccounts = data.accounts || [];
            renderDashboard(currentAccounts);
            updateGlobalStats(currentAccounts);
        });

        refreshLogs();
        setInterval(refreshLogs, 5000);
    }
});

function setLoading(active) {
    const loader = document.getElementById('global-loader');
    if (loader) loader.style.display = active ? 'block' : 'none';
}

async function forceInitialSync() {
    const data = await apiCall('/api/dashboard/sync');
    if (data && data.status === 'success') {
        currentAccounts = data.accounts || [];
        renderDashboard(currentAccounts);
        updateGlobalStats(currentAccounts);
    }
}

// ──────────────────────────────────────────────
// 2. CORE API INTERFACE
// ──────────────────────────────────────────────

async function apiCall(url, options = {}) {
    const token = localStorage.getItem('token');
    const headers = {
        'Authorization': `Bearer ${token}`,
        'Content-Type': 'application/json',
        ...(options.headers || {})
    };

    try {
        const response = await fetch(url, { ...options, headers });
        
        if (response.status === 401) {
            localStorage.removeItem('token');
            window.location.href = '/login';
            return { status: 'error', message: 'Unauthorized' };
        }

        const contentType = response.headers.get("content-type");
        if (contentType && contentType.indexOf("application/json") !== -1) {
            return await response.json();
        } else {
            return { status: 'success', text: await response.text() };
        }
    } catch (e) {
        console.error("API Error:", e);
        return { status: 'error', message: 'Connection lost' };
    }
}

// ──────────────────────────────────────────────
// 3. UI RENDERING
// ──────────────────────────────────────────────

function renderDashboard(accounts) {
    const grid = document.getElementById('sessions-grid');
    if (!grid) return;
    
    const currentIds = new Set(accounts.map(a => `card-${a.clean_phone}`));
    Array.from(grid.children).forEach(child => {
        if (!currentIds.has(child.id)) child.remove();
    });

    accounts.forEach(acc => {
        let card = document.getElementById(`card-${acc.clean_phone}`);
        if (!card) {
            card = createCardNode(acc);
            grid.appendChild(card);
        }
        updateCardContent(card, acc);
    });
}

function getDisplayName(acc) {
    if (acc.nickname && acc.nickname.trim()) return acc.nickname.trim();
    return acc.phone;
}

function createCardNode(acc) {
    const card = document.createElement('div');
    card.className = 'card';
    card.id = `card-${acc.clean_phone}`;
    const displayName = getDisplayName(acc);
    card.innerHTML = `
        <div class="card-top">
          <div class="card-profile">
            <div class="avatar">${displayName.slice(0, 2).toUpperCase()}</div>
            <div>
              <div class="card-name">${displayName}</div>
              <div class="card-sub">${acc.phone}</div>
            </div>
          </div>
          <div class="badge-container"></div>
        </div>
        <div class="progress-container" style="height:6px; background:#eee; border-radius:3px; margin: 15px 0 5px 0; overflow:hidden">
            <div class="progress-bar" style="height:100%; width:0%; background:var(--primary); transition:width 0.3s ease"></div>
        </div>
        <div style="font-size:0.7rem; color:var(--text2); display:flex; justify-content:space-between; margin-bottom:10px">
            <span class="progress-text">0% Complete</span>
            <span class="count-text">0 / 0</span>
        </div>
        <div class="card-meta">
          <div>Status<span class="status-text">—</span></div>
          <div>Last Sent<span class="stat-time">—</span></div>
          <div>Sent<span class="stat-sent">0</span></div>
          <div>Errors<span class="stat-errors">0</span></div>
        </div>
        <div class="card-action-bar" style="margin-top:16px; font-size:.7rem; color:var(--text2); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; margin-bottom:12px; height: 1.2em; border-left: 2px solid var(--primary); padding-left: 8px;">
            Initializing...
        </div>
        <div class="card-actions">
          <button class="btn btn-p btn-sm btn-dispatch" style="flex:2">Dispatch</button>
          <button class="btn btn-s btn-sm btn-loop"><i class="fas fa-play"></i></button>
          <button class="btn btn-s btn-sm btn-settings"><i class="fas fa-cog"></i></button>
          <button class="btn btn-s btn-sm btn-rename" title="Rename Account"><i class="fas fa-pen"></i></button>
          <button class="btn btn-s btn-sm btn-logout" title="Logout Session"><i class="fas fa-sign-out-alt"></i></button>
          <button class="btn btn-d btn-sm btn-delete" title="Delete Permanent"><i class="fas fa-trash"></i></button>
        </div>
    `;
    return card;
}

function updateCardContent(card, acc) {
    const badgeContainer = card.querySelector('.badge-container');
    const progBar = card.querySelector('.progress-bar');
    const progText = card.querySelector('.progress-text');
    const countText = card.querySelector('.count-text');

    // Update display name if changed
    const displayName = getDisplayName(acc);
    const nameEl = card.querySelector('.card-name');
    if (nameEl && nameEl.textContent !== displayName) {
        nameEl.textContent = displayName;
        card.querySelector('.card-sub').textContent = acc.phone;
        const avatar = card.querySelector('.avatar');
        if (avatar) avatar.textContent = displayName.slice(0, 2).toUpperCase();
    }

    let bClass = 'b-idle', bText = (acc.state || 'idle').toUpperCase();
    if (acc.state === 'sending') bClass = 'b-active';
    else if (acc.state === 'cooldown') bClass = 'b-cooldown';
    else if (acc.state === 'unauth') bClass = 'b-unauth';
    else if (acc.is_running) { bClass = 'b-active'; bText = 'RUNNING'; }

    badgeContainer.innerHTML = `<div class="badge ${bClass}">${bText}</div>`;
    card.querySelector('.status-text').textContent = bText;

    const p = acc.progress || 0;
    progBar.style.width = `${p}%`;
    progBar.style.background = (acc.errors || 0) > 0 ? '#ff4d4f' : 'var(--primary)';
    progText.textContent = `${p}% Complete`;
    countText.textContent = `${(acc.sent || 0) + (acc.errors || 0)} / ${acc.total || 0}`;

    card.querySelector('.stat-time').textContent = formatTime(acc.last_dispatch_time);
    card.querySelector('.stat-sent').textContent = acc.sent || 0;
    card.querySelector('.stat-errors').textContent = acc.errors || 0;
    card.querySelector('.card-action-bar').textContent = acc.last_action || 'Idle';

    const dispatchBtn = card.querySelector('.btn-dispatch');
    const loopBtn = card.querySelector('.btn-loop');
    const logoutBtn = card.querySelector('.btn-logout');
    const deleteBtn = card.querySelector('.btn-delete');
    const renameBtn = card.querySelector('.btn-rename');

    if (!acc.authenticated) {
        dispatchBtn.style.display = 'inline-flex';
        dispatchBtn.innerHTML = '<i class="fas fa-key"></i> Login';
        dispatchBtn.onclick = () => openLoginModal(acc.phone);
        loopBtn.style.display = 'none';
        logoutBtn.style.display = 'none';
    } else {
        dispatchBtn.style.display = 'none';
        loopBtn.style.display = 'inline-flex';
        loopBtn.style.flex = '2';
        loopBtn.className = `btn btn-sm btn-loop ${acc.is_running ? 'btn-d' : 'btn-p'}`;
        loopBtn.innerHTML = `<i class="fas ${acc.is_running ? 'fa-pause' : 'fa-play'}"></i> ${acc.is_running ? 'Pause' : 'Start'}`;
        loopBtn.onclick = () => toggleLoop(acc.clean_phone);
        logoutBtn.style.display = 'inline-flex';
        logoutBtn.onclick = () => logoutAccount(acc.phone);
    }
    card.querySelector('.btn-settings').onclick = () => openSessionSettings(acc.clean_phone);
    renameBtn.onclick = () => promptRename(acc.clean_phone, acc.phone, acc.nickname || '');
    deleteBtn.onclick = () => deleteAccount(acc.phone);
}

function updateGlobalStats(accounts) {
    const stats = { total: accounts.length, active: accounts.filter(a => a.is_running).length, sent: accounts.reduce((s, a) => s + (a.sent || 0), 0), errors: accounts.reduce((s, a) => s + (a.errors || 0), 0) };
    const mapping = { 'statTotal': stats.total, 'statActive': stats.active, 'statSent': stats.sent, 'statErrors': stats.errors };
    for (const [id, val] of Object.entries(mapping)) {
        const el = document.getElementById(id);
        if (el && el.textContent != val) el.textContent = val;
    }
}

// ──────────────────────────────────────────────
// 4. API ACTIONS
// ──────────────────────────────────────────────

async function logoutAccount(phone) {
    if (!confirm(`Logout session ${phone}?`)) return;
    const data = await apiCall('/api/logout-account', { method: 'POST', body: JSON.stringify({phone}) });
    if (data.status === 'success') toast('Logged out');
}

async function manualDispatch(phone) {
    const data = await apiCall('/api/session/dispatch', { method: 'POST', body: JSON.stringify({phone}) });
    toast(data.status === 'success' ? 'Dispatch triggered' : data.message, data.status === 'success' ? 'ok' : 'err');
}

async function toggleLoop(phone) {
    const acc = currentAccounts.find(a => a.clean_phone === phone);
    if (!acc) return;
    const endpoint = acc.is_running ? '/api/session/stop' : '/api/session/start';
    const data = await apiCall(endpoint, { method: 'POST', body: JSON.stringify({phone}) });
    toast(data.status === 'success' ? (acc.is_running ? 'Stopped' : 'Started') : data.message, data.status === 'success' ? 'ok' : 'err');
}

async function openSessionSettings(phone) {
    const acc = currentAccounts.find(a => a.clean_phone === phone);
    if (!acc) return;
    document.getElementById('edit-phone').value = phone;
    document.getElementById('modal-phone').textContent = `Config: ${getDisplayName(acc)}`;
    document.getElementById('edit-source').value = acc.source_channel || '';
    document.getElementById('edit-interval').value = acc.loop_interval || 15;
    document.getElementById('edit-delay').value = acc.msg_delay || 5;
    document.getElementById('edit-nickname').value = acc.nickname || '';
    const data = await apiCall(`/api/account-targets?phone=${encodeURIComponent(acc.phone)}`);
    document.getElementById('edit-targets').value = data.targets || '';
    showModal('settings-modal');
}

async function saveSessionSettings() {
    const payload = { 
        phone: document.getElementById('edit-phone').value, 
        source_channel: document.getElementById('edit-source').value, 
        loop_interval: parseInt(document.getElementById('edit-interval').value), 
        msg_delay: parseInt(document.getElementById('edit-delay').value), 
        targets: document.getElementById('edit-targets').value.split('\n').map(x => x.trim()).filter(x => x),
        nickname: document.getElementById('edit-nickname').value.trim()
    };
    const data = await apiCall('/api/session/settings', { method: 'POST', body: JSON.stringify(payload) });
    if (data.status === 'success') { toast('Settings Saved'); closeModal(); } else toast(data.message, 'err');
}

async function promptAddAccount() {
    const phone = prompt("Enter Phone Number (+):"); 
    if (!phone) return;
    const data = await apiCall('/api/add-account', { method: 'POST', body: JSON.stringify({ phone: phone.trim() }) });
    toast(data.status === 'success' ? 'Account added' : data.message, data.status === 'success' ? 'ok' : 'err');
}

async function deleteAccount(phone) {
    if (!confirm(`Permanently delete ${phone}?`)) return;
    const data = await apiCall('/api/delete-account', { method: 'POST', body: JSON.stringify({phone}) });
    toast(data.status === 'success' ? 'Deleted' : data.message);
}

async function promptRename(cleanPhone, phone, currentNick) {
    const nickname = prompt(`Set a nickname for ${phone}:`, currentNick);
    if (nickname === null) return; // cancelled
    const data = await apiCall('/api/session/rename', { method: 'POST', body: JSON.stringify({ phone: cleanPhone, nickname: nickname.trim() }) });
    if (data.status === 'success') { 
        toast(nickname.trim() ? `Renamed to "${nickname.trim()}"` : 'Nickname removed'); 
        await forceInitialSync();
    } else toast(data.message, 'err');
}

// ──────────────────────────────────────────────
// 5. AUTH FLOW
// ──────────────────────────────────────────────

let pendingAuth = {};
function openLoginModal(phone) {
    pendingAuth.phone = phone;
    document.getElementById('otp-phone-display').textContent = `Auth: ${phone}`;
    document.getElementById('otp-step-1').classList.remove('hidden');
    document.getElementById('otp-step-2').classList.add('hidden');
    const step3 = document.getElementById('otp-step-3');
    if(step3) step3.classList.add('hidden');
    showModal('otp-modal');
}

async function sendOTP() {
    const btn = document.querySelector('#otp-step-1 .btn-p');
    btn.disabled = true; btn.textContent = 'Sending...';
    const payload = {
        api_id: document.getElementById('api-id').value,
        api_hash: document.getElementById('api-hash').value,
        phone: pendingAuth.phone
    };
    const data = await apiCall('/api/auth/send_code', { method: 'POST', body: JSON.stringify(payload) });
    btn.disabled = false; btn.textContent = 'Request Code';
    if (data.status === 'success') { 
        pendingAuth.hash = data.phone_code_hash; 
        document.getElementById('otp-step-1').classList.add('hidden'); 
        document.getElementById('otp-step-2').classList.remove('hidden'); 
        toast('OTP Sent'); 
    } else toast(data.message, 'err');
}

async function verifyOTP() {
    const btn = document.querySelector('#otp-step-2 .btn-p');
    btn.disabled = true; btn.textContent = 'Verifying...';
    const payload = {
        phone: pendingAuth.phone,
        phone_code_hash: pendingAuth.hash,
        code: document.getElementById('otp-code').value
    };
    const data = await apiCall('/api/auth/sign_in', { method: 'POST', body: JSON.stringify(payload) });
    btn.disabled = false; btn.textContent = 'Verify Account';
    if (data.status === 'success') { 
        toast('Verified!'); 
        closeModal(); 
        await forceInitialSync();
    } else if (data.status === '2fa_required') {
        document.getElementById('otp-step-2').classList.add('hidden');
        document.getElementById('otp-step-3').classList.remove('hidden');
        toast('2FA password required', 'warn');
    } else toast(data.message, 'err');
}

async function verifyPassword() {
    const btn = document.querySelector('#otp-step-3 .btn-p');
    btn.disabled = true; btn.textContent = 'Verifying...';
    const payload = {
        phone: pendingAuth.phone,
        password: document.getElementById('otp-password').value
    };
    const data = await apiCall('/api/auth/check_password', { method: 'POST', body: JSON.stringify(payload) });
    btn.disabled = false; btn.textContent = 'Submit Password';
    if (data.status === 'success') { 
        toast('Verified!'); 
        closeModal(); 
        await forceInitialSync();
    } else toast(data.message, 'err');
}

// ──────────────────────────────────────────────
// 6. UTILS
// ──────────────────────────────────────────────

function formatTime(ts) { 
    if (!ts) return 'Never'; 
    const d = new Date(ts * 1000); 
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }); 
}

function toast(msg, type = 'ok') { 
    const container = document.getElementById('toasts'); 
    if (!container) return;
    const t = document.createElement('div'); 
    t.className = `toast t-${type}`; 
    t.innerHTML = `<span>${msg}</span>`;
    container.appendChild(t); 
    setTimeout(() => t.classList.add('show'), 10);
    setTimeout(() => { 
        t.classList.remove('show'); 
        setTimeout(() => t.remove(), 300); 
    }, 4000);
}

async function refreshLogs() { 
    const logArea = document.getElementById('log-content'); 
    if (!logArea) return; 
    const r = await apiCall('/logs'); 
    logArea.textContent = r.text || r.message || "No logs."; 
    logArea.scrollTop = logArea.scrollHeight; 
}

function showModal(id) { document.getElementById(id).style.display = 'flex'; }
function closeModal() { document.querySelectorAll('.overlay').forEach(o => o.style.display = 'none'); }
function openGlobalSettings() { showModal('global-settings-modal'); }

async function saveGlobalSettings() {
    const payload = {
        api_id: document.getElementById('global-api-id').value,
        api_hash: document.getElementById('global-api-hash').value,
        source_channel: document.getElementById('global-source').value,
        loop_interval: parseInt(document.getElementById('global-interval').value),
        msg_delay: parseInt(document.getElementById('global-delay').value)
    };
    const data = await apiCall('/save-global', { method: 'POST', body: JSON.stringify(payload) });
    if (data.status === 'success') { toast('Saved'); closeModal(); } else toast(data.message, 'err');
}

function adminLogout() {
    localStorage.removeItem('token');
    window.location.href = '/logout';
}
