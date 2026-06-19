"""
Big Order Scanner core logic for Flask app.

Scans CSV/XLSX ticker lists using yfinance and flags recent Medium/Large
unusual-volume candles using traded value percentile, relative volume,
completed candles only, liquidity filter, and composite score ranking.
"""
from __future__ import annotations

import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

# -----------------------------------------------------------------------------
# CONFIG
# -----------------------------------------------------------------------------
TIMEFRAMES = {
    "15min": dict(interval="15m", period="60d"),
    "30min": dict(interval="30m", period="60d"),
    "60min": dict(interval="60m", period="180d"),
    "1day": dict(interval="1d", period="1y"),
}

PCT_MEDIUM = 90.0
PCT_LARGE = 97.0
USE_DOLLAR_VOLUME = True
MIN_HISTORY_BARS = 50
LOOKBACK_BARS_TO_CHECK = 3
REQUEST_DELAY_SECONDS = 0.15
SCAN_COMPLETED_CANDLES_ONLY = True
REL_VOLUME_WINDOW = 20
MIN_AVG_DOLLAR_VOLUME_20 = 5_00_00_000  # Rs. 5 crore

# Number of concurrent worker threads used to fetch/scan ticker x timeframe
# combinations. Each worker still self-throttles with REQUEST_DELAY_SECONDS
# between its own yfinance calls, so the burst rate per worker stays the same
# as the old sequential version — running several workers in parallel just
# multiplies overall throughput instead of hammering yfinance faster per-connection.
SCAN_WORKER_THREADS = 6

SCORE_WEIGHT_PERCENTILE = 0.40
SCORE_WEIGHT_REL_VOLUME = 0.30
SCORE_WEIGHT_LIQUIDITY = 0.20
SCORE_WEIGHT_RECENCY = 0.10
REL_VOLUME_SCORE_CAP = 5.0
LIQUIDITY_SCORE_CAP = 25_00_00_000  # Rs. 25 crore

ProgressCallback = Callable[[dict], None]


@dataclass
class BigOrderEvent:
    ticker: str
    timeframe: str
    side: str
    size_label: str
    percentile: float
    volume: float
    dollar_volume: float
    avg_volume_20: float
    rel_volume_20: float
    avg_dollar_volume_20: float
    rel_dollar_volume_20: float
    composite_score: float
    price: float
    bar_time: pd.Timestamp
    bars_ago: int


def load_tickers_from_file(path: str | Path, column: Optional[str] = None) -> list[str]:
    """Read tickers from CSV/XLSX and ensure .NS suffix is present."""
    path = Path(path)
    suffix = path.suffix.lower()

    if suffix in {".xlsx", ".xls"}:
        df = pd.read_excel(path)
    elif suffix == ".csv":
        df = pd.read_csv(path)
    else:
        raise ValueError("Unsupported file type. Please upload CSV, XLSX, or XLS.")

    if df.empty:
        raise ValueError("No data found in uploaded file.")

    if column and column in df.columns:
        series = df[column]
    else:
        series = df.iloc[:, 0]

    tickers: list[str] = []
    for raw in series.dropna().astype(str):
        ticker = raw.strip().upper()
        if not ticker or ticker in {"NAN", "NONE"}:
            continue
        # Accept NSE:RELIANCE, RELIANCE, RELIANCE.NS formats.
        ticker = ticker.replace("NSE:", "").strip()
        if not ticker.endswith(".NS"):
            ticker = f"{ticker}.NS"
        tickers.append(ticker)

    seen = set()
    unique = []
    for ticker in tickers:
        if ticker not in seen:
            seen.add(ticker)
            unique.append(ticker)
    return unique


def direction_from_close(df: pd.DataFrame) -> pd.Series:
    return np.sign(df["Close"].diff()).fillna(0)


def fetch_history(ticker: str, interval: str, period: str) -> Optional[pd.DataFrame]:
    try:
        df = yf.Ticker(ticker).history(interval=interval, period=period, auto_adjust=False)
    except Exception:
        return None

    if df is None or df.empty:
        return None

    df = df.dropna(subset=["Close", "Volume"])
    df = df[df["Volume"] > 0]
    return df


def compute_volume_metric(df: pd.DataFrame) -> pd.Series:
    if USE_DOLLAR_VOLUME:
        return df["Volume"] * df["Close"]
    return df["Volume"]


def drop_latest_incomplete_candle(df: pd.DataFrame) -> pd.DataFrame:
    if SCAN_COMPLETED_CANDLES_ONLY and len(df) > 1:
        return df.iloc[:-1].copy()
    return df


def previous_average(series: pd.Series, window: int) -> pd.Series:
    return series.shift(1).rolling(window=window, min_periods=window).mean()


def safe_ratio(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    denominator = denominator.replace(0, np.nan)
    return (numerator / denominator).replace([np.inf, -np.inf], np.nan)


def normalize_to_100(value: float, cap: float) -> float:
    if pd.isna(value) or cap <= 0:
        return 0.0
    return max(0.0, min((float(value) / cap) * 100.0, 100.0))


def compute_composite_score(percentile: float, rel_volume_20: float, avg_dollar_volume_20: float, bars_ago: int) -> float:
    percentile_score = max(0.0, min(float(percentile), 100.0))
    rel_volume_score = normalize_to_100(rel_volume_20, REL_VOLUME_SCORE_CAP)
    liquidity_score = normalize_to_100(avg_dollar_volume_20, LIQUIDITY_SCORE_CAP)
    max_age = max(LOOKBACK_BARS_TO_CHECK - 1, 1)
    recency_score = max(0.0, 100.0 * (1.0 - (bars_ago / max_age)))

    score = (
        SCORE_WEIGHT_PERCENTILE * percentile_score
        + SCORE_WEIGHT_REL_VOLUME * rel_volume_score
        + SCORE_WEIGHT_LIQUIDITY * liquidity_score
        + SCORE_WEIGHT_RECENCY * recency_score
    )
    return round(score, 2)


def classify_bar(percentile: float) -> str:
    if percentile >= PCT_LARGE:
        return "Large"
    if percentile >= PCT_MEDIUM:
        return "Medium"
    return "Small"


def scan_ticker_timeframe(ticker: str, tf_label: str, interval: str, period: str) -> list[BigOrderEvent]:
    df = fetch_history(ticker, interval, period)
    if df is None:
        return []

    df = drop_latest_incomplete_candle(df)
    min_required = max(MIN_HISTORY_BARS, REL_VOLUME_WINDOW + LOOKBACK_BARS_TO_CHECK + 1)
    if len(df) < min_required:
        return []

    raw_volume = df["Volume"]
    dollar_metric = df["Volume"] * df["Close"]
    vol_metric = compute_volume_metric(df)
    direction = direction_from_close(df)
    pct_rank = vol_metric.rank(pct=True) * 100.0

    avg_volume_20 = previous_average(raw_volume, REL_VOLUME_WINDOW)
    rel_volume_20 = safe_ratio(raw_volume, avg_volume_20)
    avg_dollar_volume_20 = previous_average(dollar_metric, REL_VOLUME_WINDOW)
    rel_dollar_volume_20 = safe_ratio(dollar_metric, avg_dollar_volume_20)

    events: list[BigOrderEvent] = []
    n = len(df)
    check_n = min(LOOKBACK_BARS_TO_CHECK, n)

    for i in range(n - check_n, n):
        pct = float(pct_rank.iloc[i])
        size_label = classify_bar(pct)
        if size_label == "Small":
            continue

        avg_vol = float(avg_volume_20.iloc[i]) if pd.notna(avg_volume_20.iloc[i]) else np.nan
        rel_vol = float(rel_volume_20.iloc[i]) if pd.notna(rel_volume_20.iloc[i]) else np.nan
        avg_dollar_vol = float(avg_dollar_volume_20.iloc[i]) if pd.notna(avg_dollar_volume_20.iloc[i]) else np.nan
        rel_dollar_vol = float(rel_dollar_volume_20.iloc[i]) if pd.notna(rel_dollar_volume_20.iloc[i]) else np.nan

        if pd.isna(avg_dollar_vol) or avg_dollar_vol < MIN_AVG_DOLLAR_VOLUME_20:
            continue

        bars_ago = n - 1 - i
        composite_score = compute_composite_score(pct, rel_vol, avg_dollar_vol, bars_ago)
        side = {1: "Long", -1: "Short", 0: "Flat"}.get(int(direction.iloc[i]), "Flat")
        price = float(df["Close"].iloc[i])
        raw_vol = float(df["Volume"].iloc[i])

        events.append(
            BigOrderEvent(
                ticker=ticker,
                timeframe=tf_label,
                side=side,
                size_label=size_label,
                percentile=pct,
                volume=raw_vol,
                dollar_volume=raw_vol * price,
                avg_volume_20=avg_vol,
                rel_volume_20=rel_vol,
                avg_dollar_volume_20=avg_dollar_vol,
                rel_dollar_volume_20=rel_dollar_vol,
                composite_score=composite_score,
                price=price,
                bar_time=df.index[i],
                bars_ago=bars_ago,
            )
        )
    return events


def duration_since(bar_time: pd.Timestamp) -> str:
    now = pd.Timestamp.now(tz=bar_time.tzinfo) if bar_time.tzinfo else pd.Timestamp.now()
    delta = now - bar_time
    total_minutes = int(delta.total_seconds() // 60)
    if total_minutes <= 0:
        return "Now"
    days, rem = divmod(total_minutes, 1440)
    hours, minutes = divmod(rem, 60)
    if days > 0:
        return f"{days}D"
    if hours > 0:
        return f"{hours}h{minutes}m"
    return f"{minutes}m" if minutes > 0 else "Now"


def events_to_dataframe(events: list[BigOrderEvent]) -> pd.DataFrame:
    if not events:
        return pd.DataFrame()

    rows = []
    for e in events:
        rows.append(
            {
                "Ticker": e.ticker,
                "Timeframe": e.timeframe,
                "Side": e.side,
                "Size": e.size_label,
                "CompositeScore": round(e.composite_score, 2),
                "Percentile": round(e.percentile, 2),
                "Volume": int(e.volume),
                "AvgVolume20": round(e.avg_volume_20, 2),
                "RelVolume20": round(e.rel_volume_20, 2),
                "DollarVolume": round(e.dollar_volume, 2),
                "AvgDollarVolume20": round(e.avg_dollar_volume_20, 2),
                "RelDollarVolume20": round(e.rel_dollar_volume_20, 2),
                "Price": round(e.price, 2),
                "BarTime": str(e.bar_time),
                "BarsAgo": e.bars_ago,
                "TimeSince": duration_since(e.bar_time),
            }
        )
    out = pd.DataFrame(rows)
    out.sort_values(["CompositeScore", "Percentile"], ascending=[False, False], inplace=True)
    out.reset_index(drop=True, inplace=True)
    return out


def _fetch_and_scan_one(
    ticker: str,
    tf_label: str,
    interval: str,
    period: str,
    stop_event: Optional[threading.Event] = None,
) -> tuple[str, str, list[BigOrderEvent]]:
    """Worker unit: fetch + scan a single ticker/timeframe, then self-throttle.

    The sleep happens inside the worker (not the orchestrating loop) so each
    worker thread maintains the same per-call request spacing as the old
    sequential version. Running SCAN_WORKER_THREADS of these in parallel
    multiplies overall throughput without increasing the burst rate that any
    single worker sends to yfinance.
    """
    if stop_event is not None and stop_event.is_set():
        return ticker, tf_label, []
    events = scan_ticker_timeframe(ticker, tf_label, interval, period)
    time.sleep(REQUEST_DELAY_SECONDS)
    return ticker, tf_label, events


def run_scan(
    tickers: list[str],
    progress_callback: Optional[ProgressCallback] = None,
    max_workers: int = SCAN_WORKER_THREADS,
    stop_event: Optional[threading.Event] = None,
) -> pd.DataFrame:
    all_events: list[BigOrderEvent] = []
    tasks = [
        (ticker, tf_label, params["interval"], params["period"])
        for ticker in tickers
        for tf_label, params in TIMEFRAMES.items()
    ]
    total = len(tasks)
    completed = 0
    max_workers = max(1, max_workers)

    executor = ThreadPoolExecutor(max_workers=max_workers)
    pending_tasks = list(tasks)
    running = {}

    def submit_until_full() -> None:
        while pending_tasks and len(running) < max_workers:
            if stop_event is not None and stop_event.is_set():
                break
            ticker, tf_label, interval, period = pending_tasks.pop(0)
            future = executor.submit(_fetch_and_scan_one, ticker, tf_label, interval, period, stop_event)
            running[future] = (ticker, tf_label)

    try:
        submit_until_full()
        while running:
            if stop_event is not None and stop_event.is_set():
                for future in running:
                    future.cancel()
                break

            done, _ = wait(running.keys(), timeout=0.5, return_when=FIRST_COMPLETED)
            if not done:
                continue

            for future in done:
                ticker, tf_label = running.pop(future)
                try:
                    _, _, events = future.result()
                except Exception:
                    events = []

                completed += 1
                all_events.extend(events)

                if progress_callback:
                    progress_callback(
                        {
                            "completed": completed,
                            "total": total,
                            "current_ticker": ticker,
                            "current_timeframe": tf_label,
                            "message": f"Completed {ticker} @ {tf_label}",
                            "result_count": len(all_events),
                        }
                    )

            submit_until_full()
    finally:
        cancel_futures = bool(stop_event is not None and stop_event.is_set())
        executor.shutdown(wait=not cancel_futures, cancel_futures=cancel_futures)

    return events_to_dataframe(all_events)
