"""Chunked, resumable uploads and a processing queue.

The protocol follows TikTok's Content Posting API "media transfer" flow:

  1. POST /api/upload/init   {video_size, chunk_size, total_chunk_count, ...}
                             -> {publish_id, upload_url}
  2. PUT  <upload_url>       one request per chunk, with
                             Content-Range: bytes FIRST-LAST/TOTAL
                             -> 206 while chunks are missing, 201 when complete
  3. GET  /api/upload/<id>   poll status while the server processes the video

Chunk rules match TikTok's: chunks are 5-64 MiB, files under 5 MiB go up as
a single chunk, at most 1000 chunks, total_chunk_count = video_size //
chunk_size and the last chunk carries the remainder (up to 128 MiB). Like
TikTok, each reply carries Content-Range: bytes 0-N/TOTAL for the part
received so far. Unlike TikTok, chunks may arrive in any order and can be
re-sent, so an interrupted upload resumes by asking which are missing.
Failures carry TikTok-style reason codes such as file_format_check_failed.

Where TikTok then transcodes *down* to a ladder of smaller renditions, this
server makes one full-quality file: upscaled to 4K and interpolated to
120 fps when the source is lower (see enhance.py).

Locking: every read or write of an upload's fields and every _save() happens
while holding self.lock. Slow work (file I/O for chunks, ffmpeg) happens
outside it.
"""

import errno
import json
import os
import queue
import re
import secrets
import shutil
import threading
import time
import traceback
from pathlib import Path

import enhance

MB = 1024 * 1024
MIN_CHUNK = 5 * MB
MAX_CHUNK = 64 * MB
MAX_FINAL_CHUNK = 128 * MB
MAX_CHUNKS = 1000
COPY_BUFFER = 1 * MB
STALE_AFTER = 24 * 3600  # incomplete uploads are dropped after a day
SWEEP_EVERY = 3600
MAX_TITLE = 150
MAX_CAPTION = 2200
# Only containers the processing step accepts (see enhance.INPUT_FORMATS).
UPLOAD_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}

UPLOADING = "UPLOADING"
QUEUED = "QUEUED"
PROCESSING = "PROCESSING"
COMPLETE = "PUBLISH_COMPLETE"
FAILED = "FAILED"
CANCELLED = "CANCELLED"
FINAL = (COMPLETE, FAILED, CANCELLED)
PUBLISHING = "Publishing"


class UploadError(Exception):
    """An error the HTTP layer turns into a JSON response.

    body_consumed means the request body was (partly) read already, so the
    HTTP layer must not try to drain it.
    """

    def __init__(self, status, code, message, body_consumed=False):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.body_consumed = body_consumed


def slugify(text, fallback="video"):
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60].strip("-")
    return slug or fallback


def check_chunk_plan(size, chunk_size, total, max_bytes):
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise UploadError(400, "invalid_params", "video_size must be a positive number of bytes.")
    if size > max_bytes:
        raise UploadError(413, "file_too_large", f"This server accepts files up to {max_bytes / (1024 * MB):g} GB.")
    if not isinstance(chunk_size, int) or not isinstance(total, int) or isinstance(chunk_size, bool) or isinstance(total, bool):
        raise UploadError(400, "invalid_params", "chunk_size and total_chunk_count must be whole numbers.")
    if size < MIN_CHUNK:
        if chunk_size != size or total != 1:
            raise UploadError(400, "invalid_params", "Files under 5 MB are uploaded as one chunk: chunk_size = video_size, total_chunk_count = 1.")
        return
    if not MIN_CHUNK <= chunk_size <= MAX_CHUNK:
        raise UploadError(400, "invalid_params", "chunk_size must be between 5 MB and 64 MB.")
    if chunk_size > size:
        raise UploadError(400, "invalid_params", "chunk_size can't be larger than video_size.")
    if total != size // chunk_size:
        raise UploadError(400, "invalid_params", "total_chunk_count must be video_size // chunk_size (the last chunk carries the remainder).")
    if total > MAX_CHUNKS:
        raise UploadError(400, "invalid_params", "Use a bigger chunk_size: at most 1000 chunks are allowed.")
    if size - chunk_size * (total - 1) > MAX_FINAL_CHUNK:
        raise UploadError(400, "invalid_params", "The final chunk can be at most 128 MB.")


def chunk_bounds(upload, index):
    """Byte range [first, last] that chunk `index` must cover."""
    first = index * upload["chunk_size"]
    last = upload["video_size"] - 1 if index == upload["total_chunk_count"] - 1 else first + upload["chunk_size"] - 1
    return first, last


def received_bytes(upload):
    return sum(chunk_bounds(upload, i)[1] - chunk_bounds(upload, i)[0] + 1 for i in upload["received"])


def received_prefix(upload):
    """Bytes received without gaps from the start, for TikTok's Content-Range reply."""
    index = 0
    while index in upload["received"]:
        index += 1
    return chunk_bounds(upload, index - 1)[1] + 1 if index else 0


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
        """Write state.json atomically. Call with self.lock held."""
        path = self._dir(upload["publish_id"]) / "state.json"
        tmp = path.with_name(f"state.{threading.get_ident()}.tmp")
        data = {k: v for k, v in upload.items() if not k.startswith("_")}
        data["received"] = sorted(upload["received"])
        tmp.write_text(json.dumps(data))
        os.replace(tmp, path)

    def _load(self):
        """Pick up uploads from a previous run, so a restart doesn't lose them."""
        pending = []
        for state in self.root.glob("*/state.json"):
            try:
                upload = json.loads(state.read_text())
                upload["received"] = set(upload.get("received", []))
                upload["publish_id"]
            except (OSError, json.JSONDecodeError, KeyError, TypeError):
                continue
            if upload.get("status") in (QUEUED, PROCESSING):
                upload["status"] = QUEUED
                pending.append(upload)
            self.uploads[upload["publish_id"]] = upload
        for upload in sorted(pending, key=lambda u: u.get("queued_at") or u.get("created", 0)):
            self.queue.put(upload["publish_id"])
        self._sweep()

    def _sweep(self):
        """Drop uploads abandoned for a day, and finish any cleanup that failed earlier."""
        now = time.time()
        to_clean = []
        with self.lock:
            for upload in self.uploads.values():
                if upload["status"] == UPLOADING and now - upload.get("updated", 0) > STALE_AFTER:
                    upload.update(status=CANCELLED, fail_reason="The upload was never finished.", updated=now)
                    self._try_save(upload)
                if upload["status"] in FINAL:
                    to_clean.append(upload)
        for upload in to_clean:
            self._cleanup(upload)

    def _try_save(self, upload):
        try:
            self._save(upload)
        except OSError:
            traceback.print_exc()

    # ---------- step 1: init ----------

    def init(self, body):
        size = body.get("video_size")
        chunk_size = body.get("chunk_size")
        total = body.get("total_chunk_count")
        check_chunk_plan(size, chunk_size, total, self.max_bytes)
        if not enhance.available():
            raise UploadError(503, "processing_unavailable", "Install ffmpeg on the computer running serve.py to enable uploads.")

        cover_ms = body.get("video_cover_timestamp_ms", 1000)  # same name as TikTok's post_info field
        if not isinstance(cover_ms, int) or isinstance(cover_ms, bool) or cover_ms < 0:
            raise UploadError(400, "invalid_params", "video_cover_timestamp_ms must be a whole number of milliseconds.")
        filename = str(body.get("filename") or "video.mp4")[:200]
        ext = Path(filename).suffix.lower()
        if ext not in UPLOAD_EXTS:
            ext = ".mp4"
        publish_id = secrets.token_hex(12)
        now = time.time()
        upload = {
            "publish_id": publish_id,
            "filename": filename,
            "ext": ext,
            "title": str(body.get("title") or Path(filename).stem)[:MAX_TITLE].strip(),
            "caption": str(body.get("caption") or "")[:MAX_CAPTION].strip(),
            "enhance": body.get("enhance", True) is not False,
            "cover_ms": cover_ms,
            "video_size": size,
            "chunk_size": chunk_size,
            "total_chunk_count": total,
            "received": set(),
            "status": UPLOADING,
            "created": now,
            "updated": now,
        }
        with self.lock:
            # source files are sparse, so count what other unfinished uploads will still write
            promised = sum(u["video_size"] - received_bytes(u) for u in self.uploads.values() if u["status"] == UPLOADING)
            if shutil.disk_usage(self.root).free - promised < size * 2:
                raise UploadError(507, "insufficient_storage", "The server is low on disk space for this upload.")
            self.uploads[publish_id] = upload
        try:
            self._dir(publish_id).mkdir(parents=True)
            with open(self._source(upload), "wb") as f:
                f.truncate(size)
            with self.lock:
                self._save(upload)
        except OSError:
            with self.lock:
                self.uploads.pop(publish_id, None)
            shutil.rmtree(self._dir(publish_id), ignore_errors=True)
            raise UploadError(507, "insufficient_storage", "The server couldn't reserve space for this upload.")
        return {
            "publish_id": publish_id,
            "upload_url": f"/api/upload/{publish_id}",
            "chunk_size": chunk_size,
            "total_chunk_count": total,
        }

    # ---------- step 2: chunks ----------

    def put_chunk(self, publish_id, content_range, length, rfile):
        upload = self._get(publish_id)
        match = re.fullmatch(r"bytes (\d{1,15})-(\d{1,15})/(\d{1,15})", (content_range or "").strip())
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

        with self.lock:
            status = upload["status"]
            already = index in upload["received"]
        if status != UPLOADING:
            if already and status != CANCELLED:
                # a re-send of a chunk we have (e.g. its 201 got lost): answer as if it just arrived
                self._discard(rfile, length)
                return True, self.status(publish_id)
            raise UploadError(409, "upload_closed", "This upload isn't accepting chunks anymore.")

        written = 0
        try:
            with open(self._source(upload), "r+b") as f:
                f.seek(first)
                while written < length:
                    data = rfile.read(min(COPY_BUFFER, length - written))
                    if not data:
                        break
                    f.write(data)
                    written += len(data)
        except FileNotFoundError:
            raise UploadError(409, "upload_closed", "This upload was cancelled.", body_consumed=True)
        except OSError as e:
            full = e.errno == errno.ENOSPC
            raise UploadError(507 if full else 500, "insufficient_storage" if full else "write_failed",
                              "The server ran out of disk space." if full else "The server couldn't save this chunk.",
                              body_consumed=True)
        if written != length:
            raise UploadError(400, "incomplete_chunk", "The chunk was cut off. Send it again.", body_consumed=True)

        with self.lock:
            if upload["status"] != UPLOADING:  # cancelled while this chunk was being written
                raise UploadError(409, "upload_closed", "This upload isn't accepting chunks anymore.", body_consumed=True)
            upload["received"].add(index)
            upload["updated"] = time.time()
            done = len(upload["received"]) == upload["total_chunk_count"]
            if done:
                upload["status"] = QUEUED
                upload["queued_at"] = time.time()
            self._save(upload)
        if done:
            self.queue.put(publish_id)
        return done, self.status(publish_id)

    @staticmethod
    def _discard(rfile, length):
        while length > 0:
            data = rfile.read(min(COPY_BUFFER, length))
            if not data:
                break
            length -= len(data)

    # ---------- step 3: status ----------

    def status(self, publish_id):
        upload = self._get(publish_id)
        with self.lock:
            result = {
                "publish_id": publish_id,
                "status": upload["status"],
                "title": upload["title"],
                "enhance": upload["enhance"],
                "video_size": upload["video_size"],
                "uploaded_bytes": received_bytes(upload),
                "received_prefix": received_prefix(upload),
                "missing_chunks": [i for i in range(upload["total_chunk_count"]) if i not in upload["received"]],
            }
            for key in ("stage", "progress", "eta_seconds", "speed", "fail_reason", "fail_code", "video", "source", "target"):
                if upload.get(key) is not None:
                    result[key] = upload[key]
            if upload["status"] == QUEUED:
                mine = upload.get("queued_at", 0)
                result["queue_position"] = sum(
                    1 for u in self.uploads.values()
                    if u["status"] == PROCESSING or (u["status"] == QUEUED and u.get("queued_at", 0) < mine)
                )
        return result

    def cancel(self, publish_id):
        upload = self._get(publish_id)
        event = None
        with self.lock:
            if upload["status"] == PROCESSING and upload.get("stage") == PUBLISHING:
                raise UploadError(409, "too_late", "This video is already being posted.")
            finished = upload["status"] in FINAL
            if not finished:
                upload["status"] = CANCELLED
                upload["updated"] = time.time()
                event = self.cancel_events.get(publish_id)
                if event:
                    event.set()
                self._try_save(upload)
        if not finished and not event:  # not being processed right now, so nothing else will clean up
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
            try:
                publish_id = self.queue.get(timeout=SWEEP_EVERY)
            except queue.Empty:
                self._sweep()
                continue
            with self.lock:
                upload = self.uploads.get(publish_id)
                runnable = upload is not None and upload["status"] == QUEUED and not self.stopping
                if runnable:
                    self.idle.clear()
            if not runnable:
                continue
            try:
                self._process(upload)
            except Exception:  # one bad job must never stop the queue
                traceback.print_exc()
            finally:
                self.idle.set()

    def _set(self, upload, **fields):
        with self.lock:
            upload.update(fields)
            upload["updated"] = time.time()

    def _fail(self, upload, reason, code="internal"):
        with self.lock:
            if upload["status"] == PROCESSING:
                upload.update(status=FAILED, fail_reason=reason, fail_code=code, updated=time.time())
                self._try_save(upload)

    def _process(self, upload):
        publish_id = upload["publish_id"]
        cancel = threading.Event()
        with self.lock:
            if upload["status"] != QUEUED:
                return
            self.cancel_events[publish_id] = cancel
            upload.update(status=PROCESSING, stage="Checking the video", progress=0.0)

        work_dir = self._dir(publish_id)
        try:
            with self.lock:
                self._save(upload)
            info = enhance.probe(self._source(upload))
            plan = enhance.make_plan(info, enhance=upload["enhance"])
            need = plan["estimated_bytes"] * 1.2 + 200 * MB
            if shutil.disk_usage(self.video_dir).free < need:
                raise enhance.EnhanceError("The server doesn't have enough disk space to save the processed video.", "insufficient_storage")
            self._set(upload, stage=plan["summary"], source=plan["source_label"], target=plan["target_label"])

            def on_progress(fraction, eta, speed):
                self._set(upload, progress=round(fraction, 4), eta_seconds=eta, speed=speed)

            outs = [work_dir / f"out{o['suffix']}.mp4" for o in plan["outputs"]]
            enhance.run(self._source(upload), outs, plan, on_progress=on_progress, cancel=cancel)

            with self.lock:
                if cancel.is_set():
                    raise enhance.Cancelled()
                upload.update(stage=PUBLISHING, progress=1.0, eta_seconds=0)  # from here on cancel() says "too late"
            video = self._publish(upload, plan, outs)
            with self.lock:
                upload.update(status=COMPLETE, stage="Done", video=video, updated=time.time())
                self._try_save(upload)
        except enhance.Cancelled:
            pass  # cancel() already set the status, or the server is shutting down
        except enhance.EnhanceError as e:
            self._fail(upload, str(e), e.code)
        except Exception:
            traceback.print_exc()
            self._fail(upload, "Processing failed unexpectedly. The server's terminal shows the details.")
        finally:
            with self.lock:
                self.cancel_events.pop(publish_id, None)
                keep = upload["status"] == PROCESSING  # interrupted by a shutdown: resume next time
            if not keep:
                self._cleanup(upload)

    def _publish(self, upload, plan, outs):
        """Move the finished files into videos/ so the video appears in the feed all at once.

        The first output (HEVC) is the main file; others (AV1) sit next to it
        as name.av1.mp4 and are listed in the sidecar's "sources".
        """
        name = self._unique_name(upload["title"] or Path(upload["filename"]).stem)
        finals = [self.video_dir / f"{name}{o['suffix']}.mp4" for o in plan["outputs"]]
        parts = [f.with_name(f".{f.name}.part") for f in finals]  # hidden from the feed and from static serving
        poster = self.video_dir / f"{name}.jpg"
        sidecar = self.video_dir / f"{name}.json"
        meta = {
            "title": upload["title"],
            "caption": upload["caption"],
            "enhanced_from": plan["source_label"] if plan["enhanced"] else None,
            "uploaded": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        srcs = enhance.sources(plan, [f"videos/{f.name}" for f in finals])
        if len(srcs) > 1:
            meta["sources"] = srcs
        created = [*parts, *finals[1:], poster, sidecar, sidecar.with_name(f".{sidecar.name}.part")]
        try:
            try:
                enhance.make_poster(outs[0], poster, at=min(upload.get("cover_ms", 1000) / 1000, max(plan["duration"] - 0.1, 0)))
                meta["poster"] = f"videos/{poster.name}"
            except enhance.EnhanceError:
                pass
            for out, part in zip(outs, parts):
                shutil.move(str(out), str(part))  # may copy when videos/ is on another drive
            for part, final in list(zip(parts, finals))[1:]:
                os.replace(part, final)  # alternates first: nothing lists them until the main file exists
            tmp = sidecar.with_name(f".{sidecar.name}.part")
            tmp.write_text(json.dumps(meta, indent=2))
            os.replace(tmp, sidecar)
            os.replace(parts[0], finals[0])  # atomic: the feed sees the whole video or nothing
        except BaseException:
            for path in created:
                path.unlink(missing_ok=True)
            raise
        return {"src": f"videos/{finals[0].name}", **meta}

    def _cleanup(self, upload):
        """Delete the uploaded source and temp output; keep state.json for status lookups.

        Windows can refuse to delete a file another request still has open;
        the hourly sweep tries again.
        """
        directory = self._dir(upload["publish_id"])
        if not directory.is_dir():
            return
        for path in directory.iterdir():
            if path.name != "state.json":
                try:
                    path.unlink()
                except OSError:
                    pass

    def _unique_name(self, title):
        base = slugify(title)
        # any file sharing the name (any extension, any case) would pick up our sidecar and poster
        taken = {p.name.lower().lstrip(".").split(".")[0] for p in self.video_dir.iterdir()}
        name, n = base, 2
        while name in taken:
            name, n = f"{base}-{n}", n + 1
        return name

    # ---------- helpers ----------

    def _get(self, publish_id):
        upload = self.uploads.get(publish_id) if re.fullmatch(r"[0-9a-f]{24}", publish_id or "") else None
        if not upload:
            raise UploadError(404, "not_found", "No upload with that publish_id.")
        return upload
