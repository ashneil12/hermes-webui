// ── TTS (text-to-speech) ────────────────────────────────────────────────────
// Two engines, picked from settings:
//   • 'browser' — Web Speech API (native, instant, no download, robotic on some platforms)
//   • 'kokoro'  — Kokoro 82M via kokoro-js (free MIT model, runs in WebGPU/WASM,
//                 ~80MB one-time download, cached in IndexedDB by transformers.js)
// Per-message 🔊 button toggles play/stop. Auto-speak toggle re-speaks each
// final assistant turn. Failures gracefully degrade (kokoro → browser → toast).
//
// All state is local to this module + a few localStorage keys. No server hop.

const TTS_VOICES = [
  // High-quality (TARGET_QUALITY=A/B), English. Kokoro voice IDs below.
  { id: 'af_heart',   label: 'Heart (F · US)',   q: 'A' },
  { id: 'af_bella',   label: 'Bella (F · US)',   q: 'A' },
  { id: 'af_nicole',  label: 'Nicole (F · US)',  q: 'B' },
  { id: 'af_sarah',   label: 'Sarah (F · US)',   q: 'B' },
  { id: 'am_adam',    label: 'Adam (M · US)',    q: 'B' },
  { id: 'am_michael', label: 'Michael (M · US)', q: 'B' },
  { id: 'bf_emma',    label: 'Emma (F · UK)',    q: 'B' },
  { id: 'bm_george',  label: 'George (M · UK)',  q: 'B' },
];

const TTS = {
  engine:    localStorage.getItem('hermes-tts-engine') || 'browser',     // 'browser' | 'kokoro'
  voice:     localStorage.getItem('hermes-tts-voice')  || 'af_heart',
  rate:      parseFloat(localStorage.getItem('hermes-tts-rate') || '1.0'),
  autoSpeak: localStorage.getItem('hermes-tts-auto') === '1',
  // runtime
  kokoro:    null,
  kokoroLoading: null,
  current:   null,   // { audio, btn, utter? }
};

function _ttsSave(){
  localStorage.setItem('hermes-tts-engine', TTS.engine);
  localStorage.setItem('hermes-tts-voice',  TTS.voice);
  localStorage.setItem('hermes-tts-rate',   String(TTS.rate));
  localStorage.setItem('hermes-tts-auto',   TTS.autoSpeak ? '1' : '0');
}

function _ttsStripMarkdown(text){
  return String(text || '')
    .replace(/```[\s\S]*?```/g, ' ')           // fenced code
    .replace(/`[^`]*`/g, ' ')                  // inline code
    .replace(/!\[[^\]]*\]\([^)]*\)/g, ' ')     // images
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')   // links → label
    .replace(/<\/?[a-z][^>]*>/gi, ' ')         // raw html
    .replace(/^\s*#{1,6}\s+/gm, '')            // heading markers
    .replace(/(\*\*|__)(.*?)\1/g, '$2')        // bold
    .replace(/(\*|_)(.*?)\1/g, '$2')           // italic
    .replace(/^\s*[-*+]\s+/gm, '')             // list bullets
    .replace(/^\s*>\s?/gm, '')                 // blockquote
    .replace(/https?:\/\/\S+/g, ' ')           // raw URLs
    .replace(/[#*_~|]+/g, ' ')                 // stray markers
    .replace(/\s+/g, ' ')
    .trim();
}

// ── Kokoro engine ─────────────────────────────────────────────────────────
async function _ensureKokoro(onProgress){
  if (TTS.kokoro) return TTS.kokoro;
  if (TTS.kokoroLoading) return TTS.kokoroLoading;
  TTS.kokoroLoading = (async () => {
    try {
      // ESM CDN. kokoro-js bundles transformers.js for ONNX inference in WASM.
      const mod = await import('https://cdn.jsdelivr.net/npm/kokoro-js@1.2.0/+esm');
      const KokoroTTS = mod.KokoroTTS || (mod.default && mod.default.KokoroTTS);
      if (!KokoroTTS) throw new Error('Kokoro module missing KokoroTTS export');
      const tts = await KokoroTTS.from_pretrained(
        'onnx-community/Kokoro-82M-v1.0-ONNX',
        { dtype: 'q8', device: 'wasm', progress_callback: onProgress }
      );
      TTS.kokoro = tts;
      return tts;
    } finally {
      TTS.kokoroLoading = null;
    }
  })();
  return TTS.kokoroLoading;
}

async function _kokoroSpeak(text, btn){
  let toastEl = null;
  const onProgress = (p) => {
    if (!p) return;
    if (p.status === 'progress' && typeof p.progress === 'number'){
      const pct = Math.round(p.progress);
      if (!toastEl && typeof showToast === 'function'){ toastEl = showToast(`Loading Kokoro voice… ${pct}%`, 30000); }
    }
  };
  const tts = await _ensureKokoro(onProgress);
  if (toastEl && toastEl.remove) toastEl.remove();
  const audio = await tts.generate(text, { voice: TTS.voice, speed: TTS.rate });
  // kokoro-js returns RawAudio with .toBlob('audio/wav')
  const blob = (audio.toBlob && audio.toBlob('audio/wav')) || (audio.audio && new Blob([audio.audio], { type: 'audio/wav' }));
  if (!blob) throw new Error('Kokoro returned no audio');
  const url = URL.createObjectURL(blob);
  const el = new Audio(url);
  el.addEventListener('ended', () => { URL.revokeObjectURL(url); ttsStop(); });
  el.addEventListener('error', () => { URL.revokeObjectURL(url); ttsStop(); });
  TTS.current = { audio: el, btn };
  if (btn) btn.classList.add('speaking');
  await el.play().catch(err => { console.warn('TTS play() rejected', err); ttsStop(); });
}

// ── Browser (Web Speech) engine ───────────────────────────────────────────
function _browserSpeak(text, btn){
  if (!('speechSynthesis' in window)) {
    if (typeof showToast === 'function') showToast('TTS not supported in this browser', 3500);
    return;
  }
  const u = new SpeechSynthesisUtterance(text);
  u.rate = TTS.rate;
  u.pitch = 1.0;
  // Pick a sensible voice — prefer en-US Google/Apple where available.
  try {
    const voices = window.speechSynthesis.getVoices();
    const preferred = voices.find(v => /en-US/i.test(v.lang) && /(Google|Samantha|Alex|Aria)/i.test(v.name))
                   || voices.find(v => /en-US/i.test(v.lang))
                   || voices.find(v => /^en/i.test(v.lang));
    if (preferred) u.voice = preferred;
  } catch(e){}
  u.addEventListener('end', ttsStop);
  u.addEventListener('error', ttsStop);
  TTS.current = { utter: u, btn };
  if (btn) btn.classList.add('speaking');
  try { window.speechSynthesis.cancel(); } catch(e){}
  window.speechSynthesis.speak(u);
}

// ── Public API ─────────────────────────────────────────────────────────────
async function speakMessage(btn){
  if (TTS.current && TTS.current.btn === btn) { ttsStop(); return; }
  ttsStop();
  let text = '';
  try {
    const row = btn.closest('.assistant-segment, .msg, [data-msg-row]') || btn.parentElement?.parentElement;
    const body = row ? row.querySelector('.msg-body') : null;
    text = body ? (body.innerText || body.textContent || '') : '';
  } catch(e){}
  text = _ttsStripMarkdown(text);
  if (!text) { if (typeof showToast === 'function') showToast('Nothing to speak', 1500); return; }
  // Truncate to keep latency reasonable.
  if (text.length > 6000) text = text.slice(0, 6000) + '…';
  try {
    if (TTS.engine === 'kokoro') {
      try { await _kokoroSpeak(text, btn); return; }
      catch (err) {
        console.warn('Kokoro failed, falling back to browser TTS', err);
        if (typeof showToast === 'function') showToast('Kokoro failed — using browser voice', 2500);
        // fall through to browser
      }
    }
    _browserSpeak(text, btn);
  } catch(err) {
    console.error('TTS error', err);
    ttsStop();
    if (typeof showToast === 'function') showToast('TTS error: ' + (err.message || err), 3500);
  }
}

function ttsStop(){
  try { window.speechSynthesis && window.speechSynthesis.cancel(); } catch(e){}
  if (TTS.current) {
    if (TTS.current.audio) { try { TTS.current.audio.pause(); } catch(e){} }
    if (TTS.current.btn)   { TTS.current.btn.classList.remove('speaking'); }
  }
  TTS.current = null;
}

// Auto-speak hook: messages.js can call this when a final assistant turn
// completes. Looks for a 🔊 button on the last assistant segment.
function ttsMaybeAutoSpeak(){
  if (!TTS.autoSpeak) return;
  // Defer one tick so the DOM is settled.
  setTimeout(() => {
    const segs = document.querySelectorAll('.assistant-segment, .msg-row.assistant');
    const last = segs[segs.length - 1];
    if (!last) return;
    const btn = last.querySelector('.msg-tts-btn');
    if (btn) speakMessage(btn);
  }, 60);
}

// ── Settings UI helpers ────────────────────────────────────────────────────
function ttsApplySettingsUI(){
  const eng = document.getElementById('settingsTtsEngine');     if (eng) eng.value = TTS.engine;
  const vc  = document.getElementById('settingsTtsVoice');      if (vc)  vc.value  = TTS.voice;
  const rt  = document.getElementById('settingsTtsRate');       if (rt)  rt.value  = String(TTS.rate);
  const rl  = document.getElementById('settingsTtsRateLabel');  if (rl)  rl.textContent = TTS.rate.toFixed(2) + '×';
  const au  = document.getElementById('settingsTtsAuto');       if (au)  au.checked = TTS.autoSpeak;
  // Voice picker is only meaningful for Kokoro.
  const wrap = document.getElementById('settingsTtsVoiceWrap');
  if (wrap) wrap.style.display = TTS.engine === 'kokoro' ? '' : 'none';
}

function ttsPopulateVoicePicker(){
  const sel = document.getElementById('settingsTtsVoice');
  if (!sel || sel.dataset.populated === '1') return;
  sel.innerHTML = TTS_VOICES.map(v => `<option value="${v.id}">${v.label}</option>`).join('');
  sel.dataset.populated = '1';
}

function ttsBindSettingsHandlers(){
  const eng = document.getElementById('settingsTtsEngine');
  if (eng && !eng.dataset.bound) {
    eng.dataset.bound = '1';
    eng.addEventListener('change', () => { TTS.engine = eng.value; _ttsSave(); ttsApplySettingsUI(); });
  }
  const vc = document.getElementById('settingsTtsVoice');
  if (vc && !vc.dataset.bound) {
    vc.dataset.bound = '1';
    vc.addEventListener('change', () => { TTS.voice = vc.value; _ttsSave(); });
  }
  const rt = document.getElementById('settingsTtsRate');
  if (rt && !rt.dataset.bound) {
    rt.dataset.bound = '1';
    rt.addEventListener('input', () => {
      TTS.rate = Math.max(0.5, Math.min(2.0, parseFloat(rt.value) || 1.0));
      const rl = document.getElementById('settingsTtsRateLabel');
      if (rl) rl.textContent = TTS.rate.toFixed(2) + '×';
      _ttsSave();
    });
  }
  const au = document.getElementById('settingsTtsAuto');
  if (au && !au.dataset.bound) {
    au.dataset.bound = '1';
    au.addEventListener('change', () => { TTS.autoSpeak = au.checked; _ttsSave(); });
  }
  const test = document.getElementById('settingsTtsTest');
  if (test && !test.dataset.bound) {
    test.dataset.bound = '1';
    test.addEventListener('click', () => {
      const fakeRow = document.createElement('div');
      fakeRow.className = 'assistant-segment';
      const body = document.createElement('div');
      body.className = 'msg-body';
      body.textContent = 'Hi, this is a quick voice test from Hermes. The quick brown fox jumps over the lazy dog.';
      fakeRow.appendChild(body);
      const btn = document.createElement('button');
      btn.className = 'msg-tts-btn';
      fakeRow.appendChild(btn);
      document.body.appendChild(fakeRow);
      fakeRow.style.position = 'fixed'; fakeRow.style.left = '-9999px';
      speakMessage(btn);
      setTimeout(() => fakeRow.remove(), 30000);
    });
  }
}

// One-time init when the settings preferences pane is opened.
function ttsInitSettings(){
  ttsPopulateVoicePicker();
  ttsApplySettingsUI();
  ttsBindSettingsHandlers();
}

// Stop speaking when the user navigates away or the chat re-renders.
window.addEventListener('beforeunload', ttsStop);

// Expose
window.speakMessage = speakMessage;
window.ttsStop = ttsStop;
window.ttsMaybeAutoSpeak = ttsMaybeAutoSpeak;
window.ttsInitSettings = ttsInitSettings;
window.TTS = TTS;
