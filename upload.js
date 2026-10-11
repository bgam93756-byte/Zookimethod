'use strict';

// Upload flow, modeled on TikTok's: init -> chunked PUTs with Content-Range
// -> poll status while the server processes. Only shown on the private
// upload link that serve.py prints (it carries the upload key in #upload=).

(function () {
  const CHUNK = 8 * 1024 * 1024; // TikTok allows 5-64 MB per chunk
  const MIN_CHUNK = 5 * 1024 * 1024;
  const RETRIES = 5;
  const POLL_MS = 1500;
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
  let processingAvailable = true;

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

  const fingerprint = (file) => `${file.name}|${file.size}|${file.lastModified}`;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

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

  class ApiError extends Error {
    constructor(status, code, message) {
      super(message);
      this.status = status;
      this.code = code;
    }
  }

  async function api(method, url, body) {
    const res = await fetch(url, {
      method,
      headers: { Authorization: `Bearer ${token}`, ...(body ? { 'Content-Type': 'application/json' } : {}) },
      body: body ? JSON.stringify(body) : undefined,
      cache: 'no-store',
    });
    let json = null;
    try {
      json = await res.json();
    } catch {
      // non-JSON error page
    }
    if (!res.ok) {
      const err = json?.error || {};
      if (res.status === 401) forgetToken();
      throw new ApiError(res.status, err.code || 'http_error', err.message || `Server error ${res.status}`);
    }
    return json?.data;
  }

  // XMLHttpRequest instead of fetch: it reports upload progress within a chunk.
  function putChunk(url, blob, first, last, total, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open('PUT', url);
      xhr.setRequestHeader('Authorization', `Bearer ${token}`);
      xhr.setRequestHeader('Content-Type', 'application/octet-stream');
      xhr.setRequestHeader('Content-Range', `bytes ${first}-${last}/${total}`);
      xhr.upload.onprogress = (e) => onProgress(e.loaded);
      xhr.onload = () => {
        let json = null;
        try {
          json = JSON.parse(xhr.responseText);
        } catch {
          // fall through to the status check
        }
        if (xhr.status === 201 || xhr.status === 206) resolve({ done: xhr.status === 201, data: json?.data });
        else reject(new ApiError(xhr.status, json?.error?.code || 'http_error', json?.error?.message || `Server error ${xhr.status}`));
      };
      xhr.onerror = () => reject(new ApiError(0, 'network', 'Connection lost.'));
      xhr.ontimeout = () => reject(new ApiError(0, 'network', 'Connection timed out.'));
      xhr.send(blob);
    });
  }

  function chunkPlan(size) {
    if (size < MIN_CHUNK || size < CHUNK) return { chunk_size: size, total_chunk_count: 1 };
    return { chunk_size: CHUNK, total_chunk_count: Math.floor(size / CHUNK) };
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
      .map((j) => ({ publishId: j.publishId, title: j.title, fingerprint: j.fingerprint, size: j.size, chunkSize: j.chunkSize, totalChunks: j.totalChunks, final: j.final || null }));
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
      resumeInput: el.querySelector('.job-resume input'),
    };
    job.ui.title.textContent = job.title;
    job.ui.cancel.addEventListener('click', () => cancelJob(job));
    job.ui.watch.addEventListener('click', () => watchJob(job));
    job.ui.resumeInput.addEventListener('change', () => {
      const file = job.ui.resumeInput.files[0];
      job.ui.resumeInput.value = '';
      if (!file) return;
      if (fingerprint(file) !== job.fingerprint) {
        updateRow(job, { detail: "That's a different file. Pick the same video you started uploading." });
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
      job.el.dataset.state = state;
      ui.cancel.hidden = !['uploading', 'waiting', 'processing'].includes(state);
      ui.watch.hidden = state !== 'done';
      ui.resume.hidden = state !== 'paused';
      ui.bar.hidden = ['done', 'failed', 'cancelled'].includes(state);
    }
  }

  // ---------- upload ----------

  async function startUpload(file, title, caption, enhance) {
    const plan = chunkPlan(file.size);
    const job = {
      file,
      title: title || file.name.replace(/\.[^.]+$/, ''),
      fingerprint: fingerprint(file),
      size: file.size,
      chunkSize: plan.chunk_size,
      totalChunks: plan.total_chunk_count,
      publishId: null,
      cancelled: false,
    };
    createJobRow(job);
    updateRow(job, { stage: 'Starting upload…', progress: 0, detail: formatBytes(file.size), state: 'uploading' });

    try {
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
    job.uploadUrl = job.uploadUrl || `api/upload/${job.publishId}`;
    updateRow(job, { stage: 'Uploading…', state: 'uploading' });

    let status;
    try {
      status = await api('GET', `api/upload/${job.publishId}`);
    } catch (e) {
      updateRow(job, { stage: 'Upload failed', detail: e.message, state: 'failed' });
      return;
    }
    if (status.status !== 'UPLOADING') return pollJob(job);

    let doneBytes = status.uploaded_bytes;
    const started = performance.now();
    let sentThisSession = 0;
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
      if (job.cancelled) return;
      const [first, last] = chunkRange(job, index);
      const blob = job.file.slice(first, last + 1);
      for (let attempt = 0; ; attempt++) {
        try {
          const res = await putChunk(job.uploadUrl, blob, first, last, job.size, show);
          doneBytes += blob.size;
          sentThisSession += blob.size;
          show(0);
          if (res.done) return pollJob(job);
          break;
        } catch (e) {
          if (job.cancelled) return;
          const retryable = e.status === 0 || e.status >= 500 || e.code === 'incomplete_chunk';
          if (!retryable || attempt >= RETRIES) {
            updateRow(job, {
              stage: retryable ? 'Upload paused' : 'Upload failed',
              detail: retryable ? `${e.message} Pick the same file to resume.` : e.message,
              state: retryable ? 'paused' : 'failed',
            });
            return;
          }
          updateRow(job, { detail: `${e.message} Retrying…` });
          await sleep(1000 * 2 ** attempt);
        }
      }
    }
    pollJob(job); // every chunk was already there
  }

  async function pollJob(job) {
    let failures = 0;
    while (!job.cancelled) {
      let s;
      try {
        s = await api('GET', `api/upload/${job.publishId}`);
        failures = 0;
      } catch (e) {
        if (e.status === 404 || e.status === 401 || ++failures > 20) {
          updateRow(job, { stage: 'Lost track of this upload', detail: e.message, state: 'failed' });
          finish(job, 'failed');
          return;
        }
        await sleep(POLL_MS * 2);
        continue;
      }

      if (s.status === 'UPLOADING') {
        if (job.file) return runUpload(job);
        updateRow(job, {
          stage: 'Upload paused',
          progress: s.uploaded_bytes / s.video_size,
          detail: `${formatBytes(s.uploaded_bytes)} of ${formatBytes(s.video_size)} uploaded. Pick the same file to resume.`,
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

  function finish(job, final) {
    job.final = final;
    saveJobs();
  }

  async function cancelJob(job) {
    job.cancelled = true;
    updateRow(job, { stage: 'Cancelling…' });
    try {
      await api('DELETE', `api/upload/${job.publishId}`);
    } catch {
      // already gone
    }
    updateRow(job, { stage: 'Cancelled', detail: '', state: 'cancelled' });
    finish(job, 'cancelled');
  }

  async function addToFeed(job) {
    if (!job.video?.src || job.slide) return;
    // a video posted before this page loaded is already in the feed
    const existing = state.slides.find((s) => s.entry.src === job.video.src);
    if (existing) {
      job.slide = existing;
      return;
    }
    let entry = { ...job.video };
    try {
      // the manifest entry also carries resolution, fps, codec and bitrate
      const res = await fetch('videos.json', { cache: 'no-store' });
      const list = await res.json();
      entry = (Array.isArray(list) ? list : list.videos || []).find((e) => e.src === job.video.src) || entry;
    } catch {
      // the basic entry still plays
    }
    [job.slide] = addSlides([entry], { atTop: true });
  }

  function watchJob(job) {
    dialog.close();
    const slide = job.slide || state.slides.find((s) => s.entry.src === job.video?.src);
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
    if (!titleField.value) titleField.value = file.name.replace(/\.[^.]+$/, '').replace(/[_-]+/g, ' ');
    // read resolution and length locally; nothing is uploaded yet
    const probe = document.createElement('video');
    probe.preload = 'metadata';
    probe.muted = true;
    const url = URL.createObjectURL(file);
    probe.onloadedmetadata = () => {
      const { videoWidth: w, videoHeight: h, duration } = probe;
      const mins = Math.floor(duration / 60);
      const secs = String(Math.round(duration % 60)).padStart(2, '0');
      fileInfo.textContent = `${w}×${h} · ${mins}:${secs} · ${formatBytes(file.size)}`;
      URL.revokeObjectURL(url);
    };
    probe.onerror = () => {
      fileInfo.textContent = formatBytes(file.size);
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
    fileName.textContent = 'Choose a video';
    fileInfo.textContent = '';
  });

  openBtn.addEventListener('click', () => {
    formError.hidden = true;
    dialog.showModal();
  });
  dialog.querySelector('.sheet-close').addEventListener('click', () => dialog.close());
  dialog.addEventListener('click', (e) => {
    if (e.target === dialog) dialog.close(); // tap outside the sheet
  });
  document.getElementById('upload-play-local').addEventListener('click', () => dialog.close());

  // ---------- start ----------

  function forgetToken() {
    token = null;
    setUp = false;
    storage.set(TOKEN_KEY, null);
    openBtn.hidden = true;
    playLabel.hidden = false;
  }

  let setUp = false;
  let settingUp = null;

  function init() {
    const fromLink = /(?:^|[#&])upload=([\w-]+)/.exec(location.hash);
    if (fromLink) {
      token = fromLink[1];
      storage.set(TOKEN_KEY, token);
      history.replaceState(null, '', location.pathname + location.search); // keep the key out of the address bar
    } else if (!token) {
      token = storage.get(TOKEN_KEY, null);
    }
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
    if (!config?.uploads || setUp) return;
    setUp = true;
    processingAvailable = config.processing;
    if (!processingAvailable) {
      enhanceField.checked = false;
      enhanceField.disabled = true;
      enhanceField.closest('.switch').querySelector('small').textContent = "ffmpeg isn't installed on the server, so videos are posted as they are.";
    }
    openBtn.hidden = false;
    playLabel.hidden = true;

    for (const saved of storage.get(STORE_KEY, [])) {
      if (saved.final === 'cancelled') continue;
      const job = { ...saved, cancelled: false };
      jobs.set(job.publishId, job);
      createJobRow(job);
      updateRow(job, { stage: 'Checking…', progress: null });
      pollJob(job);
    }
  }

  // opening the upload link in a tab that already shows the site only changes the hash
  window.addEventListener('hashchange', init);
  init();
})();
