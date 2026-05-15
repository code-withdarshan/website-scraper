/* ── Tab switching ─────────────────────────────────────────────── */
document.getElementById("tab-text").addEventListener("click", function () { switchTab("text"); });
document.getElementById("tab-file").addEventListener("click", function () { switchTab("file"); });

function switchTab(name) {
  ["text", "file"].forEach(function (t) {
    var btn   = document.getElementById("tab-" + t);
    var panel = document.getElementById("panel-" + t);
    if (t === name) {
      btn.classList.add("active");
      panel.classList.add("active");
    } else {
      btn.classList.remove("active");
      panel.classList.remove("active");
    }
  });
}

/* ── State ─────────────────────────────────────────────────────── */
let allResults = [];

/* ── DOM refs ──────────────────────────────────────────────────── */
const urlInput      = document.getElementById("url-input");
const urlCount      = document.getElementById("url-count");
const clearBtn      = document.getElementById("clear-btn");
const scrapeTextBtn = document.getElementById("scrape-text-btn");
const dropZone      = document.getElementById("drop-zone");
const fileInput     = document.getElementById("file-input");
const filePreview   = document.getElementById("file-preview");
const fileNameText  = document.getElementById("file-name-text");
const removeFileBtn = document.getElementById("remove-file-btn");
const scrapeFileBtn = document.getElementById("scrape-file-btn");
const progressCard  = document.getElementById("progress-card");
const progressTitle = document.getElementById("progress-title");
const errorBox      = document.getElementById("error-box");
const errorMsg      = document.getElementById("error-msg");
const resultsSection= document.getElementById("results-section");
const statSites     = document.getElementById("stat-sites");
const statEmails    = document.getElementById("stat-emails");
const statCities    = document.getElementById("stat-cities");
const exportBtn     = document.getElementById("export-btn");
const filterInput   = document.getElementById("filter-input");
const showingLabel  = document.getElementById("showing-label");
const resultsBody   = document.getElementById("results-body");
const emptyFilter   = document.getElementById("empty-filter");
const toast         = document.getElementById("toast");

/* ── URL counter ───────────────────────────────────────────────── */
function updateUrlCount() {
  const n = urlInput.value.trim()
    ? urlInput.value.split(/[\n,]+/).map(s => s.trim()).filter(Boolean).length
    : 0;
  urlCount.textContent = n === 0 ? "0 URLs" : n + " URL" + (n !== 1 ? "s" : "");
}
urlInput.addEventListener("input", updateUrlCount);

/* ── Auto-format pasted URLs into comma-separated list ─────────── */
urlInput.addEventListener("paste", function (e) {
  e.preventDefault();
  const pasted = (e.clipboardData || window.clipboardData).getData("text");

  // Split on any whitespace, newline, comma, semicolon, or tab
  const urls = pasted.split(/[\s,;]+/).map(s => s.trim()).filter(Boolean);
  if (urls.length === 0) return;

  // Insert the comma-separated form into the textarea at the cursor position
  const start = urlInput.selectionStart;
  const end   = urlInput.selectionEnd;
  const before = urlInput.value.slice(0, start);
  const after  = urlInput.value.slice(end);

  // If the textarea already has content, separate with ", "
  const sep = before && !before.endsWith(",") && !before.endsWith(", ") ? ", " : "";
  const insert = sep + urls.join(", ");

  urlInput.value = before + insert + after;

  // Put cursor right after inserted text
  const newPos = (before + insert).length;
  urlInput.setSelectionRange(newPos, newPos);

  updateUrlCount();
});

clearBtn.addEventListener("click", function () {
  urlInput.value = "";
  urlCount.textContent = "0 URLs";
  urlInput.focus();
});

/* ── Drop zone ─────────────────────────────────────────────────── */
dropZone.addEventListener("click", function () { fileInput.click(); });

dropZone.addEventListener("dragover", function (e) {
  e.preventDefault();
  dropZone.style.borderColor = "#2563eb";
  dropZone.style.background  = "#eff6ff";
});
dropZone.addEventListener("dragleave", function () {
  dropZone.style.borderColor = "";
  dropZone.style.background  = "";
});
dropZone.addEventListener("drop", function (e) {
  e.preventDefault();
  dropZone.style.borderColor = "";
  dropZone.style.background  = "";
  if (e.dataTransfer.files.length) handleFile(e.dataTransfer.files[0]);
});
fileInput.addEventListener("change", function () {
  if (fileInput.files.length) handleFile(fileInput.files[0]);
});

removeFileBtn.addEventListener("click", function (e) {
  e.stopPropagation();
  fileInput.value        = "";
  scrapeFileBtn._file    = null;
  scrapeFileBtn.disabled = true;
  filePreview.style.display = "none";
  dropZone.style.display    = "";
  hideError();
});

function handleFile(file) {
  const allowed = [".csv", ".xls", ".xlsx"];
  const ext = file.name.slice(file.name.lastIndexOf(".")).toLowerCase();
  if (!allowed.includes(ext)) {
    showError("Unsupported file type. Please upload a .csv, .xls, or .xlsx file.");
    return;
  }
  scrapeFileBtn._file       = file;
  scrapeFileBtn.disabled    = false;
  fileNameText.textContent  = file.name;
  filePreview.style.display = "flex";
  dropZone.style.display    = "none";
  hideError();
}

/* ── Scrape: text ──────────────────────────────────────────────── */
scrapeTextBtn.addEventListener("click", async function () {
  const raw = urlInput.value.trim();
  if (!raw) { showError("Please enter at least one URL."); return; }
  hideError();
  showProgress("Scraping URLs…");
  try {
    const res  = await fetch("/scrape", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ urls: raw }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Server error");
    renderResults(data.results);
  } catch (err) {
    showError(err.message);
  } finally {
    hideProgress();
  }
});

/* ── Scrape: file ──────────────────────────────────────────────── */
scrapeFileBtn.addEventListener("click", async function () {
  const file = scrapeFileBtn._file;
  if (!file) { showError("No file selected."); return; }
  hideError();
  showProgress("Uploading & scraping file…");
  const formData = new FormData();
  formData.append("file", file);
  try {
    const res  = await fetch("/scrape", { method: "POST", body: formData });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Server error");
    renderResults(data.results);
  } catch (err) {
    showError(err.message);
  } finally {
    hideProgress();
  }
});

/* ── Render results ────────────────────────────────────────────── */
function renderResults(results) {
  allResults = results;
  if (!results.length) {
    showError("No emails or city info found. Sites may be inaccessible or have no contact details in footer/contact page.");
    resultsSection.style.display = "none";
    return;
  }

  const totalEmails = results.reduce((acc, r) =>
    acc + (r.emails ? r.emails.split(",").map(e => e.trim()).filter(Boolean).length : 0), 0);
  const totalCities = results.filter(r => r.city).length;

  animateCount(statSites,  results.length);
  animateCount(statEmails, totalEmails);
  animateCount(statCities, totalCities);

  filterInput.value = "";
  renderRows(results);
  resultsSection.style.display = "block";
  setTimeout(() => resultsSection.scrollIntoView({ behavior: "smooth", block: "start" }), 100);
}

function renderRows(rows) {
  resultsBody.innerHTML = "";
  const total = allResults.length;

  if (rows.length === 0) {
    emptyFilter.style.display = "block";
    showingLabel.textContent  = "0 of " + total;
    return;
  }
  emptyFilter.style.display = "none";
  showingLabel.textContent  = rows.length === total
    ? total + " result" + (total !== 1 ? "s" : "")
    : rows.length + " of " + total;

  rows.forEach(function (row) {
    const num = allResults.indexOf(row) + 1;
    const emailList = row.emails
      ? row.emails.split(",").map(e => e.trim()).filter(Boolean)
      : [];

    const emailsHtml = emailList.length
      ? emailList.map(e => `
          <span class="inline-flex items-center gap-1.5 bg-emerald-50 border border-emerald-200 text-emerald-800 text-xs font-medium rounded-lg px-2.5 py-1">
            <span class="break-all">${esc(e)}</span>
            <button class="copy-btn flex-shrink-0 text-emerald-300 hover:text-emerald-700 transition" data-email="${esc(e)}" title="Copy">
              <svg class="w-3 h-3 pointer-events-none" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24">
                <rect x="9" y="9" width="13" height="13" rx="2"/>
                <path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/>
              </svg>
            </button>
          </span>`).join("")
      : `<span class="text-gray-300 text-xs italic">—</span>`;

    const cityHtml = row.city
      ? `<span class="inline-flex items-center gap-1.5 bg-violet-50 border border-violet-200 text-violet-800 text-xs font-medium rounded-lg px-2.5 py-1">
           <svg class="w-3 h-3 flex-shrink-0" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24">
             <path stroke-linecap="round" stroke-linejoin="round" d="M17.657 16.657L13.414 20.9a1.998 1.998 0 01-2.827 0l-4.244-4.243a8 8 0 1111.314 0z"/>
             <path stroke-linecap="round" stroke-linejoin="round" d="M15 11a3 3 0 11-6 0 3 3 0 016 0z"/>
           </svg>
           ${esc(row.city)}
         </span>`
      : `<span class="text-gray-300 text-xs italic">—</span>`;

    const tr = document.createElement("tr");
    tr.className = "border-b border-slate-100 hover:bg-slate-50 transition-colors";
    tr.innerHTML = `
      <td class="px-4 py-3.5 text-xs text-gray-300 font-semibold align-top">${num}</td>
      <td class="px-4 py-3.5 align-top">
        <a href="https://${esc(row.url)}" target="_blank" rel="noopener"
           class="text-blue-700 font-semibold text-sm hover:underline break-all">${esc(row.url)}</a>
      </td>
      <td class="px-4 py-3.5 align-top"><div class="flex flex-col gap-1.5">${emailsHtml}</div></td>
      <td class="px-4 py-3.5 align-top">${cityHtml}</td>
    `;
    resultsBody.appendChild(tr);
  });

  resultsBody.querySelectorAll(".copy-btn").forEach(function (btn) {
    btn.addEventListener("click", function (e) {
      e.stopPropagation();
      navigator.clipboard.writeText(btn.dataset.email).then(showToast);
    });
  });
}

/* ── Live filter ───────────────────────────────────────────────── */
filterInput.addEventListener("input", function () {
  const q = filterInput.value.trim().toLowerCase();
  renderRows(q
    ? allResults.filter(r =>
        r.url.toLowerCase().includes(q) ||
        r.emails.toLowerCase().includes(q) ||
        r.city.toLowerCase().includes(q))
    : allResults
  );
});

/* ── Export CSV ────────────────────────────────────────────────── */
exportBtn.addEventListener("click", async function () {
  if (!allResults.length) return;
  try {
    const res  = await fetch("/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ results: allResults }),
    });
    const blob = await res.blob();
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement("a");
    a.href = url; a.download = "scrape-results.csv"; a.click();
    URL.revokeObjectURL(url);
  } catch (err) {
    showError("Export failed: " + err.message);
  }
});

/* ── UI helpers ────────────────────────────────────────────────── */
function showProgress(msg) {
  progressTitle.textContent    = msg;
  progressCard.style.display   = "block";
  scrapeTextBtn.disabled       = true;
  scrapeFileBtn.disabled       = true;
}
function hideProgress() {
  progressCard.style.display   = "none";
  scrapeTextBtn.disabled       = false;
  if (scrapeFileBtn._file) scrapeFileBtn.disabled = false;
}
function showError(msg) {
  errorMsg.textContent       = msg;
  errorBox.style.display     = "flex";
}
function hideError() {
  errorBox.style.display     = "none";
}

let toastTimer;
function showToast() {
  toast.style.opacity = "1";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(function () { toast.style.opacity = "0"; }, 1800);
}

function animateCount(el, target) {
  const start = performance.now();
  const from  = parseInt(el.textContent) || 0;
  (function step(now) {
    const t = Math.min((now - start) / 600, 1);
    el.textContent = Math.round(from + (target - from) * (1 - Math.pow(1 - t, 3)));
    if (t < 1) requestAnimationFrame(step);
  })(start);
}

function esc(str) {
  return String(str)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
