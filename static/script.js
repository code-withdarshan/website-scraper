/* ════════════════════════════════════════════════════════════════
   Email & Contact Scraper — frontend
   ══════════════════════════════════════════════════════════════ */

const HISTORY_KEY  = "scraper_history_v1";
const HISTORY_MAX  = 5;

/* ── State ──────────────────────────────────────────────────────── */
let allResults    = [];
let failedUrls    = [];
let currentXHR    = null;     // active streaming request
let sortColumn    = null;
let sortDir       = "asc";

/* ── DOM refs ───────────────────────────────────────────────────── */
const $ = (id) => document.getElementById(id);

const urlInput      = $("url-input");
const urlCount      = $("url-count");
const clearBtn      = $("clear-btn");
const scrapeTextBtn = $("scrape-text-btn");

const dropZone      = $("drop-zone");
const fileInput     = $("file-input");
const filePreview   = $("file-preview");
const fileNameText  = $("file-name-text");
const removeFileBtn = $("remove-file-btn");
const scrapeFileBtn = $("scrape-file-btn");

const progressCard  = $("progress-card");
const progressTitle = $("progress-title");
const progressSub   = $("progress-sub");
const progressFill  = $("progress-fill");
const liveTicker    = $("live-ticker");
const cancelBtn     = $("cancel-btn");

const errorBox      = $("error-box");
const errorMsg      = $("error-msg");

const resultsSection= $("results-section");
const statSites     = $("stat-sites");
const statEmails    = $("stat-emails");
const statPhones    = $("stat-phones");
const statCities    = $("stat-cities");
const copyAllBtn    = $("copy-all-btn");
const exportCsvBtn  = $("export-csv-btn");
const exportXlsxBtn = $("export-xlsx-btn");
const filterInput   = $("filter-input");
const showingLabel  = $("showing-label");
const resultsBody   = $("results-body");
const emptyFilter   = $("empty-filter");

const failedSection = $("failed-section");
const failedCount   = $("failed-count");
const toggleFailedBtn = $("toggle-failed-btn");
const failedBody    = $("failed-body");

const historyBtn    = $("history-btn");
const historyModal  = $("history-modal");
const closeHistoryBtn = $("close-history-btn");
const clearHistoryBtn = $("clear-history-btn");
const historyList   = $("history-list");

const toast         = $("toast");

/* ── URL counter & paste handler ───────────────────────────────── */
function updateUrlCount() {
  const n = urlInput.value.trim()
    ? urlInput.value.split(/[\n,]+/).map(s => s.trim()).filter(Boolean).length
    : 0;
  urlCount.textContent = n === 0 ? "0 URLs" : `${n} URL${n !== 1 ? "s" : ""}`;
}
urlInput.addEventListener("input", updateUrlCount);

urlInput.addEventListener("paste", (e) => {
  e.preventDefault();
  const pasted = (e.clipboardData || window.clipboardData).getData("text");
  const urls = pasted.split(/[\s,;]+/).map(s => s.trim()).filter(Boolean);
  if (!urls.length) return;
  const start = urlInput.selectionStart;
  const end   = urlInput.selectionEnd;
  const before = urlInput.value.slice(0, start);
  const after  = urlInput.value.slice(end);
  const sep = (before && !before.endsWith(",") && !before.endsWith(", ")) ? ", " : "";
  const insert = sep + urls.join(", ");
  urlInput.value = before + insert + after;
  const newPos = (before + insert).length;
  urlInput.setSelectionRange(newPos, newPos);
  updateUrlCount();
});

clearBtn.addEventListener("click", () => {
  urlInput.value = "";
  urlCount.textContent = "0 URLs";
  urlInput.focus();
});

/* ── Drop zone ──────────────────────────────────────────────────── */
dropZone.addEventListener("click", () => fileInput.click());
dropZone.addEventListener("dragover", (e) => {
  e.preventDefault();
  dropZone.style.borderColor = "#2563eb";
  dropZone.style.background  = "#eff6ff";
});
["dragleave", "dragend"].forEach(ev =>
  dropZone.addEventListener(ev, () => {
    dropZone.style.borderColor = "";
    dropZone.style.background  = "";
  })
);
dropZone.addEventListener("drop", (e) => {
  e.preventDefault();
  dropZone.style.borderColor = "";
  dropZone.style.background  = "";
  if (e.dataTransfer.files.length) handleFile(e.dataTransfer.files[0]);
});
fileInput.addEventListener("change", () => {
  if (fileInput.files.length) handleFile(fileInput.files[0]);
});
removeFileBtn.addEventListener("click", (e) => {
  e.stopPropagation();
  fileInput.value = "";
  scrapeFileBtn._file = null;
  scrapeFileBtn.disabled = true;
  filePreview.style.display = "none";
  dropZone.style.display = "";
  hideError();
});
function handleFile(file) {
  const ok = [".csv", ".xls", ".xlsx"];
  const ext = file.name.slice(file.name.lastIndexOf(".")).toLowerCase();
  if (!ok.includes(ext)) {
    showError("Unsupported file. Use .csv, .xls, or .xlsx.");
    return;
  }
  scrapeFileBtn._file = file;
  scrapeFileBtn.disabled = false;
  fileNameText.textContent = file.name;
  filePreview.style.display = "flex";
  dropZone.style.display = "none";
  hideError();
}

/* ── Scrape (streaming via fetch + ReadableStream → SSE-like) ──── */
scrapeTextBtn.addEventListener("click", () => {
  const raw = urlInput.value.trim();
  if (!raw) { showError("Please enter at least one URL."); return; }
  const fd = new FormData();
  fd.append("urls", raw);
  startStream(fd);
});

scrapeFileBtn.addEventListener("click", () => {
  const f = scrapeFileBtn._file;
  if (!f) { showError("No file selected."); return; }
  const fd = new FormData();
  fd.append("file", f);
  startStream(fd);
});

cancelBtn.addEventListener("click", () => {
  if (currentXHR && currentXHR.abort) currentXHR.abort();
  hideProgress();
  finalizeRender();
});

/* ── Streaming logic ────────────────────────────────────────────── */
async function startStream(formData) {
  hideError();
  allResults = [];
  failedUrls = [];
  resultsSection.style.display = "none";
  failedSection.style.display = "none";
  liveTicker.innerHTML = "";
  showProgress("Scraping in progress…", "Connecting…");

  const controller = new AbortController();
  currentXHR = controller;

  let response;
  try {
    response = await fetch("/scrape-stream", {
      method: "POST",
      body: formData,
      signal: controller.signal,
    });
  } catch (err) {
    hideProgress();
    if (err.name !== "AbortError") showError("Network error: " + err.message);
    return;
  }

  // Check for JSON error response
  if (response.headers.get("Content-Type", "").includes("application/json") ||
      !response.headers.get("Content-Type", "").includes("event-stream")) {
    try {
      const j = await response.json();
      hideProgress();
      showError(j.error || "Server error");
      return;
    } catch {
      hideProgress();
      showError("Server error");
      return;
    }
  }

  const reader  = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buffer.indexOf("\n\n")) !== -1) {
        const chunk = buffer.slice(0, idx).trim();
        buffer = buffer.slice(idx + 2);
        if (!chunk.startsWith("data:")) continue;
        try {
          const evt = JSON.parse(chunk.slice(5).trim());
          handleStreamEvent(evt);
        } catch {}
      }
    }
  } catch (err) {
    if (err.name === "AbortError") return;
    showError("Stream error: " + err.message);
  } finally {
    currentXHR = null;
  }

  hideProgress();
  finalizeRender();
  saveToHistory();
}

function handleStreamEvent(evt) {
  if (evt.event === "start") {
    progressTitle.textContent = `Scraping ${evt.total} URL${evt.total !== 1 ? "s" : ""}…`;
    progressSub.textContent = `0 / ${evt.total} done`;
    progressFill.style.width = "0%";
  } else if (evt.event === "row") {
    allResults.push(evt.result);
    pushTickerRow(evt.result.url, true);
    updateProgress(evt.done, evt.total);
    renderResultsLive();
  } else if (evt.event === "failed") {
    failedUrls.push(evt.url);
    pushTickerRow(evt.url, false);
    updateProgress(evt.done, evt.total);
  } else if (evt.event === "end") {
    progressSub.textContent = `Done — ${evt.count} with emails, ${evt.failed_count} skipped`;
  }
}

function pushTickerRow(url, ok) {
  const row = document.createElement("div");
  row.className = "ticker-row " + (ok ? "success" : "failed");
  row.innerHTML = ok
    ? `<svg class="ticker-icon" fill="none" stroke="currentColor" stroke-width="3" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"/></svg>
       <span class="ticker-url">${esc(url)}</span>`
    : `<svg class="ticker-icon" fill="none" stroke="currentColor" stroke-width="3" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12"/></svg>
       <span class="ticker-url">${esc(url)}</span> <span class="ticker-meta">no email</span>`;
  liveTicker.insertBefore(row, liveTicker.firstChild);
  // Keep last 50 in the DOM
  while (liveTicker.children.length > 50) liveTicker.removeChild(liveTicker.lastChild);
}

function updateProgress(done, total) {
  progressSub.textContent = `${done} / ${total} done`;
  progressFill.style.width = (total ? (done / total * 100) : 0) + "%";
}

function renderResultsLive() {
  // Show results card on first row
  if (allResults.length === 1) resultsSection.style.display = "block";
  updateStats();
  renderRows(allResults);
}

function finalizeRender() {
  updateStats();
  renderRows(allResults);
  if (allResults.length) resultsSection.style.display = "block";
  if (failedUrls.length) {
    failedSection.style.display = "block";
    failedCount.textContent = `(${failedUrls.length})`;
    failedBody.innerHTML = failedUrls.map(u =>
      `<div class="failed-row">
        <svg class="ticker-icon" fill="none" stroke="currentColor" stroke-width="3" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12"/></svg>
        ${esc(u)}
      </div>`).join("");
  }
  if (!allResults.length && !failedUrls.length) {
    showError("No URLs were processed.");
  } else if (!allResults.length) {
    showError("No emails found in any of the URLs scanned.");
  }
}

/* ── Stats ──────────────────────────────────────────────────────── */
function updateStats() {
  const emails = allResults.reduce((a, r) =>
    a + (r.emails ? r.emails.split(",").filter(Boolean).length : 0), 0);
  const phones = allResults.reduce((a, r) =>
    a + (r.phones ? r.phones.split(",").filter(Boolean).length : 0), 0);
  const cities = allResults.filter(r => r.city).length;
  animate(statSites,  allResults.length);
  animate(statEmails, emails);
  animate(statPhones, phones);
  animate(statCities, cities);
}

/* ── Sort ───────────────────────────────────────────────────────── */
document.querySelectorAll(".sortable").forEach(th => {
  th.addEventListener("click", () => {
    const col = th.dataset.sort;
    if (sortColumn === col) {
      sortDir = sortDir === "asc" ? "desc" : "asc";
    } else {
      sortColumn = col;
      sortDir = "asc";
    }
    document.querySelectorAll(".sortable").forEach(t => {
      t.classList.remove("sort-asc", "sort-desc");
    });
    th.classList.add(sortDir === "asc" ? "sort-asc" : "sort-desc");
    renderRows(currentFiltered());
  });
});

function sortRows(rows) {
  if (!sortColumn) return rows;
  return [...rows].sort((a, b) => {
    const av = (a[sortColumn] || "").toLowerCase();
    const bv = (b[sortColumn] || "").toLowerCase();
    if (av < bv) return sortDir === "asc" ? -1 : 1;
    if (av > bv) return sortDir === "asc" ? 1 : -1;
    return 0;
  });
}

/* ── Filter ─────────────────────────────────────────────────────── */
filterInput.addEventListener("input", () => renderRows(currentFiltered()));

function currentFiltered() {
  const q = filterInput.value.trim().toLowerCase();
  if (!q) return allResults;
  return allResults.filter(r =>
    (r.url     || "").toLowerCase().includes(q) ||
    (r.emails  || "").toLowerCase().includes(q) ||
    (r.phones  || "").toLowerCase().includes(q) ||
    (r.socials || "").toLowerCase().includes(q) ||
    (r.city    || "").toLowerCase().includes(q)
  );
}

/* ── Render ─────────────────────────────────────────────────────── */
function renderRows(rows) {
  rows = sortRows(rows);
  resultsBody.innerHTML = "";
  const total = allResults.length;

  if (rows.length === 0) {
    emptyFilter.style.display = "block";
    showingLabel.textContent = `0 of ${total}`;
    return;
  }
  emptyFilter.style.display = "none";
  showingLabel.textContent = rows.length === total
    ? `${total} result${total !== 1 ? "s" : ""}`
    : `${rows.length} of ${total}`;

  rows.forEach((row) => {
    const num = allResults.indexOf(row) + 1;
    const emailHtml = pillsFromCSV(row.emails, "emerald");
    const phoneHtml = phonePills(row.phones);
    const socialHtml = socialPills(row.socials);
    const cityHtml = row.city
      ? `<span class="inline-flex items-center gap-1 bg-violet-50 border border-violet-200 text-violet-800 text-xs font-medium rounded-lg px-2 py-1">${esc(row.city)}</span>`
      : `<span class="text-gray-300 text-xs italic">—</span>`;

    const tr = document.createElement("tr");
    tr.className = "border-b border-slate-100 hover:bg-slate-50 transition-colors";
    tr.innerHTML = `
      <td class="px-4 py-3 text-xs text-gray-300 font-semibold align-top">${num}</td>
      <td class="px-4 py-3 align-top">
        <a href="https://${esc(row.url)}" target="_blank" rel="noopener"
           class="text-blue-700 font-semibold text-sm hover:underline break-all">${esc(row.url)}</a>
      </td>
      <td class="px-4 py-3 align-top">${emailHtml}</td>
      <td class="px-4 py-3 align-top">${phoneHtml}</td>
      <td class="px-4 py-3 align-top">${socialHtml}</td>
      <td class="px-4 py-3 align-top">${cityHtml}</td>
    `;
    resultsBody.appendChild(tr);
  });

  // Wire up copy buttons
  resultsBody.querySelectorAll(".copy-btn").forEach(btn => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      navigator.clipboard.writeText(btn.dataset.value).then(showToast);
    });
  });
}

function pillsFromCSV(csv, color) {
  if (!csv) return `<span class="text-gray-300 text-xs italic">—</span>`;
  const items = csv.split(",").map(x => x.trim()).filter(Boolean);
  return `<div class="flex flex-col gap-1">${
    items.map(v => `
      <span class="inline-flex items-center gap-1.5 bg-${color}-50 border border-${color}-200 text-${color}-800 text-xs font-medium rounded-lg px-2 py-1">
        <span class="break-all">${esc(v)}</span>
        <button class="copy-btn flex-shrink-0 text-${color}-300 hover:text-${color}-700 transition" data-value="${esc(v)}" title="Copy">
          <svg class="w-3 h-3 pointer-events-none" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24">
            <rect x="9" y="9" width="13" height="13" rx="2"/>
            <path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/>
          </svg>
        </button>
      </span>`).join("")
  }</div>`;
}

function phonePills(csv) {
  if (!csv) return `<span class="text-gray-300 text-xs italic">—</span>`;
  const items = csv.split(",").map(x => x.trim()).filter(Boolean);
  return `<div class="flex flex-col gap-1">${
    items.map(p => `
      <span class="phone-pill">
        <span>${esc(p)}</span>
        <button class="copy-btn" data-value="${esc(p)}" title="Copy">
          <svg class="w-3 h-3 pointer-events-none" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24">
            <rect x="9" y="9" width="13" height="13" rx="2"/>
            <path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/>
          </svg>
        </button>
      </span>`).join("")
  }</div>`;
}

function socialPills(csv) {
  if (!csv) return `<span class="text-gray-300 text-xs italic">—</span>`;
  const items = csv.split(",").map(x => x.trim()).filter(Boolean);
  return `<div class="flex flex-col gap-1">${
    items.map(item => {
      const [platform, ...rest] = item.split(":");
      const url = rest.join(":").trim();
      const p = platform.trim().toLowerCase();
      return `<a href="${esc(url)}" target="_blank" rel="noopener" class="social-pill social-${esc(p)}">${esc(p)}</a>`;
    }).join("")
  }</div>`;
}

/* ── Copy all emails ────────────────────────────────────────────── */
copyAllBtn.addEventListener("click", () => {
  const allEmails = allResults
    .map(r => r.emails)
    .filter(Boolean)
    .join(", ");
  if (!allEmails) return;
  navigator.clipboard.writeText(allEmails).then(() => showToast(`Copied ${allEmails.split(",").length} emails`));
});

/* ── Export ─────────────────────────────────────────────────────── */
async function exportAs(format) {
  if (!allResults.length) return;
  try {
    const res = await fetch("/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ results: allResults, format }),
    });
    const blob = await res.blob();
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement("a");
    a.href = url;
    a.download = "scrape-results." + format;
    a.click();
    URL.revokeObjectURL(url);
  } catch (err) {
    showError("Export failed: " + err.message);
  }
}
exportCsvBtn.addEventListener("click",  () => exportAs("csv"));
exportXlsxBtn.addEventListener("click", () => exportAs("xlsx"));

/* ── Failed section ─────────────────────────────────────────────── */
toggleFailedBtn.addEventListener("click", () => {
  const hidden = failedBody.style.display === "none";
  failedBody.style.display = hidden ? "block" : "none";
  toggleFailedBtn.textContent = hidden ? "Hide" : "Show";
});

/* ── History (localStorage) ─────────────────────────────────────── */
function loadHistory() {
  try { return JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]"); }
  catch { return []; }
}
function saveHistoryList(list) {
  try { localStorage.setItem(HISTORY_KEY, JSON.stringify(list)); } catch {}
}
function saveToHistory() {
  if (!allResults.length) return;
  const list = loadHistory();
  list.unshift({
    when: Date.now(),
    count: allResults.length,
    failed: failedUrls.length,
    results: allResults,
    failedUrls: failedUrls,
  });
  saveHistoryList(list.slice(0, HISTORY_MAX));
}

historyBtn.addEventListener("click", () => {
  const list = loadHistory();
  historyList.innerHTML = list.length
    ? list.map((h, i) => `
        <div class="history-item" data-idx="${i}">
          <div class="meta">
            <strong>${h.count}</strong> result${h.count !== 1 ? "s" : ""}
            ${h.failed ? ` · <span class="text-amber-700">${h.failed} skipped</span>` : ""}
            <div class="when">${new Date(h.when).toLocaleString()}</div>
          </div>
          <button class="del-btn" data-del="${i}" title="Delete">
            <svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24"><path d="M6 18L18 6M6 6l12 12"/></svg>
          </button>
        </div>`).join("")
    : `<div class="text-center text-sm text-gray-400 py-6">No previous scrapes yet.</div>`;
  historyModal.style.display = "flex";
});

historyList.addEventListener("click", (e) => {
  const del = e.target.closest("[data-del]");
  if (del) {
    e.stopPropagation();
    const idx = +del.dataset.del;
    const list = loadHistory();
    list.splice(idx, 1);
    saveHistoryList(list);
    historyBtn.click();   // refresh modal
    return;
  }
  const item = e.target.closest(".history-item");
  if (!item) return;
  const list = loadHistory();
  const h = list[+item.dataset.idx];
  if (!h) return;
  allResults = h.results || [];
  failedUrls = h.failedUrls || [];
  finalizeRender();
  closeHistory();
});

closeHistoryBtn.addEventListener("click", closeHistory);
clearHistoryBtn.addEventListener("click", () => {
  if (confirm("Clear all history?")) {
    saveHistoryList([]);
    historyBtn.click();
  }
});
historyModal.addEventListener("click", (e) => {
  if (e.target === historyModal) closeHistory();
});
function closeHistory() { historyModal.style.display = "none"; }

/* ── UI helpers ─────────────────────────────────────────────────── */
function showProgress(title, sub) {
  progressTitle.textContent = title;
  progressSub.textContent = sub || "";
  progressFill.style.width = "0%";
  progressCard.style.display = "block";
  scrapeTextBtn.disabled = true;
  scrapeFileBtn.disabled = true;
}
function hideProgress() {
  progressCard.style.display = "none";
  scrapeTextBtn.disabled = false;
  if (scrapeFileBtn._file) scrapeFileBtn.disabled = false;
}
function showError(msg) {
  errorMsg.textContent = msg;
  errorBox.style.display = "flex";
}
function hideError() { errorBox.style.display = "none"; }

let toastTimer;
function showToast(msg) {
  if (msg) toast.textContent = msg;
  else toast.textContent = "Copied!";
  toast.style.opacity = "1";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.style.opacity = "0", 1800);
}

function animate(el, target) {
  const start = performance.now();
  const from = parseInt(el.textContent) || 0;
  (function step(now) {
    const t = Math.min((now - start) / 500, 1);
    el.textContent = Math.round(from + (target - from) * (1 - Math.pow(1 - t, 3)));
    if (t < 1) requestAnimationFrame(step);
  })(start);
}

function esc(s) {
  return String(s || "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
