function setText(el, value) {
  if (el) el.textContent = value;
}

const form = document.getElementById('scanForm');
const startBtn = document.getElementById('startBtn');
const stopBtn = document.getElementById('stopBtn');
const clearResultsBtn = document.getElementById('clearResultsBtn');
const statusText = document.getElementById('statusText');
const progressBar = document.getElementById('progressBar');
const progressCount = document.getElementById('progressCount');
const tickerCount = document.getElementById('tickerCount');
const currentTicker = document.getElementById('currentTicker');
const currentTimeframe = document.getElementById('currentTimeframe');
const signalCount = document.getElementById('signalCount');
const resultsBody = document.querySelector('#resultsTable tbody');
const downloadLink = document.getElementById('downloadLink');
const filterSummary = document.getElementById('filterSummary');

const tickerFilter = document.getElementById('tickerFilter');
const tfFilter = document.getElementById('tfFilter');
const sideFilter = document.getElementById('sideFilter');
const sizeFilter = document.getElementById('sizeFilter');
const tickerColorFilter = document.getElementById('tickerColorFilter');
const scoreFilter = document.getElementById('scoreFilter');
const relVolFilter = document.getElementById('relVolFilter');
const clearFiltersBtn = document.getElementById('clearFiltersBtn');

let pollTimer = null;
let currentJobId = null;
let allResults = [];
let tickerTfMap = new Map();

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const fileInput = document.getElementById('tickerFile');
  if (!fileInput.files.length) return;

  const payload = new FormData();
  payload.append('ticker_file', fileInput.files[0]);

  resetUI();
  startBtn.disabled = true;
  startBtn.textContent = 'Scanning...';
  statusText.textContent = 'Uploading file and starting scan...';

  try {
    const response = await fetch('/start_scan', { method: 'POST', body: payload });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Unable to start scan.');
    currentJobId = data.job_id;
    stopBtn.disabled = false;
    pollProgress(data.job_id);
  } catch (error) {
    statusText.textContent = error.message;
    startBtn.disabled = false;
    startBtn.textContent = 'Start Scan';
    stopBtn.disabled = true;
  }
});


stopBtn.addEventListener('click', async () => {
  if (!currentJobId) return;
  stopBtn.disabled = true;
  statusText.textContent = 'Stopping scan...';
  try {
    const response = await fetch(`/stop_scan/${currentJobId}`, { method: 'POST' });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Unable to stop scan.');
  } catch (error) {
    statusText.textContent = error.message;
    stopBtn.disabled = false;
  }
});

clearResultsBtn.addEventListener('click', async () => {
  const jobToClear = currentJobId;
  if (pollTimer) clearInterval(pollTimer);
  if (jobToClear) {
    try {
      await fetch(`/clear_job/${jobToClear}`, { method: 'POST' });
    } catch (error) {
      console.warn('Clear request failed:', error);
    }
  }
  resetUI();
  statusText.textContent = 'Results cleared. Waiting for file upload.';
  startBtn.disabled = false;
  startBtn.textContent = 'Start Scan';
  stopBtn.disabled = true;
});

[tickerFilter, tfFilter, sideFilter, sizeFilter, tickerColorFilter, scoreFilter, relVolFilter].forEach((el) => {
  el.addEventListener('input', applyFiltersAndRender);
  el.addEventListener('change', applyFiltersAndRender);
});

clearFiltersBtn.addEventListener('click', () => {
  tickerFilter.value = '';
  tfFilter.value = '';
  sideFilter.value = '';
  sizeFilter.value = '';
  tickerColorFilter.value = '';
  scoreFilter.value = '';
  relVolFilter.value = '';
  applyFiltersAndRender();
});

function resetUI() {
  currentJobId = null;
  allResults = [];
  tickerTfMap = new Map();
  progressBar.style.width = '0%';
  progressCount.textContent = '0 / 0';
  setText(tickerCount, '0');
  setText(currentTicker, '-');
  setText(currentTimeframe, '-');
  setText(signalCount, '0');
  downloadLink.href = '#';
  downloadLink.classList.add('disabled');
  stopBtn.disabled = true;
  filterSummary.textContent = 'Filters apply after scan completes.';
  resultsBody.innerHTML = '<tr><td colspan="10" class="empty">No results yet.</td></tr>';
  if (pollTimer) clearInterval(pollTimer);
}

function pollProgress(jobId) {
  pollTimer = setInterval(async () => {
    try {
      const response = await fetch(`/progress/${jobId}`);
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Progress check failed.');
      updateProgress(data, jobId);

      if (data.status === 'done' || data.status === 'error' || data.status === 'stopped') {
        clearInterval(pollTimer);
        startBtn.disabled = false;
        startBtn.textContent = 'Start Scan';
        stopBtn.disabled = true;
      }
    } catch (error) {
      clearInterval(pollTimer);
      statusText.textContent = error.message;
      startBtn.disabled = false;
      startBtn.textContent = 'Start Scan';
      stopBtn.disabled = true;
    }
  }, 1000);
}

function updateProgress(data, jobId) {
  const completed = data.completed || 0;
  const total = data.total || 0;
  const pct = total > 0 ? Math.round((completed / total) * 100) : 0;

  progressBar.style.width = `${pct}%`;
  progressCount.textContent = `${completed} / ${total}`;
  setText(tickerCount, data.ticker_count || 0);
  setText(currentTicker, data.current_ticker || '-');
  setText(currentTimeframe, data.current_timeframe || '-');
  setText(signalCount, data.result_count || (data.results ? data.results.length : 0));
  statusText.textContent = data.message || data.status || 'Scanning...';

  if (data.status === 'done' || data.status === 'stopped') {
    allResults = data.results || [];
    tickerTfMap = buildTickerTimeframeMap(allResults);
    applyFiltersAndRender();
    downloadLink.href = `/download/${jobId}`;
    downloadLink.classList.remove('disabled');
  }
  if (data.status === 'error') {
    statusText.textContent = `Error: ${data.message || 'Unknown error'}`;
  }
}

function applyFiltersAndRender() {
  const filters = {
    ticker: tickerFilter.value.trim().toUpperCase(),
    timeframe: tfFilter.value,
    side: sideFilter.value,
    size: sizeFilter.value,
    tickerColor: tickerColorFilter.value,
    score: parseNumericFilter(scoreFilter.value),
    relVol: parseNumericFilter(relVolFilter.value),
  };

  const filteredRows = allResults.filter((row) => {
    if (filters.ticker && !String(row.Ticker || '').toUpperCase().includes(filters.ticker)) return false;
    if (filters.timeframe && row.Timeframe !== filters.timeframe) return false;
    if (filters.side && row.Side !== filters.side) return false;
    if (filters.size && row.Size !== filters.size) return false;
    if (filters.tickerColor && getTickerColorGroup(row.Ticker) !== filters.tickerColor) return false;
    if (filters.score && !compareNumber(Number(row.CompositeScore), filters.score.operator, filters.score.value)) return false;
    if (filters.relVol && !compareNumber(Number(row.RelVolume20), filters.relVol.operator, filters.relVol.value)) return false;
    return true;
  });

  renderResults(filteredRows);
  updateFilterSummary(filteredRows.length, allResults.length, filters);
}

function parseNumericFilter(raw) {
  const text = String(raw || '').trim();
  if (!text) return null;

  const match = text.match(/^(>=|<=|>|<|=)\s*(-?\d+(?:\.\d+)?)$/);
  if (!match) return null;

  return {
    operator: match[1],
    value: Number(match[2]),
  };
}

function compareNumber(actual, operator, expected) {
  if (!Number.isFinite(actual) || !Number.isFinite(expected)) return false;
  switch (operator) {
    case '>': return actual > expected;
    case '>=': return actual >= expected;
    case '<': return actual < expected;
    case '<=': return actual <= expected;
    case '=': return actual === expected;
    default: return true;
  }
}

function updateFilterSummary(filteredCount, totalCount, filters) {
  const invalid = [];
  if (scoreFilter.value.trim() && !filters.score) invalid.push('Score');
  if (relVolFilter.value.trim() && !filters.relVol) invalid.push('RelVol20');

  if (invalid.length) {
    filterSummary.textContent = `Invalid filter format for ${invalid.join(', ')}. Use >, >=, <, <=, or = followed by a number.`;
    filterSummary.classList.add('warning');
    return;
  }

  filterSummary.classList.remove('warning');
  if (!totalCount) {
    filterSummary.textContent = 'No scan results available yet.';
    return;
  }
  filterSummary.textContent = `Showing ${filteredCount} of ${totalCount} signals.`;
}

function buildTickerTimeframeMap(rows) {
  const map = new Map();
  rows.forEach((row) => {
    const ticker = String(row.Ticker || '');
    const timeframe = String(row.Timeframe || '');
    if (!ticker || !timeframe) return;
    if (!map.has(ticker)) map.set(ticker, new Set());
    map.get(ticker).add(timeframe);
  });
  return map;
}

function getTickerHighlightClass(ticker) {
  const tfCount = tickerTfMap.get(String(ticker || ''))?.size || 0;
  if (tfCount >= 4) return 'ticker-all-tf';
  if (tfCount === 3) return 'ticker-three-tf';
  if (tfCount === 2) return 'ticker-two-tf';
  return '';
}

function getTickerColorGroup(ticker) {
  const tfCount = tickerTfMap.get(String(ticker || ''))?.size || 0;
  if (tfCount >= 4) return 'all';
  if (tfCount === 3) return 'three';
  if (tfCount === 2) return 'two';
  return '';
}

function renderResults(rows) {
  if (!rows.length) {
    resultsBody.innerHTML = '<tr><td colspan="10" class="empty">No matching signals found.</td></tr>';
    return;
  }

  resultsBody.innerHTML = rows.map(row => {
    const tickerClass = getTickerHighlightClass(row.Ticker);
    return `
    <tr>
      <td><a class="ticker-name ticker-link ${tickerClass}" href="${tradingViewUrl(row.Ticker)}" target="_blank" rel="noopener noreferrer">${escapeHtml(row.Ticker)}</a></td>
      <td>${escapeHtml(displayTimeframe(row.Timeframe))}</td>
      <td><span class="side ${row.Side === 'Long' ? 'long' : row.Side === 'Short' ? 'short' : ''}">${escapeHtml(row.Side)}</span></td>
      <td>${escapeHtml(row.Size)}</td>
      <td>${formatNumber(row.CompositeScore)}</td>
      <td>${formatNumber(row.Percentile)}</td>
      <td>${formatNumber(row.RelVolume20)}</td>
      <td>${formatNumber(row.Price)}</td>
      <td>${formatInt(row.Volume)}</td>
      <td>${escapeHtml(row.TimeSince)}</td>
    </tr>
  `;
  }).join('');
}

function tradingViewUrl(ticker) {
  const symbol = String(ticker || '').replace(/\.NS$/i, '');
  return `https://www.tradingview.com/chart/?symbol=${encodeURIComponent(symbol)}&interval=60`;
}

function displayTimeframe(value) {
  return value === '1day' ? '1D' : value;
}

function formatNumber(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n.toFixed(2) : '';
}

function formatInt(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n.toLocaleString('en-IN') : '';
}

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}
