from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

import pandas as pd
from flask import Flask, jsonify, render_template, request
from werkzeug.utils import secure_filename

from scanner import TIMEFRAMES, events_to_dataframe, load_tickers_from_file, scan_ticker_timeframe

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024

ALLOWED_EXTENSIONS = {"csv", "xlsx", "xls"}
RUNTIME_DIR = Path(tempfile.gettempdir()) / "big_order_scanner_uploads"
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


@app.route("/")
def index():
    return render_template("index.html", timeframes=list(TIMEFRAMES.keys()))


@app.route("/parse_tickers", methods=["POST"])
def parse_tickers():
    upload = request.files.get("ticker_file")
    if not upload or upload.filename == "":
        return jsonify({"error": "Please select a CSV/XLSX ticker file."}), 400
    if not allowed_file(upload.filename):
        return jsonify({"error": "Only CSV, XLSX, and XLS files are supported."}), 400

    filename = secure_filename(upload.filename)
    temp_path = RUNTIME_DIR / f"{uuid.uuid4().hex}_{filename}"
    try:
        upload.save(temp_path)
        tickers = load_tickers_from_file(temp_path)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass

    if not tickers:
        return jsonify({"error": "No valid ticker symbols found in uploaded file."}), 400

    return jsonify({
        "tickers": tickers,
        "timeframes": list(TIMEFRAMES.keys()),
        "total_checks": len(tickers) * len(TIMEFRAMES),
    })


@app.route("/scan_one", methods=["POST"])
def scan_one():
    data = request.get_json(silent=True) or {}
    ticker = str(data.get("ticker", "")).strip().upper()
    timeframe = str(data.get("timeframe", "")).strip()

    if not ticker:
        return jsonify({"error": "Ticker is required."}), 400
    if not ticker.endswith(".NS"):
        ticker = ticker.replace("NSE:", "")
        ticker = f"{ticker}.NS"
    if timeframe not in TIMEFRAMES:
        return jsonify({"error": f"Unsupported timeframe: {timeframe}"}), 400

    params = TIMEFRAMES[timeframe]
    try:
        events = scan_ticker_timeframe(ticker, timeframe, params["interval"], params["period"])
        df = events_to_dataframe(events)
        rows = df.fillna("").to_dict(orient="records") if not df.empty else []
        return jsonify({"ticker": ticker, "timeframe": timeframe, "results": rows})
    except Exception as exc:
        # Return a non-fatal scan error so the browser can continue the remaining symbols.
        return jsonify({"ticker": ticker, "timeframe": timeframe, "results": [], "warning": str(exc)}), 200


@app.route("/health")
def health():
    return jsonify({"ok": True})


# Vercel Python runtime discovers this Flask object as the WSGI app.
if __name__ == "__main__":
    app.run(debug=True)
