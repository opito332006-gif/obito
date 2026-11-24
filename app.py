"""OBITO Stems - music separation website (Flask)."""
import collections
import hmac
import io
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
import zipfile

from flask import (Flask, abort, jsonify, render_template, request,
                   send_file)

import separator

BASE = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.environ.get("OBITO_MODEL", os.path.join(BASE, "models", "htdemucs_fp16weights.onnx"))
WORK_DIR = os.environ.get("OBITO_WORK_DIR", os.path.join(BASE, "work"))
MAX_UPLOAD_MB = int(os.environ.get("OBITO_MAX_UPLOAD_MB", "60"))
MAX_MINUTES = int(os.environ.get("OBITO_MAX_MINUTES", "10"))
TTL_MINUTES = int(os.environ.get("OBITO_TTL_MINUTES", "60"))
MAX_QUEUE = int(os.environ.get("OBITO_MAX_QUEUE", "5"))
UPLOADS_PER_HOUR = int(os.environ.get("OBITO_UPLOADS_PER_HOUR", "10"))
ACCESS_CODE = os.environ.get("OBITO_ACCESS_CODE", "")      # optional gate
TRUST_CF = os.environ.get("OBITO_TRUST_CLOUDFLARE", "") == "1"
KEEP_MODEL = os.environ.get("OBITO_KEEP_MODEL", "") == "1"
OUTPUT_FORMAT = os.environ.get("OBITO_OUTPUT_FORMAT", "mp3")  # mp3 | wav
THREADS = int(os.environ.get("OBITO_THREADS", "0")) or None

ALLOWED_EXT = {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".opus", ".aac", ".mp4", ".webm"}
ID_RE = re.compile(r"^[0-9a-f]{32}$")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
os.makedirs(WORK_DIR, exist_ok=True)

jobs = {}
jobs_lock = threading.Lock()
job_queue = queue.Queue(maxsize=MAX_QUEUE)
hits = collections.defaultdict(collections.deque)
_cached = {}


# ---------- helpers ----------
def client_ip():
    if TRUST_CF and request.headers.get("CF-Connecting-IP"):
        return request.headers["CF-Connecting-IP"]
    return request.remote_addr or "?"


def rate_limited(ip):
    now = time.time()
    dq = hits[ip]
    while dq and now - dq[0] > 3600:
        dq.popleft()
    if len(dq) >= UPLOADS_PER_HOUR:
        return True
    dq.append(now)
    return False


def job_dir(job_id):
    return os.path.join(WORK_DIR, job_id)


def get_infer():
    """Loads the ONNX model (cached if OBITO_KEEP_MODEL=1)."""
    if "fn" in _cached:
        return _cached["fn"], (lambda: None)
    if not os.path.exists(MODEL_PATH):
        raise separator.SeparationError("Model file missing - run download_model.py")
    infer, close = separator.load_onnx_infer(MODEL_PATH, THREADS)
    if KEEP_MODEL:
        _cached["fn"] = infer
        return infer, (lambda: None)
    return infer, close


def set_job(job_id, **kw):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(kw)


def encode_stems(jdir, paths, fmt):
    out = {}
    for stem, wav in paths.items():
        if fmt == "mp3":
            dst = os.path.join(jdir, f"{stem}.mp3")
            r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", wav,
                                "-codec:a", "libmp3lame", "-b:a", "256k", dst],
                               capture_output=True)
            if r.returncode == 0:
                os.remove(wav)
                out[stem] = dst
                continue
        out[stem] = wav            # wav fallback
    return out


def run_job(job_id):
    jdir = job_dir(job_id)
    src = os.path.join(jdir, "input")
    close = lambda: None
    try:
        set_job(job_id, status="decoding", progress=0.0)
        audio = separator.decode_audio(src, MAX_MINUTES * 60)
        os.remove(src)
        set_job(job_id, status="loading")
        infer, close = get_infer()
        set_job(job_id, status="processing")
        paths = separator.separate(
            audio, infer, jdir,
            on_progress=lambda p: set_job(job_id, progress=round(p, 3)),
            should_cancel=lambda: job_id not in jobs)
        del audio
        set_job(job_id, status="encoding", progress=1.0)
        files = encode_stems(jdir, paths, OUTPUT_FORMAT)
        set_job(job_id, status="done", files={k: os.path.basename(v) for k, v in files.items()})
    except separator.SeparationError as e:
        set_job(job_id, status="error", error=str(e))
    except Exception as e:                      # noqa: BLE001
        app.logger.exception("job failed")
        set_job(job_id, status="error", error="Separation failed")
    finally:
        try:
            close()
        except Exception:                       # noqa: BLE001
            pass


def worker():
    while True:
        job_id = job_queue.get()
        if job_id in jobs:
            run_job(job_id)
        job_queue.task_done()


def cleaner():
    while True:
        time.sleep(60)
        cutoff = time.time() - TTL_MINUTES * 60
        with jobs_lock:
            old = [j for j, v in jobs.items()
                   if v["created"] < cutoff and v["status"] in ("done", "error")]
            for j in old:
                jobs.pop(j, None)
        for j in old:
            shutil.rmtree(job_dir(j), ignore_errors=True)


def start_threads():
    # wipe leftovers from a previous run
    for d in os.listdir(WORK_DIR):
        shutil.rmtree(os.path.join(WORK_DIR, d), ignore_errors=True)
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=cleaner, daemon=True).start()


# ---------- security ----------
@app.before_request
def gate():
    if ACCESS_CODE and request.path.startswith("/api/"):
        given = request.headers.get("X-Access-Code", "") or request.args.get("code", "")
        if not hmac.compare_digest(given, ACCESS_CODE):
            return jsonify(error="access_code"), 401


@app.after_request
def headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; media-src 'self' blob:; "
        "style-src 'self'; script-src 'self'; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------- routes ----------
@app.get("/")
def index():
    return render_template("index.html", max_mb=MAX_UPLOAD_MB, max_min=MAX_MINUTES,
                           ttl=TTL_MINUTES, gated=bool(ACCESS_CODE))


@app.post("/api/upload")
def upload():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify(error="no_file"), 400
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ALLOWED_EXT:
        return jsonify(error="bad_type"), 400
    if job_queue.full():
        return jsonify(error="busy"), 503
    if rate_limited(client_ip()):
        return jsonify(error="rate"), 429
    job_id = uuid.uuid4().hex
    jdir = job_dir(job_id)
    os.makedirs(jdir)
    f.save(os.path.join(jdir, "input"))
    with jobs_lock:
        jobs[job_id] = {"status": "queued", "progress": 0.0, "created": time.time(),
                        "name": os.path.basename(f.filename)[:120], "files": {}, "error": ""}
    try:
        job_queue.put_nowait(job_id)
    except queue.Full:
        with jobs_lock:
            jobs.pop(job_id, None)
        shutil.rmtree(jdir, ignore_errors=True)
        return jsonify(error="busy"), 503
    return jsonify(id=job_id)


def get_job(job_id):
    if not ID_RE.match(job_id):
        abort(404)
    with jobs_lock:
        j = jobs.get(job_id)
        if not j:
            abort(404)
        return dict(j)


@app.get("/api/status/<job_id>")
def status(job_id):
    j = get_job(job_id)
    pos = 0
    if j["status"] == "queued":
        with jobs_lock:
            order = sorted((v["created"], k) for k, v in jobs.items() if v["status"] == "queued")
        pos = [k for _, k in order].index(job_id) + 1
    return jsonify(status=j["status"], progress=j["progress"], error=j["error"],
                   name=j["name"], stems=list(j["files"].keys()), position=pos)


@app.get("/api/stem/<job_id>/<stem>")
def stem(job_id, stem):
    j = get_job(job_id)
    if j["status"] != "done" or stem not in j["files"]:
        abort(404)
    path = os.path.join(job_dir(job_id), j["files"][stem])
    base = os.path.splitext(j["name"])[0] or "song"
    base = re.sub(r"[^\w\-. ]", "_", base)[:80]
    ext = os.path.splitext(path)[1]
    return send_file(path, as_attachment=request.args.get("dl") == "1",
                     download_name=f"{base} - {stem}{ext}")


@app.get("/api/zip/<job_id>")
def zip_all(job_id):
    j = get_job(job_id)
    if j["status"] != "done":
        abort(404)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for s, fn in j["files"].items():
            z.write(os.path.join(job_dir(job_id), fn), fn)
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name="OBITO-stems.zip")


@app.delete("/api/job/<job_id>")
def delete(job_id):
    get_job(job_id)
    with jobs_lock:
        jobs.pop(job_id, None)
    shutil.rmtree(job_dir(job_id), ignore_errors=True)
    return jsonify(ok=True)


@app.errorhandler(413)
def too_big(_):
    return jsonify(error="too_big"), 413


start_threads()

if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "5000")),
            threaded=True)
