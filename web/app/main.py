"""journey-viewer: web service wrapping timeline_movie.py.

Serial job queue, unique job links, online ETA estimation.
"""
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import estimator
import timeline_movie as tm

PREFIX = os.environ.get("URL_PREFIX", "/journey-viewer")
DATA = os.environ.get("DATA_DIR", "/data")
JOBS_DIR = os.path.join(DATA, "jobs")
DB_PATH = os.path.join(DATA, "jobs.db")
RETENTION_DAYS = 7
MAX_UPLOAD = 300 * 1024 * 1024
JST = timezone(timedelta(hours=9))

os.makedirs(JOBS_DIR, exist_ok=True)
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
here = os.path.dirname(__file__)
templates = Jinja2Templates(directory=os.path.join(here, "templates"))
app.mount("/static", StaticFiles(directory=os.path.join(here, "static")),
          name="static")


# ------------------------------------------------------------------ db

def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, created REAL, status TEXT,
            params TEXT, n_points INTEGER, distance_km REAL,
            video_seconds REAL, total_frames INTEGER, n_videos INTEGER,
            pred_seconds REAL, started REAL, finished REAL,
            actual_seconds REAL, error TEXT)""")


init_db()


def history_rows():
    with db() as c:
        rows = c.execute("SELECT total_frames, n_points, actual_seconds "
                         "FROM jobs WHERE status='done'").fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


# ------------------------------------------------------------------ queue math

def queue_state():
    with db() as c:
        queued = c.execute("SELECT * FROM jobs WHERE status='queued' "
                           "ORDER BY created").fetchall()
        running = c.execute("SELECT * FROM jobs WHERE status='running'")\
                   .fetchone()
    return queued, running


def running_remaining(running):
    if not running:
        return 0.0
    elapsed = time.time() - (running["started"] or time.time())
    return max(running["pred_seconds"] - elapsed, running["pred_seconds"] * 0.05)


def eta_for(job_id):
    """Seconds until the given job is expected to finish."""
    queued, running = queue_state()
    total = running_remaining(running)
    if running and running["id"] == job_id:
        return total
    for q in queued:
        total += q["pred_seconds"]
        if q["id"] == job_id:
            return total
    return None


# ------------------------------------------------------------------ worker

def render_progress(job_id):
    """Parse 'frame X/Y' from the render log -> fraction or None."""
    fp = os.path.join(JOBS_DIR, job_id, "render.log")
    try:
        with open(fp, "rb") as f:
            f.seek(max(-4096, -os.path.getsize(fp)), 2)
            tail = f.read().decode(errors="replace")
        pairs = re.findall(r"frame (\d+)/(\d+)", tail)
        videos = re.findall(r"-> wrote", tail)
        if not pairs:
            return None
        x, y = map(int, pairs[-1])
        with db() as c:
            row = c.execute("SELECT n_videos FROM jobs WHERE id=?",
                            (job_id,)).fetchone()
        n = row["n_videos"] if row else 1
        return min(0.999, (len(videos) + x / max(y, 1)) / n)
    except OSError:
        return None


def worker_loop():
    while True:
        try:
            with db() as c:
                row = c.execute("SELECT * FROM jobs WHERE status='queued' "
                                "ORDER BY created LIMIT 1").fetchone()
            if not row:
                cleanup_old()
                time.sleep(3)
                continue
            run_job(row)
        except Exception as e:  # keep the worker alive no matter what
            print("worker error:", e, flush=True)
            time.sleep(5)


def run_job(row):
    job_id = row["id"]
    jd = os.path.join(JOBS_DIR, job_id)
    params = json.loads(row["params"])
    with db() as c:
        c.execute("UPDATE jobs SET status='running', started=? WHERE id=?",
                  (time.time(), job_id))
    cmd = [os.sys.executable, tm.__file__,
           "-i", os.path.join(jd, "location-history.json"),
           "-o", os.path.join(jd, "movie"),
           "--tile-cache", os.path.join(DATA, "tile_cache")]
    cmd += params_to_args(params)
    t0 = time.time()
    with open(os.path.join(jd, "render.log"), "w") as log:
        proc = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT,
                              timeout=6 * 3600)
    dt = time.time() - t0
    outputs = [f for f in os.listdir(jd) if f.endswith(".mp4")]
    if proc.returncode == 0 and outputs:
        with db() as c:
            c.execute("UPDATE jobs SET status='done', finished=?, "
                      "actual_seconds=? WHERE id=?",
                      (time.time(), dt, job_id))
    else:
        with db() as c:
            c.execute("UPDATE jobs SET status='error', finished=?, error=? "
                      "WHERE id=?",
                      (time.time(), f"renderer exit {proc.returncode}",
                       job_id))


def cleanup_old():
    cutoff = time.time() - RETENTION_DAYS * 86400
    with db() as c:
        old = c.execute("SELECT id FROM jobs WHERE created < ?",
                        (cutoff,)).fetchall()
    for r in old:
        jd = os.path.join(JOBS_DIR, r["id"])
        if os.path.isdir(jd):
            for f in os.listdir(jd):
                os.unlink(os.path.join(jd, f))
            os.rmdir(jd)
        with db() as c:
            c.execute("DELETE FROM jobs WHERE id=?", (r["id"],))


threading.Thread(target=worker_loop, daemon=True).start()


# ------------------------------------------------------------------ params

def params_to_args(p):
    args = []
    if p.get("start"):
        args += ["--start", p["start"]]
    if p.get("end"):
        args += ["--end", p["end"]]
    if p.get("speedup"):
        args += ["--speedup", str(p["speedup"])]
    else:
        args += ["--duration", str(p.get("duration", 75))]
    args += ["--orientation", p.get("orientation", "both"),
             "--style", p.get("style", "dark"),
             "--fps", str(p.get("fps", 30)),
             "--idle-speedup", str(p.get("idle_speedup", 10)),
             "--home-speedup", str(p.get("home_speedup", 1)),
             "--pan-kms", str(p.get("pan_kms", 2500)),
             "--min-leg-seconds", str(p.get("min_leg_seconds", 1.5)),
             "--trip-min-seconds", str(p.get("trip_min_seconds", 0))]
    if p.get("trail_hours"):
        args += ["--trail-hours", str(p["trail_hours"])]
    if p.get("view_seconds"):
        args += ["--view-seconds", str(p["view_seconds"])]
    return args


def clamp(v, lo, hi, default):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, x))


def analyze(points, p):
    """Exact video length via the real warp, plus dataset features."""
    t0d, t1d = points[0][0], points[-1][0]
    t_start = tm.parse_user_date(p["start"]) if p.get("start") else t0d
    t_end = tm.parse_user_date(p["end"], end=True) if p.get("end") else t1d
    t_start, t_end = max(t_start, t0d), min(t_end, t1d)
    if t_end <= t_start:
        raise ValueError("指定期間にデータがありません")
    homes = None
    if p.get("home_speedup", 1) > 1:
        homes = tm.detect_homes(points)
    if p.get("speedup"):
        sp = p["speedup"]
    else:
        sp = tm.solve_speedup(points, t_start, t_end, p.get("duration", 75),
                              p.get("idle_speedup", 10), homes, 50.0,
                              p.get("home_speedup", 1), p.get("pan_kms", 2500),
                              p.get("min_leg_seconds", 1.5),
                              p.get("trip_min_seconds", 0), 100.0)
    _, total_v = tm.build_timewarp(points, t_start, t_end, sp,
                                   p.get("idle_speedup", 10), homes, 50.0,
                                   p.get("home_speedup", 1),
                                   p.get("pan_kms", 2500),
                                   p.get("min_leg_seconds", 1.5),
                                   p.get("trip_min_seconds", 0), 100.0)
    dist = 0.0
    for i in range(1, len(points)):
        dist += tm.haversine_km(points[i - 1][1], points[i - 1][2],
                                points[i][1], points[i][2])
    n_videos = 2 if p.get("orientation", "both") == "both" else 1
    frames = int(total_v * p.get("fps", 30)) * n_videos
    return {"video_seconds": total_v, "total_frames": frames,
            "distance_km": dist, "n_videos": n_videos,
            "t_start": t_start, "t_end": t_end}


# ------------------------------------------------------------------ routes

def ctx(**kw):
    return {"prefix": PREFIX, **kw}


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    queued, running = queue_state()
    wait = running_remaining(running) + sum(q["pred_seconds"] for q in queued)
    info = estimator.model_info(history_rows())
    return templates.TemplateResponse(request, "index.html", ctx(
        n_queued=len(queued) + (1 if running else 0),
        wait_min=wait / 60,
        model=info))


@app.post("/submit")
async def submit(request: Request,
                 file: UploadFile = File(...),
                 start: str = Form(""), end: str = Form(""),
                 duration: str = Form("75"), speedup: str = Form(""),
                 orientation: str = Form("both"), style: str = Form("dark"),
                 fps: str = Form("30"), idle_speedup: str = Form("10"),
                 home_speedup: str = Form("1"), trail_hours: str = Form(""),
                 view_seconds: str = Form(""), pan_kms: str = Form("2500"),
                 min_leg_seconds: str = Form("1.5"),
                 trip_min_seconds: str = Form("0")):
    raw = await file.read()
    if len(raw) > MAX_UPLOAD:
        return JSONResponse({"error": "ファイルが大きすぎます (max 300MB)"},
                            status_code=413)
    job_id = uuid.uuid4().hex
    jd = os.path.join(JOBS_DIR, job_id)
    os.makedirs(jd)
    with open(os.path.join(jd, "location-history.json"), "wb") as f:
        f.write(raw)
    try:
        points = tm.load_points(os.path.join(jd, "location-history.json"))
        if not points:
            raise ValueError("位置情報が見つかりません（Googleマップの"
                             "タイムラインエクスポート JSON を指定してください）")
        p = {
            "start": start.strip() or None,
            "end": end.strip() or None,
            "orientation": orientation if orientation in
                           ("landscape", "portrait", "both") else "both",
            "style": style if style in ("dark", "light") else "dark",
            "fps": int(clamp(fps, 10, 60, 30)),
            "idle_speedup": clamp(idle_speedup, 1, 200, 10),
            "home_speedup": clamp(home_speedup, 1, 200, 1),
            "pan_kms": clamp(pan_kms, 100, 100000, 2500),
            "min_leg_seconds": clamp(min_leg_seconds, 0, 10, 1.5),
            "trip_min_seconds": clamp(trip_min_seconds, 0, 30, 0),
        }
        if speedup.strip():
            p["speedup"] = clamp(speedup, 10, 10_000_000, 3600)
        else:
            p["duration"] = clamp(duration, 5, 1800, 75)
        if trail_hours.strip():
            p["trail_hours"] = clamp(trail_hours, 1, 2000, 48)
        if view_seconds.strip():
            p["view_seconds"] = clamp(view_seconds, 1, 60, 8)
        feats = analyze(points, p)
    except (ValueError, KeyError, json.JSONDecodeError) as e:
        for f2 in os.listdir(jd):
            os.unlink(os.path.join(jd, f2))
        os.rmdir(jd)
        return templates.TemplateResponse(
            request, "index.html",
            ctx(error=str(e), n_queued=0, wait_min=0,
                model=estimator.model_info(history_rows())),
            status_code=400)
    pred = estimator.predict(history_rows(), feats["total_frames"],
                             len(points))
    with db() as c:
        c.execute("INSERT INTO jobs (id, created, status, params, n_points, "
                  "distance_km, video_seconds, total_frames, n_videos, "
                  "pred_seconds) VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (job_id, time.time(), "queued", json.dumps(p), len(points),
                   feats["distance_km"], feats["video_seconds"],
                   feats["total_frames"], feats["n_videos"], pred))
    return RedirectResponse(f"{PREFIX}/job/{job_id}", status_code=303)


@app.get("/job/{job_id}", response_class=HTMLResponse)
def job_page(request: Request, job_id: str):
    with db() as c:
        row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        return templates.TemplateResponse(
            request, "job.html", ctx(missing=True), status_code=404)
    return templates.TemplateResponse(request, "job.html", ctx(
        missing=False, job=dict(row),
        created_s=datetime.fromtimestamp(row["created"], JST)
        .strftime("%Y-%m-%d %H:%M")))


@app.get("/api/job/{job_id}")
def job_api(job_id: str):
    with db() as c:
        row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        return JSONResponse({"error": "not found"}, status_code=404)
    queued, running = queue_state()
    pos = None
    if row["status"] == "queued":
        pos = next((i + 1 for i, q in enumerate(queued)
                    if q["id"] == job_id), None)
    files = []
    if row["status"] == "done":
        jd = os.path.join(JOBS_DIR, job_id)
        files = sorted(f for f in os.listdir(jd) if f.endswith(".mp4"))
    return {
        "status": row["status"],
        "queue_position": pos,
        "eta_seconds": eta_for(job_id) if row["status"] in
                       ("queued", "running") else 0,
        "progress": render_progress(job_id)
                    if row["status"] == "running" else None,
        "pred_seconds": row["pred_seconds"],
        "video_seconds": row["video_seconds"],
        "n_points": row["n_points"],
        "distance_km": row["distance_km"],
        "error": row["error"],
        "files": files,
    }


@app.get("/api/queue")
def queue_api():
    queued, running = queue_state()
    wait = running_remaining(running) + sum(q["pred_seconds"] for q in queued)
    return {"n_jobs": len(queued) + (1 if running else 0),
            "wait_seconds": wait,
            "model": estimator.model_info(history_rows())}


@app.get("/download/{job_id}/{name}")
def download(job_id: str, name: str):
    if not re.fullmatch(r"movie_(landscape|portrait)\.mp4", name):
        return JSONResponse({"error": "bad name"}, status_code=400)
    fp = os.path.join(JOBS_DIR, job_id, name)
    if not os.path.exists(fp):
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(fp, media_type="video/mp4",
                        filename=f"journey_{job_id[:8]}_{name}")
