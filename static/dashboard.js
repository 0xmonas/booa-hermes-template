async function gatewayAction(action) {
  await fetch(`/gateway/${action}`, { method: 'POST' });
  location.reload();
}

async function checkErrors() {
  const res = await fetch('/gateway/errors');
  const data = await res.json();
  const card = document.getElementById('error-card');
  const msg = document.getElementById('error-message');

  if (data.errors && data.errors.length > 0) {
    const latest = data.errors[data.errors.length - 1];
    card.style.display = 'block';

    if (latest.includes('credit balance is too low') || latest.includes('Rate limited')) {
      msg.textContent = 'Your LLM provider returned an error. Check your API key credits and billing. Go to Settings to update.';
    } else if (latest.includes('Invalid token') || latest.includes('Unauthorized')) {
      msg.textContent = 'Telegram bot token is invalid. Go to Settings to update.';
    } else {
      msg.textContent = latest.slice(0, 200);
    }
  } else {
    card.style.display = 'none';
  }
}

// user_name comes straight from Telegram — treat every field as hostile
// before it touches innerHTML.
function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[c]);
}

async function checkPairing() {
  const res = await fetch('/pairing');
  const data = await res.json();
  const pending = document.getElementById('pairing-pending');
  const empty = document.getElementById('pairing-empty');
  const setup = document.getElementById('pairing-setup');
  const connected = document.getElementById('pairing-connected');
  const users = document.getElementById('pairing-users');

  // Check if any users are already approved
  if (data.approved && data.approved.length > 0) {
    connected.style.display = 'block';
    users.innerHTML = data.approved.map(u => `
      <div class="flex items-center justify-between" style="padding:6px 0;">
        <span>${esc(u.user_name || u.user_id)}<span class="text-xs text-muted"> — ${esc(u.platform)}</span></span>
        <button class="btn-sm btn-outline" data-action="pair-unpair" data-platform="${esc(u.platform)}" data-user="${esc(u.user_id)}">Unpair</button>
      </div>
    `).join('');
    if (!data.pending || data.pending.length === 0) {
      setup.style.display = 'none';
    }
  } else {
    connected.style.display = 'none';
    setup.style.display = 'block';
  }

  if (data.pending && data.pending.length > 0) {
    setup.style.display = 'block';
    empty.style.display = 'none';
    pending.innerHTML = data.pending.map(p => `
      <div class="flex items-center justify-between" style="padding:8px 0;border-bottom:1px solid var(--border);">
        <div>
          <strong>${esc(p.user_name || 'Unknown')}</strong>
          <span class="text-xs text-muted"> — code: ${esc(p.code)} — ${esc(p.age_minutes)}m ago</span>
        </div>
        <div class="flex gap-1">
          <button data-action="pair-approve" data-platform="${esc(p.platform)}" data-code="${esc(p.code)}" class="btn-sm">Approve</button>
          <button data-action="pair-deny" data-platform="${esc(p.platform)}" data-code="${esc(p.code)}" class="btn-sm btn-outline">Deny</button>
        </div>
      </div>
    `).join('');
  } else if (!data.approved || data.approved.length === 0) {
    empty.style.display = 'block';
    pending.innerHTML = '';
  }
}

async function unpairUser(platform, userId) {
  if (!confirm(`Unpair this ${platform} user? They lose access to your agent.`)) return;
  const res = await fetch('/pairing/unpair', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ platform, user_id: userId })
  });
  const d = await res.json().catch(() => ({}));
  if (!res.ok) { alert(d.error || 'Unpair failed.'); return; }
  if (d.restart_needed && confirm('They were also in the startup allowlist. Restart the gateway now to fully revoke access?')) {
    await fetch('/gateway/stop', { method: 'POST' });
    await fetch('/gateway/start', { method: 'POST' });
  }
  checkPairing();
}

async function pairingAction(action, platform, code) {
  await fetch(`/pairing/${action}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ platform, code })
  });
  checkPairing();
}

async function confirmExport() {
  const pw = prompt('Enter your admin password to export data:');
  if (!pw) return;
  const fd = new FormData();
  fd.append('password', pw);
  const res = await fetch('/download', { method: 'POST', body: fd });
  if (!res.ok) { alert('Export failed — wrong password?'); return; }
  const blob = await res.blob();
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  const cd = res.headers.get('Content-Disposition') || '';
  const m = cd.match(/filename="([^"]+)"/);
  a.download = m ? m[1] : 'booa-backup.zip';
  a.click();
  URL.revokeObjectURL(a.href);
}

function confirmImport() {
  const input = document.createElement('input');
  input.type = 'file';
  input.accept = '.zip';
  input.onchange = async () => {
    const file = input.files[0];
    if (!file) return;
    const pw = prompt('Enter your admin password to import. This overwrites memories, skills, context, sessions and SOUL.md, then restarts the gateway. config.yaml and your wallet vault are not touched.');
    if (!pw) return;
    let archivePw = '';
    if (!confirm('Was this backup exported with the same admin password? OK = yes, Cancel = enter the archive password.')) {
      archivePw = prompt('Archive password:') || '';
      if (!archivePw) return;
    }
    await sendImport(file, pw, archivePw, false);
  };
  input.click();
}

async function sendImport(file, pw, archivePw, confirmMismatch) {
  const fd = new FormData();
  fd.append('archive', file);
  fd.append('admin_password', pw);
  if (archivePw) fd.append('archive_password', archivePw);
  if (confirmMismatch) fd.append('confirm_token_mismatch', '1');
  const res = await fetch('/import', { method: 'POST', body: fd });
  const d = await res.json().catch(() => ({}));
  if (res.status === 409 && d.error === 'token_mismatch') {
    if (confirm(`This backup belongs to BOOA #${d.manifest_token_id} but this instance runs BOOA #${d.instance_token_id}. Import anyway?`)) {
      await sendImport(file, pw, archivePw, true);
    }
    return;
  }
  if (res.ok) {
    alert('Import complete. Restored: ' + (d.restored || []).join(', ') + ((d.warnings || []).length ? '\n' + d.warnings.join('\n') : ''));
    location.reload();
  } else {
    alert('Import failed: ' + (d.error || 'unknown error'));
  }
}

const WALLET_STATES = {
  'no-wallet':  { emoji: '⚠',  text: 'No wallet yet. Open Telegram and tell your agent: "set up my wallet". It creates an encrypted OWS wallet and this card updates automatically.', canVerify: false, canLink: false, verifyLabel: 'Verify Wallet' },
  'unverified': { emoji: '🟡', text: 'Wallet ready. Generate a link code to set it onchain — signing the code also proves you control the wallet.', canVerify: true, canLink: true, verifyLabel: 'Verify Wallet' },
  'verified':   { emoji: '🟠', text: 'Wallet verified but not linked yet. Generate the link code to set this wallet onchain via booa.app/bridge.', canVerify: true, canLink: true, verifyLabel: 'Verify another wallet' },
  'linked':     { emoji: '✅', text: 'Wallet verified and linked as the agent wallet onchain.', canVerify: true, canLink: true, verifyLabel: 'Verify a different wallet' },
  'orphan':     { emoji: '🔴', text: 'ORPHAN — NFT ownership has moved to another wallet.', canVerify: true, canLink: false, verifyLabel: 'Verify the new owner wallet' },
  'unknown':    { emoji: '❓', text: 'Status unknown (BOOA API unreachable). Click Refresh to retry. Wallet setup happens in Telegram: tell your agent "set up my wallet", then "link my wallet" to register it onchain.', canVerify: false, canLink: false, verifyLabel: 'Verify Wallet' },
};

async function renderWalletStatus() {
  try {
    const res = await fetch('/api/wallet/status');
    const data = await res.json();
    const body = document.getElementById('wallet-status-body');
    const btn = document.getElementById('wallet-verify-btn');
    const linkBtn = document.getElementById('wallet-link-btn');
    const def = WALLET_STATES[data.state] || WALLET_STATES['unknown'];
    const active = data.verified_wallet || data.agent_wallet;
    const walletStr = active ? ` · <code>${esc(active.slice(0,6))}...${esc(active.slice(-4))}</code>` : '';
    body.innerHTML = `${def.emoji} <strong>${esc(data.state)}</strong>${walletStr}<br><span class="text-xs text-muted">${esc(def.text)}</span>`;
    btn.textContent = def.verifyLabel;
    btn.style.display = def.canVerify ? '' : 'none';
    linkBtn.textContent = data.state === 'linked' ? 'Relink runtime wallet' : 'Generate link code';
    linkBtn.style.display = def.canLink ? '' : 'none';
  } catch (e) {
    document.getElementById('wallet-status-body').textContent = 'failed to fetch status';
  }
}

async function generateLinkCode() {
  const box = document.getElementById('wallet-link-box');
  const out = document.getElementById('wallet-link-code');
  const urlBtn = document.getElementById('wallet-link-url');
  const qrBox = document.getElementById('wallet-link-qr');
  const qrImg = document.getElementById('wallet-link-qr-img');
  const agentSpan = document.getElementById('wallet-link-agent');
  const result = document.getElementById('wallet-link-result');
  box.style.display = '';
  out.value = '';
  urlBtn.style.display = 'none';
  qrBox.style.display = 'none';
  result.textContent = 'Signing with OWS…';
  try {
    const res = await fetch('/api/wallet/link-code', { method: 'POST' });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      result.textContent = 'Failed: ' + (data.error || 'unknown');
      return;
    }
    out.value = data.blob;
    if (data.agentId != null) agentSpan.textContent = data.agentId;
    if (data.url) {
      urlBtn.href = data.url;
      urlBtn.style.display = '';
    }
    if (data.qr) {
      qrImg.src = data.qr;
      qrBox.style.display = '';
    }
    result.textContent = 'Ready — open the link or scan the QR to finish onchain.';
  } catch (e) {
    result.textContent = 'Request failed.';
  }
}

async function refreshWalletStatus() {
  await fetch('/api/wallet/refresh', { method: 'POST' });
  renderWalletStatus();
}

async function startWalletVerify() {
  const res = await fetch('/api/wallet/challenge', { method: 'POST' });
  if (!res.ok) {
    const err = await res.json();
    alert('Challenge failed: ' + (err.error || 'unknown'));
    return;
  }
  const data = await res.json();
  const escaped = data.message.replace(/"/g, '\\"');
  const walletName = data.suggested_name || 'my-agent';
  const cmd = `OWS_PASSPHRASE=$OWS_API_KEY ows sign message \\\n  --wallet ${walletName} --chain ethereum \\\n  --message "${escaped}"`;
  document.getElementById('wallet-ows-command').value = cmd;
  document.getElementById('wallet-signature-input').value = '';
  document.getElementById('wallet-verify-result').textContent = '';
  document.getElementById('wallet-challenge-box').style.display = '';
  document.getElementById('wallet-challenge-box').dataset.nonce = data.nonce;
}

async function submitWalletSignature() {
  const box = document.getElementById('wallet-challenge-box');
  const nonce = box.dataset.nonce;
  const signature = document.getElementById('wallet-signature-input').value.trim();
  if (!signature) return;
  const res = await fetch('/api/wallet/verify', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ nonce, signature }),
  });
  const data = await res.json();
  const out = document.getElementById('wallet-verify-result');
  if (data.ok) {
    out.textContent = `✅ Verified (state: ${data.state})`;
    box.style.display = 'none';
    renderWalletStatus();
  } else {
    out.textContent = '❌ ' + (data.error || 'verification failed');
  }
}

const OC_KEYS = ['BOOA_ONCHAIN_MCP','BOOA_ONCHAIN_WRITES','BOOA_MAX_TX_ETH','BOOA_DAILY_CAP_ETH','BOOA_SEND_ALLOWLIST','BOOA_SWAP_TOKEN_ALLOWLIST','BOOA_MAX_SLIPPAGE_BPS','BOOA_OPENSEA_MCP','BOOA_OPENSEA_REQUIRE_VERIFIED'];
const OC_BOOLS = ['BOOA_ONCHAIN_MCP','BOOA_ONCHAIN_WRITES','BOOA_OPENSEA_MCP','BOOA_OPENSEA_REQUIRE_VERIFIED'];
const isOn = v => ['1','true','yes','on'].includes(String(v).toLowerCase());
async function loadOnchainSettings() {
  try {
    const res = await fetch('/api/onchain-settings');
    if (!res.ok) return;
    const d = await res.json();
    for (const k of OC_KEYS) {
      const el = document.getElementById('oc-'+k);
      if (!el) continue;
      if (OC_BOOLS.includes(k)) el.checked = isOn(d[k]);
      else el.value = d[k] || '';
    }
    const ks = document.getElementById('oc-key-status');
    if (ks) ks.textContent = d.OPENSEA_API_KEY_set ? 'set ✓' : 'not set';
  } catch (e) {}
}
async function saveOnchainSettings() {
  const result = document.getElementById('oc-result');
  result.textContent = 'Saving…';
  const fd = new FormData();
  for (const k of OC_KEYS) {
    const el = document.getElementById('oc-'+k);
    if (!el) continue;
    fd.append(k, OC_BOOLS.includes(k) ? (el.checked ? '1' : '0') : el.value.trim());
  }
  try {
    const res = await fetch('/api/onchain-settings', { method: 'POST', body: fd });
    const d = await res.json();
    result.textContent = res.ok ? (d.note || 'Saved.') : ('Failed: ' + (d.error || 'unknown'));
    if (res.ok) loadOnchainSettings();
  } catch (e) { result.textContent = 'Request failed.'; }
}

function fmtPerMTok(v) {
  const n = parseFloat(v);
  return Number.isFinite(n) ? '$' + (n * 1e6).toFixed(2).replace(/\.?0+$/, '') : '?';
}

let modelCatalog = [];
let modelActiveIdx = -1;

function modelDd() { return document.getElementById('model-dd'); }

function renderModelList(query) {
  const dd = modelDd();
  const q = query.trim().toLowerCase();
  const hits = modelCatalog.filter(m =>
    m.id.toLowerCase().includes(q) || m.name.toLowerCase().includes(q));
  modelActiveIdx = hits.length ? 0 : -1;
  if (!hits.length) {
    dd.innerHTML = '<div class="combo-empty">No match — a raw provider/model id also works.</div>';
  } else {
    dd.innerHTML = hits.slice(0, 250).map((m, i) => `
      <div class="combo-item${i === 0 ? ' active' : ''}" data-action="model-pick" data-model-id="${esc(m.id)}">
        <div>
          <div class="combo-name">${esc(m.name)}</div>
          <div class="combo-id">${esc(m.id)}</div>
        </div>
        <div class="combo-price">${fmtPerMTok(m.prompt_price)}/${fmtPerMTok(m.completion_price)} MTok</div>
      </div>
    `).join('');
  }
  dd.classList.add('open');
}

function moveModelActive(delta) {
  const items = [...modelDd().querySelectorAll('.combo-item')];
  if (!items.length) return;
  modelActiveIdx = Math.min(items.length - 1, Math.max(0, modelActiveIdx + delta));
  items.forEach((el, i) => el.classList.toggle('active', i === modelActiveIdx));
  items[modelActiveIdx].scrollIntoView({ block: 'nearest' });
}

function pickModel(id) {
  document.getElementById('model-input').value = id;
  modelDd().classList.remove('open');
}

async function openModelBox() {
  const box = document.getElementById('model-box');
  const hidden = box.style.display === 'none';
  box.style.display = hidden ? '' : 'none';
  if (!hidden) return;
  const input = document.getElementById('model-input');
  const result = document.getElementById('model-result');
  input.value = document.getElementById('current-model').textContent.trim();
  input.focus();
  if (modelCatalog.length) { renderModelList(''); return; }
  result.textContent = 'Loading the OpenRouter catalog…';
  try {
    const res = await fetch('/api/models');
    if (!res.ok) throw 0;
    modelCatalog = (await res.json()).models || [];
    result.textContent = `${modelCatalog.length} tool-capable models, newest first.`;
    renderModelList('');
  } catch (e) {
    result.textContent = 'Catalog unavailable — type a model id manually (provider/model).';
  }
}

async function saveModel() {
  const model = document.getElementById('model-input').value.trim();
  const result = document.getElementById('model-result');
  if (!model) return;
  modelDd().classList.remove('open');
  result.textContent = 'Saving…';
  const res = await fetch('/api/model', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ model })
  });
  const d = await res.json().catch(() => ({}));
  if (!res.ok) { result.textContent = 'Failed: ' + (d.error || 'unknown'); return; }
  document.getElementById('current-model').textContent = model;
  result.textContent = 'Saved.';
  if (confirm('Model saved. Restart the gateway now to apply it?')) {
    result.textContent = 'Restarting gateway…';
    await fetch('/gateway/stop', { method: 'POST' });
    await fetch('/gateway/start', { method: 'POST' });
    result.textContent = 'Saved — gateway restarted.';
  }
}

function initModelCombo() {
  const input = document.getElementById('model-input');
  if (!input) return;
  input.addEventListener('input', () => renderModelList(input.value));
  input.addEventListener('focus', () => { if (modelCatalog.length) renderModelList(input.value); });
  input.addEventListener('keydown', (e) => {
    const dd = modelDd();
    if (e.key === 'ArrowDown') { e.preventDefault(); moveModelActive(1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); moveModelActive(-1); }
    else if (e.key === 'Enter') {
      e.preventDefault();
      const active = dd.querySelector('.combo-item.active');
      if (dd.classList.contains('open') && active) pickModel(active.dataset.modelId);
    } else if (e.key === 'Escape') {
      dd.classList.remove('open');
    }
  });
  document.addEventListener('click', (e) => {
    if (!e.target.closest('.combo') && !e.target.closest('[data-action="model-change"]')) {
      modelDd().classList.remove('open');
    }
  });
}

let consoleKeyRevealed = false;
async function loadConsoleConfig() {
  try {
    const res = await fetch('/api/console/config');
    if (!res.ok) return;
    const data = await res.json();
    document.getElementById('console-status').textContent = data.enabled ? 'Enabled' : 'Disabled';
    document.getElementById('console-enable-btn').style.display = data.enabled ? 'none' : '';
    document.getElementById('console-disable-btn').style.display = data.enabled ? '' : 'none';
    const el = document.getElementById('console-key');
    el.dataset.key = data.key;
    el.textContent = consoleKeyRevealed ? data.key : '••••••••••••';
    document.getElementById('console-url').textContent = location.origin;
  } catch (e) {}
}
function toggleConsoleKey() {
  consoleKeyRevealed = !consoleKeyRevealed;
  const el = document.getElementById('console-key');
  el.textContent = consoleKeyRevealed ? el.dataset.key : '••••••••••••';
  document.getElementById('console-key-toggle').textContent = consoleKeyRevealed ? 'Hide' : 'Reveal';
}
async function copyConsoleKey() {
  await navigator.clipboard.writeText(document.getElementById('console-key').dataset.key);
  document.getElementById('console-result').textContent = 'Key copied';
}
async function copyConsoleUrl() {
  await navigator.clipboard.writeText(location.origin);
  document.getElementById('console-result').textContent = 'URL copied';
}
async function setConsole(action) {
  const res = await fetch('/api/console/config', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action }) });
  if (res.ok) loadConsoleConfig();
}
async function rotateConsoleKey() {
  if (!confirm('Rotate the console key? The old key stops working immediately — paste the new one into booa.app.')) return;
  const res = await fetch('/api/console/config', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'rotate' }) });
  if (res.ok) { document.getElementById('console-result').textContent = 'Key rotated'; loadConsoleConfig(); }
}

// CSP forbids inline handlers, so every button carries a data-action and one
// delegated listener dispatches it. Delegation also covers the pairing rows
// rendered dynamically above.
const ACTIONS = {
  'gateway-stop': el => gatewayAction('stop'),
  'gateway-start': el => gatewayAction('start'),
  'wallet-copy': el => {
    navigator.clipboard.writeText(el.dataset.addr || '');
    el.textContent = 'copied!';
    setTimeout(() => { el.textContent = 'copy'; }, 1500);
  },
  'wallet-refresh': el => refreshWalletStatus(),
  'wallet-verify': el => startWalletVerify(),
  'wallet-linkcode': el => generateLinkCode(),
  'wallet-submit-sig': el => submitWalletSignature(),
  'onchain-save': el => saveOnchainSettings(),
  'model-change': el => openModelBox(),
  'model-save': el => saveModel(),
  'model-cancel': el => { document.getElementById('model-box').style.display = 'none'; },
  'model-pick': el => pickModel(el.dataset.modelId),
  'console-copy-url': el => copyConsoleUrl(),
  'console-toggle-key': el => toggleConsoleKey(),
  'console-copy-key': el => copyConsoleKey(),
  'console-enable': el => setConsole('enable'),
  'console-disable': el => setConsole('disable'),
  'console-rotate': el => rotateConsoleKey(),
  'export': el => confirmExport(),
  'import': el => confirmImport(),
  'pair-approve': el => pairingAction('approve', el.dataset.platform, el.dataset.code),
  'pair-deny': el => pairingAction('deny', el.dataset.platform, el.dataset.code),
  'pair-unpair': el => unpairUser(el.dataset.platform, el.dataset.user),
};

document.addEventListener('click', (e) => {
  const el = e.target.closest('[data-action]');
  if (!el) return;
  const fn = ACTIONS[el.dataset.action];
  if (!fn) return;
  if (el.tagName === 'A') e.preventDefault();
  fn(el);
});

initModelCombo();
setInterval(() => { checkPairing(); checkErrors(); renderWalletStatus(); }, 3000);
checkPairing();
checkErrors();
renderWalletStatus();
loadOnchainSettings();
loadConsoleConfig();
