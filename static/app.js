// ── NAV ──
function switchTab(id){
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.nav-item[data-tab]').forEach(n=>n.classList.remove('active'));
  const tab=document.getElementById(id+'-tab');
  const nav=document.querySelector(`.nav-item[data-tab="${id}"]`);
  if(tab)tab.classList.add('active');
  if(nav)nav.classList.add('active');
  document.getElementById('pageTitle').textContent={dashboard:'Dashboard',settings:'Configuration',logs:'System Logs'}[id]||id;
}
document.querySelectorAll('.nav-item[data-tab]').forEach(n=>n.addEventListener('click',()=>switchTab(n.dataset.tab)));

// ── TOAST ──
function toast(msg,type='ok'){
  const c=document.getElementById('toasts'),t=document.createElement('div');
  t.className=`toast t-${type}`;t.textContent=msg;c.appendChild(t);
  requestAnimationFrame(()=>t.classList.add('show'));
  setTimeout(()=>{t.classList.remove('show');setTimeout(()=>t.remove(),300)},4000);
}

// ── MODAL ──
const otpModal=document.getElementById('otpModal');
let pendingAuth={};
function closeModal(){otpModal.style.display='none'}

// ── AUTH ──
let _otpLock = false;

async function requestOTP(phone,clean){
  if(_otpLock) return;
  const apiId=document.getElementById('g_api_id')?.value||'';
  const apiHash=document.getElementById('g_api_hash')?.value||'';
  if(!apiId||!apiHash){toast('Save API credentials in Settings first','warn');switchTab('settings');return}
  
  _otpLock = true;
  toast('Requesting OTP, please wait...', 'ok');
  const fd=new FormData();fd.append('api_id',apiId);fd.append('api_hash',apiHash);fd.append('phone',phone);
  try{
    const r=await fetch('/api/auth/send_code',{method:'POST',body:fd});
    const d=await r.json();
    if(d.status==='success'){
      pendingAuth={phone,clean,hash:d.phone_code_hash};
      document.getElementById('otpDesc').textContent='Code sent to '+phone;
      document.getElementById('otpInput').value='';
      otpModal.style.display='flex';
      document.getElementById('otpInput').focus();
      toast('OTP sent successfully');
    }else toast(d.message,'err');
  }catch(e){toast('Network error','err')}
  _otpLock = false;
}

document.getElementById('verifyBtn')?.addEventListener('click',async()=>{
  const code=document.getElementById('otpInput').value;
  if(!code||code.length<5){toast('Enter valid 5-digit code','warn');return}
  const btn=document.getElementById('verifyBtn');btn.disabled=true;btn.textContent='Verifying...';
  const fd=new FormData();
  fd.append('api_id',document.getElementById('g_api_id').value);
  fd.append('api_hash',document.getElementById('g_api_hash').value);
  fd.append('phone',pendingAuth.phone);fd.append('phone_code_hash',pendingAuth.hash);fd.append('code',code);
  try{
    const r=await fetch('/api/auth/sign_in',{method:'POST',body:fd});
    const d=await r.json();
    if(d.status==='success'){toast('Account authenticated!');closeModal();setTimeout(()=>location.reload(),800)}
    else toast(d.message,'err');
  }catch(e){toast('Verification failed','err')}
  btn.disabled=false;btn.textContent='Verify';
});

async function logoutAccount(phone){
  if(!confirm('Revoke session for '+phone+'?'))return;
  const fd=new FormData();fd.append('phone',phone);
  try{
    const r=await fetch('/api/auth/logout_account',{method:'POST',body:fd});
    const d=await r.json();
    if(d.status==='success'){toast('Session revoked');setTimeout(()=>location.reload(),800)}
    else toast(d.message,'err');
  }catch(e){toast('Failed','err')}
}

// ── BOT CONTROL ──
async function startBot(){
  try{const r=await fetch('/start',{method:'POST'});const d=await r.json();
    if(d.status==='success'){toast('Dispatch started!');setTimeout(()=>location.reload(),800)}
    else toast(d.message,'err');
  }catch(e){toast('Failed to start','err')}
}
async function stopBot(){
  try{const r=await fetch('/stop',{method:'POST'});const d=await r.json();
    if(d.status==='success'){toast('Dispatch stopped');setTimeout(()=>location.reload(),800)}
    else toast(d.message,'err');
  }catch(e){toast('Failed to stop','err')}
}

// ── CONFIG ──
async function saveConfig(){
  const fd=new FormData(document.getElementById('configForm'));
  try{const r=await fetch('/save',{method:'POST',body:fd});const d=await r.json();
    if(d.status==='success')toast('Configuration saved!');else toast(d.message,'err');
  }catch(e){toast('Save failed','err')}
}

// ── LIVE LOGS ──
let lastLog='',userScrolling=false;
const logEl=document.getElementById('logBox');
if(logEl){
  logEl.addEventListener('scroll',()=>{userScrolling=logEl.scrollHeight-logEl.scrollTop-logEl.clientHeight>40});
  setInterval(async()=>{
    try{const r=await fetch('/logs');const t=await r.text();
      if(t!==lastLog){lastLog=t;logEl.textContent=t;if(!userScrolling)logEl.scrollTop=logEl.scrollHeight}
    }catch(e){}
  },2000);
}

// ── SOCKETIO REAL-TIME (optional, degrades gracefully) ──
if(typeof io!=='undefined'){
  try{
    const socket=io();
    socket.on('status_update',data=>{
      const dot=document.getElementById('sysDot');
      const label=document.getElementById('sysLabel');
      if(dot)dot.style.background=data.bot_running?'var(--green)':'var(--text3)';
      if(label)label.textContent=data.bot_running?'System Running':'System Idle';
      const accounts=data.accounts||[];
      const el=id=>document.getElementById(id);
      if(el('statTotal'))el('statTotal').textContent=accounts.length;
      if(el('statActive'))el('statActive').textContent=accounts.filter(a=>a.status==='active'||a.authenticated).length;
      if(el('statSent'))el('statSent').textContent=accounts.reduce((s,a)=>s+(a.sent||0),0);
      if(el('statErrors'))el('statErrors').textContent=accounts.reduce((s,a)=>s+(a.errors||0),0);
    });
  }catch(e){}
}

// ── ADD ACCOUNT MODAL ──
let _addLock=false; // Prevents rapid double-submit

function openAddModal(){
  document.getElementById('addModal').style.display='flex';
  const inp=document.getElementById('addPhoneInput');
  inp.value='';inp.focus();
}
function closeAddModal(){
  document.getElementById('addModal').style.display='none';
}

async function handleAddAccount(){
  if(_addLock)return; // Debounce
  const phone=document.getElementById('addPhoneInput').value.trim();
  if(!phone){toast('Enter a phone number','warn');return}

  _addLock=true;
  const btn=document.getElementById('addAccountBtn');
  btn.disabled=true;btn.textContent='Saving...';

  try{
    const res=await fetch('/api/add-account',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({phone})
    });
    const data=await res.json();
    if(data.status==='success'){
      toast('Account added!');
      closeAddModal();
      injectCard(data.account);
    }else{
      toast(data.message,'err');
    }
  }catch(e){toast('Failed to add account','err')}

  btn.disabled=false;btn.textContent='Save Account';
  _addLock=false;
}

// ── DROPDOWN & ACCOUNT ACTIONS ──
function toggleDropdown(id) {
  const menu = document.getElementById('menu-' + id);
  if (menu) {
    const isShowing = menu.classList.contains('show');
    document.querySelectorAll('.dropdown-menu').forEach(m => m.classList.remove('show'));
    if (!isShowing) menu.classList.add('show');
  }
}

window.addEventListener('click', (e) => {
  if (!e.target.matches('.icon-btn')) {
    document.querySelectorAll('.dropdown-menu').forEach(m => m.classList.remove('show'));
  }
});

async function togglePause(phone) {
  try {
    const res = await fetch('/api/pause-account', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ phone })
    });
    const data = await res.json();
    if (data.status === 'success') {
      toast(data.message, 'ok');
      setTimeout(() => location.reload(), 800);
    } else toast(data.message, 'err');
  } catch (e) { toast('Network error', 'err'); }
}

async function deleteAccount(phone) {
  if (!confirm('Are you sure you want to completely delete ' + phone + ' and its session?')) return;
  try {
    const res = await fetch('/api/delete-account', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ phone })
    });
    const data = await res.json();
    if (data.status === 'success') {
      toast('Account deleted');
      setTimeout(() => location.reload(), 800);
    } else toast(data.message, 'err');
  } catch (e) { toast('Network error', 'err'); }
}

// ── TARGET MODAL ──
async function openTargetModal(phone, clean_phone){
  document.getElementById('targetModalSubtitle').textContent = 'Targets for ' + phone;
  document.getElementById('targetPhoneInput').value = phone;
  const tArea = document.getElementById('targetListInput');
  tArea.value = 'Loading...';
  document.getElementById('targetModal').style.display='flex';
  
  try {
    const res = await fetch('/api/account-targets?phone=' + encodeURIComponent(phone));
    const data = await res.json();
    if(data.status === 'success') {
      tArea.value = data.targets || '';
    } else {
      tArea.value = '';
      toast(data.message, 'err');
    }
  } catch(e) {
    tArea.value = '';
    toast('Failed to load targets', 'err');
  }
}

function closeTargetModal(){
  document.getElementById('targetModal').style.display='none';
}

async function saveAccountTargets(){
  const phone = document.getElementById('targetPhoneInput').value;
  const targets = document.getElementById('targetListInput').value;
  const btn = document.getElementById('saveTargetsBtn');
  
  btn.disabled = true; btn.textContent = 'Saving...';
  try {
    const res = await fetch('/api/account-targets', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({phone, targets})
    });
    const data = await res.json();
    if(data.status === 'success'){
      toast('Targets saved successfully!');
      closeTargetModal();
    } else {
      toast(data.message, 'err');
    }
  } catch(e) {
    toast('Failed to save targets', 'err');
  }
  btn.disabled = false; btn.textContent = 'Save Targets';
}

function injectCard(acc){
  const grid=document.getElementById('accountGrid');
  if(!grid)return;

  // ── DUPLICATE GUARD ──
  // If a card with this ID already exists, do NOT create another one
  const existingCard=document.getElementById('card-'+acc.clean_phone);
  if(existingCard){
    // Flash the existing card to show "it's already here"
    existingCard.style.transition='box-shadow .2s';
    existingCard.style.boxShadow='0 0 0 3px var(--accent)';
    setTimeout(()=>{existingCard.style.boxShadow=''},1500);
    return;
  }

  // Remove empty state placeholder if present
  const empty=grid.querySelector('.empty');
  if(empty)empty.remove();

  // ── CREATE NEW CARD (unique, independent DOM node) ──
  const card=document.createElement('div');
  card.className='card';
  card.id='card-'+acc.clean_phone; // Guaranteed unique per phone
  card.style.opacity='0';card.style.transform='translateY(8px)';
  card.innerHTML=`
    <div class="card-top">
      <div class="card-profile">
        <div class="avatar">${acc.phone.slice(-2)}</div>
        <div><div class="card-name">${acc.phone}</div><div class="card-sub">session_${acc.clean_phone}</div></div>
      </div>
      <div class="badge b-unauth">Auth Required</div>
    </div>
    <div class="card-meta">
      <div>Status<span>Idle</span></div>
      <div>Last Action<span>Just added</span></div>
      <div>Sent<span>0</span></div>
      <div>Errors<span>0</span></div>
    </div>
    <div class="card-actions">
      <button class="btn btn-s btn-sm" onclick="openTargetModal('${acc.phone}', '${acc.clean_phone}')">Targets</button>
      <button class="btn btn-p btn-sm" onclick="requestOTP('${acc.phone}','${acc.clean_phone}')">Login via OTP</button>
    </div>`;

  // APPEND (not prepend) — new cards go to the end, existing ones stay in place
  grid.appendChild(card);

  // Trigger smooth fade-in animation
  requestAnimationFrame(()=>{
    card.style.transition='opacity .35s ease, transform .35s ease';
    card.style.opacity='1';card.style.transform='translateY(0)';
  });

  // Scroll the card into view smoothly
  card.scrollIntoView({behavior:'smooth',block:'nearest'});
}
