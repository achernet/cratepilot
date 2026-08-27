'use client';

import Link from 'next/link';
import { CSSProperties, DragEvent, PointerEvent as ReactPointerEvent, useMemo, useRef, useState } from 'react';
import { scoreTransition } from './lib/scorer';

type Track = { id: string; artist: string; title: string; bpm: number; key: string; energy: number };
type DiscoveryNode = Track & { relation: string; owned?: boolean; score: number };

const catalog: Track[] = [
  { id: 'eternal-light', artist: 'Soundwave Sphere', title: 'Eternal Light', bpm: 128, key: '8A', energy: 41 },
  { id: 'liberation', artist: 'Kraftamt', title: 'Liberation', bpm: 131, key: '9A', energy: 59 },
  { id: 'seven', artist: 'bwatts', title: 'Seven (New Music Remix)', bpm: 135, key: '10A', energy: 82 },
  { id: 'ready', artist: 'Analog By Nature', title: 'Are You Ready', bpm: 130, key: '8A', energy: 52 },
  { id: 'language', artist: 'Even After', title: 'Universal Language', bpm: 137, key: '11A', energy: 91 },
  { id: 'summer-house', artist: 'Robbero', title: 'Summer House', bpm: 130, key: '10A', energy: 70 },
];

const drafts = [
  ['eternal-light', 'ready', 'liberation', 'summer-house', 'language', 'seven'],
  ['ready', 'eternal-light', 'liberation', 'seven', 'language', 'summer-house'],
  ['eternal-light', 'liberation', 'ready', 'summer-house', 'seven', 'language'],
];

const discoveryFixture: DiscoveryNode[] = catalog.map((track, index) => ({
  ...track,
  relation: index === 0 ? 'seed' : index < 3 ? 'same-artist evidence' : 'similar transition neighborhood',
  owned: index === 0,
  score: [100, 94, 91, 88, 86, 82][index],
}));
const fixturePositions = [[44, 42], [70, 22], [73, 69], [20, 24], [19, 72], [46, 80]];

function keyFrequency(key: string) {
  return 98 * 2 ** ((Number.parseInt(key, 10) - 1) / 12);
}

function makePulse(context: AudioContext, destination: AudioNode, start: number, end: number, bpm: number, pitch: number) {
  const step = 60 / bpm;
  for (let time = start; time < end; time += step) {
    const oscillator = context.createOscillator();
    const gain = context.createGain();
    oscillator.type = 'sine';
    oscillator.frequency.setValueAtTime(pitch, time);
    oscillator.frequency.exponentialRampToValueAtTime(46, time + .09);
    gain.gain.setValueAtTime(.0001, time);
    gain.gain.exponentialRampToValueAtTime(.42, time + .006);
    gain.gain.exponentialRampToValueAtTime(.0001, time + .15);
    oscillator.connect(gain).connect(destination);
    oscillator.start(time);
    oscillator.stop(time + .17);
  }
}

function auditionTransition(a: Track, b: Track, onEnd: () => void) {
  const AudioContextClass = window.AudioContext || (window as typeof window & { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
  const context = new AudioContextClass();
  const now = context.currentTime + .08;
  const duration = 9;
  const master = context.createGain();
  const compressor = context.createDynamicsCompressor();
  compressor.threshold.value = -10;
  compressor.ratio.value = 8;
  master.gain.value = .42;
  master.connect(compressor).connect(context.destination);
  const outgoing = context.createGain();
  const incoming = context.createGain();
  const filter = context.createBiquadFilter();
  filter.type = 'lowpass';
  filter.frequency.setValueAtTime(160, now + 2.3);
  filter.frequency.exponentialRampToValueAtTime(8200, now + 6.8);
  outgoing.gain.setValueAtTime(.9, now);
  outgoing.gain.linearRampToValueAtTime(.02, now + duration);
  incoming.gain.setValueAtTime(.001, now);
  incoming.gain.linearRampToValueAtTime(.94, now + 6.3);
  outgoing.connect(master);
  incoming.connect(filter).connect(master);
  makePulse(context, outgoing, now, now + duration, a.bpm, 72);
  makePulse(context, incoming, now + 2.3, now + duration, b.bpm, 84);
  for (const [track, gain, start] of [[a, outgoing, now], [b, incoming, now + 2.3]] as const) {
    const oscillator = context.createOscillator();
    oscillator.type = 'sawtooth';
    oscillator.frequency.value = keyFrequency(track.key);
    const bed = context.createGain();
    bed.gain.value = .075;
    oscillator.connect(bed).connect(gain);
    oscillator.start(start);
    oscillator.stop(now + duration);
  }
  window.setTimeout(() => { void context.close(); onEnd(); }, duration * 1000 + 250);
}

export default function Home() {
  const [order, setOrder] = useState<string[]>(drafts[0]);
  const [locks, setLocks] = useState<Set<string>>(new Set());
  const [selected, setSelected] = useState(1);
  const [draft, setDraft] = useState(0);
  const [history, setHistory] = useState<string[][]>([]);
  const [playing, setPlaying] = useState(false);
  const [notice, setNotice] = useState('Draft 1 of 3 · optimized for a confident rise');
  const [discovered, setDiscovered] = useState(1);
  const [reviewed, setReviewed] = useState<Set<string>>(new Set());
  const [crateBuilt, setCrateBuilt] = useState(false);
  const [visualizer, setVisualizer] = useState(false);
  const [graphFocus, setGraphFocus] = useState('eternal-light');
  const [reviewSort, setReviewSort] = useState<'track' | 'score'>('score');
  const [visualizerScene, setVisualizerScene] = useState(0);
  const [signalPointer, setSignalPointer] = useState({ x: 50, y: 50 });
  const dragIndex = useRef<number | null>(null);

  const tracks = useMemo(() => order.map(id => catalog.find(track => track.id === id)!), [order]);
  const transitions = useMemo(() => tracks.slice(0, -1).map((track, index) => scoreTransition(track, tracks[index + 1])), [tracks]);
  const activeIndex = Math.min(selected, transitions.length - 1);
  const active = transitions[activeIndex];
  const outgoing = tracks[activeIndex];
  const incoming = tracks[activeIndex + 1];
  const focusedGraphTrack = discoveryFixture.find(node => node.id === graphFocus) || discoveryFixture[0];
  const visibleReview = useMemo(() => [...discoveryFixture.slice(1, discovered)].sort((a, b) => reviewSort === 'score' ? b.score - a.score : `${a.artist} ${a.title}`.localeCompare(`${b.artist} ${b.title}`)), [discovered, reviewSort]);

  const commit = (next: string[], message: string) => {
    setHistory(previous => [...previous.slice(-9), order]);
    setOrder(next);
    window.localStorage.setItem('cratepilot-demo-plan', JSON.stringify(next));
    setNotice(message);
  };

  const generate = () => {
    const nextDraft = (draft + 1) % drafts.length;
    const candidate = [...drafts[nextDraft]];
    locks.forEach(id => {
      const currentPosition = order.indexOf(id);
      const candidatePosition = candidate.indexOf(id);
      [candidate[currentPosition], candidate[candidatePosition]] = [candidate[candidatePosition], candidate[currentPosition]];
    });
    setDraft(nextDraft);
    commit(candidate, `Draft ${nextDraft + 1} of 3 · locked tracks stayed in place`);
  };

  const replace = (index: number) => {
    if (locks.has(order[index])) { setNotice('Unlock this track before replacing it.'); return; }
    const next = [...order];
    const swapIndex = (index + 2) % order.length;
    if (locks.has(order[swapIndex])) { setNotice('That replacement would move a locked track.'); return; }
    [next[index], next[swapIndex]] = [next[swapIndex], next[index]];
    commit(next, 'Replacement applied · adjacent transitions rescored');
    setSelected(Math.max(0, Math.min(index, order.length - 2)));
  };

  const drop = (event: DragEvent, targetIndex: number) => {
    event.preventDefault();
    const sourceIndex = dragIndex.current;
    if (sourceIndex === null || sourceIndex === targetIndex) return;
    if (locks.has(order[sourceIndex]) || locks.has(order[targetIndex])) { setNotice('Locked tracks keep their position.'); return; }
    const next = [...order];
    const [moved] = next.splice(sourceIndex, 1);
    next.splice(targetIndex, 0, moved);
    commit(next, 'Manual order accepted · weak transitions stay visible as warnings');
    setSelected(Math.max(0, Math.min(targetIndex, order.length - 2)));
  };

  const audition = () => {
    if (playing) return;
    setPlaying(true);
    auditionTransition(outgoing, incoming, () => setPlaying(false));
  };

  return (
    <main className="site-shell">
      <header className="topbar">
        <a className="brand" href="#top" aria-label="CratePilot home"><span className="brand-mark">CP</span><span>CratePilot</span></a>
        <nav aria-label="Primary navigation"><a href="#discovery">Discovery</a><a href="#planner">Planner</a><Link href="/case-study">Case study</Link><a className="nav-cta" href="#local">Run locally</a></nav>
      </header>

      <section className="hero" id="top">
        <div>
          <p className="eyebrow"><span /> Example-based transition intelligence</p>
          <h1>Build a set you can trust<br />before you enter the booth.</h1>
          <p className="hero-copy">CratePilot turns tempo, key, energy, and patterns learned from real DJ sets into an editable plan you can hear, understand, and take to Rekordbox.</p>
          <div className="hero-actions"><a className="button button-primary" href="#discovery">Discover a demo crate</a><Link className="button button-quiet" href="/case-study">Read the engineering story <span>↗</span></Link></div>
        </div>
        <aside className="proof-card" aria-label="Project proof points">
          <p className="proof-label">Built from the signal up</p>
          <dl><div><dt>48</dt><dd>curated transitions</dd></div><div><dt>4</dt><dd>reference sets</dd></div><div><dt>99</dt><dd>passing workshop tests</dd></div></dl>
          <p>DSP heuristics + learned nearest-neighbor preferences. No black-box “AI DJ” claims.</p>
        </aside>
      </section>

      <section className="discovery-demo" id="discovery">
        <div className="section-heading">
          <div><p className="eyebrow"><span /> V2 · Sanitized interactive fixture</p><h2>One seed. A library with reasons.</h2></div>
          <div className="readiness-badge"><small>Demo readiness</small><b>{crateBuilt ? '3 / 3' : `${Math.min(2, Math.floor(discovered / 2))} / 3`}</b><span>short representative drafts</span></div>
        </div>
        <p className="discovery-intro">This recruiter-safe simulation uses only the credited catalog below. Local mode can resolve Spotify metadata, recognize your own files, search two graph hops, and prepare a reviewed acquisition batch; the public site makes no provider calls and downloads nothing.</p>
        <div className="discovery-grid">
          <div className="seed-console"><div className="panel-label"><span>01 · Seed</span><b>Owned track</b></div><article className="seed-card"><span className="signal-dot" /><div><strong>Eternal Light</strong><small>Soundwave Sphere · local analysis</small></div><span className="key-pill">8A</span></article><dl><div><dt>2</dt><dd>graph hops</dd></div><div><dt>150</dt><dd>node cap</dd></div><div><dt>30</dt><dd>review max</dd></div></dl><button className="button button-primary" type="button" disabled={discovered === discoveryFixture.length} onClick={() => setDiscovered(value => Math.min(discoveryFixture.length, value + 2))}>{discovered === 1 ? 'Expand related tracks' : discovered < discoveryFixture.length ? 'Continue discovery' : 'Discovery limits reached'}</button></div>
          <div className="graph-console"><div className="panel-label"><span>02 · Provenance graph</span><b>{discovered} canonical nodes</b></div><div className="fixture-graph" aria-label="Interactive track relationship graph"><svg className="fixture-edges" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">{discoveryFixture.slice(1, discovered).map((node, index) => <line key={node.id} x1={fixturePositions[0][0]} y1={fixturePositions[0][1]} x2={fixturePositions[index + 1][0]} y2={fixturePositions[index + 1][1]} className={graphFocus === node.id || graphFocus === 'eternal-light' ? 'active' : ''} />)}</svg>{discoveryFixture.slice(0, discovered).map((node, index) => <button key={node.id} className={`graph-node node-${index} ${node.owned ? 'owned' : ''} ${graphFocus === node.id ? 'is-focused' : ''} ${graphFocus !== node.id && graphFocus !== 'eternal-light' && index !== 0 ? 'is-dimmed' : ''}`} title={`${node.artist} · ${node.relation}`} type="button" onClick={() => setGraphFocus(node.id)}><span>{node.title}</span><small>{node.score}%</small></button>)}</div><div className="graph-inspector"><strong>{focusedGraphTrack.artist} — {focusedGraphTrack.title}</strong><small>{focusedGraphTrack.relation} · {focusedGraphTrack.bpm} BPM · {focusedGraphTrack.key} · score {focusedGraphTrack.score}</small></div><div className="graph-legend"><span><i className="lime" /> seed</span><span><i className="cyan" /> already owned</span><span><i /> click a node to inspect</span></div></div>
          <aside className="review-console"><div className="panel-label"><span>03 · Best mixable versions</span><b>{reviewed.size} selected</b></div><div className="review-tools"><button type="button" onClick={() => setReviewed(new Set(visibleReview.map(node => node.id)))}>Select all</button><button type="button" onClick={() => setReviewed(new Set())}>Clear selection</button><button type="button" onClick={() => { setReviewed(new Set()); setDiscovered(1); }}>Clear panel</button></div><div className="review-head"><button type="button" onClick={() => setReviewSort('track')}>Artist / version {reviewSort === 'track' ? '↑' : '↕'}</button><button type="button" onClick={() => setReviewSort('score')}>Score {reviewSort === 'score' ? '↓' : '↕'}</button></div><div className="review-scroll">{visibleReview.map(node => <label className="review-row" key={node.id}><input type="checkbox" checked={reviewed.has(node.id)} onChange={() => setReviewed(current => { const next = new Set(current); if (next.has(node.id)) next.delete(node.id); else next.add(node.id); return next; })} /><div><strong>{node.artist} — {node.title}</strong><small>{node.relation} · {node.bpm} BPM · {node.key}</small></div><b>{node.score}</b></label>)}{discovered === 1 && <div className="review-empty">Expand the graph to rank potential crate additions.</div>}</div><button className="button button-primary" disabled={reviewed.size < 3} type="button" onClick={() => { setCrateBuilt(true); setNotice('Smart crate materialized · 3 distinct short demo drafts ready'); }}>Build smart crate</button><button className="button button-quiet visualizer-button" type="button" onClick={() => setVisualizer(true)}>Open live crate signal</button></aside>
        </div>
      </section>

      <section className="planner" id="planner">
        <div className="section-heading">
          <div><p className="eyebrow"><span /> First Booth · Demo Cut</p><h2>Your set, with reasons.</h2></div>
          <div className="planner-actions"><button className="button button-quiet" type="button" disabled={!history.length} onClick={() => { const previous = history.at(-1); if (previous) { setOrder(previous); setHistory(items => items.slice(0, -1)); setNotice('Last edit undone.'); } }}>Undo</button><button className="button button-primary" type="button" onClick={generate}>Build my demo set</button></div>
        </div>
        <div className="status-line" role="status"><span className="status-dot" />{notice}</div>

        <div className="planner-grid">
          <aside className="library-panel">
            <div className="panel-label"><span>Demo library</span><b>6 CC BY tracks</b></div>
            {catalog.map((track, index) => <article className="library-track" key={track.id}><span className="track-index">{String(index + 1).padStart(2, '0')}</span><div><strong>{track.title}</strong><small>{track.artist}</small></div><span className="track-meta">{track.bpm}<small>BPM</small></span><span className="key-pill">{track.key}</span></article>)}
            <a className="library-more" href="#credits">View licenses and credits <span>↓</span></a>
          </aside>

          <div className="set-panel">
            <div className="panel-label"><span>Energy path</span><b>Confident → peak → controlled close</b></div>
            <div className="energy-chart" aria-label="Set energy rises to a late peak">
              <svg viewBox="0 0 100 40" preserveAspectRatio="none" aria-hidden="true"><polyline points={tracks.map((track, index) => `${index * 20},${39 - track.energy * .36}`).join(' ')} /></svg>
              {tracks.map((track, index) => <span key={track.id} style={{ left: `${index * 20}%`, bottom: `${track.energy * .8}%` }} />)}
            </div>
            <div className="set-list">
              {tracks.map((track, index) => {
                const warning = index > 0 && transitions[index - 1].total < 78;
                return <div key={track.id}>
                  {index > 0 && <button className={`transition-row ${warning ? 'transition-warning' : ''}`} type="button" onClick={() => setSelected(index - 1)} aria-label={`Inspect transition into ${track.title}`}><span>{warning ? '△' : '↳'}</span><b>{transitions[index - 1].total}</b> {warning ? 'needs attention' : 'compatible'} · {transitions[index - 1].energyDelta >= 0 ? '+' : ''}{transitions[index - 1].energyDelta} energy</button>}
                  <article className={`set-track ${locks.has(track.id) ? 'is-locked' : ''}`} draggable={!locks.has(track.id)} onDragStart={() => { dragIndex.current = index; }} onDragOver={event => event.preventDefault()} onDrop={event => drop(event, index)}>
                    <span className="drag" aria-hidden="true">⠿</span><span className="set-number">{index + 1}</span>
                    <div><strong>{track.title}</strong><small>{track.artist} · {track.bpm} BPM · {track.key}</small></div><span className="energy-pill">Energy {track.energy}</span>
                    <div className="track-actions"><button aria-label={`Replace ${track.title}`} title="Replace" type="button" onClick={() => replace(index)}>↻</button><button aria-pressed={locks.has(track.id)} aria-label={`${locks.has(track.id) ? 'Unlock' : 'Lock'} ${track.title}`} title="Lock position" type="button" onClick={() => setLocks(current => { const next = new Set(current); if (next.has(track.id)) next.delete(track.id); else next.add(track.id); return next; })}>{locks.has(track.id) ? '◆' : '◇'}</button></div>
                  </article>
                </div>;
              })}
            </div>
          </div>

          <aside className="transition-panel">
            <div className="panel-label"><span>Transition {String(activeIndex + 1).padStart(2, '0')} → {String(activeIndex + 2).padStart(2, '0')}</span><b className={active.total < 78 ? 'score score-low' : 'score'}>{active.total} match</b></div>
            <div className={`transition-visual ${playing ? 'is-playing' : ''}`}><div className="wave wave-one" /><div className="wave wave-two" /><button type="button" onClick={audition} aria-label="Audition transition">{playing ? '■' : '▶'}</button></div>
            <h3>{outgoing.title} → {incoming.title}</h3>
            <ul><li><span>Tempo</span><strong>{outgoing.bpm} → {incoming.bpm} BPM</strong><b>{active.tempo > 88 ? 'Excellent' : 'Workable'}</b></li><li><span>Harmony</span><strong>{outgoing.key} → {incoming.key}</strong><b>{active.harmony > 85 ? 'Compatible' : 'Tension'}</b></li><li><span>Energy</span><strong>{active.energyDelta >= 0 ? '+' : ''}{active.energyDelta} toward peak</strong><b>{active.energy > 80 ? 'On curve' : 'Review'}</b></li><li><span>Learned</span><strong>32-bar overlap</strong><b>{active.learned > 80 ? 'Strong' : 'Neutral'}</b></li></ul>
            <div className="transition-note"><span>Mix note</span><p>Bring {incoming.title} in over 32 bars. Hand off the bass at 58%, then close the outgoing filter late.</p></div>
            <p className="approximation">Browser audition is a lightweight synthesized approximation of tempo, harmony, crossfade, bass handoff, and filtering. Full-quality stretching and mastering run locally.</p>
          </aside>
        </div>
      </section>

      <section className="story-preview" id="story"><p className="eyebrow"><span /> Independent Product Engineer · CratePilot · 2026—present</p><h2>A personal constraint became an audio systems project.</h2><p>After a career break, I returned to hands-on engineering by turning a long-standing goal—playing my first DJ set—into an applied audio product.</p><Link className="text-link" href="/case-study">Problem, architecture, tradeoffs, and validation →</Link></section>

      <section className="local-section" id="local"><div><p className="eyebrow"><span /> Private by design</p><h2>From your music folder to a booth-ready crate.</h2><p>Local mode scans without altering originals, returns up to 30 distinct 45-minute drafts, renders previews and a reference mix, then stages a non-destructive Rekordbox package.</p></div><div className="launch-card"><p>Windows 10/11 · one installer · private local processing</p><code>cratepilot --library &quot;D:\Music\Trance&quot;</code><ol><li>Install Python 3.13, SongRec, FFmpeg, MP3Gain, and yt-dlp together</li><li>Choose a music folder and compose in the browser</li><li>Export M3U8, cues, notes, checksums, and FLAC</li></ol><div className="launch-actions"><a href="https://github.com/achernet/cratepilot/releases/latest/download/CratePilot-Setup-x64.exe" className="button button-primary">Download for Windows ↓</a><a href="https://github.com/achernet/cratepilot" className="button button-quiet">Source ↗</a></div></div></section>

      <section className="credits" id="credits"><div><p className="eyebrow"><span /> Demo catalog</p><h2>Cleared, credited, transparent.</h2></div><div className="credit-list">{catalog.map(track => <a key={track.id} href={track.id === 'eternal-light' ? 'https://freemusicarchive.org/music/soundwave-sphere/single/eternal-lightmp3/' : ({ liberation: 'https://ccmixter.org/files/Karstenholymoly/61513', seven: 'https://ccmixter.org/files/bwatts/39793', ready: 'https://ccmixter.org/files/AnalogByNature/41473', language: 'https://ccmixter.org/files/evenafter/42087', 'summer-house': 'https://ccmixter.org/files/Robbero/46630' } as Record<string, string>)[track.id]}><span>{track.title}</span><small>{track.artist} · CC BY ↗</small></a>)}</div></section>
      <footer><a className="brand" href="#top"><span className="brand-mark">CP</span><span>CratePilot</span></a><p>Designed and engineered by Alex Chernetz · 2026</p><div><Link href="/case-study">Case study</Link><a href="https://github.com/achernet/cratepilot">Source</a></div></footer>
      {visualizer && <div className={`public-visualizer scene-${visualizerScene}`} role="dialog" aria-modal="true" aria-label="Signal visualizer" style={{ '--pointer-x': `${signalPointer.x}%`, '--pointer-y': `${signalPointer.y}%` } as CSSProperties} onPointerMove={(event: ReactPointerEvent<HTMLDivElement>) => { const bounds = event.currentTarget.getBoundingClientRect(); setSignalPointer({ x: (event.clientX - bounds.left) / bounds.width * 100, y: (event.clientY - bounds.top) / bounds.height * 100 }); }} onPointerDown={() => setVisualizerScene(scene => (scene + 1) % 3)}><div className="visualizer-field">{Array.from({ length: 64 }, (_, index) => <i key={index} style={{ '--i': index, '--score': discoveryFixture[index % discovered].score / 100 } as CSSProperties} />)}</div><div className="visualizer-copy"><span>CRATEPILOT SIGNAL / SCENE {visualizerScene + 1}</span><b>{focusedGraphTrack.title} · {focusedGraphTrack.bpm} BPM · {focusedGraphTrack.key}</b><small>Move to steer · click to change scene · driven by {discovered} graph nodes</small></div><button className="button button-quiet" type="button" onPointerDown={event => event.stopPropagation()} onClick={() => setVisualizer(false)}>Close visualizer</button></div>}
    </main>
  );
}
