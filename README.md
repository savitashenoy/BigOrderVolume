# Big Order Scanner - Vercel Deployable Flask App

A Flask + yfinance scanner for NSE ticker CSV/XLSX uploads.

## Features

- CSV/XLSX/XLS ticker upload
- Start, Stop, and Clear scan controls
- Progress bar with completed ticker-timeframe checks
- Results table with filters
- Ticker search
- TF, Side, Size, Ticker Color, Score, and RelVol20 filters
- TradingView hyperlinks in the ticker column
- CSV result download
- Vercel serverless-compatible runtime directories using `/tmp`

## Local Run

```bash
pip install -r requirements.txt
python app.py
```

Open:

```text
http://127.0.0.1:5000
```

## Deploy to Vercel

1. Upload this folder to GitHub.
2. Import the repository in Vercel.
3. Use the default Vercel settings.
4. Vercel will use `vercel.json` and `requirements.txt` automatically.

## Notes for Vercel

This app is packaged for Vercel, but scans using yfinance can be slow for large watchlists. Vercel serverless functions have execution time limits, so keep ticker files smaller or deploy on Render/Railway for long scans.
