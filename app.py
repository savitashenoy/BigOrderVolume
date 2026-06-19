from __future__ import annotations

import os
import threading
import time
import uuid
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

from scanner import TIMEFRAMES, load_tickers_from_file, run_scan

BASE_DIR = Path(__file__).resolve().parent
IS_VERCEL = bool(os.environ.get("VERCEL"))
# Vercel's function filesystem is read-only except for /tmp.
# Local runs keep using project folders for easier debugging.
if IS_VERCEL:
    RUNTIME_DIR = Path(tempfile.gettempdir()) / "big_order_scanner"
    UPLOAD_DIR = RUNTIME_DIR / "uploads"
    RESULT_DIR = RUNTIME_DIR / "results"
else:
    UPLOAD_DIR = BASE_DIR / "uploads"
    RESULT_DIR = BASE_DIR / "results"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
RESULT_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_EXTENSIONS = {"csv", "xlsx", "xls"}

# Cap how many scans run at the same time across *all* users. Each scan job
# internally spins up its own small worker pool (see scanner.SCAN_WORKER_THREADS)
# to fetch tickers concurrently, so the real worst-case concurrent yfinance
# load is roughly SCAN_JOB_CONCURRENCY x SCAN_WORKER_THREADS. Extra scan
# requests beyond this cap simply wait in the executor queue with status
# "queued" until a slot frees up, instead of spawning unbounded threads.
SCAN_JOB_CONCURRENCY = 2

# How long a finished/errored job (and its uploaded + result files) is kept
# around before being swept away, and how often the sweep runs.
JOB_TTL_HOURS = 24
CLEANUP_INTERVAL_SECONDS = 30 * 60

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024

jobs: dict[str, dict] = {}
stop_events: dict[str, threading.Event] = {}
lock = threading.Lock()

scan_executor = ThreadPoolExecutor(max_workers=SCAN_JOB_CONCURRENCY, thread_name_prefix="scan-job")


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def set_job(job_id: str, **kwargs) -> None:
    with lock:
        jobs.setdefault(job_id, {}).update(kwargs)


def get_job(job_id: str) -> dict:
    with lock:
        return dict(jobs.get(job_id, {}))


def _safe_unlink(path_str: str | None) -> None:
    if not path_str:
        return
    try:
        Path(path_str).unlink(missing_ok=True)
    except OSError:
        app.logger.warning("Could not delete file %s", path_str, exc_info=True)


def _cleanup_orphan_files(directory: Path, cutoff: datetime) -> None:
    """Sweep files left behind even if the in-memory job record is gone
    (e.g. after a process restart, since `jobs` doesn't survive that)."""
    for path in directory.iterdir():
        if path.name == ".gitkeep" or not path.is_file():
            continue
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime)
            if mtime < cutoff:
                path.unlink(missing_ok=True)
        except OSError:
            app.logger.warning("Could not clean up orphan file %s", path, exc_info=True)


def cleanup_expired_jobs() -> None:
    """Drop job records and delete their files once older than JOB_TTL_HOURS."""
    cutoff = datetime.now() - timedelta(hours=JOB_TTL_HOURS)

    with lock:
        expired_ids = [
            job_id
            for job_id, job in jobs.items()
            if job.get("created_at") and job["created_at"] < cutoff
        ]
        expired_jobs = [jobs.pop(job_id) for job_id in expired_ids]
        for job_id in expired_ids:
            stop_events.pop(job_id, None)

    for job in expired_jobs:
        _safe_unlink(job.get("upload_file"))
        _safe_unlink(job.get("result_file"))

    _cleanup_orphan_files(UPLOAD_DIR, cutoff)
    _cleanup_orphan_files(RESULT_DIR, cutoff)

    if expired_ids:
        app.logger.info("Cleaned up %d expired job(s): %s", len(expired_ids), expired_ids)


def _cleanup_loop() -> None:
    while True:
        time.sleep(CLEANUP_INTERVAL_SECONDS)
        try:
            cleanup_expired_jobs()
        except Exception:
            app.logger.exception("Background job/file cleanup failed")


def scan_worker(job_id: str, file_path: Path) -> None:
    stop_event = stop_events.get(job_id)
    try:
        set_job(job_id, status="reading", message="Reading uploaded ticker file")
        tickers = load_tickers_from_file(file_path)
        if not tickers:
            raise ValueError("No valid ticker symbols found in uploaded file.")

        total_steps = len(tickers) * len(TIMEFRAMES)
        set_job(
            job_id,
            status="running",
            total=total_steps,
            completed=0,
            ticker_count=len(tickers),
            current_ticker="",
            current_timeframe="",
            message=f"Loaded {len(tickers)} tickers",
        )

        def progress(update: dict) -> None:
            if stop_event is not None and stop_event.is_set():
                set_job(job_id, status="stopping", **update)
            else:
                set_job(job_id, status="running", **update)

        results_df = run_scan(tickers, progress_callback=progress, stop_event=stop_event)
        result_file = RESULT_DIR / f"big_order_results_{job_id}.csv"
        results_df.to_csv(result_file, index=False)

        rows = results_df.fillna("").to_dict(orient="records") if not results_df.empty else []
        if stop_event is not None and stop_event.is_set():
            completed_now = get_job(job_id).get("completed", 0)
            set_job(
                job_id,
                status="stopped",
                completed=completed_now,
                total=total_steps,
                current_ticker="",
                current_timeframe="",
                message=f"Scan stopped. {len(rows)} signals found before stop.",
                result_file=str(result_file),
                result_count=len(rows),
                results=rows[:500],
                finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )
        else:
            set_job(
                job_id,
                status="done",
                completed=total_steps,
                total=total_steps,
                current_ticker="",
                current_timeframe="",
                message=f"Scan completed. {len(rows)} signals found.",
                result_file=str(result_file),
                result_count=len(rows),
                results=rows[:500],
                finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )
    except Exception as exc:
        app.logger.exception("Scan job %s failed", job_id)
        set_job(job_id, status="error", message=str(exc))
    finally:
        # The raw upload isn't needed once it's been read; remove it now
        # rather than waiting for the TTL sweep, to keep uploads/ small.
        _safe_unlink(str(file_path))


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/start_scan", methods=["POST"])
def start_scan():
    upload = request.files.get("ticker_file")
    if not upload or upload.filename == "":
        return jsonify({"error": "Please select a CSV/XLSX file."}), 400
    if not allowed_file(upload.filename):
        return jsonify({"error": "Only CSV, XLSX, and XLS files are supported."}), 400

    job_id = uuid.uuid4().hex[:12]
    filename = secure_filename(upload.filename)
    saved_path = UPLOAD_DIR / f"{job_id}_{filename}"
    upload.save(saved_path)

    with lock:
        stop_events[job_id] = threading.Event()

    set_job(
        job_id,
        status="queued",
        created_at=datetime.now(),
        upload_file=str(saved_path),
        completed=0,
        total=1,
        ticker_count=0,
        current_ticker="",
        current_timeframe="",
        message="Scan queued",
        results=[],
        result_count=0,
    )

    # Submitted to a bounded pool instead of an unbounded threading.Thread,
    # so a burst of requests queues up (status stays "queued") rather than
    # firing unlimited concurrent scans at yfinance.
    scan_executor.submit(scan_worker, job_id, saved_path)
    return jsonify({"job_id": job_id})


@app.route("/progress/<job_id>")
def progress(job_id: str):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found."}), 404
    return jsonify(job)


@app.route("/stop_scan/<job_id>", methods=["POST"])
def stop_scan(job_id: str):
    with lock:
        job = jobs.get(job_id)
        stop_event = stop_events.get(job_id)
        if not job:
            return jsonify({"error": "Job not found."}), 404
        if stop_event:
            stop_event.set()
        job["status"] = "stopping"
        job["message"] = "Stop requested. Finishing any active request and cancelling remaining checks."
    return jsonify({"ok": True})


@app.route("/clear_job/<job_id>", methods=["POST"])
def clear_job(job_id: str):
    with lock:
        job = jobs.pop(job_id, None)
        stop_event = stop_events.pop(job_id, None)
        if stop_event:
            stop_event.set()
    if job:
        _safe_unlink(job.get("upload_file"))
        _safe_unlink(job.get("result_file"))
    return jsonify({"ok": True})


@app.route("/download/<job_id>")
def download(job_id: str):
    job = get_job(job_id)
    result_file = job.get("result_file")
    if not result_file or not Path(result_file).exists():
        return jsonify({"error": "Result file not available yet."}), 404
    return send_file(result_file, as_attachment=True, download_name="big_order_scan_results.csv")


# Background sweeper for expired jobs/files. Runs locally as a daemon thread.
# On Vercel, files are placed in /tmp and are ephemeral, so avoid starting
# an infinite cleanup loop inside serverless invocations.
if not IS_VERCEL:
    threading.Thread(target=_cleanup_loop, daemon=True).start()


if __name__ == "__main__":
    app.run(debug=True)
