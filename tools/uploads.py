"""Chunked, resumable uploads and a processing queue.

The protocol follows TikTok's Content Posting API "media transfer" flow:

  1. POST /api/upload/init   {video_size, chunk_size, total_chunk_count, ...}
                             -> {publish_id, upload_url}
  2. PUT  <upload_url>       one request per chunk, with
                             Content-Range: bytes FIRST-LAST/TOTAL
                             -> 206 while chunks are missing, 201 when complete
  3. GET  /api/upload/<id>   poll status while the server processes the video

Chunk rules match TikTok's: chunks are 5-64 MB, files under 5 MB go up as a
single chunk, total_chunk_count = video_size // chunk_size and the last chunk
carries the remainder. Any chunk can be re-sent, so an interrupted upload
resumes by asking which chunks are still missing.

Where TikTok then transcodes *down* to a ladder of smaller renditions, this
server makes one full-quality file: upscaled to 4K and interpolated to
120 fps when the source is lower (see enhance.py).
"""

import json
import queue
import re
import secrets
import shutil
import threading
import time
from pathlib import Path

import enhance

MB = 1024 * 1024
MIN_CHUNK = 5 * MB
MAX_CHUNK = 64 * MB
MAX_FINAL_CHUNK = 128 * MB
COPY_BUFFER = 1 * MB
STALE_AFTER = 24 * 3600  # incomplete uploads are dropped after a day
MAX_TITLE = 150
MAX_CAPTION = 2200

UPLOADING = "UPLOADING"
QUEUED = "QUEUED"
PROCESSING = "PROCESSING"
COMPLETE = "PUBLISH_COMPLETE"
FAILED = "FAILED"
CANCELLED = "CANCELLED"


class UploadError(Exception):
    """An error the HTTP layer turns into a JSON response."""

    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def slugify(text, fallback="video"):
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60].strip("-")
    return slug or fallback


def check_chunk_plan(size, chunk_size, total, max_bytes):
    if not isinstance(size, int) or size <= 0:
        raise UploadError(400, "invalid_params", "video_size must be a positive number of bytes.")
    if size > max_bytes:
        raise UploadError(413, "file_too_large", f"This server accepts files up to {max_bytes // (1024 * MB)} GB.")
    if not isinstance(chunk_size, int) or not isinstance(total, int):
        raise UploadError(400, "invalid_params", "chunk_size and total_chunk_count must be numbers.")
    if size < MIN_CHUNK:
        if chunk_size != size or total != 1:
            raise UploadError(400, "invalid_params", "Files under 5 MB are uploaded as one chunk: chunk_size = video_size, total_chunk_count = 1.")
        return
    if not MIN_CHUNK <= chunk_size <= MAX_CHUNK:
        raise UploadError(400, "invalid_params", "chunk_size must be between 5 MB and 64 MB.")
    if total != size // chunk_size:
        raise UploadError(400, "invalid_params", "total_chunk_count must be video_size // chunk_size (the last chunk carries the remainder).")
    if size - chunk_size * (total - 1) > MAX_FINAL_CHUNK:
        raise UploadError(400, "invalid_params", "The final chunk can be at most 128 MB.")


def chunk_bounds(upload, index):
    """Byte range [first, last] that chunk `index` must cover."""
    first = index * upload["chunk_size"]
    last = upload["video_size"] - 1 if index == upload["total_chunk_count"] - 1 else first + upload["chunk_size"] - 1
    return first, last


class UploadStore:
    def __init__(self, root, video_dir, max_bytes):
        self.root = Path(root)
        self.video_dir = Path(video_dir)
        self.max_bytes = max_bytes
        self.uploads = {}
        self.cancel_events = {}
        self.stopping = False
        self.idle = threading.Event()
        self.idle.set()
        self.lock = threading.Lock()
        self.queue = queue.Queue()
        self.root.mkdir(exist_ok=True)
        self._load()
        threading.Thread(target=self._work, name="video-processing", daemon=True).start()

    # ---------- persistence ----------

    def _dir(self, publish_id):
        return self.root / publish_id

    def _source(self, upload):
        return self._dir(upload["publish_id"]) / f"source{upload['ext']}"

    def _save(self, upload):
        path = self._dir(upload["publish_id"]) / "state.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({k: v for k, v in upload.items() if not k.startswith("_")}))
        tmp.replace(path)

    def _load(self):
        """Pick up uploads from a previous run, so a restart doesn't lose them."""
        now = time.time()
        pending = []
        for state in self.root.glob("*/state.json"):
            try:
                upload = json.loads(state.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if upload.get("status") == UPLOADING and now - upload.get("updated", 0) > STALE_AFTER:
                shutil.rmtree(state.parent, ignore_errors=True)
                continue
            upload["received"] = set(upload.get("received", []))
            if upload.get("status") in (QUEUED, PROCESSING):
                upload["status"] = QUEUED
                pending.append(upload)
            self.uploads[upload["publish_id"]] = upload
        for upload in sorted(pending, key=lambda u: u.get("created", 0)):
            self.queue.put(upload["publish_id"])

    # ---------- step 1: init ----------

    def init(self, body):
        size = body.get("video_size")
        chunk_size = body.get("chunk_size")
        total = body.get("total_chunk_count")
        check_chunk_plan(size, chunk_size, total, self.max_bytes)

        free = shutil.disk_usage(self.root).free
        if free < size * 2:
            raise UploadError(507, "insufficient_storage", "The server is low on disk space for this upload.")

        filename = str(body.get("filename") or "video.mp4")[:200]
        ext = Path(filename).suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,5}", ext):
            ext = ".mp4"
        publish_id = secrets.token_hex(12)
        upload = {
            "publish_id": publish_id,
            "filename": filename,
            "ext": ext,
            "title": str(body.get("title") or Path(filename).stem)[:MAX_TITLE].strip(),
            "caption": str(body.get("caption") or "")[:MAX_CAPTION].strip(),
            "enhance": body.get("enhance", True) is not False,
            "video_size": size,
            "chunk_size": chunk_size,
            "total_chunk_count": total,
            "received": set(),
            "status": UPLOADING,
            "created": time.time(),
            "updated": time.time(),
        }
        self._dir(publish_id).mkdir(parents=True)
        with open(self._source(upload), "wb") as f:
            f.truncate(size)
        with self.lock:
            self.uploads[publish_id] = upload
            self._save(self._serializable(upload))
        return {
            "publish_id": publish_id,
            "upload_url": f"/api/upload/{publish_id}",
            "chunk_size": chunk_size,
            "total_chunk_count": total,
        }

    # ---------- step 2: chunks ----------

    def put_chunk(self, publish_id, content_range, length, rfile):
        upload = self._get(publish_id)
        if upload["status"] != UPLOADING:
            raise UploadError(409, "upload_closed", "This upload isn't accepting chunks anymore.")

        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", (content_range or "").strip())
        if not match:
            raise UploadError(400, "invalid_range", "Each chunk needs a Content-Range: bytes FIRST-LAST/TOTAL header.")
        first, last, total_size = map(int, match.groups())
        if total_size != upload["video_size"]:
            raise UploadError(400, "invalid_range", "Content-Range total doesn't match video_size.")
        index, offset = divmod(first, upload["chunk_size"])
        if offset or index >= upload["total_chunk_count"] or (first, last) != chunk_bounds(upload, index):
            raise UploadError(400, "invalid_range", "Chunks must follow the chunk_size and total_chunk_count from init.")
        if length != last - first + 1:
            raise UploadError(400, "invalid_range", "Content-Length doesn't match the Content-Range.")

        written = 0
        with open(self._source(upload), "r+b") as f:
            f.seek(first)
            while written < length:
                data = rfile.read(min(COPY_BUFFER, length - written))
                if not data:
                    break
                f.write(data)
                written += len(data)
        if written != length:
            raise UploadError(400, "incomplete_chunk", "The chunk was cut off. Send it again.")

        with self.lock:
            upload["received"].add(index)
            upload["updated"] = time.time()
            done = len(upload["received"]) == upload["total_chunk_count"] and upload["status"] == UPLOADING
            if done:
                upload["status"] = QUEUED
            self._save(self._serializable(upload))
        if done:
            self.queue.put(publish_id)
        return done, self.status(publish_id)

    # ---------- step 3: status ----------

    def status(self, publish_id):
        upload = self._get(publish_id)
        with self.lock:
            received = sorted(upload["received"])
            missing = [i for i in range(upload["total_chunk_count"]) if i not in upload["received"]]
            uploaded = sum(chunk_bounds(upload, i)[1] - chunk_bounds(upload, i)[0] + 1 for i in received)
            result = {
                "publish_id": publish_id,
                "status": upload["status"],
                "title": upload["title"],
                "enhance": upload["enhance"],
                "video_size": upload["video_size"],
                "uploaded_bytes": uploaded,
                "missing_chunks": missing,
            }
            for key in ("stage", "progress", "eta_seconds", "speed", "fail_reason", "video", "source", "target"):
                if upload.get(key) is not None:
                    result[key] = upload[key]
            if upload["status"] == QUEUED:
                waiting = [u for u in self.uploads.values() if u["status"] in (QUEUED, PROCESSING)]
                result["queue_position"] = sorted(waiting, key=lambda u: u["created"]).index(upload)
        return result

    def cancel(self, publish_id):
        upload = self._get(publish_id)
        with self.lock:
            if upload["status"] in (COMPLETE, FAILED, CANCELLED):
                return self.status(publish_id)
            upload["status"] = CANCELLED
            upload["updated"] = time.time()
            event = self.cancel_events.get(publish_id)
            if event:
                event.set()
            self._save(self._serializable(upload))
        if not event:  # not being processed right now, so nothing else will clean up
            self._cleanup(upload)
        return self.status(publish_id)

    # ---------- processing ----------

    def shutdown(self, timeout=10):
        """Stop ffmpeg before the server exits. Interrupted jobs restart on the next run."""
        with self.lock:
            self.stopping = True
            for event in self.cancel_events.values():
                event.set()
        self.idle.wait(timeout)

    def _work(self):
        while True:
            publish_id = self.queue.get()
            upload = self.uploads.get(publish_id)
            if upload and upload["status"] == QUEUED and not self.stopping:
                self.idle.clear()
                try:
                    self._process(upload)
                finally:
                    self.idle.set()

    def _set(self, upload, **fields):
        with self.lock:
            upload.update(fields)
            upload["updated"] = time.time()

    def _process(self, upload):
        publish_id = upload["publish_id"]
        cancel = threading.Event()
        with self.lock:
            if upload["status"] != QUEUED:
                return
            self.cancel_events[publish_id] = cancel
            upload.update(status=PROCESSING, stage="Checking the video", progress=0.0)
            self._save(self._serializable(upload))

        work_dir = self._dir(publish_id)
        try:
            info = enhance.probe(self._source(upload))
            plan = enhance.make_plan(info, enhance=upload["enhance"])
            need = plan["estimated_bytes"] * 1.2 + 200 * MB
            if shutil.disk_usage(self.video_dir).free < need:
                raise enhance.EnhanceError("The server doesn't have enough disk space to save the processed video.")
            self._set(upload, stage=plan["summary"], source=plan["source_label"], target=plan["target_label"])

            def on_progress(fraction, eta, speed):
                self._set(upload, progress=round(fraction, 4), eta_seconds=eta, speed=speed)

            out = work_dir / "out.mp4"
            enhance.run(self._source(upload), out, plan, on_progress=on_progress, cancel=cancel)

            self._set(upload, stage="Publishing", progress=1.0, eta_seconds=0)
            name = self._unique_name(upload["title"] or Path(upload["filename"]).stem)
            final = self.video_dir / f"{name}.mp4"
            poster = self.video_dir / f"{name}.jpg"
            try:
                enhance.make_poster(out, poster)
            except enhance.EnhanceError:
                poster = None
            meta = {
                "title": upload["title"],
                "caption": upload["caption"],
                "enhanced_from": plan["source_label"] if plan["enhanced"] else None,
                "uploaded": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            if poster:
                meta["poster"] = f"videos/{poster.name}"
            (self.video_dir / f"{name}.json").write_text(json.dumps(meta, indent=2))
            shutil.move(str(out), str(final))  # last, so the feed never lists a half-written file

            with self.lock:
                upload.update(status=COMPLETE, stage="Done", video={"src": f"videos/{final.name}", **meta})
                self._save(self._serializable(upload))
        except enhance.Cancelled:
            pass  # cancel() already set the status, or the server is shutting down
        except enhance.EnhanceError as e:
            with self.lock:
                upload.update(status=FAILED, fail_reason=str(e))
                self._save(self._serializable(upload))
        except Exception as e:  # never let one bad file kill the worker thread
            with self.lock:
                upload.update(status=FAILED, fail_reason=f"Processing failed unexpectedly: {e}")
                self._save(self._serializable(upload))
        finally:
            with self.lock:
                self.cancel_events.pop(publish_id, None)
            if upload["status"] != PROCESSING:  # keep the source when interrupted by a shutdown
                self._cleanup(upload)

    def _cleanup(self, upload):
        """Delete the uploaded source and temp output; keep state.json for status lookups."""
        for path in self._dir(upload["publish_id"]).iterdir():
            if path.name != "state.json":
                path.unlink(missing_ok=True)

    def _unique_name(self, title):
        base = slugify(title)
        name, n = base, 2
        while any((self.video_dir / f"{name}{ext}").exists() for ext in (".mp4", ".json", ".jpg")):
            name, n = f"{base}-{n}", n + 1
        return name

    # ---------- helpers ----------

    def _get(self, publish_id):
        upload = self.uploads.get(publish_id) if re.fullmatch(r"[0-9a-f]{24}", publish_id or "") else None
        if not upload:
            raise UploadError(404, "not_found", "No upload with that publish_id.")
        return upload

    @staticmethod
    def _serializable(upload):
        return {**upload, "received": sorted(upload["received"])}
