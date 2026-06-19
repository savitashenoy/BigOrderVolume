# Big Order Scanner - Vercel Package

This package is designed for Vercel deployment.

## What changed in this fixed package

- CSS and JavaScript are embedded directly in `templates/index.html` so the UI does not become bare-bones if `/static` files are not served by Vercel.
- The app uses Vercel-friendly scanning:
  - Upload file is parsed by `/parse_tickers`.
  - The browser scans one ticker-timeframe at a time through `/scan_one`.
  - Progress bar updates in the browser after every completed request.
  - Stop and Clear work client-side without relying on background server threads.
- Results are built in the browser and exported as CSV from the browser.

## Deploy on Vercel

1. Upload this folder to GitHub.
2. Import the GitHub repo into Vercel.
3. Keep the project root as this folder.
4. Deploy.

## Local run

```bash
pip install -r requirements.txt
python app.py
```

Open: `http://127.0.0.1:5000`

## Note

For very large watchlists, Vercel can still be slower or rate-limited because yfinance requests run through serverless functions. Render/Railway is better for large scans, but this package avoids the CSS/JS/static-file issue and avoids Vercel background-thread limitations.
