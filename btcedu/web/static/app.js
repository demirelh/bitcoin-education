/* btcedu dashboard - vanilla JS */
(function () {
  "use strict";

  let episodes = [];
  let selected = null;

  // ── API helpers ──────────────────────────────────────────────
  // Use relative URL so requests stay within the reverse-proxy prefix
  // (e.g. /dashboard/api/... when served behind Caddy at /dashboard/).
  async function api(method, path, body) {
    const opts = { method, headers: {} };
    if (body) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const endpoint = "api" + path;
    const res = await fetch(endpoint, opts);
    if (!res.ok) {
      // Handle non-JSON error responses (e.g. 401 from proxy)
      const ct = res.headers.get("content-type") || "";
      if (!ct.includes("application/json")) {
        return { error: `HTTP ${res.status} ${res.statusText}` };
      }
    }
    const data = await res.json();
    if (!res.ok && !data.error) data.error = `HTTP ${res.status}`;
    return data;
  }
  const GET = (p) => api("GET", p);
  const POST = (p, b) => api("POST", p, b);

  // ── Toast ────────────────────────────────────────────────────
  function toast(msg, ok = true) {
    const el = document.getElementById("toast");
    el.textContent = msg;
    el.className = "toast show " + (ok ? "toast-ok" : "toast-err");
    setTimeout(() => (el.className = "toast"), 4000);
  }

  // ── Job polling ──────────────────────────────────────────────
  let activeJobId = null;
  let pollTimer = null;
  let logPollTimer = null;

  function submitJob(label, endpoint, body) {
    toast(label + "...");
    disableActions(true);
    POST(endpoint, body).then((r) => {
      if (r.error && !r.job_id) {
        toast(r.error, false);
        disableActions(false);
        return;
      }
      if (r.job_id) {
        activeJobId = r.job_id;
        showSpinner(label);
        pollJob(r.job_id, label);
      }
    }).catch((err) => {
      toast("Failed: " + err.message, false);
      disableActions(false);
    });
  }

  function pollJob(jobId, label) {
    clearInterval(pollTimer);
    pollTimer = setInterval(async () => {
      try {
        const j = await GET("/jobs/" + jobId);
        if (j.error && !j.state) {
          clearInterval(pollTimer);
          hideSpinner();
          disableActions(false);
          toast(j.error, false);
          activeJobId = null;
          return;
        }
        updateSpinner(label + ": " + (j.stage || j.state));

        // Auto-refresh logs tab while running
        const activeTab = document.querySelector(".tab.active");
        if (activeTab && activeTab.dataset.tab === "logs") {
          loadLogTail();
        }

        if (j.state === "success") {
          clearInterval(pollTimer);
          hideSpinner();
          disableActions(false);
          activeJobId = null;
          let msg = label + " complete";
          if (j.result) {
            if (j.result.path) msg += ": " + j.result.path.split("/").pop();
            if (j.result.count) msg += ": " + j.result.count + " chunks";
            if (j.result.artifacts) msg += ": " + j.result.artifacts + " artifacts";
            if (j.result.cost_usd) msg += " ($" + j.result.cost_usd.toFixed(4) + ")";
          }
          toast(msg);
          refresh();
        } else if (j.state === "error") {
          clearInterval(pollTimer);
          hideSpinner();
          disableActions(false);
          activeJobId = null;
          toast(j.message || "Job failed", false);
          refresh();
        }
      } catch (err) {
        // Network error, keep polling
      }
    }, 2000);
  }

  function disableActions(disabled) {
    document.querySelectorAll(".detail-actions button").forEach((btn) => {
      btn.disabled = disabled;
    });
  }

  function showSpinner(label) {
    let el = document.getElementById("job-spinner");
    if (!el) {
      const actions = document.querySelector(".detail-actions");
      if (!actions) return;
      el = document.createElement("span");
      el.id = "job-spinner";
      el.className = "spinner-inline";
      actions.appendChild(el);
    }
    el.innerHTML = '<span class="spinner"></span> <span class="spinner-text">' + esc(label) + "</span>";
    el.style.display = "inline-flex";
  }

  function updateSpinner(text) {
    const el = document.querySelector(".spinner-text");
    if (el) el.textContent = text;
  }

  function hideSpinner() {
    const el = document.getElementById("job-spinner");
    if (el) el.style.display = "none";
  }

  // ── Render episode table ─────────────────────────────────────
  const FILE_KEYS = [
    "audio", "transcript_raw", "transcript_clean", "chunks",
    "outline", "script", "shorts", "visuals", "qa", "publishing",
    "outline_v2", "script_v2", "publishing_v2"
  ];
  const FILE_LABELS = [
    "Audio", "Transcript DE", "Transcript Clean", "Chunks",
    "Outline TR", "Script TR", "Shorts", "Visuals", "QA", "Publishing",
    "Outline v2", "Script v2", "Publishing v2"
  ];

  function renderTable(eps) {
    const tbody = document.getElementById("ep-tbody");
    tbody.innerHTML = "";
    eps.forEach((ep) => {
      const tr = document.createElement("tr");
      if (selected && selected.episode_id === ep.episode_id) tr.classList.add("selected");
      tr.onclick = () => selectEpisode(ep);

      const pub = ep.published_at ? ep.published_at.slice(0, 10) : "\u2014";
      const dots = FILE_KEYS.map((k, i) => {
        const present = ep.files && ep.files[k];
        return `<span class="file-dot ${present ? "present" : ""}" title="${FILE_LABELS[i]}"></span>`;
      }).join("");

      tr.innerHTML =
        `<td><span class="badge badge-${ep.status}">${ep.status}</span></td>` +
        `<td title="${esc(ep.title)}">${esc(trunc(ep.title, 45))}</td>` +
        `<td>${pub}</td>` +
        `<td><div class="files-row">${dots}</div></td>` +
        `<td>${ep.retry_count > 0 ? ep.retry_count : ""}</td>`;
      tbody.appendChild(tr);
    });
  }

  // ── Filters ──────────────────────────────────────────────────
  function applyFilters() {
    const status = document.getElementById("filter-status").value;
    const search = document.getElementById("filter-search").value.toLowerCase();
    const filtered = episodes.filter((ep) => {
      if (status && ep.status !== status) return false;
      if (search && !ep.title.toLowerCase().includes(search) && !ep.episode_id.toLowerCase().includes(search)) return false;
      return true;
    });
    renderTable(filtered);
  }

  // ── Detail panel ─────────────────────────────────────────────
  async function selectEpisode(ep) {
    clearInterval(logPollTimer);
    selected = ep;
    applyFilters(); // re-render to highlight row

    const det = document.getElementById("detail");
    det.innerHTML = `
      <div class="detail-header">
        <h2>${esc(ep.title)}</h2>
        <div class="detail-meta">
          <span class="badge badge-${ep.status}">${ep.status}</span>
          ${ep.episode_id} &middot; ${ep.published_at ? ep.published_at.slice(0, 10) : "\u2014"}
          &middot; <a href="${esc(ep.url)}" target="_blank" style="color:var(--accent)">source</a>
          ${ep.error_message ? `<br><span style="color:var(--red)">Error: ${esc(trunc(ep.error_message, 120))}</span>` : ""}
          ${ep.retry_count > 0 ? ` &middot; retries: ${ep.retry_count}` : ""}
        </div>
        <div class="detail-actions">
          <button class="btn btn-sm" onclick="actions.download()" title="Download episode audio via yt-dlp">Download</button>
          <button class="btn btn-sm" onclick="actions.transcribe()" title="Transcribe audio via Whisper API">Transcribe</button>
          <button class="btn btn-sm" onclick="actions.chunk()" title="Split transcript into searchable chunks">Chunk</button>
          <button class="btn btn-sm btn-primary" onclick="actions.generate()" title="Generate Turkish content via Claude API">Generate</button>
          <button class="btn btn-sm" onclick="actions.refine()" title="Refine generated content using QA feedback (v1 → v2)">Refine</button>
          <button class="btn btn-sm" onclick="actions.run()" title="Run full pipeline from the earliest incomplete stage">Run All</button>
          <button class="btn btn-sm btn-danger" onclick="actions.retry()" title="Resume from the last failed stage">Retry</button>
          <label><input type="checkbox" id="chk-force"> force</label>
          <label><input type="checkbox" id="chk-dryrun"> dry-run</label>
        </div>
      </div>
      <div class="tabs" id="tabs">
        <div class="tab active" data-tab="transcript_clean">DE Transcript</div>
        <div class="tab" data-tab="outline">Outline TR</div>
        <div class="tab" data-tab="script">Script TR</div>
        <div class="tab" data-tab="qa">QA</div>
        <div class="tab" data-tab="publishing">Publishing</div>
        <div class="tab" data-tab="outline_v2">Outline v2</div>
        <div class="tab" data-tab="script_v2">Script v2</div>
        <div class="tab" data-tab="publishing_v2">Publishing v2</div>
        <div class="tab" data-tab="report">Report</div>
        <div class="tab" data-tab="logs">Logs</div>
      </div>
      <div class="viewer" id="viewer">Click a tab to load content.</div>
    `;

    // Bind tabs
    det.querySelectorAll(".tab").forEach((t) => {
      t.onclick = () => loadTab(t.dataset.tab);
    });

    // Auto-load first tab
    loadTab("transcript_clean");
  }

  async function loadTab(type) {
    if (!selected) return;
    document.querySelectorAll(".tab").forEach((t) => {
      t.classList.toggle("active", t.dataset.tab === type);
    });
    const viewer = document.getElementById("viewer");

    // Stop previous log polling
    clearInterval(logPollTimer);

    if (type === "logs") {
      viewer.textContent = "Loading logs...";
      viewer.classList.add("log-viewer");
      await loadLogTail();
      // Auto-refresh while a job is active
      if (activeJobId) {
        logPollTimer = setInterval(loadLogTail, 2000);
      }
      return;
    }

    viewer.classList.remove("log-viewer");
    viewer.textContent = "Loading...";
    const data = await GET(`/episodes/${selected.episode_id}/files/${type}`);
    if (data.error) {
      viewer.textContent = data.error;
    } else {
      viewer.textContent = data.content;
    }
  }

  async function loadLogTail() {
    if (!selected) return;
    const viewer = document.getElementById("viewer");
    try {
      const data = await GET(`/episodes/${selected.episode_id}/action-log?tail=200`);
      if (data.lines && data.lines.length > 0) {
        viewer.textContent = data.lines.join("\n");
        viewer.scrollTop = viewer.scrollHeight;
      } else {
        viewer.textContent = "No logs yet for this episode.";
      }
    } catch (err) {
      viewer.textContent = "Failed to load logs.";
    }
  }

  // ── Actions ──────────────────────────────────────────────────
  window.actions = {
    download() {
      if (!selected) return;
      submitJob("Download", `/episodes/${selected.episode_id}/download`, { force: isForce() });
    },
    transcribe() {
      if (!selected) return;
      submitJob("Transcribe", `/episodes/${selected.episode_id}/transcribe`, { force: isForce() });
    },
    chunk() {
      if (!selected) return;
      submitJob("Chunk", `/episodes/${selected.episode_id}/chunk`, { force: isForce() });
    },
    generate() {
      if (!selected) return;
      submitJob("Generate", `/episodes/${selected.episode_id}/generate`, {
        force: isForce(),
        dry_run: isDryRun(),
      });
    },
    refine() {
      if (!selected) return;
      submitJob("Refine", `/episodes/${selected.episode_id}/refine`, { force: isForce() });
    },
    run() {
      if (!selected) return;
      submitJob("Run All", `/episodes/${selected.episode_id}/run`, { force: isForce() });
    },
    retry() {
      if (!selected) return;
      submitJob("Retry", `/episodes/${selected.episode_id}/retry`);
    },
  };

  function isForce() {
    const el = document.getElementById("chk-force");
    return el ? el.checked : false;
  }
  function isDryRun() {
    const el = document.getElementById("chk-dryrun");
    return el ? el.checked : false;
  }

  // ── Global actions ───────────────────────────────────────────
  window.detectEpisodes = async function () {
    toast("Detecting...");
    const r = await POST("/detect");
    r.success ? toast(`Found: ${r.found}, New: ${r.new}`) : toast(r.error, false);
    refresh();
  };

  window.showCost = async function () {
    const data = await GET("/cost");
    const modal = document.getElementById("cost-modal");
    let rows = "";
    (data.stages || []).forEach((s) => {
      rows += `<tr><td>${s.stage}</td><td>${s.runs}</td><td>${s.input_tokens}</td><td>${s.output_tokens}</td><td>$${s.cost_usd.toFixed(4)}</td></tr>`;
    });
    document.getElementById("cost-body").innerHTML = rows;
    document.getElementById("cost-total").textContent =
      `Total: $${(data.total_usd || 0).toFixed(4)} | ${data.episodes_processed || 0} episodes | avg $${(data.avg_per_episode || 0).toFixed(4)}/ep`;
    modal.classList.add("open");
  };

  window.closeCost = function () {
    document.getElementById("cost-modal").classList.remove("open");
  };

  // ── What's new ───────────────────────────────────────────────
  async function loadWhatsNew() {
    try {
      const data = await GET("/whats-new");
      if (data.error) return;
      const bar = document.getElementById("whats-new");
      let html = "";
      const nn = (data.new_episodes || []).length;
      const nf = (data.failed || []).length;
      const ni = (data.incomplete || []).length;
      if (nn) html += `<span class="wn-badge wn-new">${nn} new</span>`;
      if (nf) html += `<span class="wn-badge wn-failed">${nf} failed</span>`;
      if (ni) html += `<span class="wn-badge wn-incomplete">${ni} incomplete</span>`;
      if (!html) html = "All episodes up to date.";
      bar.innerHTML = html;
    } catch (err) {
      // Non-critical, don't block the UI
    }
  }

  // ── Refresh ──────────────────────────────────────────────────
  async function refresh() {
    try {
      const data = await GET("/episodes");
      if (data.error) {
        showError("API error: " + data.error);
        return;
      }
      episodes = Array.isArray(data) ? data : [];
      applyFilters();
      loadWhatsNew();
      // Re-select if still exists
      if (selected) {
        const found = episodes.find((e) => e.episode_id === selected.episode_id);
        if (found) selectEpisode(found);
      }
    } catch (err) {
      showError("Cannot reach API: " + err.message);
    }
  }
  window.refresh = refresh;

  function showError(msg) {
    const tbody = document.getElementById("ep-tbody");
    tbody.innerHTML = `<tr><td colspan="5" class="empty" style="color:var(--red)">${esc(msg)}</td></tr>`;
    toast(msg, false);
  }

  // ── Utils ────────────────────────────────────────────────────
  function esc(s) {
    if (!s) return "";
    const d = document.createElement("div");
    d.textContent = s;
    return d.innerHTML;
  }
  function trunc(s, n) {
    return s && s.length > n ? s.slice(0, n) + "..." : s || "";
  }

  // ── Init ─────────────────────────────────────────────────────
  document.addEventListener("DOMContentLoaded", () => {
    document.getElementById("filter-status").onchange = applyFilters;
    document.getElementById("filter-search").oninput = applyFilters;
    refresh();
  });
})();
