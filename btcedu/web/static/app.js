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
    setTimeout(() => (el.className = "toast"), 3000);
  }

  // ── Render episode table ─────────────────────────────────────
  const FILE_KEYS = [
    "audio", "transcript_raw", "transcript_clean", "chunks",
    "outline", "script", "shorts", "visuals", "qa", "publishing"
  ];
  const FILE_LABELS = [
    "Audio", "Transcript DE", "Transcript Clean", "Chunks",
    "Outline TR", "Script TR", "Shorts", "Visuals", "QA", "Publishing"
  ];

  function renderTable(eps) {
    const tbody = document.getElementById("ep-tbody");
    tbody.innerHTML = "";
    eps.forEach((ep) => {
      const tr = document.createElement("tr");
      if (selected && selected.episode_id === ep.episode_id) tr.classList.add("selected");
      tr.onclick = () => selectEpisode(ep);

      const pub = ep.published_at ? ep.published_at.slice(0, 10) : "—";
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
    selected = ep;
    applyFilters(); // re-render to highlight row

    const det = document.getElementById("detail");
    det.innerHTML = `
      <div class="detail-header">
        <h2>${esc(ep.title)}</h2>
        <div class="detail-meta">
          <span class="badge badge-${ep.status}">${ep.status}</span>
          ${ep.episode_id} &middot; ${ep.published_at ? ep.published_at.slice(0, 10) : "—"}
          &middot; <a href="${esc(ep.url)}" target="_blank" style="color:var(--accent)">source</a>
          ${ep.error_message ? `<br><span style="color:var(--red)">Error: ${esc(trunc(ep.error_message, 120))}</span>` : ""}
          ${ep.retry_count > 0 ? ` &middot; retries: ${ep.retry_count}` : ""}
        </div>
        <div class="detail-actions">
          <button class="btn btn-sm" onclick="actions.download()">Download</button>
          <button class="btn btn-sm" onclick="actions.transcribe()">Transcribe</button>
          <button class="btn btn-sm" onclick="actions.chunk()">Chunk</button>
          <button class="btn btn-sm btn-primary" onclick="actions.generate()">Generate</button>
          <button class="btn btn-sm" onclick="actions.run()">Run All</button>
          <button class="btn btn-sm btn-danger" onclick="actions.retry()">Retry</button>
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
        <div class="tab" data-tab="report">Report</div>
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
    viewer.textContent = "Loading...";
    const data = await GET(`/episodes/${selected.episode_id}/files/${type}`);
    if (data.error) {
      viewer.textContent = data.error;
    } else {
      viewer.textContent = data.content;
    }
  }

  // ── Actions ──────────────────────────────────────────────────
  window.actions = {
    async download() {
      if (!selected) return;
      toast("Downloading...");
      const r = await POST(`/episodes/${selected.episode_id}/download`, { force: isForce() });
      r.success ? toast("Downloaded: " + (r.path || "ok")) : toast(r.error, false);
      refresh();
    },
    async transcribe() {
      if (!selected) return;
      toast("Transcribing...");
      const r = await POST(`/episodes/${selected.episode_id}/transcribe`, { force: isForce() });
      r.success ? toast("Transcribed") : toast(r.error, false);
      refresh();
    },
    async chunk() {
      if (!selected) return;
      toast("Chunking...");
      const r = await POST(`/episodes/${selected.episode_id}/chunk`, { force: isForce() });
      r.success ? toast(`${r.count} chunks`) : toast(r.error, false);
      refresh();
    },
    async generate() {
      if (!selected) return;
      toast("Generating (this may take minutes)...");
      const r = await POST(`/episodes/${selected.episode_id}/generate`, {
        force: isForce(),
        dry_run: isDryRun(),
      });
      r.success ? toast(`${r.artifacts} artifacts, $${(r.cost_usd || 0).toFixed(4)}`) : toast(r.error, false);
      refresh();
    },
    async run() {
      if (!selected) return;
      toast("Running full pipeline...");
      const r = await POST(`/episodes/${selected.episode_id}/run`, { force: isForce() });
      r.success ? toast(`Pipeline OK, $${(r.cost_usd || 0).toFixed(4)}`) : toast(r.error || "Failed", false);
      refresh();
    },
    async retry() {
      if (!selected) return;
      toast("Retrying...");
      const r = await POST(`/episodes/${selected.episode_id}/retry`);
      r.success ? toast("Retry succeeded") : toast(r.error, false);
      refresh();
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
