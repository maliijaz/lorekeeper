/* Lorekeeper front-end: library, reader, spoiler-aware codex, Q&A. No build step. */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

const TYPE_LABELS = {
  character: 'Characters', place: 'Places', object: 'Objects', event: 'Events', lore: 'Lore',
  history: 'History', faction: 'Factions', creature: 'Creatures', other: 'Other',
};
const TYPE_SINGULAR = {
  character: 'Character', place: 'Place', object: 'Object', event: 'Event', lore: 'Lore',
  history: 'History', faction: 'Faction', creature: 'Creature', other: 'Other',
};
const tcolor = t => `var(--t-${t || 'other'})`;

const state = {
  book: null, page: 1, pageData: null,
  codexType: '', codexQuery: '', highlights: null, highlightsUpto: -1,
  entityId: null, pollTimer: null, libTimer: null, lastProcessed: -1,
};

// ------------------------------------------------------------------ utilities
async function api(path, opts = {}) {
  const init = { ...opts };
  if (opts.json !== undefined) {
    init.method = init.method || 'POST';
    init.headers = { 'Content-Type': 'application/json' };
    init.body = JSON.stringify(opts.json);
  }
  const r = await fetch(path, init);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch { /* not JSON */ }
    throw new Error(msg);
  }
  return r.json();
}

function toast(msg, ms = 3500) {
  const t = $('#toast');
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.hidden = true), ms);
}

const esc = s => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const escAttr = s => esc(s).replace(/"/g, '&quot;');

function store(key, val) {
  try {
    if (val === undefined) return JSON.parse(localStorage.getItem('sr.' + key));
    localStorage.setItem('sr.' + key, JSON.stringify(val));
  } catch { return null; }
}

/** Tiny Markdown renderer for summaries/answers; turns [p. 12] into page links. */
function md(src) {
  const lines = esc(src).split('\n');
  let html = '', inList = false;
  const inline = s => s
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*(?!\s)(.+?)\*/g, '$1<em>$2</em>')
    .replace(/\[(?:p|pp|page)\.?\s*(\d+)(?:\s*[-–,]\s*\d+)*\]/gi, (m, p) => `<button class="pg" data-page="${p}">${m}</button>`);
  for (const raw of lines) {
    const line = raw.trim();
    if (/^[-*•]\s+/.test(line)) {
      if (!inList) { html += '<ul>'; inList = true; }
      html += `<li>${inline(line.replace(/^[-*•]\s+/, ''))}</li>`;
      continue;
    }
    if (inList) { html += '</ul>'; inList = false; }
    if (!line) continue;
    const h = line.match(/^(#{1,6})\s+(.*)$/) || line.match(/^()\*\*([^*]{1,40})\*\*:?$/) ||
      // Section names the model wrote as plain lines ("Appearance", "Role in the Story").
      (line.length <= 32 && /^[A-Z][A-Za-z' ]+$/.test(line) && line.split(' ').length <= 4 ? [0, '', line] : null);
    if (h) html += `<h4>${inline(h[2])}</h4>`;
    else html += `<p>${inline(line)}</p>`;
  }
  if (inList) html += '</ul>';
  return html;
}

function coverStyle(title) {
  let h = 0;
  for (const c of title || '?') h = (h * 31 + c.charCodeAt(0)) % 360;
  return `background: linear-gradient(155deg, hsl(${h} 38% 34%), hsl(${(h + 40) % 360} 45% 18%));`;
}

// ------------------------------------------------------------------ theme & typography
const TYPO_DEFAULTS = {
  theme: 'light', font: "'Literata', Georgia, serif", size: 19, lh: 1.65, width: 40, para: 0.8,
  ls: 0, justify: false, indent: false, highlight: true, pageNums: true,
};
let typo = { ...TYPO_DEFAULTS, ...(store('typo') || {}) };

function applyTypo() {
  const r = document.documentElement;
  r.dataset.theme = typo.theme;
  r.style.setProperty('--reader-font', typo.font);
  r.style.setProperty('--reader-size', typo.size + 'px');
  r.style.setProperty('--reader-lh', typo.lh);
  r.style.setProperty('--reader-width', typo.width + 'em');
  r.style.setProperty('--para-gap', typo.para + 'em');
  r.style.setProperty('--reader-ls', typo.ls + 'em');
  r.style.setProperty('--indent', typo.indent ? '1.5em' : '0');
  $('#page').classList.toggle('justify', typo.justify);
  $('#page').classList.toggle('no-pagenums', !typo.pageNums);
  $('#pageNumChk').checked = typo.pageNums;
  $('#fontSel').value = typo.font;
  $('#sizeRng').value = typo.size; $('#sizeVal').textContent = typo.size + 'px';
  $('#lhRng').value = typo.lh; $('#lhVal').textContent = (+typo.lh).toFixed(2);
  $('#widthRng').value = typo.width; $('#widthVal').textContent = typo.width + 'em';
  $('#paraRng').value = typo.para; $('#paraVal').textContent = (+typo.para).toFixed(1) + 'em';
  $('#lsRng').value = typo.ls; $('#lsVal').textContent = (+typo.ls).toFixed(2) + 'em';
  $('#justifyChk').checked = typo.justify;
  $('#indentChk').checked = typo.indent;
  $('#hlChk').checked = typo.highlight;
  $$('#themeSeg button').forEach(b => b.classList.toggle('on', b.dataset.v === typo.theme));
  store('typo', typo);
}

function bindTypo() {
  // Text size/spacing changes reflow the book: keep the reader anchored to the same spot.
  const set = (k, v) => {
    const keep = state.book && !$('#reader').hidden && reader.max ? readingPos() : null;
    typo[k] = v;
    applyTypo();
    if (keep) scrollToPage(keep.page, keep.frac);
  };
  $('#fontSel').onchange = e => set('font', e.target.value);
  $('#sizeRng').oninput = e => set('size', +e.target.value);
  $('#lhRng').oninput = e => set('lh', +e.target.value);
  $('#widthRng').oninput = e => set('width', +e.target.value);
  $('#paraRng').oninput = e => set('para', +e.target.value);
  $('#lsRng').oninput = e => set('ls', +e.target.value);
  $('#justifyChk').onchange = e => set('justify', e.target.checked);
  $('#indentChk').onchange = e => set('indent', e.target.checked);
  $('#hlChk').onchange = e => { set('highlight', e.target.checked); refreshHighlights(true); };
  $('#pageNumChk').onchange = e => set('pageNums', e.target.checked);
  $$('#themeSeg button').forEach(b => (b.onclick = () => set('theme', b.dataset.v)));
  $('#typoReset').onclick = () => {
    const keep = state.book && !$('#reader').hidden && reader.max ? readingPos() : null;
    typo = { ...TYPO_DEFAULTS, theme: typo.theme };
    applyTypo();
    refreshHighlights(true);
    if (keep) scrollToPage(keep.page, keep.frac);
  };
  $('#typoBtn').onclick = e => { e.stopPropagation(); $('#typoPanel').hidden = !$('#typoPanel').hidden; };
  document.addEventListener('click', e => {
    if (!$('#typoPanel').hidden && !$('#typoPanel').contains(e.target) && e.target !== $('#typoBtn')) $('#typoPanel').hidden = true;
  });
}

// ------------------------------------------------------------------ library
async function loadLibrary() {
  const books = await api('/api/books');
  const grid = $('#bookGrid');
  $('#emptyLib').hidden = books.length > 0;
  grid.innerHTML = books.map(b => {
    const pct = Math.round((b.progress || 0) * 100);
    let status;
    if (b.status === 'done') status = `Codex ready · ${b.num_pages} pages`;
    else if (b.status === 'processing') status = `${esc(b.stage || 'Processing')} · ${pct}%`;
    else if (b.status === 'queued') status = 'Waiting to be analysed…';
    else if (b.status === 'paused') status = `Analysis paused at ${pct}%`;
    else status = `<span style="color:var(--danger)">Error: ${esc(b.error || '')}</span>`;
    const readPct = b.num_pages ? Math.round(100 * (b.max_page || 1) / b.num_pages) : 0;
    const canResume = ['paused', 'error'].includes(b.status);
    const canPause = ['processing', 'queued'].includes(b.status);
    return `<div class="book-card">
      <div class="cover" style="${coverStyle(b.title)}" data-open="${b.id}">
        <div class="c-title">${esc(b.title)}</div>
        <div class="c-author">${esc(b.author || '')}</div>
      </div>
      <div class="card-body">
        <div class="status">${status}<br><span>Read ${readPct}%</span></div>
        ${b.status !== 'done' ? `<div class="bar"><div style="width:${pct}%"></div></div>` : ''}
        <div class="card-actions">
          <button class="btn tiny primary" data-open="${b.id}">Read</button>
          ${canPause ? `<button class="btn tiny ghost" data-pause="${b.id}">Pause</button>` : ''}
          ${canResume ? `<button class="btn tiny ghost" data-resume="${b.id}">Resume</button>` : ''}
          ${b.status === 'done' ? `<button class="btn tiny ghost" data-reset="${b.id}" title="Re-analyse with current model">Re-analyse</button>` : ''}
          <button class="btn tiny ghost danger" data-del="${b.id}">Delete</button>
        </div>
      </div>
    </div>`;
  }).join('');
  clearTimeout(state.libTimer);
  if (!$('#library').hidden && books.some(b => ['processing', 'queued'].includes(b.status))) {
    state.libTimer = setTimeout(loadLibrary, 3000);
  }
}

function bindLibrary() {
  $('#bookGrid').onclick = async e => {
    const t = e.target.closest('[data-open],[data-pause],[data-resume],[data-del],[data-reset]');
    if (!t) return;
    const d = t.dataset;
    try {
      if (d.open) return openBook(d.open);
      if (d.pause) await api(`/api/books/${d.pause}/pause`, { method: 'POST' });
      if (d.resume) await api(`/api/books/${d.resume}/process`, { method: 'POST' });
      if (d.reset && confirm('Discard the codex and analyse this book again with the current model?'))
        await api(`/api/books/${d.reset}/reset`, { method: 'POST' });
      if (d.del && confirm('Delete this book and its codex?')) await api(`/api/books/${d.del}`, { method: 'DELETE' });
    } catch (err) { toast(err.message); }
    loadLibrary();
  };
  $('#fileInput').onchange = e => { [...e.target.files].forEach(upload); e.target.value = ''; };
  const dz = $('#dropzone');
  ['dragenter', 'dragover'].forEach(ev => document.addEventListener(ev, e => { e.preventDefault(); dz.classList.add('over'); }));
  ['dragleave', 'drop'].forEach(ev => document.addEventListener(ev, e => { e.preventDefault(); if (ev === 'drop' || !e.relatedTarget) dz.classList.remove('over'); }));
  document.addEventListener('drop', e => { if (!$('#library').hidden) [...e.dataTransfer.files].forEach(upload); });
}

async function upload(file) {
  toast(`Importing “${file.name}”…`, 60000);
  const fd = new FormData();
  fd.append('file', file);
  try {
    const b = await api('/api/books', { method: 'POST', body: fd });
    toast(`Added “${b.title}” (${b.num_pages} pages). Analysis started.`);
  } catch (err) { toast(err.message, 6000); }
  loadLibrary();
}

async function loadHealth() {
  try {
    const h = await api('/api/health');
    const ok = h.llm.ok;
    $('#health').innerHTML = `<span class="dot" style="background:${ok ? '#2e9d5b' : '#c0392b'}"></span>` +
      `${h.provider === 'ollama' ? 'Ollama' : 'API'} ${ok ? 'ready' : 'unavailable'}${h.gpu ? ' · ' + esc(h.gpu) : ''}`;
    $('#health').title = h.llm.message || '';
    return h;
  } catch { return null; }
}

// ------------------------------------------------------------------ first-run setup
async function checkSetup() {
  const banner = $('#setupBanner');
  let s;
  try { s = await api('/api/setup'); } catch { return; }
  clearTimeout(checkSetup._t);
  const p = s.pull || {};
  if (s.ready && !p.active) { banner.hidden = true; if (p.status === 'success') loadHealth(); return; }
  banner.hidden = false;
  if (s.provider !== 'ollama') {
    banner.innerHTML = `<div class="b-text"><strong>Add an API key</strong><br><span class="muted small">Open ⚙ Models and paste your Groq (or other) API key to analyse books.</span></div>`;
    return;
  }
  if (!s.ollama_installed) {
    banner.innerHTML = `<div class="b-text"><strong>Ollama isn't installed</strong><br><span class="muted small">Lorekeeper uses Ollama to run AI models on your GPU. Install it from
      <a href="https://ollama.com/download" target="_blank">ollama.com/download</a>, or run the Lorekeeper installer again. You can also use Groq under ⚙ Models.</span></div>
      <button class="btn" id="setupRetry">Check again</button>`;
    $('#setupRetry').onclick = checkSetup;
    return;
  }
  if (p.active) {
    const pct = p.total ? Math.round(100 * p.completed / p.total) : 0;
    const gb = x => (x / 1e9).toFixed(1);
    banner.innerHTML = `<div class="b-text"><strong>Downloading ${esc(p.model)}…</strong>
      <span class="muted small">${esc(p.status)}${p.total ? ` · ${gb(p.completed)} / ${gb(p.total)} GB` : ''}</span></div>
      <div class="bar"><div style="width:${pct}%"></div></div>`;
    checkSetup._t = setTimeout(checkSetup, 1500);
    return;
  }
  if (!s.ollama_running) {
    banner.innerHTML = `<div class="b-text"><strong>Starting the AI engine…</strong><br><span class="muted small">Waiting for Ollama to start.</span></div>`;
    try { await api('/api/setup/start-ollama', { method: 'POST' }); } catch { /* retried below */ }
    checkSetup._t = setTimeout(checkSetup, 2000);
    return;
  }
  banner.innerHTML = `<div class="b-text"><strong>One more step: download the AI model</strong><br>
    <span class="muted small">${esc(s.model)} (about 5 GB) runs locally on your GPU. Books you add are read once the download finishes.
    ${p.error ? `<br><span style="color:var(--danger)">Last attempt failed: ${esc(p.error)}</span>` : ''}</span></div>
    <button class="btn primary" id="setupPull">Download model</button>`;
  $('#setupPull').onclick = async () => { await api('/api/setup/pull', { method: 'POST' }); checkSetup(); };
}

// ------------------------------------------------------------------ reader
const spoilerOn = () => store('spoiler.' + state.book.id) !== false;
const upto = () => (spoilerOn() ? state.book.max_page : state.book.num_pages);

/*
 * Continuous reader: the book is one scroll. Pages are fetched in batches as you approach either end
 * and far-away pages are dropped so the DOM stays small. The page at 30% of the viewport height is
 * the "current" page; reaching it counts as read and advances the spoiler shield.
 */
const reader = {
  min: 0, max: 0,          // loaded page range
  texts: new Map(),        // page_no -> raw text (for re-highlighting without refetching)
  cur: 1, loading: false, token: 0,
  lockShield: false,       // user jumped ahead but chose not to count skipped pages as read
  progTimer: null, uptoTimer: null, hlKey: '',
};
const BATCH = 8, KEEP = 90;

async function openBook(id) {
  const book = await api(`/api/books/${id}`);
  state.book = book;
  state.highlights = null; state.highlightsUpto = -1; state.entityId = null; state.lastProcessed = -1;
  reader.lockShield = false;
  $('#library').hidden = true;
  $('#reader').hidden = false;
  clearTimeout(state.libTimer);
  $('#bookTitle').textContent = book.title;
  $('#pageSlider').max = book.num_pages;
  $('#spoilerToggle').checked = spoilerOn();
  $('#chat').innerHTML = '';
  showList();
  updateShield();
  const pos = store('pos.' + id);
  const start = book.last_page || 1;
  await openAt(start, pos && pos.page === start ? pos.frac : 0);
  $('#pageWrap').focus({ preventScroll: true });
  if (store('codexOpen') !== false) toggleCodex(true);
  pollBook();
}

const fetchPages = (a, b) => api(`/api/books/${state.book.id}/pages?start=${a}&end=${b}`);

/** Short lines at the very start of a chapter ("4", "THE GIFT") are headings even when the
 *  e-book marks them up as ordinary paragraphs. */
const looksLikeHeading = s => s.length <= 60 && (!/[.!?,;:"”’…]$/.test(s) || /^[\dIVXLC.\s]+$/.test(s));

function pageHtml(text, chapterStart = false) {
  const hl = typo.highlight ? state.highlights : null;
  let leading = chapterStart;
  return text.split('\n\n').map((par, i) => {
    if (par.startsWith('# ')) return `<h2>${esc(par.slice(2))}</h2>`;
    if (leading && i < 3 && looksLikeHeading(par)) return `<h2>${esc(par)}</h2>`;
    leading = false;
    let s = esc(par);
    if (hl) s = s.replace(hl.re, m => {
      const h = hl.byText.get(m.replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>'));
      return h ? `<span class="ent" data-eid="${h.id}" style="--ent-color:${tcolor(h.type)}">${m}</span>` : m;
    });
    return `<p>${s}</p>`;
  }).join('');
}

function pageSection(p) {
  const ch = state.book.chapters.find(c => c.start_page === p.page_no);
  const sec = document.createElement('section');
  sec.className = 'pg-block' + (ch ? ' chapter-start' : '');
  sec.dataset.page = p.page_no;
  sec.innerHTML = (ch && p.page_no > 1 ? '<div class="ornament" aria-hidden="true">❦</div>' : '') +
    `<div class="pg-marker" aria-hidden="true">${p.page_no}</div><div class="pg-content">${pageHtml(p.text, !!ch)}</div>`;
  reader.texts.set(p.page_no, p.text);
  return sec;
}

const section = n => $(`#page .pg-block[data-page="${n}"]`);

/** Rebuild the view around page n; frac = how far into that page to scroll (0..1). */
async function openAt(n, frac = 0) {
  const b = state.book;
  n = Math.max(1, Math.min(b.num_pages, n | 0));
  const tok = ++reader.token;
  const start = Math.max(1, n - 2), end = Math.min(b.num_pages, n + BATCH);
  const [pages] = await Promise.all([fetchPages(start, end), ensureHighlights().catch(() => null)]);
  if (tok !== reader.token) return;
  const art = $('#page');
  art.innerHTML = '';
  reader.texts.clear();
  const frag = document.createDocumentFragment();
  pages.forEach(p => frag.appendChild(pageSection(p)));
  art.appendChild(frag);
  reader.min = start; reader.max = end;
  scrollToPage(n, frac, false);
  reader.cur = -1;
  trackCurrent();
}

function scrollToPage(n, frac = 0, smooth = false) {
  const el = section(n);
  if (!el) return;
  const top = el.offsetTop + frac * el.offsetHeight - (frac ? 0 : 8);
  $('#pageWrap').scrollTo({ top, behavior: smooth ? 'smooth' : 'auto' });
}

async function loadMore(dir) {
  const b = state.book, wrap = $('#pageWrap'), art = $('#page');
  if (reader.loading) return;
  reader.loading = true;
  const tok = reader.token;
  try {
    if (dir === 'down' && reader.max < b.num_pages) {
      const a = reader.max + 1, z = Math.min(b.num_pages, reader.max + BATCH);
      const pages = await fetchPages(a, z);
      if (tok !== reader.token) return;
      const frag = document.createDocumentFragment();
      pages.forEach(p => frag.appendChild(pageSection(p)));
      art.appendChild(frag);
      reader.max = z;
      // Drop pages far above; keep the reading position steady.
      while (reader.max - reader.min > KEEP) {
        const first = art.firstElementChild, h = first.offsetHeight;
        first.remove(); reader.texts.delete(reader.min); reader.min++;
        wrap.scrollTop -= h;
      }
    } else if (dir === 'up' && reader.min > 1) {
      const a = Math.max(1, reader.min - BATCH), z = reader.min - 1;
      const pages = await fetchPages(a, z);
      if (tok !== reader.token) return;
      const frag = document.createDocumentFragment();
      pages.forEach(p => frag.appendChild(pageSection(p)));
      const before = wrap.scrollHeight;
      art.prepend(frag);
      wrap.scrollTop += wrap.scrollHeight - before;
      reader.min = a;
      while (reader.max - reader.min > KEEP) {
        art.lastElementChild.remove(); reader.texts.delete(reader.max); reader.max--;
      }
    }
  } finally {
    reader.loading = false;
  }
}

/** Current reading position: the page crossing 30% of the viewport, and how far into it we are. */
function readingPos() {
  const wrap = $('#pageWrap');
  const line = wrap.scrollTop + wrap.clientHeight * 0.3;
  let sec = null;
  for (const s of $('#page').children) {
    if (s.offsetTop <= line) sec = s; else break;
  }
  if (!sec) return { page: reader.min || 1, frac: 0 };
  const frac = Math.max(0, Math.min(0.999, (wrap.scrollTop - sec.offsetTop) / Math.max(1, sec.offsetHeight)));
  return { page: +sec.dataset.page, frac };
}

function trackCurrent() {
  if (!state.book || !reader.max) return;
  const pos = readingPos();
  store('pos.' + state.book.id, pos);
  if (pos.page === reader.cur) return;
  reader.cur = pos.page;
  state.page = pos.page;
  updateLocation();
  clearTimeout(reader.progTimer);
  reader.progTimer = setTimeout(sendProgress, 500);
}

function updateLocation() {
  const b = state.book, n = reader.cur;
  $('#pageSlider').value = n;
  renderChapters();
  const chEnd = b.chapters.find(c => c.start_page > n);
  const left = (chEnd ? chEnd.start_page : b.num_pages + 1) - n;
  const mins = Math.max(1, Math.round(left * 320 / 250));  // ~320 words per page, 250 wpm
  $('#pageLabel').textContent = `p. ${n} of ${b.num_pages} · ${Math.round(100 * n / b.num_pages)}%` +
    (chEnd || left > 1 ? ` · ~${mins} min left in chapter` : '');
  $('#readProgress').style.width = `${100 * n / b.num_pages}%`;
}

async function sendProgress() {
  const b = state.book, n = reader.cur;
  const body = { page: n };
  if (reader.lockShield) {
    if (n > b.max_page + 2) body.max_page = b.max_page;
    else reader.lockShield = false;
  }
  let prog;
  try { prog = await api(`/api/books/${b.id}/progress`, { json: body }); } catch { return; }
  if (state.book !== b) return;
  const changed = prog.max_page !== b.max_page;
  b.last_page = prog.last_page; b.max_page = prog.max_page;
  if (changed) onUptoChanged();
}

/** Reading further (or changing the shield) reveals more of the codex. */
function onUptoChanged() {
  updateShield();
  renderChapters();
  clearTimeout(reader.uptoTimer);
  reader.uptoTimer = setTimeout(() => {
    refreshHighlights();
    if (!$('#codex').hidden) refreshCodexView();
  }, 1200);
}

/** Re-underline names in the loaded pages (heights don't change, so the scroll position holds). */
async function refreshHighlights(force = false) {
  if (typo.highlight) await ensureHighlights().catch(() => null);
  const key = typo.highlight && state.highlights ? state.highlights.re.source : '';
  if (!force && key === reader.hlKey) return;
  reader.hlKey = key;
  for (const sec of $('#page').children) {
    const text = reader.texts.get(+sec.dataset.page);
    if (text != null) sec.querySelector('.pg-content').innerHTML = pageHtml(text, sec.classList.contains('chapter-start'));
  }
}

/** Navigate (chapter menu, slider, page links). Far jumps ask before counting pages as read. */
async function jumpTo(n) {
  const b = state.book;
  n = Math.max(1, Math.min(b.num_pages, n | 0));
  if (spoilerOn() && n > b.max_page + 2) {
    const ok = confirm(`Jumping ahead to page ${n}.\n\nCount everything up to page ${n} as read? ` +
      `(Choose Cancel to keep the spoiler shield at page ${b.max_page}.)`);
    reader.lockShield = !ok;
  }
  if (n >= reader.min && n <= reader.max && Math.abs(n - reader.cur) <= 3) scrollToPage(n, 0, true);
  else await openAt(n);
  const el = section(n);
  if (el) { el.classList.remove('flash'); void el.offsetWidth; el.classList.add('flash'); }
}

function chapterJump(dir) {
  const chs = state.book.chapters, n = reader.cur;
  const idx = chs.reduce((acc, c, i) => (c.start_page <= n ? i : acc), 0);
  const cur = chs[idx];
  // "Previous" first returns to the start of the current chapter if we're inside it.
  const target = dir > 0 ? chs[idx + 1] : (n > cur.start_page ? cur : chs[idx - 1]);
  if (target) jumpTo(target.start_page);
}

/** Chapter titles can be spoilers ("The Death of X"): hide unread ones while the shield is on. */
function renderChapters() {
  const b = state.book, hide = spoilerOn();
  $('#chapterSelect').innerHTML = b.chapters.map((c, i) => {
    const label = !hide || c.start_page <= b.max_page ? c.title : `Section ${i + 1} · p. ${c.start_page} (unread)`;
    return `<option value="${c.start_page}">${esc(label)}</option>`;
  }).join('');
  const ch = [...b.chapters].reverse().find(c => c.start_page <= state.page);
  if (ch) $('#chapterSelect').value = ch.start_page;
}

async function ensureHighlights() {
  const u = upto();
  if (state.highlights && state.highlightsUpto === u) return state.highlights;
  const list = await api(`/api/books/${state.book.id}/highlights?upto=${u}`);
  const byText = new Map();
  for (const h of list) if (!byText.has(h.text)) byText.set(h.text, h);
  const keys = [...byText.keys()].sort((a, b) => b.length - a.length)
    .map(k => esc(k).replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
  state.highlights = keys.length ? { re: new RegExp(`(?<![\\w])(${keys.join('|')})(?![\\w])`, 'g'), byText } : null;
  state.highlightsUpto = u;
  return state.highlights;
}

function bindReader() {
  $('#backBtn').onclick = () => {
    clearTimeout(state.pollTimer);
    clearTimeout(reader.progTimer);
    if (state.book && reader.cur > 0) sendProgress();
    reader.token++;
    $('#reader').hidden = true; $('#library').hidden = false;
    loadLibrary();
  };
  $('#prevBtn').onclick = () => chapterJump(-1);
  $('#nextBtn').onclick = () => chapterJump(1);
  $('#pageSlider').onchange = e => jumpTo(+e.target.value);
  $('#pageSlider').oninput = e => ($('#pageLabel').textContent = `Go to p. ${e.target.value} of ${state.book.num_pages}`);
  $('#chapterSelect').onchange = e => jumpTo(+e.target.value);

  const wrap = $('#pageWrap');
  let ticking = false;
  wrap.addEventListener('scroll', () => {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(() => {
      ticking = false;
      if (!state.book || !reader.max) return;
      const nearBottom = wrap.scrollTop + wrap.clientHeight > wrap.scrollHeight - wrap.clientHeight * 1.5;
      const nearTop = wrap.scrollTop < wrap.clientHeight;
      if (nearBottom) loadMore('down'); else if (nearTop) loadMore('up');
      trackCurrent();
    });
  }, { passive: true });

  document.addEventListener('keydown', e => {
    if ($('#reader').hidden || /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) return;
    if (!$('#typoPanel').hidden || $('#settingsDlg').open) return;
    const page = wrap.clientHeight * 0.9;
    if (e.key === 'ArrowRight' || e.key === 'PageDown' || (e.key === ' ' && !e.shiftKey)) {
      e.preventDefault(); wrap.scrollBy({ top: page, behavior: 'smooth' });
    } else if (e.key === 'ArrowLeft' || e.key === 'PageUp' || (e.key === ' ' && e.shiftKey)) {
      e.preventDefault(); wrap.scrollBy({ top: -page, behavior: 'smooth' });
    } else if (e.key === 'ArrowDown') {
      e.preventDefault(); wrap.scrollBy({ top: 60 });
    } else if (e.key === 'ArrowUp') {
      e.preventDefault(); wrap.scrollBy({ top: -60 });
    } else if (e.key === 'Home' && e.ctrlKey) {
      e.preventDefault(); jumpTo(1);
    }
  });
  $('#page').onclick = e => {
    const ent = e.target.closest('.ent');
    if (ent) { toggleCodex(true); switchTab('entries'); openEntity(+ent.dataset.eid); }
  };
  document.addEventListener('click', e => {
    const pg = e.target.closest('.pg');
    if (pg && pg.dataset.page && !$('#reader').hidden) jumpTo(+pg.dataset.page);
  });
  // Keep the reading position when the window or the codex panel changes the text width.
  let lastWidth = 0;
  new ResizeObserver(entries => {
    const w = Math.round(entries[0].contentRect.width);
    if (w === lastWidth) return;  // height changes (pages loading) are handled by loadMore
    lastWidth = w;
    if (!state.book || $('#reader').hidden || !reader.max) return;
    const pos = store('pos.' + state.book.id);
    if (pos) scrollToPage(pos.page, pos.frac);
  }).observe($('#page'));
}

// ------------------------------------------------------------------ codex
function toggleCodex(force) {
  const show = force ?? $('#codex').hidden;
  $('#codex').hidden = !show;
  $('#codexBtn').classList.toggle('active', show);
  store('codexOpen', show);
  if (show) refreshCodexView();
}

function switchTab(name) {
  $$('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  $('#tab-entries').hidden = name !== 'entries';
  $('#tab-ask').hidden = name !== 'ask';
}

function updateShield() {
  const b = state.book, on = spoilerOn();
  $('#shield').classList.toggle('off', !on);
  $('#shieldInfo').textContent = on
    ? `Showing only what's known by page ${b.max_page} of ${b.num_pages}`
    : 'Off — showing the whole book, including spoilers';
  $('#editUpto').hidden = !on;
}

async function pollBook() {
  clearTimeout(state.pollTimer);
  if ($('#reader').hidden || !state.book) return;
  try {
    const b = await api(`/api/books/${state.book.id}`);
    Object.assign(state.book, { status: b.status, stage: b.stage, progress: b.progress, processed_page: b.processed_page, error: b.error });
    const info = $('#procInfo');
    if (b.status === 'done') info.hidden = true;
    else {
      info.hidden = false;
      const pct = Math.round((b.progress || 0) * 100);
      const msg = {
        processing: `Analysing… ${esc(b.stage)} — codex covers pages 1–${b.processed_page}`,
        queued: 'Waiting to be analysed…',
        paused: `Analysis paused — codex covers pages 1–${b.processed_page}. <button class="btn tiny ghost" id="resumeBtn">Resume</button>`,
        error: `Analysis error: ${esc(b.error)} <button class="btn tiny ghost" id="resumeBtn">Retry</button>`,
      }[b.status] || '';
      info.innerHTML = `${msg}<div class="bar"><div style="width:${pct}%"></div></div>`;
      const rb = $('#resumeBtn');
      if (rb) rb.onclick = async () => { await api(`/api/books/${b.id}/process`, { method: 'POST' }); pollBook(); };
    }
    if (b.processed_page !== state.lastProcessed) {
      const first = state.lastProcessed === -1;
      state.lastProcessed = b.processed_page;
      if (!first) {
        state.highlights = null;
        if (!$('#codex').hidden && !state.entityId) refreshList();
        if (typo.highlight) refreshHighlights();
      }
    }
    if (['processing', 'queued'].includes(b.status)) state.pollTimer = setTimeout(pollBook, 5000);
  } catch { state.pollTimer = setTimeout(pollBook, 10000); }
}

function refreshCodexView() {
  if (state.entityId) openEntity(state.entityId, { keepScroll: true });
  else refreshList();
}

async function refreshList() {
  const b = state.book;
  const q = new URLSearchParams({ upto: upto(), type: state.codexType, q: state.codexQuery });
  const data = await api(`/api/books/${b.id}/codex?${q}`);
  const total = Object.values(data.counts).reduce((a, c) => a + c, 0);
  $('#typeChips').innerHTML = `<button class="chip ${state.codexType ? '' : 'on'}" data-type="">All <span class="n">${total}</span></button>` +
    Object.entries(data.counts).filter(([, n]) => n).map(([t, n]) =>
      `<button class="chip ${state.codexType === t ? 'on' : ''}" data-type="${t}" style="--c:${tcolor(t)}"><i></i>${TYPE_LABELS[t]} <span class="n">${n}</span></button>`).join('');
  const list = $('#entityList');
  if (!data.entities.length) {
    list.innerHTML = `<p class="muted small">${b.processed_page === 0
      ? 'The codex is being built — entries appear here as the book is analysed.'
      : state.codexQuery || state.codexType ? 'Nothing matches.' : 'No entries up to this page yet.'}</p>`;
    return;
  }
  list.innerHTML = data.entities.slice(0, 400).map(e => `
    <button class="entity" data-eid="${e.id}">
      <span class="type-dot" style="--c:${tcolor(e.type)}"></span>
      <span class="e-name">${esc(e.name)}${e.aliases.length ? ` <span class="e-aka">· ${esc(e.aliases.slice(0, 2).join(', '))}</span>` : ''}</span>
      <span class="e-meta">${e.facts} notes</span>
    </button>`).join('');
}

function showList() {
  state.entityId = null;
  $('#detailView').hidden = true;
  $('#listView').hidden = false;
}

async function openEntity(id, { keepScroll = false } = {}) {
  const b = state.book;
  state.entityId = id;
  $('#listView').hidden = true;
  $('#detailView').hidden = false;
  const panel = $('#tab-entries');
  const scroll = panel.scrollTop;
  let d;
  try {
    d = await api(`/api/books/${b.id}/entities/${id}?upto=${upto()}`);
  } catch (err) {
    $('#detail').innerHTML = `<p class="muted">${esc(err.message)}</p>`;
    return;
  }
  if (state.entityId !== id) return;
  let factsHtml = '', lastCh = null;
  for (const f of d.facts) {
    if (f.chapter !== lastCh) { factsHtml += `<li class="chapter-sep" style="display:block">${esc(f.chapter)}</li>`; lastCh = f.chapter; }
    factsHtml += `<li><button class="pg" data-page="${f.page}">p. ${f.page}</button><span>${esc(f.text)}</span></li>`;
  }
  const u = d.upto, bins = Math.min(60, u);
  const counts = new Array(bins).fill(0), firstPage = new Array(bins).fill(null);
  for (const m of d.mentions) {
    const i = Math.min(bins - 1, Math.floor((m.page - 1) / u * bins));
    counts[i] += m.count;
    if (firstPage[i] == null) firstPage[i] = m.page;
  }
  const maxC = Math.max(1, ...counts);
  const strip = d.mentions.length ? `<div class="mention-strip" title="Where this appears (pages 1–${u})">${counts.map((c, i) =>
    `<div style="height:${c ? 8 + 92 * c / maxC : 0}%" ${firstPage[i] ? `class="pg" data-page="${firstPage[i]}" title="p. ${firstPage[i]}"` : ''}></div>`).join('')}</div>
    <p class="muted small">Mentioned ${d.mentions.reduce((a, m) => a + m.count, 0)} times on ${d.mentions.length} pages.</p>` : '';

  $('#detail').innerHTML = `<div class="detail">
    <span class="type-badge" style="--c:${tcolor(d.type)}"><i class="type-dot"></i>${TYPE_SINGULAR[d.type]}</span>
    <h3>${esc(d.name)}</h3>
    ${d.aliases.length ? `<p class="aka">Also: ${esc(d.aliases.join(' · '))}</p>` : ''}
    <p class="muted small">First appears on <button class="pg" data-page="${d.first_page}">p. ${d.first_page}</button> — ${esc(d.first_chapter)}</p>
    <div class="sect"><h4>Overview</h4><div id="summaryBox" class="summary">${d.summary ? md(d.summary)
      : '<div class="loading"><span class="spinner"></span>Writing a spoiler-safe entry…</div>'}</div></div>
    ${d.related.length ? `<div class="sect"><h4>Connected to</h4><div class="rel">${d.related.map(r =>
      `<button class="chip" data-eid="${r.id}" style="--c:${tcolor(r.type)}"><i></i>${esc(r.name)}</button>`).join('')}</div></div>` : ''}
    ${strip ? `<div class="sect"><h4>Appearances</h4>${strip}</div>` : ''}
    <div class="sect"><h4>Everything the book says (${d.facts.length})</h4><ul class="facts">${factsHtml}</ul></div>
    ${d.appears_in.length ? `<div class="sect"><h4>Mentioned in other entries</h4><ul class="facts">${d.appears_in.map(f =>
      `<li><button class="pg" data-page="${f.page}">p. ${f.page}</button><span><button class="pg" data-eid="${f.entity_id}" style="color:var(--text);font-weight:600">${esc(f.entity)}</button> — ${esc(f.text)}</span></li>`).join('')}</ul></div>` : ''}
  </div>`;
  panel.scrollTop = keepScroll ? scroll : 0;
  const write = async () => {
    $('#summaryBox').innerHTML = '<div class="loading"><span class="spinner"></span>Writing a spoiler-safe entry…</div>';
    try {
      const s = await api(`/api/books/${b.id}/entities/${id}/summary?upto=${u}`, { method: 'POST' });
      if (state.entityId === id) {
        $('#summaryBox').innerHTML = md(s.summary);
        state.lastSummary = { id, html: md(s.summary) };
      }
    } catch (err) {
      if (state.entityId === id) $('#summaryBox').innerHTML = `<p class="muted small">Couldn't write a summary: ${esc(err.message)}</p>`;
    }
  };
  if (!d.summary) {
    if (!keepScroll) return write();
    // Refreshed because you read further: keep the old text and offer an update instead of
    // calling the model on every page turn.
    const prev = state.lastSummary && state.lastSummary.id === id ? state.lastSummary.html : '';
    $('#summaryBox').innerHTML = `${prev}<button class="btn tiny ghost" id="updSummary">Update overview with new pages</button>`;
    $('#updSummary').onclick = write;
  } else {
    state.lastSummary = { id, html: $('#summaryBox').innerHTML };
  }
}

function bindCodex() {
  $('#codexBtn').onclick = () => toggleCodex();
  $('#closeCodex').onclick = () => toggleCodex(false);
  $$('.tab').forEach(t => (t.onclick = () => switchTab(t.dataset.tab)));
  $('#typeChips').onclick = e => {
    const c = e.target.closest('[data-type]');
    if (c) { state.codexType = c.dataset.type; refreshList(); }
  };
  let timer;
  $('#codexSearch').oninput = e => { clearTimeout(timer); timer = setTimeout(() => { state.codexQuery = e.target.value; refreshList(); }, 200); };
  $('#entityList').onclick = e => { const el = e.target.closest('[data-eid]'); if (el) openEntity(+el.dataset.eid); };
  $('#detail').onclick = e => {
    const el = e.target.closest('[data-eid]');
    if (el) { e.stopPropagation(); openEntity(+el.dataset.eid); }
  };
  $('#detailBack').onclick = () => { showList(); refreshList(); };
  $('#spoilerToggle').onchange = e => {
    if (!e.target.checked && !confirm('Turn off the spoiler shield? The codex will show information from the entire book.')) {
      e.target.checked = true; return;
    }
    store('spoiler.' + state.book.id, e.target.checked);
    state.highlights = null;
    updateShield(); refreshCodexView(); refreshHighlights(true); renderChapters();
  };
  $('#editUpto').onclick = async () => {
    const b = state.book;
    const v = prompt(`You've read up to which page? (1–${b.num_pages})`, b.max_page);
    if (v == null) return;
    const n = parseInt(v, 10);
    if (!n) return;
    const r = await api(`/api/books/${b.id}/progress`, { json: { page: state.page, max_page: n } });
    b.max_page = r.max_page;
    // Set below the page on screen: don't let scrolling here push the shield forward again.
    reader.lockShield = b.max_page < reader.cur - 2;
    state.highlights = null;
    updateShield(); refreshCodexView(); refreshHighlights(true); renderChapters();
  };
  $('#askForm').onsubmit = async e => {
    e.preventDefault();
    const q = $('#askInput').value.trim();
    if (!q) return;
    $('#askInput').value = '';
    const chat = $('#chat');
    chat.insertAdjacentHTML('beforeend', `<div class="msg user">${esc(q)}</div>`);
    const pending = document.createElement('div');
    pending.className = 'msg bot';
    pending.innerHTML = '<div class="loading"><span class="spinner"></span>Searching the pages you\'ve read…</div>';
    chat.appendChild(pending);
    pending.scrollIntoView({ block: 'end' });
    try {
      const r = await api(`/api/books/${state.book.id}/ask`, { json: { question: q, upto: upto() } });
      pending.innerHTML = md(r.answer) + (r.pages.length ? `<p class="muted small">Sources: ${r.pages.map(p => `<button class="pg" data-page="${p}">p. ${p}</button>`).join(' ')}</p>` : '');
    } catch (err) { pending.innerHTML = `<p class="muted">${esc(err.message)}</p>`; }
    pending.scrollIntoView({ block: 'end' });
  };
}

// ------------------------------------------------------------------ settings dialog
function bindSettings() {
  const dlg = $('#settingsDlg'), form = $('#settingsForm');
  let provider = 'ollama';
  const setProvider = p => {
    provider = p;
    $$('#providerSeg button').forEach(b => b.classList.toggle('on', b.dataset.v === p));
    $('#ollamaFields').hidden = p !== 'ollama';
    $('#openaiFields').hidden = p !== 'openai';
  };
  $$('#providerSeg button').forEach(b => (b.onclick = () => setProvider(b.dataset.v)));
  $('#openSettings').onclick = async () => {
    const s = await api('/api/settings');
    for (const [k, v] of Object.entries(s)) if (form.elements[k]) form.elements[k].value = v;
    setProvider(s.provider);
    const h = await loadHealth();
    if (h) {
      $('#ollamaModels').innerHTML = (h.provider === 'ollama' ? h.llm.models : []).map(m => `<option value="${escAttr(m)}">`).join('');
      $('#settingsHealth').textContent = h.llm.ok ? `Connected (${h.provider}).` : `Not connected: ${h.llm.message}`;
    }
    dlg.showModal();
  };
  $('#saveSettings').onclick = async () => {
    const body = { provider };
    for (const k of ['ollama_url', 'ollama_model', 'ollama_num_ctx', 'openai_base_url', 'openai_api_key', 'openai_model', 'embed_model']) {
      body[k] = k === 'ollama_num_ctx' ? +form.elements[k].value : form.elements[k].value.trim();
    }
    await api('/api/settings', { json: body });
    checkSetup();
    const h = await loadHealth();
    $('#settingsHealth').textContent = h && h.llm.ok ? 'Saved and connected.' : `Saved, but not connected: ${h ? h.llm.message : ''}`;
  };
}

// ------------------------------------------------------------------ boot
applyTypo();
bindTypo();
bindLibrary();
bindReader();
bindCodex();
bindSettings();
loadLibrary();
loadHealth();
checkSetup();
