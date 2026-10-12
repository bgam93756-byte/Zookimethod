'use strict';

// Plays every video from its original file: no transcoding, no adaptive
// bitrate, no resolution ladder. The browser decodes exactly what you
// uploaded, so a device that can't handle 4K 120fps genuinely struggles.

const feed = document.getElementById('feed');
const slideTpl = document.getElementById('slide-template');
const emptyTpl = document.getElementById('empty-template');
const fileInput = document.getElementById('file-input');
const soundBtn = document.getElementById('sound-btn');
const statsBtn = document.getElementById('stats-btn');

const state = {
  muted: true,
  showStats: false,
  slides: [],
  active: null,
  emptyEl: null,
};

const COMMON_FPS = [23.976, 24, 25, 29.97, 30, 48, 50, 59.94, 60, 90, 100, 119.88, 120, 144, 240];
const COMMON_HZ = [30, 48, 50, 60, 72, 75, 90, 100, 120, 144, 165, 240];

// ---------- screen refresh rate ----------
// The fastest requestAnimationFrame rate we've seen is the best estimate of
// how many frames per second this screen/browser can actually show.

const refresh = { hz: 0 };
(function measureRefresh() {
  let frames = 0;
  let start = performance.now();
  function tick(now) {
    frames++;
    const elapsed = now - start;
    if (elapsed >= 1000) {
      if (elapsed < 1500) refresh.hz = Math.max(refresh.hz, snap((frames * 1000) / elapsed, COMMON_HZ));
      frames = 0;
      start = now;
    }
    requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
})();

function snap(value, table, tolerance = 0.04) {
  for (const t of table) if (Math.abs(value - t) / t <= tolerance) return t;
  return value;
}

// ---------- manifest ----------

async function loadManifest() {
  try {
    const res = await fetch('videos.json', { cache: 'no-store' });
    if (!res.ok) return [];
    const data = await res.json();
    const list = Array.isArray(data) ? data : data.videos;
    return Array.isArray(list) ? list.filter((e) => e && (e.src || e.sources)) : [];
  } catch {
    return [];
  }
}

// An entry can list several encodes of the same clip, e.g. HEVC first for
// Safari and an H.264 fallback. Pick the first one this browser can decode.
function pickSource(entry) {
  if (!Array.isArray(entry.sources)) return entry.src;
  const probe = document.createElement('video');
  const playable = entry.sources.find((s) => !s.type || probe.canPlayType(s.type) !== '');
  return (playable || entry.sources[0]).src;
}

// ---------- slides ----------

function createSlide(entry) {
  const el = slideTpl.content.firstElementChild.cloneNode(true);
  const video = el.querySelector('video');
  const slide = {
    el,
    video,
    entry,
    src: pickSource(entry),
    loaded: false,
    monitor: null,
    bitrate: entry.bitrate || null,
    badge: el.querySelector('.badge'),
    stats: el.querySelector('.stats'),
    lagWarning: el.querySelector('.lag-warning'),
    hint: el.querySelector('.tap-hint'),
  };

  el.querySelector('.title').textContent = entry.title || '';
  el.querySelector('.text').textContent = entry.caption || '';
  if (entry.poster) video.poster = entry.poster;
  video.muted = state.muted;
  slide.stats.hidden = !state.showStats;

  video.addEventListener('click', () => togglePlay(slide));
  video.addEventListener('loadedmetadata', () => {
    updateBadge(slide);
    findBitrate(slide);
  });
  video.addEventListener('playing', () => (slide.hint.hidden = true));
  video.addEventListener('error', () => {
    if (!slide.loaded || !video.error) return;
    const codec = entry.codec ? ` (${entry.codec})` : '';
    showHint(slide, `This browser can't play this video format${codec}. Try a different browser.`);
  });
  el.querySelector('.fullscreen-btn').addEventListener('click', () => toggleFullscreen(video));

  return slide;
}

function load(slide, preload) {
  slide.video.preload = preload;
  if (!slide.loaded) {
    slide.loaded = true;
    slide.video.src = slide.src;
  }
}

// Free the decoder and memory of slides that are far away; a feed of 4K
// videos would otherwise exhaust the device before playback even starts.
function unload(slide) {
  if (!slide.loaded) return;
  stopMonitor(slide);
  slide.loaded = false;
  slide.video.pause();
  slide.video.removeAttribute('src');
  slide.video.load();
}

function activate(slide) {
  if (state.active === slide) return;
  if (state.active) {
    state.active.video.pause();
    stopMonitor(state.active);
  }
  state.active = slide;

  const index = state.slides.indexOf(slide);
  state.slides.forEach((s, i) => {
    if (i === index) load(s, 'auto');
    else if (Math.abs(i - index) === 1) load(s, 'metadata');
    else unload(s);
  });

  slide.video.muted = state.muted;
  play(slide);
  startMonitor(slide);
}

function play(slide) {
  const p = slide.video.play();
  // only ask for a tap if autoplay was really blocked (e.g. iPhone Low Power Mode)
  if (p && p.catch) p.catch(() => slide.video.paused && !slide.video.error && showHint(slide, 'Tap to play'));
}

function togglePlay(slide) {
  if (slide.video.paused) play(slide);
  else {
    slide.video.pause();
    showHint(slide, 'Paused');
  }
}

function showHint(slide, text) {
  slide.hint.textContent = text;
  slide.hint.hidden = false;
}

function toggleFullscreen(video) {
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  else if (video.requestFullscreen) video.requestFullscreen().catch(() => {});
  else if (video.webkitEnterFullscreen) video.webkitEnterFullscreen(); // iPhone
}

const observer = new IntersectionObserver(
  (entries) => {
    for (const e of entries) {
      if (!e.isIntersecting) continue;
      const slide = state.slides.find((s) => s.el === e.target);
      if (slide) activate(slide);
    }
  },
  { root: feed, threshold: 0.6 },
);

function addSlides(entries, { atTop = false } = {}) {
  if (entries.length && state.emptyEl) {
    state.emptyEl.remove();
    state.emptyEl = null;
  }
  const created = entries.map(createSlide);
  if (atTop) {
    for (const s of [...created].reverse()) feed.prepend(s.el);
    state.slides.unshift(...created);
  } else {
    for (const s of created) feed.append(s.el);
    state.slides.push(...created);
  }
  created.forEach((s) => observer.observe(s.el));
  return created;
}

// ---------- labels ----------

function resolutionLabel(w, h) {
  const short = Math.min(w, h);
  if (short >= 4320) return '8K';
  if (short >= 2160) return '4K';
  if (short >= 1440) return '1440p';
  return `${short}p`;
}

function sourceFps(slide) {
  if (slide.entry.fps) return slide.entry.fps;
  return slide.monitor?.sourceFps ? snap(slide.monitor.sourceFps, COMMON_FPS) : null;
}

function updateBadge(slide) {
  const { videoWidth: w, videoHeight: h } = slide.video;
  if (!w || !h) return;
  const fps = sourceFps(slide);
  const text = fps ? `${resolutionLabel(w, h)} · ${Math.round(fps)} FPS` : resolutionLabel(w, h);
  if (slide.badge.textContent !== text) slide.badge.textContent = text;
}

async function findBitrate(slide) {
  if (slide.bitrate) return;
  const duration = slide.video.duration;
  if (!isFinite(duration) || duration <= 0) return;
  let bytes = slide.entry.size;
  if (!bytes && !slide.src.startsWith('blob:')) {
    try {
      const res = await fetch(slide.src, { method: 'HEAD' });
      bytes = Number(res.headers.get('content-length')) || 0;
    } catch {
      // cross-origin host without CORS: the bitrate just stays unknown
    }
  }
  if (bytes) slide.bitrate = (bytes * 8) / duration;
}

// ---------- live stats ----------
// "Smoothness" compares frames actually shown against the most the screen
// could show (min of the video's fps and the screen's refresh rate), so a
// 120fps clip on a 60Hz screen isn't reported as lag — only real stutter is.

function startMonitor(slide) {
  stopMonitor(slide);
  const v = slide.video;
  const m = {
    prev: null,
    presented: null,
    sourceFps: null,
    shownFps: null,
    smoothness: null,
    lagStreak: 0,
    buffering: false,
    rvfc: 0,
    timer: 0,
    listeners: [],
  };

  if ('requestVideoFrameCallback' in v) {
    const onFrame = (_now, meta) => {
      m.presented = meta.presentedFrames;
      m.rvfc = v.requestVideoFrameCallback(onFrame);
    };
    m.rvfc = v.requestVideoFrameCallback(onFrame);
  }

  const on = (type, fn) => {
    v.addEventListener(type, fn);
    m.listeners.push([type, fn]);
  };
  on('waiting', () => (m.buffering = true));
  on('playing', () => (m.buffering = false));
  on('seeked', () => (m.prev = null));

  m.timer = setInterval(() => sample(slide), 1000);
  slide.monitor = m;
  render(slide);
}

function stopMonitor(slide) {
  const m = slide.monitor;
  if (!m) return;
  clearInterval(m.timer);
  if (m.rvfc && slide.video.cancelVideoFrameCallback) slide.video.cancelVideoFrameCallback(m.rvfc);
  for (const [type, fn] of m.listeners) slide.video.removeEventListener(type, fn);
  slide.monitor = null;
  slide.lagWarning.hidden = true;
}

const smooth = (prev, next) => (prev == null ? next : prev * 0.6 + next * 0.4);

function bufferedAhead(v) {
  for (let i = 0; i < v.buffered.length; i++) {
    if (v.buffered.start(i) <= v.currentTime && v.currentTime <= v.buffered.end(i)) {
      return v.buffered.end(i) - v.currentTime;
    }
  }
  return 0;
}

function sample(slide) {
  const v = slide.video;
  const m = slide.monitor;
  const q = v.getVideoPlaybackQuality ? v.getVideoPlaybackQuality() : null;
  const now = performance.now();
  const cur = {
    wall: now,
    media: v.currentTime,
    total: q ? q.totalVideoFrames : null,
    dropped: q ? q.droppedVideoFrames : null,
    presented: m.presented,
  };

  // A stall with plenty of data downloaded means the decoder is the bottleneck,
  // not the connection; only network stalls are excluded from smoothness.
  m.networkStall = m.buffering && bufferedAhead(v) < 1;
  const playing = !v.paused && !v.seeking && !m.networkStall;
  if (m.prev && playing) {
    const dMedia = cur.media - m.prev.media; // negative when the clip loops
    const dWall = (cur.wall - m.prev.wall) / 1000;
    if (dMedia > 0.25 && dWall > 0.25) {
      if (q && cur.total > m.prev.total) {
        m.sourceFps = smooth(m.sourceFps, (cur.total - m.prev.total) / dMedia);
      }
      if (cur.presented != null && m.prev.presented != null) {
        m.shownFps = smooth(m.shownFps, (cur.presented - m.prev.presented) / dWall);
      }
      const src = sourceFps(slide);
      if (m.shownFps != null && src) {
        const screenHz = Math.max(refresh.hz, m.shownFps);
        const possible = Math.min(src * v.playbackRate, screenHz || src);
        m.smoothness = Math.min(1, m.shownFps / possible);
      } else if (q && cur.total > m.prev.total) {
        // no requestVideoFrameCallback: fall back to the browser's dropped-frame count
        m.smoothness = smooth(m.smoothness, 1 - (cur.dropped - m.prev.dropped) / (cur.total - m.prev.total));
      }
      m.lagStreak = m.smoothness != null && m.smoothness < 0.85 ? m.lagStreak + 1 : 0;
    }
  }
  m.prev = cur;

  updateBadge(slide);
  updateLagWarning(slide);
  render(slide);
}

function updateLagWarning(slide) {
  const m = slide.monitor;
  const el = slide.lagWarning;
  let text = '';
  if (m.networkStall) {
    const rate = slide.bitrate ? ` at ${formatBitrate(slide.bitrate)}` : '';
    text = `Buffering — the connection can't keep up with the full-quality stream${rate}.`;
  } else if (m.lagStreak >= 2 || m.buffering) {
    const missed = m.smoothness != null ? Math.round((1 - m.smoothness) * 100) : 0;
    const how = missed >= 5 ? `${missed}% of frames are being skipped` : 'playback keeps stalling';
    text = `This device can't keep up: ${how}. That's the full-quality file, not an effect.`;
  }
  el.textContent = text;
  el.hidden = !text;
}

function formatBitrate(bps) {
  const mbps = bps / 1e6;
  return `${mbps < 10 ? mbps.toFixed(1) : Math.round(mbps)} Mbps`;
}

function grade(value, okAt, warnAt) {
  if (value == null) return '';
  return value >= okAt ? 'good' : value >= warnAt ? 'warn' : 'bad';
}

function render(slide) {
  if (slide.stats.hidden) return;
  const v = slide.video;
  const m = slide.monitor || {};
  const q = v.getVideoPlaybackQuality ? v.getVideoPlaybackQuality() : null;
  const dpr = window.devicePixelRatio || 1;
  const src = sourceFps(slide);

  const rows = [
    ['Video', v.videoWidth ? `${v.videoWidth}×${v.videoHeight} (${resolutionLabel(v.videoWidth, v.videoHeight)})` : '…'],
    ['Video fps', src ? `${src.toFixed(src % 1 ? 2 : 0)}${slide.entry.fps ? '' : ' (measured)'}` : '…'],
    ['Shown fps', m.shownFps != null ? m.shownFps.toFixed(1) : '—'],
    ['Smoothness', m.smoothness != null ? `${Math.round(m.smoothness * 100)}%` : '—', grade(m.smoothness, 0.95, 0.85)],
    ['Dropped', q ? `${q.droppedVideoFrames.toLocaleString()} / ${q.totalVideoFrames.toLocaleString()}` : '—'],
    ['Bitrate', slide.bitrate ? formatBitrate(slide.bitrate) : '—'],
    ['Codec', slide.entry.codec || '—'],
    ...(slide.entry.enhanced_from ? [['Source', `${slide.entry.enhanced_from}, enhanced`]] : []),
    ['Buffer', `${bufferedAhead(v).toFixed(1)} s${m.networkStall ? ' · stalled' : ''}`, m.networkStall ? 'bad' : ''],
    ['Screen', `${Math.round(screen.width * dpr)}×${Math.round(screen.height * dpr)} @ ${refresh.hz ? Math.round(refresh.hz) + ' Hz' : '…'}`],
  ];

  const frag = document.createDocumentFragment();
  for (const [label, value, cls] of rows) {
    const dt = document.createElement('dt');
    const dd = document.createElement('dd');
    dt.textContent = label;
    dd.textContent = value;
    if (cls) dd.className = cls;
    frag.append(dt, dd);
  }
  slide.stats.replaceChildren(frag);
}

// ---------- controls ----------

function setMuted(muted) {
  state.muted = muted;
  for (const s of state.slides) s.video.muted = muted;
  soundBtn.textContent = muted ? '🔇' : '🔊';
  soundBtn.setAttribute('aria-label', muted ? 'Sound off' : 'Sound on');
  soundBtn.setAttribute('aria-pressed', String(!muted));
  if (!muted && state.active && state.active.video.paused) play(state.active);
}

function setShowStats(show) {
  state.showStats = show;
  statsBtn.setAttribute('aria-pressed', String(show));
  for (const s of state.slides) {
    s.stats.hidden = !show;
    if (show) render(s);
  }
}

function scrollBy(step) {
  const i = state.slides.indexOf(state.active);
  const next = state.slides[i + step];
  if (next) next.el.scrollIntoView({ behavior: 'smooth' });
}

soundBtn.addEventListener('click', () => setMuted(!state.muted));
statsBtn.addEventListener('click', () => setShowStats(!state.showStats));

// Labels styled as buttons open file pickers; make them work from the keyboard too.
document.addEventListener('keydown', (e) => {
  const label = e.target.closest?.('label[role="button"]');
  if (!label || (e.key !== 'Enter' && e.key !== ' ')) return;
  e.preventDefault();
  e.stopImmediatePropagation();
  label.click();
});

document.addEventListener('keydown', (e) => {
  if (e.target.closest?.('input, textarea, select, dialog')) return;
  if (e.key === 'ArrowDown' || e.key === 'j') scrollBy(1);
  else if (e.key === 'ArrowUp' || e.key === 'k') scrollBy(-1);
  else if (e.key === ' ' && state.active) togglePlay(state.active);
  else if (e.key === 'm') setMuted(!state.muted);
  else if (e.key === 's') setShowStats(!state.showStats);
  else if (e.key === 'f' && state.active) toggleFullscreen(state.active.video);
  else return;
  e.preventDefault();
});

document.addEventListener('visibilitychange', () => {
  if (!state.active) return;
  if (document.hidden) state.active.video.pause();
  else if (state.active.hint.hidden) play(state.active);
});

// Local files play straight from disk via an object URL: nothing is
// uploaded, so nothing can be recompressed.
fileInput.addEventListener('change', () => {
  const files = [...fileInput.files];
  fileInput.value = '';
  if (!files.length) return;
  const created = addSlides(
    files.map((file) => ({
      src: URL.createObjectURL(file),
      title: file.name,
      caption: 'From this device · played as-is, never uploaded',
      size: file.size,
    })),
    { atTop: true },
  );
  created[0].el.scrollIntoView();
  activate(created[0]);
});

// ---------- start ----------

(async function start() {
  // an upload that finished while the manifest loaded may already be in the feed
  const entries = (await loadManifest()).filter((e) => !state.slides.some((s) => s.entry.src === e.src));
  if (entries.length) addSlides(entries);
  else if (!state.slides.length) {
    state.emptyEl = emptyTpl.content.firstElementChild.cloneNode(true);
    feed.append(state.emptyEl);
  }
})();
