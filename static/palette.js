// ── Cmd+K command palette ───────────────────────────────────────────────────
// Universal launcher for navigation, sessions, models, themes, skills, etc.
// Opens with ⌘/Ctrl+K. Esc to close. Arrow keys + Enter to choose.

const PAL = {
  open: false,
  items: [],
  filtered: [],
  idx: 0,
  query: '',
  el: null,
};

function _palBuildItems(){
  const items = [];
  const has = (fn) => typeof window[fn] === 'function';

  // Panels
  const panels = [
    ['chat', 'Chat', 'message-square'],
    ['tasks', 'Tasks', 'calendar'],
    ['skills', 'Skills', 'layers'],
    ['memory', 'Memory', 'brain'],
    ['workspaces', 'Spaces', 'folder'],
    ['profiles', 'Profiles', 'user'],
    ['todos', 'Todos', 'list-todo'],
    ['settings', 'Settings', 'settings'],
  ];
  panels.forEach(([id, label, icon]) => items.push({
    id: 'panel:'+id, label: 'Go to ' + label, icon, kind: 'Panel',
    run: () => { if (has('switchPanel')) window.switchPanel(id); },
  }));

  // Quick actions
  items.push({ id: 'a:new', label: 'New conversation', icon: 'plus', kind: 'Action',
    run: () => { if (has('createNewSession')) window.createNewSession(); else document.getElementById('btnNewChat')?.click(); }});
  items.push({ id: 'a:term', label: 'Open Terminal', icon: 'terminal', kind: 'Action',
    run: () => { if (has('toggleComposerTerminal')) window.toggleComposerTerminal(true); }});
  items.push({ id: 'a:shortcuts', label: 'Show keyboard shortcuts', icon: 'eye', kind: 'Action',
    run: () => { if (has('showShortcutsOverlay')) window.showShortcutsOverlay(); }});
  items.push({ id: 'a:tts-toggle', label: (window.TTS && window.TTS.autoSpeak) ? 'Disable auto-speak' : 'Enable auto-speak', icon: 'play', kind: 'TTS',
    run: () => {
      if (!window.TTS) return;
      window.TTS.autoSpeak = !window.TTS.autoSpeak;
      try { localStorage.setItem('hermes-tts-auto', window.TTS.autoSpeak ? '1' : '0'); } catch(e){}
      if (typeof showToast === 'function') showToast('Auto-speak ' + (window.TTS.autoSpeak ? 'on' : 'off'), 1800);
    }});
  items.push({ id: 'a:tts-stop', label: 'Stop speaking', icon: 'square', kind: 'TTS',
    run: () => { if (has('ttsStop')) window.ttsStop(); }});

  // Themes
  const themes = [
    ['light','default'], ['dark','default'],
    ['dark','ares'], ['dark','mono'], ['dark','slate'],
    ['dark','poseidon'], ['dark','sisyphus'], ['dark','charizard'],
  ];
  themes.forEach(([t, skin]) => items.push({
    id: 't:'+t+':'+skin, label: `Theme: ${t}${skin!=='default'?' · '+skin:''}`, icon: 'star', kind: 'Theme',
    run: () => {
      try {
        localStorage.setItem('hermes-theme', t);
        localStorage.setItem('hermes-skin', skin);
      } catch(e){}
      document.documentElement.classList.toggle('dark', t === 'dark');
      document.documentElement.dataset.skin = skin;
      if (has('applyTheme')) window.applyTheme();
      if (typeof showToast === 'function') showToast(`Theme: ${t}${skin!=='default'?' · '+skin:''}`, 1500);
    }
  }));

  // Sessions (most recent ~30)
  try {
    const ls = (window.S && Array.isArray(window.S.sessions)) ? window.S.sessions : null;
    if (ls) {
      ls.slice(0, 30).forEach(s => {
        const title = s.title || s.session_id || 'Untitled';
        items.push({
          id: 's:'+s.session_id, label: title, icon: 'message-square', kind: 'Session',
          run: () => { if (has('switchSession')) window.switchSession(s.session_id); }
        });
      });
    }
  } catch(e){}

  // Models (from the settings dropdown if loaded)
  try {
    const opts = document.querySelectorAll('#settingsModel option');
    Array.from(opts).slice(0, 40).forEach(o => {
      if (!o.value) return;
      items.push({
        id: 'm:'+o.value, label: 'Model: ' + (o.textContent || o.value), icon: 'brain', kind: 'Model',
        run: () => {
          if (has('executeCommand')) window.executeCommand('/model ' + o.value);
          else if (has('cmdModel')) window.cmdModel(o.value);
        }
      });
    });
  } catch(e){}

  return items;
}

function _palScore(label, q){
  if (!q) return 1;
  const l = label.toLowerCase();
  if (l === q) return 1000;
  if (l.startsWith(q)) return 500 - l.length;
  const idx = l.indexOf(q);
  if (idx >= 0) return 200 - idx - l.length;
  // Subsequence match (fuzzy)
  let qi = 0;
  for (let i = 0; i < l.length && qi < q.length; i++) {
    if (l[i] === q[qi]) qi++;
  }
  if (qi === q.length) return 50 - l.length;
  return 0;
}

function _palFilter(){
  const q = PAL.query.trim().toLowerCase();
  const scored = PAL.items
    .map(it => ({ it, s: _palScore(it.label, q) }))
    .filter(x => x.s > 0)
    .sort((a, b) => b.s - a.s)
    .slice(0, 60);
  PAL.filtered = scored.map(x => x.it);
  PAL.idx = 0;
  _palRender();
}

function _palRender(){
  if (!PAL.el) return;
  const list = PAL.el.querySelector('.cmdk-list');
  list.innerHTML = PAL.filtered.map((it, i) => {
    const icon = (typeof li === 'function' ? li(it.icon || 'arrow-right', 14) : '');
    return `<div class="cmdk-item${i===PAL.idx?' active':''}" data-i="${i}">
      <span class="cmdk-icon">${icon}</span>
      <span class="cmdk-label">${_palEsc(it.label)}</span>
      <span class="cmdk-kind">${_palEsc(it.kind||'')}</span>
    </div>`;
  }).join('') || '<div class="cmdk-empty">No matches</div>';
  // Scroll active into view
  const active = list.querySelector('.cmdk-item.active');
  if (active && active.scrollIntoView) active.scrollIntoView({ block: 'nearest' });
}

function _palEsc(s){ return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

function _palEnsure(){
  if (PAL.el) return;
  const root = document.createElement('div');
  root.className = 'cmdk-root';
  root.id = 'cmdkRoot';
  root.style.display = 'none';
  root.innerHTML = `
    <div class="cmdk-backdrop" data-cmdk-close></div>
    <div class="cmdk-modal" role="dialog" aria-modal="true" aria-label="Command palette">
      <input class="cmdk-input" type="text" placeholder="Type a command, session, model, theme…" autocomplete="off" spellcheck="false">
      <div class="cmdk-list"></div>
      <div class="cmdk-foot">
        <span><kbd>↑↓</kbd> nav</span>
        <span><kbd>↵</kbd> run</span>
        <span><kbd>esc</kbd> close</span>
      </div>
    </div>`;
  document.body.appendChild(root);
  PAL.el = root;

  const input = root.querySelector('.cmdk-input');
  const list = root.querySelector('.cmdk-list');
  root.addEventListener('click', (e) => {
    if (e.target.closest('[data-cmdk-close]')) closePalette();
    const hit = e.target.closest('.cmdk-item');
    if (hit) {
      const i = parseInt(hit.dataset.i, 10);
      _palRun(i);
    }
  });
  input.addEventListener('input', () => { PAL.query = input.value; _palFilter(); });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { e.preventDefault(); closePalette(); }
    else if (e.key === 'ArrowDown') { e.preventDefault(); PAL.idx = Math.min(PAL.filtered.length-1, PAL.idx+1); _palRender(); }
    else if (e.key === 'ArrowUp')   { e.preventDefault(); PAL.idx = Math.max(0, PAL.idx-1); _palRender(); }
    else if (e.key === 'Enter')     { e.preventDefault(); _palRun(PAL.idx); }
  });
}

function _palRun(i){
  const it = PAL.filtered[i];
  if (!it) return;
  closePalette();
  try { it.run(); } catch (err) { console.error('palette run failed', err); }
}

function openPalette(){
  _palEnsure();
  PAL.items = _palBuildItems();
  PAL.query = '';
  PAL.filtered = PAL.items.slice(0, 60);
  PAL.idx = 0;
  PAL.el.style.display = '';
  PAL.open = true;
  const input = PAL.el.querySelector('.cmdk-input');
  input.value = '';
  _palRender();
  setTimeout(() => input.focus(), 10);
}

function closePalette(){
  if (!PAL.el) return;
  PAL.el.style.display = 'none';
  PAL.open = false;
}

function togglePalette(){ PAL.open ? closePalette() : openPalette(); }

// Global hotkey: ⌘K / Ctrl+K. Ignore when already in another modal/textarea unless it's the palette input.
window.addEventListener('keydown', (e) => {
  const k = e.key.toLowerCase();
  if ((e.metaKey || e.ctrlKey) && k === 'k') {
    e.preventDefault();
    togglePalette();
  } else if (k === '?' && !e.metaKey && !e.ctrlKey && !e.altKey) {
    // Only if user isn't typing
    const t = e.target;
    const tag = t && t.tagName;
    const editable = t && (t.isContentEditable || tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT');
    if (!editable) {
      e.preventDefault();
      if (typeof window.showShortcutsOverlay === 'function') window.showShortcutsOverlay();
    }
  }
});

window.openPalette = openPalette;
window.closePalette = closePalette;
window.togglePalette = togglePalette;

// ── Keyboard shortcut overlay (?) ──────────────────────────────────────────
function showShortcutsOverlay(){
  let el = document.getElementById('shortcutsOverlay');
  if (!el) {
    el = document.createElement('div');
    el.id = 'shortcutsOverlay';
    el.className = 'shortcuts-overlay';
    el.innerHTML = `
      <div class="shortcuts-backdrop" data-close></div>
      <div class="shortcuts-modal" role="dialog" aria-modal="true" aria-label="Keyboard shortcuts">
        <div class="shortcuts-head">
          <span>Keyboard shortcuts</span>
          <button class="shortcuts-close" data-close type="button">×</button>
        </div>
        <div class="shortcuts-body">
          <div class="sc-group"><div class="sc-title">Global</div>
            <div class="sc-row"><kbd>⌘</kbd>+<kbd>K</kbd><span>Open command palette</span></div>
            <div class="sc-row"><kbd>?</kbd><span>Show this overlay</span></div>
            <div class="sc-row"><kbd>Esc</kbd><span>Close modal / palette</span></div>
          </div>
          <div class="sc-group"><div class="sc-title">Composer</div>
            <div class="sc-row"><kbd>↵</kbd><span>Send message</span></div>
            <div class="sc-row"><kbd>⇧</kbd>+<kbd>↵</kbd><span>Newline</span></div>
            <div class="sc-row"><kbd>/</kbd><span>Open slash-command menu</span></div>
            <div class="sc-row"><kbd>⌘</kbd>+<kbd>↑</kbd><span>Edit last user message</span></div>
          </div>
          <div class="sc-group"><div class="sc-title">Audio</div>
            <div class="sc-row"><kbd>🔊</kbd><span>Per-message: speak / stop</span></div>
            <div class="sc-row"><span style="opacity:.7">Settings → Preferences → Voice</span><span>Engine, voice, rate, auto-speak</span></div>
          </div>
          <div class="sc-group"><div class="sc-title">Terminal</div>
            <div class="sc-row"><span style="opacity:.7">Cmd palette → Open Terminal</span><span>Workspace shell (when enabled)</span></div>
          </div>
        </div>
      </div>`;
    el.addEventListener('click', (e) => { if (e.target.closest('[data-close]')) el.style.display = 'none'; });
    document.body.appendChild(el);
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && el.style.display !== 'none') el.style.display = 'none';
    });
  }
  el.style.display = '';
}

window.showShortcutsOverlay = showShortcutsOverlay;
