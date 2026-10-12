'use strict';

// Upload flow, modeled on TikTok's: init -> chunked PUTs with Content-Range
// -> poll status while the server processes. Only shown on the private
// upload link that serve.py prints (it carries the upload key in #upload=).

(function () {
  const MIB = 1024 * 1024;
  const CHUNK = 8 * MIB; // TikTok allows 5-64 MiB per chunk and at most 1000 chunks
  const MAX_CHUNK = 64 * MIB;
  const MAX_CHUNKS = 1000;
  const MIN_CHUNK = 5 * MIB;
  const RETRIES = 5;
  const POLL_MS = 1500;
  const MAX_POLL_MS = 30000;
  const STORE_KEY = 'zooki.uploads';
  const TOKEN_KEY = 'zooki.uploadKey';

  const openBtn = document.getElementById('upload-btn');
  const playLabel = document.getElementById('play-file-label');
  const dialog = document.getElementById('upload-dialog');
  const form = document.getElementById('upload-form');
  const fileField = document.getElementById('upload-file');
  const fileName = document.getElementById('upload-file-name');
  const fileInfo = document.getElementById('upload-file-info');
  const titleField = document.getElementById('upload-title');
  const captionField = document.getElementById('upload-caption');
  const enhanceField = document.getElementById('upload-enhance');
  const formError = document.getElementById('upload-error');
  const jobList = document.getElementById('upload-jobs');
  const jobTpl = document.getElementById('upload-job-template');

  const jobs = new Map(); // publish_id -> job
  let token = null;
  let setUp = false;
  let settingUp = null;

  // ---------- small helpers ----------

  const storage = {
    get(key, fallback) {
      try {
        const v = localStorage.getItem(key);
        return v == null ? fallback : JSON.parse(v);
      } catch {
        return fallback;
      }
    },
    set(key, value) {
      try {
        localStorage.setItem(key, JSON.stringify(value));
      } catch {
        // private mode: uploads still work, they just can't resume after a reload
      }
    },
  };

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // Identify a file by its size and a hash of its first and last 64 KB. The
  // name and date are left out: iPhone Photos hands over a fresh copy, with a
  // new date (and sometimes a new name), every time a video is picked.
  async function fingerprint(file) {
    const part = 64 * 1024;
    const pieces = [file.slice(0, part), file.slice(Math.max(0, file.size - part))];
    let hash = 0x811c9dc5; // FNV-1a
    for (const piece of pieces) {
      const bytes = new Uint8Array(await piece.arrayBuffer());
      for (let i = 0; i < bytes.length; i++) {
        hash ^= bytes[i];
        hash = Math.imul(hash, 0x01000193) >>> 0;
      }
    }
    return `${file.size}:${hash.toString(16)}`;
  }

  function formatBytes(n) {
    if (n >= 1e9) return `${(n / 1e9).toFixed(2)} GB`;
    if (n >= 1e6) return `${(n / 1e6).toFixed(n >= 1e8 ? 0 : 1)} MB`;
    return `${Math.max(1, Math.round(n / 1e3))} KB`;
  }

  function formatEta(seconds) {
    if (seconds == null || !isFinite(seconds)) return '';
    if (seconds < 60) return 'less than a minute left';
    const m = Math.round(seconds / 60);
    return m < 60 ? `about ${m} min left` : `about ${Math.floor(m / 60)} h ${m % 60} min left`;
  }

  function toast(text) {
    const el = document.createElement('div');
    el.className = 'toast';
    el.setAttribute('role', 'status');
    el.textContent = text;
    document.body.append(el);
    setTimeout(() => el.remove(), 7000);
  }

  class ApiError extends Error {
    constructor(status, code, message) {
      super(message);
      this.status = status;
      this.code = code;
    }
  }

  const retryable = (e) => e.status === 0 || (e.status >= 500 && e.status !== 507) || e.code === 'incomplete_chunk';

  async function api(method, url, body, key = token) {
    let res;
    try {
      res = await fetch(url, {
        method,
        headers: { Authorization: `Bearer ${key}`, ...(body ? { 'Content-Type': 'application/json' } : {}) },
        body: body ? JSON.stringify(body) : undefined,
        cache: 'no-store',
      });
    } catch {
      throw new ApiError(0, 'network', 'Connection lost.');
    }
    let json = null;
    try {
      json = await res.json();
    } catch {
      // non-JSON error page
    }
    if (!res.ok) {
      const err = json?.error || {};
      if (res.status === 401) forgetToken(key);
      throw new ApiError(res.status, err.code || 'http_error', err.message || `Server error ${res.status}`);
    }
    return json?.data;
  }

  // XMLHttpRequest instead of fetch: it reports upload progress within a chunk.
  function putChunk(job, blob, first, last, onProgress) {
    const key = token;
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      job.xhr = xhr;
      xhr.open('PUT', job.uploadUrl);
      xhr.setRequestHeader('Authorization', `Bearer ${key}`);
      xhr.setRequestHeader('Content-Type', 'application/octet-stream');
      xhr.setRequestHeader('Content-Range', `bytes ${first}-${last}/${job.size}`);
      xhr.upload.onprogress = (e) => !job.cancelled && onProgress(e.loaded);
      xhr.onload = () => {
        job.xhr = null;
        let json = null;
        try {
          json = JSON.parse(xhr.responseText);
        } catch {
          // fall through to the status check
        }
        if (xhr.status === 201 || xhr.status === 206) return resolve({ done: xhr.status === 201 });
        if (xhr.status === 401) forgetToken(key);
        reject(new ApiError(xhr.status, json?.error?.code || 'http_error', json?.error?.message || `Server error ${xhr.status}`));
      };
      xhr.onerror = xhr.ontimeout = () => {
        job.xhr = null;
        reject(new ApiError(0, 'network', 'Connection lost.'));
      };
      xhr.onabort = () => {
        job.xhr = null;
        reject(new ApiError(0, 'aborted', 'Stopped.'));
      };
      xhr.send(blob);
    });
  }

  function chunkPlan(size) {
    // big files use bigger chunks so they stay within 1000 chunks
    const chunk = Math.min(MAX_CHUNK, Math.max(CHUNK, Math.ceil(size / MAX_CHUNKS / MIB) * MIB));
    if (size < MIN_CHUNK || size < chunk) return { chunk_size: size, total_chunk_count: 1 };
    return { chunk_size: chunk, total_chunk_count: Math.floor(size / chunk) };
  }

  function chunkRange(job, index) {
    const first = index * job.chunkSize;
    const last = index === job.totalChunks - 1 ? job.size - 1 : first + job.chunkSize - 1;
    return [first, last];
  }

  // ---------- job rows ----------

  function saveJobs() {
    const list = [...jobs.values()]
      .filter((j) => j.publishId)
      .slice(-10)
      .map((j) => ({
        publishId: j.publishId,
        title: j.title,
        fingerprint: j.fingerprint,
        size: j.size,
        chunkSize: j.chunkSize,
        totalChunks: j.totalChunks,
        final: j.final || null,
      }));
    storage.set(STORE_KEY, list);
  }

  function createJobRow(job) {
    const el = jobTpl.content.firstElementChild.cloneNode(true);
    job.el = el;
    job.ui = {
      title: el.querySelector('.job-title'),
      stage: el.querySelector('.job-stage'),
      bar: el.querySelector('progress'),
      detail: el.querySelector('.job-detail'),
      cancel: el.querySelector('.job-cancel'),
      watch: el.querySelector('.job-watch'),
      resume: el.querySelector('.job-resume'),
      pick: el.querySelector('.job-pick'),
      pickInput: el.querySelector('.job-pick input'),
    };
    job.ui.title.textContent = job.title;
    job.ui.cancel.addEventListener('click', () => cancelJob(job));
    job.ui.watch.addEventListener('click', () => watchJob(job));
    job.ui.resume.addEventListener('click', () => runUpload(job));
    job.ui.pickInput.addEventListener('change', async () => {
      const file = job.ui.pickInput.files[0];
      job.ui.pickInput.value = '';
      if (!file) return;
      if ((await fingerprint(file)) !== job.fingerprint) {
        updateRow(job, { detail: "That's a different video. Pick the one you started uploading." });
        return;
      }
      job.file = file;
      runUpload(job);
    });
    jobList.prepend(el);
    jobList.hidden = false;
  }

  function updateRow(job, { stage, progress, detail, state }) {
    const ui = job.ui;
    if (stage != null) ui.stage.textContent = stage;
    if (detail != null) ui.detail.textContent = detail;
    if (progress === null) ui.bar.removeAttribute('value');
    else if (progress != null) ui.bar.value = progress;
    if (state) {
      job.state = state;
      job.el.dataset.state = state;
      ui.cancel.hidden = !['uploading', 'waiting', 'processing', 'paused'].includes(state);
      ui.watch.hidden = state !== 'done';
      ui.resume.hidden = !(state === 'paused' && job.file);
      ui.pick.hidden = !(state === 'paused' && !job.file);
      ui.bar.hidden = ['done', 'failed', 'cancelled'].includes(state);
    }
  }

  function finish(job, final) {
    if (job.final === 'done' && final !== 'done') return; // never downgrade a posted video
    job.final = final;
    saveJobs();
  }

  // ---------- upload ----------

  async function startUpload(file, title, caption, enhance) {
    const plan = chunkPlan(file.size);
    const job = {
      file,
      title: title || file.name.replace(/\.[^.]+$/, ''),
      size: file.size,
      chunkSize: plan.chunk_size,
      totalChunks: plan.total_chunk_count,
      publishId: null,
      cancelled: false,
    };
    createJobRow(job);
    updateRow(job, { stage: 'Starting upload…', progress: 0, detail: formatBytes(file.size), state: 'uploading' });

    try {
      job.fingerprint = await fingerprint(file);
      const data = await api('POST', 'api/upload/init', {
        filename: file.name,
        title,
        caption,
        enhance,
        video_size: file.size,
        ...plan,
      });
      job.publishId = data.publish_id;
      job.uploadUrl = data.upload_url;
      jobs.set(job.publishId, job);
      saveJobs();
    } catch (e) {
      updateRow(job, { stage: "Couldn't start the upload", detail: e.message, state: 'failed' });
      return;
    }
    runUpload(job);
  }

  async function runUpload(job) {
    if (job.uploading || !job.file) return;
    job.uploading = true;
    job.cancelled = false;
    try {
      await uploadChunks(job);
    } finally {
      job.uploading = false;
    }
  }

  async function uploadChunks(job) {
    job.uploadUrl = job.uploadUrl || `api/upload/${job.publishId}`;
    updateRow(job, { stage: 'Uploading…', state: 'uploading' });

    let status;
    try {
      status = await api('GET', `api/upload/${job.publishId}`);
    } catch (e) {
      pause(job, e);
      return;
    }
    if (status.status !== 'UPLOADING') return pollJob(job);

    let doneBytes = status.uploaded_bytes;
    let sentThisSession = 0;
    const started = performance.now();
    const show = (inFlight) => {
      const bytes = doneBytes + inFlight;
      const secs = (performance.now() - started) / 1000;
      const rate = secs > 1 ? (sentThisSession + inFlight) / secs : 0;
      updateRow(job, {
        progress: bytes / job.size,
        detail: `${formatBytes(bytes)} of ${formatBytes(job.size)}${rate ? ` · ${formatBytes(rate)}/s` : ''}`,
      });
    };
    show(0);

    for (const index of status.missing_chunks) {
      const [first, last] = chunkRange(job, index);
      const blob = job.file.slice(first, last + 1);
      for (let attempt = 0; ; attempt++) {
        if (job.cancelled) return;
        try {
          const res = await putChunk(job, blob, first, last, show);
          doneBytes += blob.size;
          sentThisSession += blob.size;
          show(0);
          if (res.done) return pollJob(job);
          break;
        } catch (e) {
          if (job.cancelled) return;
          if (e.code === 'upload_closed') return pollJob(job); // e.g. the last chunk's reply got lost
          if (!retryable(e) || attempt >= RETRIES) {
            pause(job, e);
            return;
          }
          updateRow(job, { detail: `${e.message} Retrying…` });
          await sleep(1000 * 2 ** attempt);
        }
      }
    }
    pollJob(job); // every chunk was already there
  }

  function pause(job, e) {
    if (e.status === 507 || e.status === 404) {
      updateRow(job, { stage: 'Upload failed', detail: e.message, state: 'failed' });
      finish(job, 'failed');
      return;
    }
    const hint = job.file ? 'Tap Resume to continue.' : 'Pick the same video to continue.';
    updateRow(job, { stage: 'Upload paused', detail: `${e.message} ${hint}`, state: 'paused' });
  }

  async function pollJob(job) {
    if (job.polling) return;
    job.polling = true;
    try {
      await pollLoop(job);
    } finally {
      job.polling = false;
    }
  }

  async function pollLoop(job) {
    let delay = POLL_MS;
    while (!job.cancelled) {
      let s;
      try {
        s = await api('GET', `api/upload/${job.publishId}`);
        delay = POLL_MS;
      } catch (e) {
        if (e.status === 404) {
          updateRow(job, { stage: 'No longer on the server', detail: '', state: 'failed' });
          finish(job, 'failed');
          return;
        }
        if (e.status === 401) {
          updateRow(job, { stage: 'Upload link expired', detail: 'Open the newest upload link to follow this video.' });
          return;
        }
        // the phone or the computer is briefly offline: keep trying, more slowly
        updateRow(job, { detail: 'Reconnecting…' });
        await sleep(delay);
        delay = Math.min(delay * 2, MAX_POLL_MS);
        continue;
      }

      if (s.status === 'UPLOADING') {
        if (job.file) {
          job.polling = false;
          return runUpload(job);
        }
        updateRow(job, {
          stage: 'Upload paused',
          progress: s.uploaded_bytes / s.video_size,
          detail: `${formatBytes(s.uploaded_bytes)} of ${formatBytes(s.video_size)} uploaded. Pick the same video to continue.`,
          state: 'paused',
        });
        return;
      }
      if (s.status === 'QUEUED') {
        const ahead = s.queue_position || 0;
        updateRow(job, {
          stage: 'Waiting to process',
          progress: null,
          detail: ahead ? `${ahead} video${ahead > 1 ? 's' : ''} ahead` : 'Starting soon',
          state: 'waiting',
        });
      } else if (s.status === 'PROCESSING') {
        const bits = [s.source && s.target ? `${s.source} → ${s.target}` : '', formatEta(s.eta_seconds)].filter(Boolean);
        updateRow(job, {
          stage: s.stage || 'Processing',
          progress: s.progress ?? null,
          detail: `${Math.round((s.progress || 0) * 100)}%${bits.length ? ' · ' + bits.join(' · ') : ''}`,
          state: 'processing',
        });
      } else if (s.status === 'PUBLISH_COMPLETE') {
        job.video = s.video;
        updateRow(job, {
          stage: 'Posted',
          detail: s.video?.enhanced_from ? `Enhanced from ${s.video.enhanced_from} to 4K 120fps` : 'Kept at its original quality',
          state: 'done',
        });
        const justFinished = job.final !== 'done'; // restored rows were added to the feed by the manifest
        finish(job, 'done');
        if (justFinished) addToFeed(job);
        return;
      } else {
        const failed = s.status === 'FAILED';
        updateRow(job, {
          stage: failed ? 'Processing failed' : 'Cancelled',
          detail: failed ? s.fail_reason || '' : '',
          state: failed ? 'failed' : 'cancelled',
        });
        finish(job, failed ? 'failed' : 'cancelled');
        return;
      }
      await sleep(POLL_MS);
    }
  }

  async function cancelJob(job) {
    if (job.cancelling) return;
    job.cancelling = true;
    job.ui.cancel.disabled = true;
    const wasState = job.state;
    job.cancelled = true; // stops the upload loop and the poller
    job.xhr?.abort();
    updateRow(job, { stage: 'Cancelling…' });
    try {
      const s = await api('DELETE', `api/upload/${job.publishId}`);
      if (s.status === 'PUBLISH_COMPLETE') {
        job.cancelled = false;
        pollJob(job); // it finished first; show it as posted
      } else {
        updateRow(job, { stage: 'Cancelled', detail: '', state: 'cancelled' });
        finish(job, 'cancelled');
      }
    } catch (e) {
      if (e.status === 404) {
        updateRow(job, { stage: 'Cancelled', detail: '', state: 'cancelled' });
        finish(job, 'cancelled');
      } else {
        // the server never heard the cancel: say so, and keep following the job
        job.cancelled = false;
        const tooLate = e.code === 'too_late';
        updateRow(job, {
          stage: tooLate ? 'Already posting' : "Couldn't cancel",
          detail: tooLate ? e.message : `${e.message} Try again.`,
          state: wasState === 'uploading' ? 'paused' : wasState,
        });
        if (wasState !== 'uploading' && wasState !== 'paused') pollJob(job);
      }
    } finally {
      job.cancelling = false;
      job.ui.cancel.disabled = false;
    }
  }

  const findSlide = (src) => state.slides.find((s) => s.entry.src === src);

  async function addToFeed(job) {
    if (!job.video?.src || job.slide) return;
    let entry = { ...job.video };
    try {
      // the manifest entry also carries resolution, fps, codec and bitrate
      const res = await fetch('videos.json', { cache: 'no-store' });
      const list = await res.json();
      entry = (Array.isArray(list) ? list : list.videos || []).find((e) => e.src === job.video.src) || entry;
    } catch {
      // the basic entry still plays
    }
    // checked after the fetch: the feed may have loaded this video meanwhile
    job.slide = findSlide(job.video.src) || addSlides([entry], { atTop: true })[0];
  }

  function watchJob(job) {
    dialog.close();
    const slide = job.slide || findSlide(job.video?.src);
    if (!slide) return;
    slide.el.scrollIntoView();
    activate(slide);
  }

  // ---------- form ----------

  fileField.addEventListener('change', () => {
    const file = fileField.files[0];
    formError.hidden = true;
    fileInfo.textContent = '';
    if (!file) {
      fileName.textContent = 'Choose a video';
      return;
    }
    fileName.textContent = file.name;
    // fill the title from the file name, unless the person typed their own
    if (!titleField.value || titleField.value === titleField.dataset.auto) {
      titleField.value = titleField.dataset.auto = file.name.replace(/\.[^.]+$/, '').replace(/[_-]+/g, ' ');
    }
    // read resolution and length locally; nothing is uploaded yet
    const probe = document.createElement('video');
    probe.preload = 'metadata';
    probe.muted = true;
    const url = URL.createObjectURL(file);
    const current = () => fileField.files[0] === file;
    probe.onloadedmetadata = () => {
      if (current()) {
        const { videoWidth: w, videoHeight: h, duration } = probe;
        const mins = Math.floor(duration / 60);
        const secs = String(Math.round(duration % 60)).padStart(2, '0');
        fileInfo.textContent = `${w}×${h} · ${mins}:${secs} · ${formatBytes(file.size)}`;
      }
      URL.revokeObjectURL(url);
    };
    probe.onerror = () => {
      if (current()) fileInfo.textContent = formatBytes(file.size);
      URL.revokeObjectURL(url);
    };
    probe.src = url;
  });

  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const file = fileField.files[0];
    if (!file) {
      formError.textContent = 'Choose a video first.';
      formError.hidden = false;
      return;
    }
    startUpload(file, titleField.value.trim(), captionField.value.trim(), enhanceField.checked);
    form.reset();
    delete titleField.dataset.auto;
    fileName.textContent = 'Choose a video';
    fileInfo.textContent = '';
  });

  openBtn.addEventListener('click', () => {
    formError.hidden = true;
    dialog.showModal();
  });
  dialog.querySelector('.sheet-close').addEventListener('click', () => dialog.close());

  // close on a tap on the dimmed backdrop, but not on the sheet's own padding,
  // or when a text selection that started inside ends outside
  const outsideSheet = (e) => {
    const r = dialog.getBoundingClientRect();
    return e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom;
  };
  let pressedOutside = false;
  dialog.addEventListener('pointerdown', (e) => (pressedOutside = outsideSheet(e)));
  dialog.addEventListener('click', (e) => {
    if (pressedOutside && outsideSheet(e)) dialog.close();
    pressedOutside = false;
  });
  document.getElementById('upload-play-local').addEventListener('click', () => dialog.close());

  // ---------- upload key ----------

  // A key that just failed is dropped, but only if it's still the one in use:
  // an old link must not wipe a newer, working key.
  function forgetToken(key) {
    if (key !== token) return;
    token = null;
    setUp = false;
    if (storage.get(TOKEN_KEY, null) === key) storage.set(TOKEN_KEY, null);
    openBtn.hidden = true;
    playLabel.hidden = false;
    toast('Your upload link stopped working. Open the newest link printed by serve.py.');
  }

  async function checkKey(key) {
    try {
      await api('GET', 'api/auth', null, key);
      return true;
    } catch (e) {
      return e.status === 401 ? false : null;
    }
  }

  async function init() {
    const fromLink = /(?:^|[#&])upload=([\w-]+)/.exec(location.hash);
    if (fromLink) {
      history.replaceState(null, '', location.pathname + location.search); // keep the key out of the address bar
      const key = fromLink[1];
      if (key !== token) {
        const ok = await checkKey(key);
        if (ok === false) {
          toast('That upload link is out of date. Open the newest link printed by serve.py.');
        } else if (ok) {
          token = key;
          storage.set(TOKEN_KEY, key);
        }
      }
    }
    if (!token) token = storage.get(TOKEN_KEY, null);
    if (!token || setUp) return;
    settingUp = settingUp || setUpUploads().finally(() => (settingUp = null));
  }

  async function setUpUploads() {
    let config;
    try {
      const res = await fetch('api/config', { cache: 'no-store' });
      config = (await res.json()).data;
    } catch {
      return; // static host: no server to upload to
    }
    if (!config?.uploads || setUp || !token) return;
    if (!config.processing) {
      toast('Uploads are off: install ffmpeg on the computer running serve.py.');
      return;
    }
    setUp = true;
    openBtn.hidden = false;
    playLabel.hidden = true;

    for (const saved of storage.get(STORE_KEY, [])) {
      if (saved.final === 'cancelled' || jobs.has(saved.publishId)) continue;
      const job = { ...saved, cancelled: false };
      jobs.set(job.publishId, job);
      createJobRow(job);
      updateRow(job, { stage: 'Checking…', progress: null });
      pollJob(job);
    }
  }

  // opening the upload link in a tab that already shows the site only changes the hash
  window.addEventListener('hashchange', init);
  // pick up paused uploads by themselves when the phone is back online
  window.addEventListener('online', () => {
    for (const job of jobs.values()) if (job.state === 'paused' && job.file) runUpload(job);
  });
  init();
})();
