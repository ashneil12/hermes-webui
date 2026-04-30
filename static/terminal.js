// ── Terminal panel (xterm.js + /api/shell PTY) ──────────────────────────────
// Loads xterm.js + xterm-addon-fit on demand from a CDN. Opens as a centered
// modal. Connects to a backend PTY shell via /api/shell/* endpoints.
//
// Commands: openTerminal(), closeTerminal(), focusTerminal()

const TERM = {
  open: false,
  loaded: false,
  loading: null,
  modules: null,        // { Terminal, FitAddon }
  inst: null,           // Terminal instance
  fit: null,            // FitAddon instance
  shellId: null,
  seq: 0,
  es: null,             // EventSource
  el: null,             // root element
  resizeObserver: null,
};

const _XTERM_VERSION = '5.5.0';
const _XTERM_FIT_VERSION = '0.10.0';

async function _loadXtermAssets(){
  if (TERM.loaded) return TERM.modules;
  if (TERM.loading) return TERM.loading;
  TERM.loading = (async () => {
    // Load CSS once
    if (!document.getElementById('xtermCss')) {
      const link = document.createElement('link');
      link.id = 'xtermCss';
      link.rel = 'stylesheet';
      link.href = `https://cdn.jsdelivr.net/npm/@xterm/xterm@${_XTERM_VERSION}/css/xterm.min.css`;
      link.crossOrigin = 'anonymous';
      document.head.appendChild(link);
    }
    const xtermMod = await import(`https://cdn.jsdelivr.net/npm/@xterm/xterm@${_XTERM_VERSION}/+esm`);
    const fitMod   = await import(`https://cdn.jsdelivr.net/npm/@xterm/addon-fit@${_XTERM_FIT_VERSION}/+esm`);
    TERM.modules = {
      Terminal: xtermMod.Terminal || (xtermMod.default && xtermMod.default.Terminal),
      FitAddon: fitMod.FitAddon  || (fitMod.default  && fitMod.default.FitAddon),
    };
    TERM.loaded = true;
    return TERM.modules;
  })();
  try { return await TERM.loading; }
  finally { TERM.loading = null; }
}

function _termEnsureRoot(){
  if (TERM.el) return TERM.el;
  const root = document.createElement('div');
  root.id = 'termRoot';
  root.className = 'term-root';
  root.style.display = 'none';
  root.innerHTML = `
    <div class="term-backdrop" data-term-close></div>
    <div class="term-modal" role="dialog" aria-modal="true" aria-label="Terminal">
      <div class="term-head">
        <span class="term-title">Terminal</span>
        <span class="term-meta" id="termMeta"></span>
        <span class="term-spacer"></span>
        <button class="term-btn" id="termClear" type="button" title="Clear">Clear</button>
        <button class="term-btn" id="termRestart" type="button" title="Restart shell">Restart</button>
        <button class="term-btn term-btn-x" data-term-close type="button" aria-label="Close">×</button>
      </div>
      <div class="term-host" id="termHost"></div>
      <div class="term-foot" id="termFoot"></div>
    </div>`;
  document.body.appendChild(root);
  TERM.el = root;
  root.addEventListener('click', (e) => {
    if (e.target.closest('[data-term-close]')) closeTerminal();
  });
  root.querySelector('#termClear').addEventListener('click', () => { if (TERM.inst) TERM.inst.clear(); });
  root.querySelector('#termRestart').addEventListener('click', restartTerminal);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && TERM.open) {
      // Don't close if user is typing in xterm — only close when focus is on backdrop/buttons
      const activeIsXterm = document.activeElement && document.activeElement.closest && document.activeElement.closest('#termHost');
      if (!activeIsXterm) closeTerminal();
    }
  });
  return root;
}

async function _termSpawnShell(cols, rows){
  const r = await fetch('api/shell/new', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ cols, rows }),
  });
  if (!r.ok) {
    const txt = await r.text().catch(() => '');
    throw new Error(`shell/new ${r.status}: ${txt || r.statusText}`);
  }
  return r.json();
}

function _termOpenStream(){
  if (TERM.es) { try { TERM.es.close(); } catch(e){} TERM.es = null; }
  if (!TERM.shellId) return;
  const url = `api/shell/stream?id=${encodeURIComponent(TERM.shellId)}&since=${TERM.seq}`;
  const es = new EventSource(url);
  TERM.es = es;
  es.addEventListener('hello', () => { /* connected */ });
  es.addEventListener('data', (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      if (msg && msg.b64 != null) {
        const bin = atob(msg.b64);
        const arr = new Uint8Array(bin.length);
        for (let i=0;i<bin.length;i++) arr[i] = bin.charCodeAt(i);
        if (TERM.inst) TERM.inst.write(arr);
        if (typeof msg.seq === 'number') TERM.seq = msg.seq;
      }
    } catch(e){ console.warn('term: bad data event', e); }
  });
  es.addEventListener('closed', () => {
    if (TERM.inst) TERM.inst.write('\r\n\x1b[33m[shell closed]\x1b[0m\r\n');
    try { es.close(); } catch(e){}
    TERM.es = null; TERM.shellId = null;
  });
  es.onerror = () => {
    // Browser will auto-reconnect after a short delay
  };
}

async function _termSendInput(data){
  if (!TERM.shellId) return;
  try {
    await fetch('api/shell/input', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: TERM.shellId, data }),
    });
  } catch(e){ /* swallow — reconnect will recover */ }
}

async function _termSendResize(rows, cols){
  if (!TERM.shellId) return;
  try {
    await fetch('api/shell/resize', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: TERM.shellId, rows, cols }),
    });
  } catch(e){}
}

async function _termCloseShell(){
  const id = TERM.shellId;
  TERM.shellId = null;
  if (TERM.es) { try { TERM.es.close(); } catch(e){} TERM.es = null; }
  if (id) {
    try {
      await fetch('api/shell/close', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id }),
      });
    } catch(e){}
  }
}

async function openTerminal(){
  // Status check: ensure shell is enabled before bothering the user with downloads.
  let st;
  try {
    const r = await fetch('api/shell/status');
    st = await r.json();
  } catch(e){ st = { enabled: false }; }
  if (!st || !st.enabled) {
    if (typeof toast === 'function') toast('Terminal disabled. Set HERMES_WEBUI_ENABLE_SHELL=1 and restart.', 5000);
    return;
  }

  _termEnsureRoot();
  TERM.el.style.display = '';
  TERM.open = true;

  const meta = TERM.el.querySelector('#termMeta');
  if (meta) meta.textContent = 'connecting…';

  let mods;
  try {
    mods = await _loadXtermAssets();
  } catch(e) {
    if (typeof toast === 'function') toast('Failed to load xterm.js: ' + (e.message || e), 4000);
    closeTerminal();
    return;
  }

  if (!TERM.inst) {
    const host = TERM.el.querySelector('#termHost');
    const term = new mods.Terminal({
      cursorBlink: true,
      fontFamily: 'ui-monospace, Menlo, Consolas, "Liberation Mono", monospace',
      fontSize: 13,
      lineHeight: 1.2,
      theme: { background: '#0a0e14', foreground: '#dcdfe4' },
      scrollback: 5000,
      allowProposedApi: true,
    });
    const fit = new mods.FitAddon();
    term.loadAddon(fit);
    term.open(host);
    fit.fit();
    TERM.inst = term;
    TERM.fit = fit;

    term.onData((data) => { _termSendInput(data); });
    term.onResize(({ rows, cols }) => { _termSendResize(rows, cols); });

    // Resize the terminal when the window resizes.
    const ro = new ResizeObserver(() => {
      try { fit.fit(); } catch(e){}
    });
    ro.observe(host);
    TERM.resizeObserver = ro;
  } else {
    // Re-fit in case viewport changed while hidden
    try { TERM.fit.fit(); } catch(e){}
  }

  // Spawn shell if needed
  if (!TERM.shellId) {
    try {
      const dims = TERM.fit.proposeDimensions() || { cols: 120, rows: 30 };
      const info = await _termSpawnShell(dims.cols, dims.rows);
      TERM.shellId = info.shell_id;
      TERM.seq = 0;
      if (meta) meta.textContent = info.cwd || '';
      _termOpenStream();
      TERM.inst.focus();
    } catch(e) {
      TERM.inst.write(`\x1b[31m${(e && e.message) || e}\x1b[0m\r\n`);
      if (meta) meta.textContent = 'error';
    }
  } else {
    if (meta) meta.textContent = 'reattached';
    TERM.inst.focus();
  }
}

function closeTerminal(){
  if (!TERM.el) return;
  TERM.el.style.display = 'none';
  TERM.open = false;
  // Keep the shell alive — user may reopen. It'll be reaped after idle timeout
  // server-side. To kill it explicitly, call restartTerminal().
}

async function restartTerminal(){
  await _termCloseShell();
  if (TERM.inst) TERM.inst.reset();
  // Re-open
  await openTerminal();
}

function focusTerminal(){
  if (TERM.inst && TERM.open) TERM.inst.focus();
}

window.openTerminal = openTerminal;
window.closeTerminal = closeTerminal;
window.restartTerminal = restartTerminal;
window.focusTerminal = focusTerminal;
