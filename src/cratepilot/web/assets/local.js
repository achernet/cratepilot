const $ = selector => document.querySelector(selector);
const token = new URLSearchParams(location.search).get('token');
const statusLine = $('#status');
const state = {
  seedKind: 'manual', seeds: [], catalog: [], edges: [], candidates: [], selectedCandidates: new Set(),
  approvedJob: null, tracks: [], plan: null, candidateSort: { key: 'score', direction: -1 },
  graphPositions: new Map(), selectedNode: null, graphView: { x: 0, y: 0, width: 800, height: 440 },
  currentJob: null, playlistTrackIds: null,
};

async function api(path, options = {}) {
  const response = await fetch('/api/v1' + path, {
    ...options,
    headers: { 'content-type': 'application/json', 'x-cratepilot-token': token, ...options.headers },
  });
  let body = {};
  try { body = await response.json(); } catch {}
  if (!response.ok) throw new Error(body.detail || response.statusText);
  return body;
}

function report(message, error = false) {
  statusLine.textContent = message || 'Finished.';
  statusLine.style.borderColor = error ? 'var(--warn)' : 'var(--cyan)';
}

async function wait(jobId) {
  state.currentJob = jobId;
  $('#task-panel').hidden = false;
  $('#cancel-task').disabled = false;
  for (;;) {
    const job = await api('/jobs/' + jobId);
    renderJob(job);
    report(job.message || job.status);
    if (job.status === 'complete') { state.currentJob = null; $('#cancel-task').disabled = true; return job.result; }
    if (job.status === 'cancelled') { state.currentJob = null; $('#cancel-task').disabled = true; throw new Error('Task cancelled. Adjust the filter or settings and restart when ready.'); }
    if (job.status === 'failed') {
      state.currentJob = null;
      $('#cancel-task').disabled = true;
      throw new Error(job.error || job.message);
    }
    await new Promise(resolve => setTimeout(resolve, 500));
  }
}

function renderJob(job) {
  const percent = Math.max(0, Math.min(100, Math.round(Number(job.progress) * 100)));
  $('#task-title').textContent = `${job.kind[0].toUpperCase()}${job.kind.slice(1)} · ${job.message}`;
  $('#progress-bar').style.width = `${percent}%`;
  $('#progress-label').textContent = `${percent}%`;
  $('.progress-track').setAttribute('aria-valuenow', String(percent));
  const log = $('#task-log');
  log.replaceChildren();
  (job.logs || []).forEach(entry => {
    const line = document.createElement('p');
    line.className = entry.level;
    line.textContent = `[${entry.created_at}] ${entry.message}`;
    log.append(line);
  });
  log.scrollTop = log.scrollHeight;
}

$('#cancel-task').onclick = async () => {
  if (!state.currentJob) return;
  $('#cancel-task').disabled = true;
  try {
    const result = await api(`/jobs/${state.currentJob}/cancel`, { method: 'POST', body: '{}' });
    report(result.cancelled ? 'Cancellation requested. Stopping at the next safe checkpoint…' : 'That task has already finished.');
  } catch (error) { report(error.message, true); }
};

function clampNumber(input) {
  const value = Number(input.value);
  const bounded = Math.max(Number(input.min), Math.min(Number(input.max), Number.isFinite(value) ? value : Number(input.min)));
  input.value = String(Math.round(bounded));
  return Math.round(bounded);
}

function drawSeeds() {
  const root = $('#seeds');
  root.replaceChildren();
  state.seeds.forEach((seed, index) => {
    const row = document.createElement('div');
    row.className = 'seed';
    const copy = document.createElement('span');
    copy.textContent = seed.kind.toUpperCase();
    const value = document.createElement('small');
    value.textContent = seed.displayName || seed.value;
    value.title = seed.value;
    copy.append(value);
    const remove = document.createElement('button');
    remove.className = 'button';
    remove.type = 'button';
    remove.setAttribute('aria-label', `Remove ${seed.displayName || seed.value}`);
    remove.textContent = '×';
    remove.onclick = () => { state.seeds.splice(index, 1); drawSeeds(); };
    row.append(copy, remove);
    root.append(row);
  });
  $('#discover').disabled = !state.seeds.length;
}

function selectSeedKind(kind) {
  state.seedKind = kind;
  document.querySelectorAll('.seed-types button').forEach(button => button.classList.toggle('active', button.dataset.kind === kind));
  const input = $('#seed-value');
  const add = $('#add-seed');
  const configurations = {
    manual: ['Artist - Title', 'Solarstone - Seven Cities', 'Add', 'Add an artist and title exactly as you know them.'],
    spotify: ['Public Spotify track or playlist URL', 'https://open.spotify.com/track/…', 'Add', 'Spotify supplies metadata and links only; CratePilot never downloads Spotify audio.'],
    local: ['Music file', 'Opens in your selected music library', 'Browse', 'Choose any supported audio file. Files from elsewhere are copied into your library without changing the original.'],
  };
  const [label, placeholder, action, help] = configurations[kind];
  $('#seed-label').textContent = label;
  input.placeholder = placeholder;
  input.hidden = kind === 'local';
  add.textContent = action;
  $('#seed-help').textContent = help;
}

async function addSeed() {
  const input = $('#seed-value');
  if (state.seedKind === 'local') {
    const button = $('#add-seed');
    button.disabled = true;
    button.textContent = 'Opening…';
    report('Choose an audio file. The picker starts in your selected library.');
    try {
      const result = await api('/library/browse', { method: 'POST', body: '{}' });
      if (result.cancelled) return report('No file selected. Nothing changed.');
      state.seeds.push({ kind: 'local', path: result.path, value: result.path, displayName: result.display_name });
      drawSeeds();
      report(result.copied_to_library ? `${result.display_name} was safely copied into the library and added.` : `${result.display_name} was added from the library.`);
    } catch (error) { report(error.message, true); }
    finally { button.disabled = false; button.textContent = 'Browse'; }
    return;
  }
  const value = input.value.trim();
  if (!value) return;
  if (state.seedKind === 'manual') {
    if (!value.includes(' - ')) return report('Use Artist - Title.', true);
    const [artist, ...title] = value.split(' - ');
    state.seeds.push({ kind: 'manual', artist, title: title.join(' - '), value });
  } else {
    state.seeds.push({ kind: 'spotify', url: value, value });
  }
  input.value = '';
  drawSeeds();
}

document.querySelectorAll('.seed-types button').forEach(button => button.onclick = () => selectSeedKind(button.dataset.kind));
$('#add-seed').onclick = addSeed;
$('#seed-value').addEventListener('keydown', event => { if (event.key === 'Enter') addSeed(); });

function visibleCandidates() {
  const limit = clampNumber($('#results'));
  const catalogById = new Map(state.catalog.map(track => [track.id, track]));
  return [...state.candidates]
    .sort((left, right) => {
      if (state.candidateSort.key === 'score') return state.candidateSort.direction * (left.total_score - right.total_score);
      const leftTrack = catalogById.get(left.catalog_track_id);
      const rightTrack = catalogById.get(right.catalog_track_id);
      const a = `${leftTrack?.artist || left.channel} ${leftTrack?.title || left.title}`.toLocaleLowerCase();
      const b = `${rightTrack?.artist || right.channel} ${rightTrack?.title || right.title}`.toLocaleLowerCase();
      return state.candidateSort.direction * a.localeCompare(b);
    })
    .slice(0, limit);
}

function updateSelectionSummary() {
  const visible = visibleCandidates();
  const selected = visible.filter(candidate => state.selectedCandidates.has(candidate.id)).length;
  $('#selected-count').textContent = `${selected} selected`;
  $('#visible-count').textContent = `${visible.length} shown`;
  $('#review-max').textContent = String(clampNumber($('#results')));
  $('#approve').disabled = selected === 0;
}

function safeExternalLink(url) {
  try {
    const parsed = new URL(url);
    return ['http:', 'https:'].includes(parsed.protocol) ? parsed.href : null;
  } catch { return null; }
}

function renderCandidates() {
  const root = $('#candidates');
  root.replaceChildren();
  const visible = visibleCandidates();
  if (!visible.length) {
    const empty = document.createElement('div');
    empty.className = 'empty';
    empty.textContent = state.candidates.length ? 'The panel is clear. Run discovery or raise the search result count to review candidates.' : 'No unowned candidates were ranked in this pass.';
    root.append(empty);
    return updateSelectionSummary();
  }
  const catalogById = new Map(state.catalog.map(track => [track.id, track]));
  visible.forEach(candidate => {
    const track = catalogById.get(candidate.catalog_track_id);
    const row = document.createElement('label');
    row.className = 'candidate';
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.value = candidate.id;
    checkbox.checked = state.selectedCandidates.has(candidate.id);
    checkbox.onchange = () => {
      if (checkbox.checked) state.selectedCandidates.add(candidate.id); else state.selectedCandidates.delete(candidate.id);
      updateSelectionSummary();
    };
    const copy = document.createElement('span');
    copy.className = 'candidate-copy';
    const title = document.createElement('strong');
    title.textContent = `${track?.artist || candidate.channel || 'Unknown artist'} — ${track?.title || candidate.title}`;
    title.title = title.textContent;
    const detail = document.createElement('small');
    const duration = candidate.duration_seconds ? `${Math.floor(candidate.duration_seconds / 60)}:${String(Math.round(candidate.duration_seconds % 60)).padStart(2, '0')}` : 'duration unknown';
    detail.textContent = `${candidate.title} · ${duration} · ${(candidate.explanation || []).slice(0, 2).join(' · ')}`;
    detail.title = detail.textContent;
    const links = document.createElement('span');
    links.className = 'legal';
    (candidate.legal_links || []).forEach(link => {
      const href = safeExternalLink(link.url);
      if (!href) return;
      const anchor = document.createElement('a');
      anchor.href = href;
      anchor.target = '_blank';
      anchor.rel = 'noreferrer';
      anchor.textContent = `${link.label} ↗`;
      anchor.onclick = event => event.stopPropagation();
      links.append(anchor);
    });
    copy.append(title, detail, links);
    const score = document.createElement('b');
    score.className = 'candidate-score';
    score.textContent = String(Math.round(candidate.total_score * 100));
    score.title = 'Mixability score';
    row.append(checkbox, copy, score);
    root.append(row);
  });
  updateSelectionSummary();
}

$('#select-all').onclick = () => { visibleCandidates().forEach(candidate => state.selectedCandidates.add(candidate.id)); renderCandidates(); };
$('#select-none').onclick = () => { visibleCandidates().forEach(candidate => state.selectedCandidates.delete(candidate.id)); renderCandidates(); };
$('#clear-panel').onclick = () => { state.candidates = []; state.selectedCandidates.clear(); state.approvedJob = null; $('#acquire').disabled = true; renderCandidates(); report('Review panel cleared. Run discovery to refill it.'); };
document.querySelectorAll('.candidate-head button').forEach(button => button.onclick = () => {
  const key = button.dataset.sort;
  state.candidateSort = { key, direction: state.candidateSort.key === key ? state.candidateSort.direction * -1 : key === 'score' ? -1 : 1 };
  $('#sort-track span').textContent = state.candidateSort.key === 'track' ? (state.candidateSort.direction > 0 ? '↑' : '↓') : '↕';
  $('#sort-score span').textContent = state.candidateSort.key === 'score' ? (state.candidateSort.direction > 0 ? '↑' : '↓') : '↕';
  renderCandidates();
});
$('#results').addEventListener('change', renderCandidates);
$('#results').addEventListener('input', updateSelectionSummary);

const svg = $('#map');
const svgNS = 'http://www.w3.org/2000/svg';
const createSvg = name => document.createElementNS(svgNS, name);
function fitGraph() { state.graphView = { x: 0, y: 0, width: 800, height: 440 }; applyGraphView(); }
function applyGraphView() { const view = state.graphView; svg.setAttribute('viewBox', `${view.x} ${view.y} ${view.width} ${view.height}`); }
function zoomGraph(factor) {
  const view = state.graphView;
  const newWidth = Math.max(280, Math.min(1500, view.width * factor));
  const newHeight = newWidth * 0.55;
  view.x += (view.width - newWidth) / 2; view.y += (view.height - newHeight) / 2;
  view.width = newWidth; view.height = newHeight; applyGraphView();
}
$('#zoom-in').onclick = () => zoomGraph(0.8);
$('#zoom-out').onclick = () => zoomGraph(1.25);
$('#graph-fit').onclick = fitGraph;

function initializeGraphPositions(tracks) {
  tracks.forEach((track, index) => {
    if (state.graphPositions.has(track.id)) return;
    const angle = index * 2.399963;
    const radius = index ? 55 + Math.sqrt(index) * 47 : 0;
    state.graphPositions.set(track.id, { x: 400 + Math.cos(angle) * radius, y: 220 + Math.sin(angle) * radius * 0.62 });
  });
}

function connectedIds(trackId) {
  const connected = new Set([trackId]);
  state.edges.forEach(edge => {
    if (edge.source_track_id === trackId) connected.add(edge.target_track_id);
    if (edge.target_track_id === trackId) connected.add(edge.source_track_id);
  });
  return connected;
}

function focusNode(trackId) {
  state.selectedNode = trackId;
  const track = state.catalog.find(item => item.id === trackId);
  const related = state.edges.filter(edge => edge.source_track_id === trackId || edge.target_track_id === trackId);
  $('#graph-detail').innerHTML = track
    ? `<strong>${escapeText(track.artist)} — ${escapeText(track.title)}</strong><br>${related.length} evidence connection${related.length === 1 ? '' : 's'} · ${escapeText(track.verification_state || 'unverified')} · click another node to compare`
    : 'Lime nodes are seeds. Cyan nodes are already owned.';
  const connected = trackId ? connectedIds(trackId) : new Set();
  svg.querySelectorAll('.graph-node').forEach(node => {
    node.classList.toggle('selected', node.dataset.id === trackId);
    node.classList.toggle('dimmed', Boolean(trackId) && !connected.has(node.dataset.id));
  });
  svg.querySelectorAll('.graph-edge').forEach(edge => edge.classList.toggle('dimmed', Boolean(trackId) && edge.dataset.source !== trackId && edge.dataset.target !== trackId));
  updateSignalTitle();
}

function escapeText(value) {
  const span = document.createElement('span');
  span.textContent = value || '';
  return span.innerHTML;
}

function updateGraphGeometry() {
  svg.querySelectorAll('.graph-edge').forEach(line => {
    const source = state.graphPositions.get(line.dataset.source);
    const target = state.graphPositions.get(line.dataset.target);
    if (!source || !target) return;
    line.setAttribute('x1', source.x); line.setAttribute('y1', source.y);
    line.setAttribute('x2', target.x); line.setAttribute('y2', target.y);
  });
  svg.querySelectorAll('.graph-node').forEach(node => {
    const point = state.graphPositions.get(node.dataset.id);
    if (point) node.setAttribute('transform', `translate(${point.x} ${point.y})`);
  });
}

function renderMap() {
  svg.replaceChildren();
  const tracks = state.catalog.slice(0, 60);
  $('#map-empty').hidden = tracks.length > 0;
  initializeGraphPositions(tracks);
  const shown = new Set(tracks.map(track => track.id));
  const edgeLayer = createSvg('g');
  state.edges.filter(edge => shown.has(edge.source_track_id) && shown.has(edge.target_track_id)).forEach(edge => {
    const line = createSvg('line');
    line.classList.add('graph-edge');
    line.dataset.source = edge.source_track_id; line.dataset.target = edge.target_track_id;
    line.dataset.relationship = edge.relationship;
    edgeLayer.append(line);
  });
  const nodeLayer = createSvg('g');
  tracks.forEach((track, index) => {
    const node = createSvg('g');
    node.classList.add('graph-node');
    if (index < state.seeds.length) node.classList.add('seed');
    if (track.verification_state === 'verified') node.classList.add('owned');
    node.dataset.id = track.id;
    node.setAttribute('role', 'button'); node.setAttribute('tabindex', '0');
    node.setAttribute('aria-label', `${track.artist} — ${track.title}`);
    const circle = createSvg('circle'); circle.setAttribute('r', '35');
    const title = createSvg('text'); title.setAttribute('y', '-2'); title.textContent = track.title.length > 14 ? `${track.title.slice(0, 13)}…` : track.title;
    const artist = createSvg('text'); artist.classList.add('node-score'); artist.setAttribute('y', '12'); artist.textContent = track.artist.length > 14 ? `${track.artist.slice(0, 13)}…` : track.artist;
    node.append(circle, title, artist);
    node.onclick = () => focusNode(track.id);
    node.onkeydown = event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); focusNode(track.id); } };
    nodeLayer.append(node);
  });
  svg.append(edgeLayer, nodeLayer);
  updateGraphGeometry(); focusNode(state.selectedNode && shown.has(state.selectedNode) ? state.selectedNode : null);
  $('#node-count').textContent = state.catalog.length;
  $('#edge-count').textContent = state.edges.length;
  $('#duplicate-count').textContent = Math.max(0, state.edges.length + state.seeds.length - state.catalog.length);
}

function svgPoint(event) {
  const point = svg.createSVGPoint(); point.x = event.clientX; point.y = event.clientY;
  return point.matrixTransform(svg.getScreenCTM().inverse());
}
let graphGesture = null;
svg.addEventListener('pointerdown', event => {
  const node = event.target.closest('.graph-node');
  const point = svgPoint(event);
  graphGesture = node ? { type: 'node', id: node.dataset.id, offset: { x: point.x - state.graphPositions.get(node.dataset.id).x, y: point.y - state.graphPositions.get(node.dataset.id).y } } : { type: 'pan', point, origin: { ...state.graphView } };
  svg.setPointerCapture(event.pointerId);
});
svg.addEventListener('pointermove', event => {
  if (!graphGesture) return;
  const point = svgPoint(event);
  if (graphGesture.type === 'node') {
    state.graphPositions.set(graphGesture.id, { x: point.x - graphGesture.offset.x, y: point.y - graphGesture.offset.y });
    updateGraphGeometry();
  } else {
    state.graphView.x = graphGesture.origin.x - (point.x - graphGesture.point.x);
    state.graphView.y = graphGesture.origin.y - (point.y - graphGesture.point.y);
    applyGraphView();
  }
});
svg.addEventListener('pointerup', event => { graphGesture = null; svg.releasePointerCapture(event.pointerId); });
svg.addEventListener('pointercancel', () => { graphGesture = null; });
svg.addEventListener('wheel', event => { event.preventDefault(); zoomGraph(event.deltaY > 0 ? 1.12 : 0.89); }, { passive: false });

$('#discover').onclick = async () => {
  try {
    const resultCount = clampNumber($('#results'));
    report('Creating a discovery session…');
    const created = await api('/discovery/sessions', { method: 'POST', body: JSON.stringify({ seeds: state.seeds, max_depth: clampNumber($('#depth')), max_nodes: clampNumber($('#nodes')), readiness_target: clampNumber($('#target')), result_count: resultCount, review_batch_size: resultCount }) });
    const queued = await api(`/discovery/sessions/${created.session.id}/run`, { method: 'POST' });
    const result = await wait(queued.job_id);
    const graph = await api('/catalog');
    state.catalog = graph.tracks; state.edges = graph.edges;
    state.candidates = (await api('/acquisition/candidates')).candidates;
    state.selectedCandidates.clear();
    $('#readiness').textContent = `${result.session.ready_plan_ids.length} / ${result.session.readiness_target}`;
    renderMap(); renderCandidates();
    const warning = result.session.warnings?.at(-1);
    report(result.session.status === 'ready' ? 'Strict readiness reached. Review sources before acquisition.' : warning || 'Discovery finished. Review the ranked candidates.');
  } catch (error) { report(error.message, true); }
};

$('#ack').onchange = async event => {
  try {
    const result = await api('/acquisition/acknowledgement', { method: 'PUT', body: JSON.stringify({ accepted: event.target.checked }) });
    report(result.accepted ? 'Permissive acquisition enabled locally.' : 'Permissive acquisition revoked.');
  } catch (error) { event.target.checked = !event.target.checked; report(error.message, true); }
};
$('#approve').onclick = async () => {
  const ids = visibleCandidates().filter(candidate => state.selectedCandidates.has(candidate.id)).map(candidate => candidate.id);
  try {
    state.approvedJob = (await api('/acquisition/jobs', { method: 'POST', body: JSON.stringify({ candidate_ids: ids }) })).job;
    $('#acquire').disabled = false;
    report(`Approved ${ids.length} candidates. No download started yet.`);
  } catch (error) { report(error.message, true); }
};
$('#acquire').onclick = async () => {
  try {
    const queued = await api(`/acquisition/jobs/${state.approvedJob.id}/run`, { method: 'POST' });
    const result = await wait(queued.job_id);
    report(result.acquisition.message);
  } catch (error) { report(error.message, true); }
};

$('#analyze').onclick = async () => {
  try {
    const queued = await api('/library/analyze', { method: 'POST', body: '{}' });
    const result = await wait(queued.job_id); state.tracks = result.tracks;
    report(`Analyzed ${state.tracks.length} tracks.`);
  } catch (error) { report(error.message, true); }
};

$('#browse-playlist').onclick = async () => {
  const button = $('#browse-playlist'); button.disabled = true; button.textContent = 'Opening…';
  try {
    const result = await api('/library/browse-playlist', { method: 'POST', body: '{}' });
    if (result.cancelled) return report('No playlist selected. The full library remains active.');
    state.playlistTrackIds = result.track_ids;
    $('#playlist-name').textContent = result.display_name;
    $('#playlist-match').textContent = `${result.matched_count.toLocaleString()} analyzed tracks matched ${result.entry_count.toLocaleString()} in-library entries.`;
    $('#clear-playlist').disabled = false;
    report(`${result.display_name} will filter the next planning run to ${result.matched_count.toLocaleString()} analyzed tracks.`);
  } catch (error) { report(error.message, true); }
  finally { button.disabled = false; button.textContent = 'Browse M3U'; }
};
$('#clear-playlist').onclick = () => {
  state.playlistTrackIds = null; $('#playlist-name').textContent = 'Entire analyzed library';
  $('#playlist-match').textContent = 'Use an M3U playlist to narrow a large library before restarting.';
  $('#clear-playlist').disabled = true; report('Playlist filter cleared. The next draft run will use the full analyzed library.');
};
function inspect(index) {
  const transition = state.plan.transitions[index];
  const outgoing = state.tracks.find(track => track.id === transition.source_track_id);
  const incoming = state.tracks.find(track => track.id === transition.target_track_id);
  $('#score').textContent = Math.round(transition.total_score * 100);
  $('#transition-title').textContent = `${outgoing.title} → ${incoming.title}`;
  $('#note').textContent = transition.warning || transition.explanation.join(' · ');
}
function renderPlan() {
  $('#plan-title').textContent = `${state.plan.title} · ${(state.plan.duration_seconds / 60).toFixed(1)} min`;
  const root = $('#tracks'); root.className = 'tracks'; root.replaceChildren();
  state.plan.track_ids.forEach((id, index) => {
    const track = state.tracks.find(item => item.id === id);
    if (index) {
      const edge = document.createElement('button'); edge.className = 'edge'; edge.textContent = `↳ ${Math.round(state.plan.transitions[index - 1].total_score * 100)} match · inspect`; edge.onclick = () => inspect(index - 1); root.append(edge);
    }
    const row = document.createElement('article'); row.className = 'track';
    const number = document.createElement('b'); number.textContent = String(index + 1);
    const copy = document.createElement('span'); const title = document.createElement('strong'); title.textContent = track.title; const artist = document.createElement('small'); artist.textContent = track.artist; copy.append(title, artist);
    const metadata = document.createElement('small'); metadata.textContent = `${track.bpm.toFixed(1)} BPM · ${track.camelot}`;
    row.append(number, copy, metadata); root.append(row);
  });
  $('#export').disabled = false;
  if (state.plan.transitions.length) inspect(0);
}
$('#generate').onclick = async () => {
  try {
    if (!state.tracks.length) state.tracks = (await api('/library')).tracks;
    const queued = await api('/plans/generate', { method: 'POST', body: JSON.stringify({ target_minutes: 45, count: 8, track_ids: state.playlistTrackIds || [] }) });
    const result = await wait(queued.job_id);
    state.plan = result.plans[0]; renderPlan(); report(`Built ${result.plans.length} ranked drafts.`);
  } catch (error) { report(error.message, true); }
};
$('#export').onclick = async () => {
  if (!$('#output').value) return report('Choose an output folder.', true);
  try {
    const queued = await api(`/plans/${state.plan.id}/export`, { method: 'POST', body: JSON.stringify({ output_directory: $('#output').value, render_reference: true }) });
    await wait(queued.job_id); report(`Rekordbox package complete: ${$('#output').value}`);
  } catch (error) { report(error.message, true); }
};

const modal = $('#visualizer');
const canvas = modal.querySelector('canvas');
const context = canvas.getContext('2d');
const signal = { animation: 0, width: 0, height: 0, pointerX: .5, pointerY: .5, pulse: 0, scene: 0, lastFrame: 0 };
const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;
function resizeSignal() {
  const ratio = Math.min(devicePixelRatio || 1, 2);
  signal.width = innerWidth; signal.height = innerHeight;
  canvas.width = Math.round(signal.width * ratio); canvas.height = Math.round(signal.height * ratio);
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
}
function updateSignalTitle() {
  const track = state.catalog.find(item => item.id === state.selectedNode);
  $('#signal-title').textContent = track ? `${track.artist} — ${track.title}` : `${state.catalog.length} tracks · ${state.edges.length} evidence connections`;
}
function signalFrame(time) {
  if (!modal.classList.contains('open')) return;
  if (reducedMotion && time - signal.lastFrame < 120) { signal.animation = requestAnimationFrame(signalFrame); return; }
  signal.lastFrame = time;
  const width = signal.width, height = signal.height;
  context.fillStyle = reducedMotion ? '#020503' : 'rgba(2,5,3,.16)'; context.fillRect(0, 0, width, height);
  const centerX = width * (.35 + signal.pointerX * .3), centerY = height * (.35 + signal.pointerY * .3);
  const scores = visibleCandidates().map(item => item.total_score);
  const average = scores.length ? scores.reduce((sum, value) => sum + value, 0) / scores.length : .55;
  const bars = Math.max(32, Math.min(120, state.catalog.length * 3 || 48));
  signal.pulse *= .94;
  for (let index = 0; index < bars; index++) {
    const phase = index / bars * Math.PI * 2;
    const dataWave = Math.sin(index * .73 + state.edges.length * .21) * .5 + .5;
    const motion = Math.sin(time * (.0008 + signal.scene * .00022) + index * .37) * .5 + .5;
    const radius = Math.min(width, height) * (.13 + .12 * motion + .1 * average + signal.pulse * .08);
    const length = 18 + 95 * dataWave * (.35 + motion * .65);
    const angle = phase + time * .00008 * (signal.scene % 2 ? -1 : 1);
    const x1 = centerX + Math.cos(angle) * radius, y1 = centerY + Math.sin(angle) * radius;
    const x2 = centerX + Math.cos(angle) * (radius + length), y2 = centerY + Math.sin(angle) * (radius + length);
    context.strokeStyle = index % 4 ? `rgba(112,232,221,${.13 + motion * .38})` : `rgba(200,250,99,${.25 + motion * .55})`;
    context.lineWidth = index % 4 ? 1 : 1.6; context.beginPath(); context.moveTo(x1, y1); context.lineTo(x2, y2); context.stroke();
  }
  const rings = Math.max(3, Math.min(9, Math.ceil((state.edges.length || 6) / 4)));
  for (let ring = 0; ring < rings; ring++) {
    const radius = (time * (.018 + ring * .004) + ring * 83) % (Math.min(width, height) * .42);
    context.strokeStyle = `rgba(${ring % 2 ? '112,232,221' : '200,250,99'},${.28 * (1 - radius / (Math.min(width, height) * .42))})`;
    context.beginPath(); context.arc(centerX, centerY, radius + signal.pulse * 30, 0, Math.PI * 2); context.stroke();
  }
  signal.animation = requestAnimationFrame(signalFrame);
}
function openSignal() { modal.classList.add('open'); resizeSignal(); updateSignalTitle(); signal.pulse = 1; cancelAnimationFrame(signal.animation); signal.animation = requestAnimationFrame(signalFrame); $('#close-visualizer').focus(); }
function closeSignal() { modal.classList.remove('open'); cancelAnimationFrame(signal.animation); $('#visualize').focus(); }
$('#visualize').onclick = openSignal;
$('#close-visualizer').onclick = closeSignal;
canvas.addEventListener('pointermove', event => { signal.pointerX = event.clientX / signal.width; signal.pointerY = event.clientY / signal.height; });
canvas.addEventListener('pointerdown', () => { signal.pulse = 1; });
addEventListener('resize', () => { if (modal.classList.contains('open')) resizeSignal(); });
addEventListener('keydown', event => {
  if (!modal.classList.contains('open')) return;
  if (event.key === 'Escape') closeSignal();
  if (event.code === 'Space') { event.preventDefault(); signal.scene = (signal.scene + 1) % 3; signal.pulse = 1; $('#signal-detail').textContent = `Scene ${signal.scene + 1} · pointer steers · click pulses · Space changes scene`; }
});

async function initialize() {
  selectSeedKind('manual'); updateSelectionSummary();
  try {
    const [graph, library, candidateResult, acknowledgement] = await Promise.all([api('/catalog'), api('/library'), api('/acquisition/candidates'), api('/acquisition/acknowledgement')]);
    state.catalog = graph.tracks; state.edges = graph.edges; state.tracks = library.tracks; state.candidates = candidateResult.candidates;
    $('#ack').checked = acknowledgement.accepted;
    renderMap(); renderCandidates(); updateSignalTitle();
  } catch (error) { report(error.message, true); }
}
initialize();
