'use strict';

const MAX_FILTERS = 10;
const PAGE = 60;

const $ = (id) => document.getElementById(id);

const state = {
  filters: [{ include: [], exclude: [] }],
  excludeAll: [],
  matchAll: false,
  order: 'scan',
  seed: null,
  maxImages: null,
  activeFilter: 0,
  total: 0,
  selected: 0,
  offset: 0,
  loading: false,
  exhausted: false,
  ready: false,
  jobId: null,
  tagging: false,
  immichBusy: false,
};

/* ---------------- fetch helpers ---------------- */

async function api(path, options) {
  const res = await fetch(path, options);
  const data = await res.json().catch(() => ({ error: 'Bad response from server' }));
  if (!res.ok || data.error) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

const postJSON = (path, body) =>
  api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });

let toastTimer = null;
function toast(message) {
  const el = $('toast');
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 4200);
}

/* ---------------- index status ---------------- */

async function pollStatus() {
  let status;
  try {
    status = await api('/api/status');
  } catch (err) {
    $('stat').textContent = 'server unreachable';
    return;
  }

  $('root').textContent = status.root;
  $('root').title = status.root;

  if (status.state === 'building') {
    $('stat').innerHTML = `<span class="spin"></span> indexing ${status.scanned.toLocaleString()}…`;
    state.ready = false;
    setTimeout(pollStatus, 400);
    return;
  }
  if (status.state === 'error') {
    $('stat').textContent = 'index failed';
    toast(status.error || 'Index failed');
    return;
  }

  $('stat').innerHTML =
    `<b>${status.images.toLocaleString()}</b> tagged images · <b>${status.tags.toLocaleString()}</b> tags`;
  renderImmich(status.immich);
  if (!state.ready) {
    state.ready = true;
    search();
    checkUntagged();
  }
}

/* ---------------- filters ---------------- */

/** Put `tag` on one side of a filter, removing it from the other. */
function addTag(filter, tag, exclude) {
  const list = exclude ? filter.exclude : filter.include;
  const other = exclude ? filter.include : filter.exclude;
  const i = other.indexOf(tag);
  if (i >= 0) other.splice(i, 1);
  if (!list.includes(tag)) list.push(tag);
}

/** Chip row. `entries` are {tag, exclude, remove}. */
function renderChips(host, entries) {
  const chips = document.createElement('div');
  chips.className = 'chips';
  entries.forEach((entry) => {
    const chip = document.createElement('span');
    chip.className = entry.exclude ? 'chip exclude' : 'chip';
    const label = document.createElement('span');
    label.className = 'label';
    label.textContent = entry.tag;
    label.title = entry.tag;
    const x = document.createElement('button');
    x.textContent = '\u00d7';
    x.title = 'Remove';
    x.onclick = entry.remove;
    chip.append(label, x);
    chips.appendChild(chip);
  });
  host.appendChild(chips);
}

function renderExcludeAll() {
  const host = $('exclude-all');
  host.textContent = '';

  renderChips(host, state.excludeAll.map((tag, i) => ({
    tag,
    exclude: true,
    remove: () => { state.excludeAll.splice(i, 1); renderExcludeAll(); search(); },
  })));

  host.appendChild(buildTagInput('never include this tag', (tag) => {
    if (!state.excludeAll.includes(tag)) state.excludeAll.push(tag);
    renderExcludeAll();
    search();
    $('exclude-all').querySelector('.tag-input input')?.focus();
  }));

  $('exclude-count').textContent = state.excludeAll.length
    ? `${state.excludeAll.length} tag${state.excludeAll.length === 1 ? '' : 's'}`
    : '';
}

function renderFilters() {
  const host = $('filters');
  host.textContent = '';

  state.filters.forEach((filter, i) => {
    const card = document.createElement('div');
    card.className = 'filter';

    const head = document.createElement('div');
    head.className = 'filter-head';
    const n = document.createElement('span');
    n.className = 'n';
    n.textContent = `FILTER ${i + 1}`;
    head.appendChild(n);

    if (state.filters.length > 1) {
      const remove = document.createElement('button');
      remove.className = 'btn';
      remove.style.padding = '1px 7px';
      remove.textContent = 'Remove';
      remove.onclick = () => {
        state.filters.splice(i, 1);
        state.activeFilter = Math.min(state.activeFilter, state.filters.length - 1);
        renderFilters();
        search();
      };
      head.appendChild(remove);
    }
    card.appendChild(head);

    const entry = (list, exclude) => list.map((tag, j) => ({
      tag,
      exclude,
      remove: () => { list.splice(j, 1); renderFilters(); search(); },
    }));
    renderChips(card, [...entry(filter.include, false), ...entry(filter.exclude, true)]);

    card.appendChild(buildTagInput('add a tag \u2014  shift-enter excludes', (tag, shift) => {
      addTag(state.filters[i], tag, shift);
      renderFilters();
      search();
      $('filters').querySelectorAll('.tag-input input')[i]?.focus();
    }, i));
    card.onmousedown = () => { state.activeFilter = i; $('viewer-target').textContent = String(i + 1); };
    host.appendChild(card);
  });

  $('filter-count').textContent = `${state.filters.length} of ${MAX_FILTERS}`;
  $('add-filter').disabled = state.filters.length >= MAX_FILTERS;
  $('viewer-target').textContent = String(state.activeFilter + 1);
}

/**
 * A tag box with autocomplete. `onCommit(tag, shiftKey)` owns what the tag means —
 * the same widget serves a filter card and the global veto list.
 * `filterIndex`, when given, marks that filter active on focus.
 */
function buildTagInput(placeholder, onCommit, filterIndex) {
  const wrap = document.createElement('div');
  wrap.className = 'tag-input';

  const input = document.createElement('input');
  input.type = 'text';
  input.placeholder = placeholder;
  input.autocomplete = 'off';
  input.spellcheck = false;

  const menu = document.createElement('div');
  menu.className = 'suggest';
  wrap.append(input, menu);

  let items = [];
  let cursor = -1;
  let token = 0;

  const close = () => { menu.textContent = ''; items = []; cursor = -1; };

  const commit = (tag, shiftKey) => {
    input.value = '';
    close();
    onCommit(tag, shiftKey);  // re-renders, which replaces this very input
  };

  const draw = () => {
    menu.textContent = '';
    items.forEach((item, i) => {
      const row = document.createElement('div');
      if (i === cursor) row.className = 'on';
      const name = document.createElement('span');
      name.textContent = item.tag;
      const count = document.createElement('span');
      count.className = 'count';
      count.textContent = item.count.toLocaleString();
      row.append(name, count);
      row.onmousedown = (event) => { event.preventDefault(); commit(item.tag, event.shiftKey); };
      menu.appendChild(row);
    });
  };

  const suggest = async () => {
    const query = input.value.trim();
    const mine = ++token;
    try {
      const data = await api(`/api/tags?q=${encodeURIComponent(query)}&limit=20`);
      if (mine !== token) return;
      items = data.tags;
      cursor = items.length ? 0 : -1;
      draw();
    } catch (err) { /* index not up yet; the next keystroke retries */ }
  };

  input.oninput = suggest;
  input.onfocus = () => {
    if (filterIndex !== undefined) state.activeFilter = filterIndex;
    suggest();
  };
  input.onblur = () => setTimeout(close, 120);

  input.onkeydown = (event) => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      if (!items.length) return;
      cursor = (cursor + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
      draw();
      menu.children[cursor]?.scrollIntoView({ block: 'nearest' });
    } else if (event.key === 'Enter') {
      event.preventDefault();
      // The highlighted row wins. Falling back to the raw text is what lets a glob
      // like blue_* through, since a glob matches no vocabulary entry to highlight.
      const tag = (cursor >= 0 && items[cursor]) ? items[cursor].tag : input.value.trim();
      if (tag) commit(tag, event.shiftKey);
    } else if (event.key === 'Escape') {
      close();
    }
  };

  return wrap;
}

/* ---------------- search + grid ---------------- */

function payload(extra) {
  return Object.assign({
    filters: state.filters.map((f) => ({ include: f.include, exclude: f.exclude })),
    excludeAll: state.excludeAll,
    matchAll: state.matchAll,
    order: state.order,
    seed: state.seed,
    maxImages: state.maxImages,
  }, extra || {});
}

let searchTimer = null;
function search() {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(runSearch, 90);
}

async function runSearch() {
  if (!state.ready) return;
  state.offset = 0;
  state.exhausted = false;
  $('grid').textContent = '';

  let data;
  try {
    data = await api('/api/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload({ offset: 0, page: PAGE })),
    });
  } catch (err) {
    toast(err.message);
    return;
  }

  state.total = data.total;
  state.selected = data.selected;

  const capped = data.selected < data.total;
  $('count').innerHTML = data.total
    ? `${data.total.toLocaleString()} match${data.total === 1 ? '' : 'es'}` +
      (capped ? `<small>exporting the first ${data.selected.toLocaleString()}</small>` : '')
    : 'No matches';

  $('empty').hidden = data.total > 0;
  $('empty').textContent = state.filters.some((f) => f.include.length || f.exclude.length)
    ? 'Nothing in the library carries this combination of tags.'
    : 'Add a tag to start filtering.';

  $('export-hint').textContent = data.selected
    ? `Exports ${data.selected.toLocaleString()} image${data.selected === 1 ? '' : 's'}.`
    : '';
  $('export').disabled = !data.selected;
  $('reshuffle').style.display = state.order === 'random' ? '' : 'none';

  appendCards(data.items, data.generation);
  state.offset = data.items.length;
  state.exhausted = state.offset >= data.selected;
  fillViewport();
}

async function loadMore() {
  if (state.loading || state.exhausted || !state.ready) return;
  state.loading = true;
  try {
    const data = await api('/api/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload({ offset: state.offset, page: PAGE })),
    });
    appendCards(data.items, data.generation);
    state.offset += data.items.length;
    if (!data.items.length || state.offset >= data.selected) state.exhausted = true;
  } catch (err) {
    state.exhausted = true;
  } finally {
    state.loading = false;
  }
}

/**
 * `generation` is the index build the ids came from. Every rebuild reassigns ids, so it
 * goes into the image URLs — otherwise the browser's cache keeps serving whatever
 * picture an id used to mean.
 */
function appendCards(items, generation) {
  const grid = $('grid');
  items.forEach((item) => {
    item.generation = generation;
    const card = document.createElement('div');
    card.className = 'card';

    const img = document.createElement('img');
    img.loading = 'lazy';
    img.src = `/api/thumb?id=${item.id}&v=${generation}`;
    img.alt = item.name;
    img.onload = () => img.classList.add('loaded');
    img.onerror = () => { card.style.opacity = '.35'; };

    const name = document.createElement('div');
    name.className = 'name';
    name.textContent = item.name;

    card.append(img, name);
    card.onclick = () => openViewer(item);
    grid.appendChild(card);
  });
}

/** Keep loading while the grid is shorter than the pane, or scrolling never starts. */
function fillViewport() {
  const main = document.querySelector('main');
  if (!state.exhausted && main.scrollHeight <= main.clientHeight + 40) {
    loadMore().then(() => { if (!state.exhausted) fillViewport(); });
  }
}

/* ---------------- viewer ---------------- */

function openViewer(item) {
  $('viewer-img').src = `/api/image?id=${item.id}&v=${item.generation}`;
  $('viewer-name').textContent = item.name;
  $('viewer-target').textContent = String(state.activeFilter + 1);

  const host = $('viewer-tags');
  host.textContent = '';
  item.tags.forEach((tag) => {
    const button = document.createElement('button');
    button.textContent = tag;
    button.onclick = (event) => {
      addTag(state.filters[state.activeFilter], tag, event.shiftKey);
      closeViewer();
      renderFilters();
      search();
    };
    host.appendChild(button);
  });
  $('viewer').hidden = false;
}

function closeViewer() {
  $('viewer').hidden = true;
  $('viewer-img').src = '';
}

/* ---------------- export ---------------- */

async function startExport() {
  const outDir = $('out-dir').value.trim();
  if (!outDir) { toast('Choose an output folder first.'); $('out-dir').focus(); return; }

  let job;
  try {
    job = await postJSON('/api/export', payload({
      outDir,
      maxSize: $('max-size').value.trim() || '1MB',
      noCompress: !$('compress').checked,
      copySidecar: $('copy-sidecar').checked,
    }));
  } catch (err) {
    toast(err.message);
    return;
  }

  state.jobId = job.id;
  $('export').disabled = true;
  showJob(job);
  pollJob();
}

async function pollJob() {
  if (!state.jobId) return;
  let job;
  try {
    job = await api(`/api/export/${state.jobId}`);
  } catch (err) {
    return;
  }
  showJob(job);
  if (job.state === 'running') {
    setTimeout(pollJob, 300);
  } else {
    state.jobId = null;
    $('export').disabled = state.selected === 0;
  }
}

function showJob(job) {
  const panel = $('export-panel');
  panel.hidden = false;
  panel.classList.toggle('done', job.state !== 'running');

  const pct = job.total ? Math.round((job.done / job.total) * 100) : 0;
  $('export-bar').style.width = `${pct}%`;
  $('export-pct').textContent = `${job.done.toLocaleString()} / ${job.total.toLocaleString()}`;

  const titles = { running: 'Exporting…', finished: 'Export complete', cancelled: 'Cancelled', error: 'Export failed' };
  $('export-title').textContent = titles[job.state] || job.state;

  const bits = [`${job.copied.toLocaleString()} copied`, `${job.compressed.toLocaleString()} compressed`, job.size];
  let html = bits.join(' · ');
  if (job.over) html += ` · <span class="warn">${job.over} still over the limit</span>`;
  if (job.failed) html += ` · <span class="bad">${job.failed} failed</span>`;
  if (job.error) html += `<br><span class="bad">${job.error}</span>`;
  if (job.messages.length && job.state !== 'running') {
    html += `<br><span class="bad">${job.messages[job.messages.length - 1]}</span>`;
  }
  $('export-detail').innerHTML = html;

  const actions = $('export-actions');
  actions.textContent = '';
  if (job.state === 'running') {
    const cancel = document.createElement('button');
    cancel.className = 'btn';
    cancel.textContent = 'Cancel';
    cancel.onclick = () => postJSON(`/api/cancel/${job.id}`, {}).catch(() => {});
    actions.appendChild(cancel);
  } else {
    const reveal = document.createElement('button');
    reveal.className = 'btn';
    reveal.textContent = 'Open folder';
    reveal.onclick = () => postJSON('/api/reveal', { path: job.outDir }).catch(() => {});
    const close = document.createElement('button');
    close.className = 'btn';
    close.textContent = 'Dismiss';
    close.onclick = () => { $('export-panel').hidden = true; };
    actions.append(reveal, close);
  }
}

/* ---------------- tagging ---------------- */

/** Label the header button with how many images still have no sidecar. */
async function checkUntagged() {
  const button = $('tag-new');
  if (state.tagging) return;
  button.disabled = true;
  button.textContent = 'Checking for new…';

  let count = null;
  try {
    count = (await api('/api/untagged')).count;
  } catch (err) { /* leave the button usable; a run does its own scan */ }

  if (state.tagging) return;  // a run started while this was counting
  button.disabled = count === 0;
  button.classList.toggle('attention', count > 0);
  button.textContent = count === null
    ? 'Tag new images'
    : count ? `Tag ${count.toLocaleString()} new` : 'No new images';
}

async function startTagging() {
  $('tag-new').disabled = true;
  let data;
  try {
    data = await postJSON('/api/tag', {});
  } catch (err) {
    toast(err.message);
    $('tag-new').disabled = false;
    return;
  }
  followTagging(data.job);
}

function followTagging(job) {
  showTagJob(job);
  if (state.tagging) return;  // already polling it
  state.tagging = true;
  const button = $('tag-new');
  button.disabled = true;
  button.classList.remove('attention');
  button.textContent = 'Tagging…';
  setTimeout(pollTagging, 500);
}

async function pollTagging() {
  let job;
  try {
    job = (await api('/api/tag')).job;
  } catch (err) {
    return;
  }
  if (job) showTagJob(job);
  if (job?.active) {
    setTimeout(pollTagging, 500);
    return;
  }

  state.tagging = false;
  if (job?.written) {
    // The server has already started rebuilding the index; following it re-runs the
    // search and the count once the new tags are in.
    state.ready = false;
    pollStatus();
  } else {
    checkUntagged();
  }
}

function showTagJob(job) {
  const panel = $('tag-panel');
  panel.hidden = false;
  panel.classList.toggle('done', !job.active);

  const pct = job.total ? Math.round((job.done / job.total) * 100) : 0;
  $('tag-bar').style.width = `${pct}%`;
  $('tag-pct').textContent = job.total ? `${job.done.toLocaleString()} / ${job.total.toLocaleString()}` : '';

  const titles = {
    scanning: 'Looking for new images…',
    loading: 'Loading the tagger model…',
    running: 'Tagging…',
    finished: job.total ? 'Tagging complete' : 'No new images',
    cancelled: 'Tagging cancelled',
    error: 'Tagging failed',
  };
  $('tag-title').textContent = titles[job.state] || job.state;

  // Numbers and fixed words only, so innerHTML is safe; file names go in as text below.
  const bits = [];
  if (job.total) bits.push(`${job.written.toLocaleString()} tagged`);
  if (job.skipped) bits.push(`${job.skipped.toLocaleString()} already tagged`);
  if (job.failed) bits.push(`<span class="bad">${job.failed.toLocaleString()} failed</span>`);
  if (job.active && job.eta !== null) {
    bits.push(job.eta < 60 ? 'under a minute left' : `about ${Math.round(job.eta / 60)} min left`);
  }
  const detail = $('tag-detail');
  detail.innerHTML = bits.join(' · ');
  const problem = job.error || (!job.active && job.messages[job.messages.length - 1]);
  if (problem) {
    const line = document.createElement('div');
    line.className = 'bad';
    line.textContent = problem;
    detail.appendChild(line);
  }

  // Rebuilt only when the run starts or ends, so a poll can't swallow a click on Cancel.
  const actions = $('tag-actions');
  const mode = job.active ? 'cancel' : 'dismiss';
  if (actions.dataset.mode !== mode) {
    actions.dataset.mode = mode;
    const button = document.createElement('button');
    button.className = 'btn';
    if (job.active) {
      button.textContent = 'Cancel';
      button.onclick = () => {
        button.disabled = true;
        postJSON('/api/tag/cancel', {}).catch(() => {});
      };
    } else {
      button.textContent = 'Dismiss';
      button.onclick = () => { $('tag-panel').hidden = true; };
    }
    actions.replaceChildren(button);
  }
}

/* ---------------- immich ---------------- */

function renderImmich(immich) {
  const sync = $('immich-sync');
  const clean = $('immich-clean');
  if (!immich || !immich.configured) {
    sync.hidden = true;
    clean.hidden = true;
    return;
  }
  clean.hidden = false;
  sync.hidden = !immich.pending && !state.immichBusy;
  if (!state.immichBusy) sync.textContent = `Sync to Immich (${immich.pending.toLocaleString()})`;
  sync.title = immich.reachable
    ? 'Ask Immich to re-read the sidecars written since the last sync'
    : 'Immich is not reachable right now — the sync will report why';
}

function immichPanel(title, { pct = null, done = false } = {}) {
  const panel = $('immich-panel');
  panel.hidden = false;
  panel.classList.toggle('done', done);
  $('immich-title').textContent = title;
  $('immich-bar').style.width = pct === null ? (done ? '100%' : '0%') : `${pct}%`;
  $('immich-pct').textContent = '';
  $('immich-detail').replaceChildren();
}

function immichActions(buttons) {
  $('immich-actions').replaceChildren(...buttons.map(({ label, primary, onclick, disabled }) => {
    const button = document.createElement('button');
    button.className = primary ? 'btn primary' : 'btn';
    button.textContent = label;
    button.disabled = !!disabled;
    button.onclick = onclick;
    return button;
  }));
}

const dismissImmich = { label: 'Dismiss', onclick: () => { $('immich-panel').hidden = true; } };

async function startImmichSync() {
  try {
    followImmich((await postJSON('/api/immich/sync', {})).job);
  } catch (err) {
    toast(err.message);
  }
}

async function previewCleanup() {
  immichPanel('Finding the empty tags animetag manages in Immich…');
  immichActions([{ label: 'Cancel', onclick: () => { $('immich-panel').hidden = true; } }]);
  let preview;
  try {
    preview = await api('/api/immich/cleanup-preview');
  } catch (err) {
    immichPanel('Could not read Immich tags', { done: true });
    const line = document.createElement('div');
    line.className = 'bad';
    line.textContent = err.message;
    $('immich-detail').appendChild(line);
    immichActions([dismissImmich]);
    return;
  }

  immichPanel(preview.count ? `${preview.count.toLocaleString()} empty tags can be deleted` : 'No empty tags to delete',
              { done: !preview.count });
  const detail = $('immich-detail');
  const prefixes = Object.entries(preview.byPrefix).map(([name, n]) => `${name} ${n.toLocaleString()}`).join(' · ');
  if (prefixes) detail.append(document.createTextNode(prefixes));
  if (preview.examples.length) {
    const list = document.createElement('div');
    list.className = 'examples';
    list.textContent = preview.examples.join('\n') +
      (preview.count > preview.examples.length ? `\n… and ${(preview.count - preview.examples.length).toLocaleString()} more` : '');
    detail.appendChild(list);
    const note = document.createElement('div');
    note.textContent = 'Only tags under character/, parody/, tag/, meta/ and rating/ (and their old | forms) are considered, '
      + 'and each is checked empty again right before it is deleted.';
    detail.appendChild(note);
  }
  if (!preview.count) {
    immichActions([dismissImmich]);
    return;
  }
  immichActions([
    { label: 'Cancel', onclick: () => { $('immich-panel').hidden = true; } },
    {
      label: `Delete ${preview.count.toLocaleString()} tags`,
      primary: true,
      onclick: async () => {
        try {
          followImmich((await postJSON('/api/immich/cleanup', { confirm: true })).job);
        } catch (err) {
          toast(err.message);
        }
      },
    },
  ]);
}

const IMMICH_PHASES = {
  starting: 'Starting…',
  mapping: 'Reading Immich’s asset list…',
  refresh: 'Asking Immich to re-read the sidecars…',
  discover: 'Starting sidecar discovery…',
  waiting: 'Waiting for Immich to re-read them…',
  planning: 'Finding empty tags…',
  cleanup: 'Removing empty tags…',
};

function followImmich(job) {
  state.immichBusy = true;
  $('immich-sync').disabled = true;
  $('immich-clean').disabled = true;
  showImmichJob(job);
  if (job.active) setTimeout(pollImmich, 1000);
}

async function pollImmich() {
  let job;
  try {
    job = (await api('/api/immich/sync')).job;
  } catch (err) {
    setTimeout(pollImmich, 3000);
    return;
  }
  if (!job) return;
  showImmichJob(job);
  if (job.active) {
    setTimeout(pollImmich, 1000);
    return;
  }
  state.immichBusy = false;
  $('immich-sync').disabled = false;
  $('immich-clean').disabled = false;
  api('/api/status').then((status) => renderImmich(status.immich)).catch(() => {});
}

function showImmichJob(job) {
  const kind = job.kind === 'cleanup' ? 'Immich tag cleanup' : 'Immich sync';
  if (job.active) {
    const pct = job.phase === 'cleanup' && job.total ? Math.round((job.done / job.total) * 100) : null;
    immichPanel(`${kind}: ${IMMICH_PHASES[job.phase] || job.phase}`, { pct });
    if (job.phase === 'waiting' && job.total) $('immich-pct').textContent = `${job.total.toLocaleString()} jobs left`;
    if (job.phase === 'cleanup' && job.total) $('immich-pct').textContent = `${job.done.toLocaleString()} / ${job.total.toLocaleString()}`;
    immichActions([]);
    return;
  }
  const r = job.result || {};
  immichPanel(job.state === 'finished' ? `${kind} complete` : `${kind} did not finish`, { done: true });
  const bits = job.kind === 'cleanup'
    ? [`${(r.deletedTags || 0).toLocaleString()} tags deleted`, r.keptInUse ? `${r.keptInUse.toLocaleString()} kept, still in use` : '']
    : [`${(r.refreshed || 0).toLocaleString()} re-read`, `${(r.discovered || 0).toLocaleString()} discovered`,
       `${(r.deletedTags || 0).toLocaleString()} empty tags removed`];
  const detail = $('immich-detail');
  if (job.state === 'finished') detail.append(document.createTextNode(bits.filter(Boolean).join(' · ')));
  const problem = job.error || r.reason;
  if (problem) {
    const line = document.createElement('div');
    line.className = job.state === 'finished' ? 'warn' : 'bad';
    line.textContent = problem;
    detail.appendChild(line);
  }
  immichActions([dismissImmich]);
}

/* ---------------- wiring ---------------- */

function init() {
  renderFilters();
  renderExcludeAll();

  $('add-filter').onclick = () => {
    if (state.filters.length >= MAX_FILTERS) return;
    state.filters.push({ include: [], exclude: [] });
    state.activeFilter = state.filters.length - 1;
    renderFilters();
    $('filters').querySelectorAll('.tag-input input')[state.activeFilter]?.focus();
    search();
  };

  $('match-mode').onclick = (event) => {
    const button = event.target.closest('button');
    if (!button) return;
    state.matchAll = button.dataset.mode === 'all';
    [...$('match-mode').children].forEach((b) => b.classList.toggle('on', b === button));
    $('match-hint').textContent = state.matchAll
      ? 'An image must match every filter.'
      : 'An image is taken if it matches any one filter.';
    search();
  };

  $('order').onchange = (event) => {
    state.order = event.target.value;
    $('seed-field').style.display = state.order === 'random' ? '' : 'none';
    search();
  };

  $('seed').oninput = (event) => {
    const value = event.target.value.trim();
    state.seed = value === '' ? null : Number(value);
    if (state.order === 'random') search();
  };

  $('max-images').oninput = (event) => {
    const value = parseInt(event.target.value, 10);
    state.maxImages = Number.isFinite(value) && value > 0 ? value : null;
    search();
  };

  $('reshuffle').onclick = () => {
    state.seed = Math.floor(Math.random() * 1e9);
    $('seed').value = String(state.seed);
    runSearch();
  };

  $('browse').onclick = async () => {
    try {
      const data = await postJSON('/api/pick-folder', {});
      if (data.path) $('out-dir').value = data.path;
      else if (data.reason) toast(data.reason);
    } catch (err) { toast(err.message); }
  };

  $('export').onclick = startExport;
  $('tag-new').onclick = startTagging;
  $('immich-sync').onclick = startImmichSync;
  $('immich-clean').onclick = previewCleanup;

  $('rescan').onclick = async () => {
    try {
      await postJSON('/api/reindex', {});
      state.ready = false;
      pollStatus();
    } catch (err) { toast(err.message); }
  };

  $('viewer').onclick = (event) => { if (event.target.id === 'viewer') closeViewer(); };
  document.addEventListener('keydown', (event) => { if (event.key === 'Escape') closeViewer(); });

  const main = document.querySelector('main');
  main.addEventListener('scroll', () => {
    if (main.scrollTop + main.clientHeight >= main.scrollHeight - 400) loadMore();
  });

  pollStatus();

  // A run carries on server-side when the page reloads; pick its progress back up.
  api('/api/tag').then((data) => { if (data.job?.active) followTagging(data.job); }).catch(() => {});
  api('/api/immich/sync').then((data) => { if (data.job?.active) followImmich(data.job); }).catch(() => {});
}

init();
