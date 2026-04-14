/* btcedu dashboard - vanilla JS */
(function () {
  "use strict";

  let episodes = [];
  let selected = null;
  let channels = [];
  let selectedChannelId = null;
  let selectedProfile = null;
  let isMobileView = false;

  // ── Mobile navigation state ──────────────────────────────────
  function updateMobileView() {
    isMobileView = window.innerWidth <= 768;
    if (!isMobileView) {
      // Reset mobile classes on desktop
      document.body.classList.remove("mobile-list-view", "mobile-detail-view");
    }
  }

  // ── iOS viewport height fix ─────────────────────────────────
  // Set CSS custom property for 1% of viewport height
  // Workaround for iOS Safari's dynamic viewport (address bar show/hide)
  function setViewportHeight() {
    const vh = window.innerHeight * 0.01;
    document.documentElement.style.setProperty('--vh', `${vh}px`);
  }

  function mobileShowList() {
    if (isMobileView) {
      document.body.classList.remove("mobile-detail-view");
      document.body.classList.add("mobile-list-view");
      window.scrollTo(0, 0);
    }
  }
  window.mobileShowList = mobileShowList;

  function mobileShowDetail() {
    if (isMobileView) {
      document.body.classList.remove("mobile-list-view");
      document.body.classList.add("mobile-detail-view");
      window.scrollTo(0, 0);
    }
  }

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
    "outline_v2", "script_v2", "publishing_v2", "chapters",
    "stories", "stories_translated"
  ];
  const FILE_LABELS = [
    "Audio", "Transcript DE", "Transcript Clean", "Chunks",
    "Outline TR", "Script TR", "Shorts", "Visuals", "QA", "Publishing",
    "Outline v2", "Script v2", "Publishing v2", "Chapters",
    "Stories DE", "Stories TR"
  ];

  // ── Review UX helpers ────────────────────────────────────────
  function renderStatusBadges(ep) {
    let html = `<span class="badge badge-${ep.status}">${ep.status}</span>`;
    if (ep.review_context && ep.review_context.state === "paused_for_review") {
      html += ` <span class="badge badge-review-pending" title="${esc(ep.review_context.next_action_text)}">&#9208; review</span>`;
    }
    return html;
  }

  function formatDuration(s) {
    if (s < 60) return Math.round(s) + "s";
    if (s < 3600) return Math.round(s / 60) + "m";
    return (s / 3600).toFixed(1) + "h";
  }

  function renderPipelineStepper(sp) {
    if (!sp || !sp.stages || sp.stages.length === 0) return "";
    let html = '<div class="pipeline-stepper">';
    sp.stages.forEach((stage, i) => {
      if (i > 0) {
        const prevState = sp.stages[i - 1].state;
        const connClass = (prevState === "done" || prevState === "skipped")
          ? "ps-done" : "ps-pending";
        html += `<div class="ps-connector ${connClass}"></div>`;
      }
      const gateClass = stage.is_gate ? " ps-gate" : "";
      const stateClass = `ps-${stage.state}`;
      const icon = stage.state === "paused" ? "\u23f8"
                 : stage.state === "failed" ? "\u2717"
                 : stage.state === "done" ? "\u2713"
                 : "";
      const dur = stage.duration_seconds != null
        ? formatDuration(stage.duration_seconds)
        : "";
      const cost = stage.cost_usd != null && stage.cost_usd > 0
        ? `$${stage.cost_usd.toFixed(3)}`
        : "";
      const tooltip = [stage.label, dur, cost, "Click for details"].filter(Boolean).join(" \u00b7 ");

      html += `
        <div class="ps-stage ps-clickable${gateClass} ${stateClass}"
             title="${esc(tooltip)}"
             onclick="showStageDetail('${esc(stage.name)}')">
          <div class="ps-blob">${icon}</div>
          <div class="ps-label">${esc(stage.label)}</div>
          ${dur ? `<div class="ps-duration">${dur}</div>` : ""}
        </div>`;
    });
    html += "</div>";
    html += `<div class="ps-summary">${sp.completed_count}/${sp.total_count} stages complete</div>`;
    return html;
  }

  function renderNextAction(ep) {
    const rc = ep.review_context;
    // Failed / cost_limit episodes
    if (ep.status === "failed" || ep.status === "cost_limit") {
      return `
        <div class="next-action next-action-failed">
          <div class="next-action-icon">&#10007;</div>
          <div class="next-action-body">
            <strong>Pipeline failed</strong>
            <p>${esc(trunc(ep.error_message || "Unknown error", 200))}</p>
            <button class="btn btn-sm btn-danger" onclick="actions.retry()">Retry</button>
          </div>
        </div>`;
    }
    if (!rc) return "";
    if (rc.state === "paused_for_review") {
      return `
        <div class="next-action next-action-review">
          <div class="next-action-icon">&#9208;</div>
          <div class="next-action-body">
            <strong>${esc(rc.next_action_text)}</strong>
            <p>${esc(rc.review_stage_label)} is ${esc(rc.review_status)} since ${timeAgo(rc.created_at)}.</p>
            <button class="btn btn-primary btn-sm" onclick="jumpToReview(${rc.review_task_id})">Review now</button>
            <button class="btn btn-sm" onclick="actions.run()">Resume pipeline</button>
          </div>
        </div>`;
    }
    if (rc.state === "review_approved") {
      return `
        <div class="next-action next-action-approved">
          <div class="next-action-icon">&#10003;</div>
          <div class="next-action-body">
            <strong>${esc(rc.review_stage_label)} approved</strong>
            <p>Run the pipeline to continue to the next stage.</p>
            <button class="btn btn-primary btn-sm" onclick="actions.run()">Continue pipeline</button>
          </div>
        </div>`;
    }
    return "";
  }

  async function jumpToReview(reviewId) {
    showReviews();
    await loadReviewList();
    await selectReview(reviewId);
  }
  window.jumpToReview = jumpToReview;

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

      const profileBadge = ep.content_profile && ep.content_profile !== "bitcoin_podcast"
        ? `<span class="badge badge-profile" title="${esc(ep.content_profile)}">${esc(ep.content_profile)}</span> `
        : "";
      tr.innerHTML =
        `<td>${renderStatusBadges(ep)}</td>` +
        `<td title="${esc(ep.title)}">${profileBadge}${esc(trunc(ep.title, 45))}</td>` +
        `<td>${pub}</td>` +
        `<td><div class="files-row">${dots}</div></td>` +
        `<td>${ep.retry_count > 0 ? ep.retry_count : ""}</td>`;
      tbody.appendChild(tr);
    });

    // Render mobile cards
    renderCards(eps);
  }

  function renderCards(eps) {
    const container = document.getElementById("ep-cards");
    if (eps.length === 0) {
      container.innerHTML = '<div class="empty">No episodes found.</div>';
      return;
    }

    container.innerHTML = "";
    eps.forEach((ep) => {
      const card = document.createElement("div");
      card.className = "ep-card";
      if (selected && selected.episode_id === ep.episode_id) {
        card.classList.add("selected");
      }
      card.onclick = () => selectEpisode(ep);

      const pub = ep.published_at ? ep.published_at.slice(0, 10) : "\u2014";

      // File indicators with icons
      const fileIcons = [];
      if (ep.files) {
        if (ep.files.audio) fileIcons.push("🎵");
        if (ep.files.transcript_clean || ep.files.transcript_raw) fileIcons.push("📝");
        if (ep.files.chunks) fileIcons.push("📦");
        if (ep.files.script || ep.files.script_v2) fileIcons.push("📄");
        if (ep.files.qa) fileIcons.push("❓");
      }

      const retryBadge = ep.retry_count > 0
        ? `<span class="ep-card-retry">retry: ${ep.retry_count}</span>`
        : "";

      card.innerHTML = `
        <div class="ep-card-header">
          ${renderStatusBadges(ep)}
          ${retryBadge}
        </div>
        <div class="ep-card-title">${esc(ep.title)}</div>
        <div class="ep-card-meta">
          <span>${pub}</span>
          ${fileIcons.length > 0 ? '<span>' + fileIcons.join(' ') + '</span>' : ''}
        </div>
      `;

      container.appendChild(card);
    });
  }

  // ── Filters ──────────────────────────────────────────────────
  function applyFilters() {
    const status = document.getElementById("filter-status").value;
    const search = document.getElementById("filter-search").value.toLowerCase();
    const filtered = episodes.filter((ep) => {
      if (status === "review_pending") {
        if (!ep.review_context || ep.review_context.state !== "paused_for_review") return false;
      } else if (status && ep.status !== status) {
        return false;
      }
      if (search && !ep.title.toLowerCase().includes(search) && !ep.episode_id.toLowerCase().includes(search)) return false;
      return true;
    });
    renderTable(filtered);
    renderStatusSummary(episodes);
  }

  function renderStatusSummary(eps) {
    const container = document.getElementById("status-summary");
    if (!container || eps.length === 0) { container.innerHTML = ""; return; }
    const counts = {};
    eps.forEach(ep => { counts[ep.status] = (counts[ep.status] || 0) + 1; });
    // Sort by count desc, limit to 8 most common
    const sorted = Object.entries(counts).sort((a, b) => b[1] - a[1]).slice(0, 8);
    container.innerHTML = "";
    sorted.forEach(([st, n]) => {
      const span = document.createElement("span");
      span.className = `status-summary-item badge badge-${esc(st)}`;
      span.title = `Filter: ${st}`;
      span.dataset.status = st;
      const countEl = document.createElement("span");
      countEl.className = "status-summary-count";
      countEl.textContent = n;
      span.appendChild(countEl);
      span.appendChild(document.createTextNode(` ${st}`));
      span.addEventListener("click", () => filterByStatus(st));
      container.appendChild(span);
    });
  }

  function filterByStatus(status) {
    const sel = document.getElementById("filter-status");
    if (sel) { sel.value = status; applyFilters(); }
  }
  window.filterByStatus = filterByStatus;

  // ── Detail panel ─────────────────────────────────────────────
  async function selectEpisode(ep) {
    clearInterval(logPollTimer);
    selected = ep;
    applyFilters(); // re-render to highlight row

    // Show detail view on mobile
    mobileShowDetail();

    const det = document.getElementById("detail");
    det.innerHTML = `
      <div class="detail-header">
        <h2>${esc(ep.title)}</h2>
        <div class="detail-meta">
          ${renderStatusBadges(ep)}
          ${ep.episode_id} &middot; ${ep.published_at ? ep.published_at.slice(0, 10) : "\u2014"}
          &middot; <a href="${esc(ep.url)}" target="_blank" style="color:var(--accent)">source</a>
          ${ep.youtube_video_id ? `&middot; <a href="https://youtu.be/${esc(ep.youtube_video_id)}" target="_blank" style="color:#f90">▶ YouTube</a>` : ""}
          ${ep.error_message ? `<br><span style="color:var(--red)">Error: ${esc(trunc(ep.error_message, 120))}</span>` : ""}
          ${ep.retry_count > 0 ? ` &middot; retries: ${ep.retry_count}` : ""}
        </div>
        ${renderPipelineStepper(ep.stage_progress)}
        ${renderNextAction(ep)}
        <div class="detail-actions">
          <button class="btn btn-sm" onclick="actions.download()" title="Download episode audio via yt-dlp">Download</button>
          <button class="btn btn-sm" onclick="actions.transcribe()" title="Transcribe audio via Whisper API">Transcribe</button>
          <button class="btn btn-sm" onclick="actions.chunk()" title="Split transcript into searchable chunks">Chunk</button>
          <button class="btn btn-sm btn-primary" onclick="actions.generate()" title="Generate Turkish content via Claude API">Generate</button>
          <button class="btn btn-sm" onclick="actions.refine()" title="Refine generated content using QA feedback (v1 → v2)">Refine</button>
          <button class="btn btn-sm" onclick="actions.run()" title="Run full pipeline from the earliest incomplete stage">Run All</button>
          <button class="btn btn-sm btn-danger" onclick="actions.retry()" title="Resume from the last failed stage">Retry</button>
          <button class="btn btn-sm" onclick="actions.resetV2()" title="Reset to TRANSCRIBED & switch to v2 pipeline" style="border-color:var(--orange,#f90);color:var(--orange,#f90)">↻ v2</button>
          <button class="btn btn-sm btn-success" onclick="actions.publish()" title="Publish approved video to YouTube">Publish</button>
          <label><input type="checkbox" id="chk-force"> force</label>
          <label><input type="checkbox" id="chk-dryrun"> dry-run</label>
        </div>
      </div>
      <div class="tabs" id="tabs">
        <div class="tab active" data-tab="transcript_clean">DE Transcript</div>
        ${ep.content_profile === "tagesschau_tr" ? `
        <div class="tab" data-tab="stories">Stories DE</div>
        <div class="tab" data-tab="stories_translated">Stories TR</div>
        ` : `
        <div class="tab" data-tab="outline">Outline TR</div>
        <div class="tab" data-tab="script">Script TR</div>
        <div class="tab" data-tab="qa">QA</div>
        <div class="tab" data-tab="publishing">Publishing</div>
        <div class="tab" data-tab="outline_v2">Outline v2</div>
        <div class="tab" data-tab="script_v2">Script v2</div>
        <div class="tab" data-tab="publishing_v2">Publishing v2</div>
        `}
        <div class="tab" data-tab="chapters">Chapters</div>
        <div class="tab" data-tab="stock_images">Stock Images</div>
        <div class="tab" data-tab="images">Images</div>
        <div class="tab" data-tab="tts_audio">TTS Audio</div>
        <div class="tab" data-tab="video">Video</div>
        <div class="tab" data-tab="report">Report</div>
        <div class="tab" data-tab="logs">Logs</div>
      </div>
      <div class="viewer" id="viewer">Click a tab to load content.</div>
    `;

    // Inline video preview — built entirely via DOM API (no innerHTML) to keep
    // user-controlled data out of template literals assigned to innerHTML.
    if (["rendered", "approved", "published"].includes(ep.status)) {
      const header = det.querySelector(".detail-header");
      if (header) {
        const previewDiv = document.createElement("div");
        previewDiv.className = "video-inline-preview";
        const video = document.createElement("video");
        video.controls = true;
        video.preload = "none";
        const source = document.createElement("source");
        source.src = "api/episodes/" + ep.episode_id + "/render/draft.mp4";
        source.type = "video/mp4";
        video.appendChild(source);
        previewDiv.appendChild(video);
        header.appendChild(previewDiv);
      }
    }

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

    if (type === "stock_images") {
      viewer.classList.remove("log-viewer");
      viewer.innerHTML = "Loading stock image candidates...";
      await loadStockImagesPanel();
      return;
    }

    if (type === "images") {
      viewer.classList.remove("log-viewer");
      viewer.innerHTML = "Loading images...";
      await loadImagesPanel();
      return;
    }

    if (type === "tts_audio") {
      viewer.classList.remove("log-viewer");
      viewer.innerHTML = "Loading TTS data...";
      await loadTTSPanel();
      return;
    }

    if (type === "video") {
      viewer.classList.remove("log-viewer");
      viewer.innerHTML = "Loading video...";
      await loadVideoPanel();
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

  // ── Images Gallery Panel ─────────────────────────────────────
  async function loadImagesPanel() {
    if (!selected) return;
    const viewer = document.getElementById("viewer");

    try {
      const data = await GET(`/episodes/${selected.episode_id}/images`);
      if (data.error) {
        viewer.innerHTML = `
          <div class="images-panel">
            <p>No images generated yet.</p>
            <button class="btn btn-sm btn-primary" onclick="actions.imagegen()">Generate Images</button>
          </div>`;
        return;
      }

      const images = data.images || [];
      const generated = images.filter(i => i.generation_method !== 'failed');

      let cards = images.map(img => {
        const imgUrl = `api/episodes/${selected.episode_id}/images/${esc(img.filename || img.chapter_id + '.png')}`;
        const isFailed = img.generation_method === 'failed';
        return `
          <div class="image-card ${isFailed ? 'image-failed' : ''}">
            <div class="image-card-header">
              <strong>${esc(img.chapter_id)}</strong>
              <span class="image-method">${esc(img.generation_method || 'generated')}</span>
            </div>
            ${isFailed
              ? '<div class="image-placeholder">Generation failed</div>'
              : `<img src="${imgUrl}" alt="${esc(img.chapter_id)}" loading="lazy" style="max-width:100%;border-radius:4px;cursor:pointer" onclick="window.open('${imgUrl}','_blank')">`
            }
            <div class="image-prompt" style="font-size:0.8em;color:#888;margin-top:4px;max-height:60px;overflow:auto">
              ${esc((img.prompt || '').substring(0, 200))}
            </div>
          </div>`;
      }).join('');

      viewer.innerHTML = `
        <div class="images-panel">
          <div class="images-summary">
            <strong>Images:</strong>
            ${generated.length}/${images.length} generated
            <button class="btn btn-sm" style="margin-left:1em" onclick="actions.imagegen()">Regenerate</button>
          </div>
          <div class="images-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:1em;margin-top:1em">
            ${cards}
          </div>
        </div>`;
    } catch (err) {
      viewer.innerHTML = `
        <div class="images-panel">
          <p>No images generated yet.</p>
          <button class="btn btn-sm btn-primary" onclick="actions.imagegen()">Generate Images</button>
        </div>`;
    }
  }

  // ── Stock Images Panel ─────────────────────────────────────
  async function loadStockImagesPanel() {
    if (!selected) return;
    const viewer = document.getElementById("viewer");

    try {
      const data = await GET(`/episodes/${selected.episode_id}/stock/candidates`);
      if (data.error) {
        viewer.innerHTML = `
          <div class="stock-panel">
            <p>No stock image candidates yet.</p>
            <button class="btn btn-sm btn-primary" onclick="actions.imagegen()">Search Stock Images</button>
          </div>`;
        return;
      }

      const chapters = data.chapters || {};
      const chapterIds = Object.keys(chapters).sort();
      const reviewId = data.review_task_id;
      const reviewStatus = data.review_status;
      const rankedAt = data.ranked_at;

      let html = '<div class="stock-panel">';

      // Summary bar
      const totalCh = chapterIds.length;
      const pinnedCh = chapterIds.filter(id => {
        const cands = (chapters[id].candidates || []);
        return cands.some(c => c.selected);
      }).length;

      html += `<div class="stock-summary">
        <strong>Stock Images:</strong> ${pinnedCh}/${totalCh} chapters pinned`;
      if (rankedAt) {
        html += ` &middot; Ranked: ${new Date(rankedAt).toLocaleString()}`;
      }
      if (reviewStatus) {
        html += ` &middot; Review: <span class="badge badge-review-${reviewStatus.replace('_','-')}">${reviewStatus}</span>`;
      }
      html += `</div>`;

      // Action bar
      html += '<div class="stock-actions">';
      if (reviewId && (reviewStatus === 'pending' || reviewStatus === 'in_review')) {
        html += `<button class="btn btn-sm btn-primary" onclick="approveStockReview(${reviewId})">Approve All Selections</button> `;
      }
      html += `<button class="btn btn-sm" onclick="rerankStock('${esc(selected.episode_id)}')">Re-rank with LLM</button>`;
      html += '</div>';

      // Chapter sections
      chapterIds.forEach(chId => {
        const ch = chapters[chId];
        const candidates = ch.candidates || [];
        const query = ch.search_query || '';
        const pinnedBy = ch.pinned_by || '';

        const chIntents = ch.intents || [];

        html += `<div class="stock-chapter">
          <div class="stock-chapter-header">
            <strong>${esc(chId)}</strong>
            <span class="stock-query">${esc(query)}</span>
            ${pinnedBy ? `<span class="badge">${esc(pinnedBy)}</span>` : ''}
          </div>`;

        // Intent tags
        if (chIntents.length > 0) {
          html += '<div class="stock-intents">';
          chIntents.forEach(intent => {
            html += `<span class="stock-intent-tag">${esc(intent)}</span>`;
          });
          html += '</div>';
        }

        html += `<div class="stock-grid">`;

        candidates.sort((a, b) => (a.rank || 999) - (b.rank || 999));

        candidates.forEach(c => {
          const isPinned = c.selected && c.locked;
          const isSelected = c.selected;
          const rankLabel = c.rank ? `#${c.rank}` : '';
          const reason = c.rank_reason || '';
          const trapWarning = c.trap_flag ? ' <span class="stock-trap-warning" title="Possible literal-trap mismatch — check carefully">⚠</span>' : '';
          const isVideo = c.asset_type === 'video';

          if (isVideo) {
            // Phase 4: Video candidate — show preview thumbnail with play badge
            const previewFilename = (c.preview_path || '').split('/').pop();
            const previewUrl = previewFilename
              ? `api/episodes/${selected.episode_id}/stock/candidate-image?chapter=${esc(chId)}&filename=${encodeURIComponent(previewFilename)}`
              : '';
            const videoFilename = (c.local_path || '').split('/').pop();
            const videoSrc = videoFilename
              ? `api/episodes/${selected.episode_id}/stock/candidate-video?chapter=${esc(chId)}&filename=${encodeURIComponent(videoFilename)}`
              : '';
            const thumbId = `vid-thumb-${chId}-${c.pexels_id}`;
            const previewId = `vid-preview-${chId}-${c.pexels_id}`;

            html += `<div class="stock-thumb ${isPinned ? 'pinned' : ''} ${isSelected ? 'selected' : ''}">
              ${rankLabel ? `<span class="stock-rank-badge">${rankLabel}${trapWarning}</span>` : ''}
              <div class="stock-thumb-media" id="${thumbId}" onclick="toggleVideoPreview('${previewId}','${esc(videoSrc)}')">
                ${previewUrl ? `<img src="${previewUrl}" alt="Video preview" loading="lazy" title="${esc(reason)}">` : '<div style="height:120px;background:#222;display:flex;align-items:center;justify-content:center;color:#aaa">No preview</div>'}
                <span class="stock-video-badge">&#9654; ${c.duration_seconds || '?'}s</span>
              </div>
              <div class="stock-video-preview" id="${previewId}" style="display:none"></div>
              <div class="stock-thumb-info">
                <span class="stock-photographer">${esc(c.photographer || '')}</span>
                ${isPinned ? '<span class="stock-locked">PINNED</span>' : ''}
              </div>
              <button class="btn btn-sm stock-pin-btn" onclick="pinStockImage('${esc(selected.episode_id)}','${esc(chId)}',${c.pexels_id})">
                ${isSelected ? 'Pinned' : 'Pin'}
              </button>
            </div>`;
          } else {
            // Photo candidate: existing rendering
            const imgUrl = `api/episodes/${selected.episode_id}/stock/candidate-image?chapter=${esc(chId)}&filename=pexels_${c.pexels_id}.jpg`;
            html += `<div class="stock-thumb ${isPinned ? 'pinned' : ''} ${isSelected ? 'selected' : ''}">
              ${rankLabel ? `<span class="stock-rank-badge">${rankLabel}${trapWarning}</span>` : ''}
              <div class="stock-thumb-media">
                <img src="${imgUrl}" alt="${esc(c.alt_text || '')}" loading="lazy"
                     onclick="window.open('${imgUrl}','_blank')" title="${esc(reason)}">
              </div>
              <div class="stock-thumb-info">
                <span class="stock-photographer">${esc(c.photographer || '')}</span>
                ${isPinned ? '<span class="stock-locked">PINNED</span>' : ''}
              </div>
              <button class="btn btn-sm stock-pin-btn" onclick="pinStockImage('${esc(selected.episode_id)}','${esc(chId)}',${c.pexels_id})">
                ${isSelected ? 'Pinned' : 'Pin'}
              </button>
            </div>`;
          }
        });

        html += '</div></div>';
      });

      html += '</div>';
      viewer.innerHTML = html;
    } catch (err) {
      viewer.innerHTML = `
        <div class="stock-panel">
          <p>No stock image candidates yet.</p>
          <button class="btn btn-sm btn-primary" onclick="actions.imagegen()">Search Stock Images</button>
        </div>`;
    }
  }

  // Phase 4: Toggle inline video preview for stock video candidates
  function toggleVideoPreview(previewId, videoSrc) {
    const container = document.getElementById(previewId);
    if (!container) return;
    if (container.style.display === 'none') {
      container.style.display = 'block';
      if (!container.querySelector('video')) {
        container.innerHTML = `<video controls preload="metadata" style="width:100%;max-height:200px">
          <source src="${videoSrc}" type="video/mp4">
        </video>`;
      }
    } else {
      container.style.display = 'none';
      // Pause video when hiding
      const vid = container.querySelector('video');
      if (vid) vid.pause();
    }
  }
  window.toggleVideoPreview = toggleVideoPreview;

  async function pinStockImage(episodeId, chapterId, pexelsId) {
    const result = await POST(`/episodes/${episodeId}/stock/pin`, {
      chapter_id: chapterId,
      pexels_id: pexelsId,
      lock: true,
    });
    if (result.error) {
      toast(result.error, false);
    } else {
      toast(`Pinned pexels:${pexelsId} for ${chapterId}`);
      await loadStockImagesPanel();
    }
  }
  window.pinStockImage = pinStockImage;

  async function rerankStock(episodeId) {
    toast("Re-ranking with LLM...");
    const result = await POST(`/episodes/${episodeId}/stock/rank`, {});
    if (result.error) {
      toast(result.error, false);
    } else {
      toast(`Ranked ${result.chapters_ranked} chapters ($${(result.cost_usd || 0).toFixed(4)})`);
      await loadStockImagesPanel();
    }
  }
  window.rerankStock = rerankStock;

  async function approveStockReview(reviewId) {
    if (!confirm("Approve stock image selections for all chapters?")) return;
    const result = await POST(`/reviews/${reviewId}/approve`, { notes: "Stock images approved via dashboard" });
    if (result.error) {
      toast(result.error, false);
    } else {
      toast("Stock images approved!");
      await loadStockImagesPanel();
    }
  }
  window.approveStockReview = approveStockReview;

  // ── TTS Audio Panel ─────────────────────────────────────────
  async function loadTTSPanel() {
    if (!selected) return;
    const viewer = document.getElementById("viewer");

    try {
      const data = await GET(`/episodes/${selected.episode_id}/tts`);
      if (data.error) {
        viewer.innerHTML = `
          <div class="tts-panel">
            <p>No TTS audio generated yet.</p>
            <button class="btn btn-sm btn-primary" onclick="actions.tts()">Generate TTS Audio</button>
          </div>`;
        return;
      }

      const totalDur = (data.total_duration_seconds || 0).toFixed(1);
      const totalChars = data.total_characters || 0;
      const totalCost = (data.total_cost_usd || 0).toFixed(3);
      const segments = data.segments || [];

      let rows = segments.map(s => {
        const dur = (s.duration_seconds || 0).toFixed(1);
        const audioUrl = `api/episodes/${selected.episode_id}/tts/${s.chapter_id}.mp3`;
        return `
          <div class="tts-chapter-row">
            <div class="tts-chapter-header">
              <strong>${esc(s.chapter_id)}</strong> — ${esc(s.chapter_title || '')}
              <span class="tts-duration">${dur}s</span>
              <span class="tts-chars">${s.text_length || 0} chars</span>
            </div>
            <audio class="tts-player" controls preload="none">
              <source src="${audioUrl}" type="audio/mpeg">
            </audio>
          </div>`;
      }).join('');

      viewer.innerHTML = `
        <div class="tts-panel">
          <div class="tts-summary">
            <strong>TTS Summary:</strong>
            ${segments.length} segments &middot; ${totalDur}s total &middot;
            ${totalChars} chars &middot; $${totalCost}
            <button class="btn btn-sm" style="margin-left:1em" onclick="actions.tts()">Regenerate</button>
          </div>
          ${rows}
        </div>`;
    } catch (err) {
      viewer.innerHTML = `
        <div class="tts-panel">
          <p>No TTS audio generated yet.</p>
          <button class="btn btn-sm btn-primary" onclick="actions.tts()">Generate TTS Audio</button>
        </div>`;
    }
  }

  // ── Video Panel ──────────────────────────────────────────────
  async function loadVideoPanel() {
    if (!selected) return;
    const viewer = document.getElementById("viewer");

    try {
      const data = await GET(`/episodes/${selected.episode_id}/render`);
      if (data.error) {
        viewer.innerHTML = `
          <div class="video-panel">
            <p>No video rendered yet.</p>
            <button class="btn btn-sm btn-primary" onclick="actions.render()">Render Video</button>
          </div>`;
        return;
      }

      const totalDur = (data.total_duration_seconds || 0).toFixed(1);
      const totalSize = ((data.total_size_bytes || 0) / 1024 / 1024).toFixed(1);
      const segments = data.segments || [];
      const videoUrl = `api/episodes/${selected.episode_id}/render/draft.mp4`;

      let chapterRows = segments.map(s => {
        const dur = (s.duration_seconds || 0).toFixed(1);
        return `
          <div class="video-chapter-row">
            <span class="video-chapter-id">${esc(s.chapter_id)}</span>
            <span class="video-duration">${dur}s</span>
            <span class="video-transition">${esc(s.transition_in)} → ${esc(s.transition_out)}</span>
          </div>`;
      }).join('');

      viewer.innerHTML = `
        <div class="video-panel">
          <div class="video-summary">
            <strong>Render Summary:</strong>
            ${segments.length} segments &middot; ${totalDur}s &middot; ${totalSize} MB
            <button class="btn btn-sm" style="margin-left:1em" onclick="actions.render()">Re-render</button>
          </div>
          <div class="video-player">
            <video controls preload="metadata" style="width:100%;max-width:800px;background:#000">
              <source src="${videoUrl}" type="video/mp4">
              Your browser does not support video playback.
            </video>
          </div>
          <div class="video-chapters">
            <strong>Chapter Timeline:</strong>
            ${chapterRows}
          </div>
        </div>`;

      // Publish panel below video
      const publishStatus = await GET(`/episodes/${selected.episode_id}/publish-status`).catch(() => null);
      if (publishStatus) {
        const ytId = publishStatus.youtube_video_id;
        const ytUrl = publishStatus.youtube_url;
        const publishHtml = ytId && ytUrl
          ? `<div class="publish-panel">
              <strong>YouTube:</strong>
              <a href="${esc(ytUrl)}" target="_blank" class="yt-link">${esc(ytUrl)}</a>
              <span class="badge badge-published">Published</span>
             </div>`
          : `<div class="publish-panel">
              <strong>Publish to YouTube:</strong>
              <button class="btn btn-sm btn-success" onclick="actions.publish()">Upload Now</button>
              <small style="color:#888"> (episode must be APPROVED)</small>
             </div>`;
        viewer.insertAdjacentHTML('beforeend', publishHtml);
      }
    } catch (err) {
      viewer.innerHTML = `
        <div class="video-panel">
          <p>No video rendered yet.</p>
          <button class="btn btn-sm btn-primary" onclick="actions.render()">Render Video</button>
        </div>`;
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
    tts() {
      if (!selected) return;
      submitJob("TTS", `/episodes/${selected.episode_id}/tts`, { force: isForce() });
    },
    render() {
      if (!selected) return;
      submitJob("Render", `/episodes/${selected.episode_id}/render`, { force: isForce() });
    },
    publish() {
      if (!selected) return;
      submitJob("Publish", `/episodes/${selected.episode_id}/publish`, { force: isForce() });
    },
    async resetV2() {
      if (!selected) return;
      if (!confirm(`Episode "${selected.title}" auf v2 zurücksetzen?\n\nStatus wird auf TRANSCRIBED gesetzt, v2-Pipeline startet ab correct.`)) return;
      try {
        const r = await POST(`/episodes/${selected.episode_id}/reset-v2`);
        if (r.error) { showError(r.error); return; }
        showSuccess(`Reset: ${r.old_status} → TRANSCRIBED (v2)`);
        refresh();
      } catch (e) { showError("Reset failed: " + e.message); }
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

  // ── Stage Detail (Jenkins-style) ─────────────────────────────

  const _REVIEW_GATES = new Set([
    "review_gate_1", "review_gate_2", "review_gate_translate",
    "review_gate_stock", "review_gate_3",
  ]);

  window.showStageDetail = async function (stageName) {
    if (!selected) return;
    const viewer = document.getElementById("viewer");
    viewer.innerHTML = "Loading stage details...";

    // Deselect tabs
    document.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));

    try {
      const data = await GET(`/episodes/${selected.episode_id}/stage-runs`);
      if (data.error) {
        viewer.innerHTML = `<p style="color:var(--red)">Error: ${esc(data.error)}</p>`;
        return;
      }
      viewer.innerHTML = renderStageDetailHTML(stageName, data);
    } catch (e) {
      viewer.innerHTML = `<p style="color:var(--red)">Failed to load stage details: ${esc(e.message)}</p>`;
    }
  };

  window.restartStage = function (stageName) {
    if (!selected) return;
    submitJob(
      stageName,
      `/episodes/${selected.episode_id}/stage/${stageName}`,
      { force: true }
    );
  };

  function renderStageDetailHTML(stageName, data) {
    const stageData = data.stages[stageName];
    const isGate = _REVIEW_GATES.has(stageName);
    const stageInfo = selected.stage_progress?.stages?.find(s => s.name === stageName);
    const label = stageInfo?.label || stageName;
    const stageState = stageInfo?.state || "pending";

    let html = `<div class="stage-detail-panel">`;
    html += `<h3 class="stage-detail-title">${esc(label)}</h3>`;

    if (!stageData) {
      // No PipelineRun records — infer status from stage_progress
      if (stageState === "done") {
        const dur = stageInfo.duration_seconds != null ? formatDuration(stageInfo.duration_seconds) : null;
        const cost = stageInfo.cost_usd != null && stageInfo.cost_usd > 0 ? `$${stageInfo.cost_usd.toFixed(4)}` : null;
        html += `<div class="stage-detail-status">
          <span class="stage-badge badge-success">COMPLETED</span>
        </div>`;
        if (dur || cost) {
          html += `<div class="stage-detail-section"><h4>Summary</h4>
            <table class="stage-detail-table">
              ${dur ? `<tr><td>Duration</td><td>${dur}</td></tr>` : ""}
              ${cost ? `<tr><td>Cost</td><td>${cost}</td></tr>` : ""}
            </table></div>`;
        }
      } else if (stageState === "failed") {
        html += `<div class="stage-detail-status">
          <span class="stage-badge badge-failed">FAILED</span>
        </div>`;
        if (data.error_message) {
          html += `<div class="stage-detail-section stage-detail-error">
            <h4>Error</h4><pre>${esc(data.error_message)}</pre></div>`;
        }
      } else if (stageState === "paused") {
        html += `<div class="stage-detail-status">
          <span class="stage-badge badge-running">PAUSED FOR REVIEW</span>
        </div>`;
      } else {
        html += `<div class="stage-detail-status">
          <span class="stage-badge" style="background:var(--border);color:var(--text-dim)">PENDING</span>
        </div>`;
      }
      if (!isGate && stageState !== "pending") {
        html += `<div class="stage-detail-actions">
          <button class="btn btn-sm btn-primary" onclick="restartStage('${esc(stageName)}')">
          Restart ${esc(label)}</button></div>`;
      } else if (!isGate && stageState === "pending") {
        html += `<div class="stage-detail-actions">
          <button class="btn btn-sm btn-primary" onclick="restartStage('${esc(stageName)}')">
          Run ${esc(label)}</button></div>`;
      }

      // Show relevant log lines even without PipelineRun records
      if (data.log_lines && data.log_lines.length > 0) {
        const relevant = data.log_lines.filter(line => line.toLowerCase().includes(stageName)).slice(-30);
        if (relevant.length > 0) {
          html += `<div class="stage-detail-section">
            <h4>Log (last ${relevant.length} lines)</h4>
            <pre class="stage-log-pre">${esc(relevant.join("\n"))}</pre>
          </div>`;
        }
      }

      html += `</div>`;
      return html;
    }

    const latest = stageData.latest;

    // Status badge
    const statusClass = latest.status === "success" ? "badge-success"
                      : latest.status === "failed" ? "badge-failed"
                      : "badge-running";
    html += `<div class="stage-detail-status">
      <span class="stage-badge ${statusClass}">${esc(latest.status.toUpperCase())}</span>
      <span class="stage-detail-meta">Run #${stageData.run_count}</span>
    </div>`;

    // Timing
    html += `<div class="stage-detail-section">
      <h4>Timing</h4>
      <table class="stage-detail-table">
        <tr><td>Started</td><td>${latest.started_at ? new Date(latest.started_at).toLocaleString() : "\u2014"}</td></tr>
        <tr><td>Completed</td><td>${latest.completed_at ? new Date(latest.completed_at).toLocaleString() : "\u2014"}</td></tr>
        <tr><td>Duration</td><td>${latest.duration_seconds != null ? formatDuration(latest.duration_seconds) : "\u2014"}</td></tr>
      </table>
    </div>`;

    // Cost & Tokens
    if (latest.input_tokens > 0 || latest.output_tokens > 0 || latest.estimated_cost_usd > 0) {
      html += `<div class="stage-detail-section">
        <h4>Cost &amp; Tokens</h4>
        <table class="stage-detail-table">
          <tr><td>Input tokens</td><td>${latest.input_tokens.toLocaleString()}</td></tr>
          <tr><td>Output tokens</td><td>${latest.output_tokens.toLocaleString()}</td></tr>
          <tr><td>Cost</td><td>$${latest.estimated_cost_usd.toFixed(4)}</td></tr>
        </table>
      </div>`;
    }

    // Error
    if (latest.error_message) {
      html += `<div class="stage-detail-section stage-detail-error">
        <h4>Error</h4>
        <pre>${esc(latest.error_message)}</pre>
      </div>`;
    }

    // Actions
    if (!isGate) {
      html += `<div class="stage-detail-actions">
        <button class="btn btn-sm btn-primary" onclick="restartStage('${esc(stageName)}')">
          Restart ${esc(label)}</button>
      </div>`;
    }

    // Run history
    if (stageData.history.length > 0) {
      html += `<div class="stage-detail-section">
        <h4>Run History</h4>
        <table class="stage-history-table">
          <thead><tr><th>Run</th><th>Status</th><th>Started</th><th>Duration</th><th>Cost</th><th>Error</th></tr></thead>
          <tbody>`;

      // Latest first
      const allRuns = [stageData.latest, ...stageData.history];
      allRuns.forEach((run, i) => {
        const rowClass = run.status === "success" ? "row-success"
                       : run.status === "failed" ? "row-failed"
                       : "";
        html += `<tr class="${rowClass}">
          <td>#${allRuns.length - i}</td>
          <td>${esc(run.status)}</td>
          <td>${run.started_at ? new Date(run.started_at).toLocaleString() : "\u2014"}</td>
          <td>${run.duration_seconds != null ? formatDuration(run.duration_seconds) : "\u2014"}</td>
          <td>${run.estimated_cost_usd > 0 ? "$" + run.estimated_cost_usd.toFixed(4) : "\u2014"}</td>
          <td>${run.error_message ? esc(trunc(run.error_message, 80)) : "\u2014"}</td>
        </tr>`;
      });
      html += `</tbody></table></div>`;
    }

    // Relevant log lines
    if (data.log_lines && data.log_lines.length > 0) {
      const stageKey = stageName.replace("review_gate_", "review_gate_");
      const relevant = data.log_lines.filter(line => {
        const lower = line.toLowerCase();
        return lower.includes(stageName) || lower.includes(stageKey);
      }).slice(-30);

      if (relevant.length > 0) {
        html += `<div class="stage-detail-section">
          <h4>Log (last ${relevant.length} lines)</h4>
          <pre class="stage-log-pre">${esc(relevant.join("\n"))}</pre>
        </div>`;
      }
    }

    html += `</div>`;
    return html;
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
    const stages = data.stages || [];
    let rows = "";
    stages.forEach((s) => {
      rows += `<tr><td>${s.stage}</td><td>${s.runs}</td><td>${s.input_tokens}</td><td>${s.output_tokens}</td><td>$${s.cost_usd.toFixed(4)}</td></tr>`;
    });
    document.getElementById("cost-body").innerHTML = rows;
    document.getElementById("cost-total").textContent =
      `Total: $${(data.total_usd || 0).toFixed(4)} | ${data.episodes_processed || 0} episodes | avg $${(data.avg_per_episode || 0).toFixed(4)}/ep`;
    modal.classList.add("open");
    // Render canvas chart after modal is visible
    setTimeout(() => renderCostChart(stages), 50);
  };

  function renderCostChart(stages) {
    const canvas = document.getElementById("cost-chart");
    if (!canvas || stages.length === 0) return;
    const container = canvas.parentElement;
    const W = container.offsetWidth || 400;
    const rowH = 28;
    const labelW = 120;
    const barAreaW = W - labelW - 60; // 60 = value label space
    const H = stages.length * rowH + 16;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = W * dpr;
    canvas.height = H * dpr;
    canvas.style.width = W + "px";
    canvas.style.height = H + "px";
    const ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);

    const maxCost = Math.max(...stages.map(s => s.cost_usd), 0.0001);
    ctx.clearRect(0, 0, W, H);

    stages.forEach((s, i) => {
      const y = i * rowH + 8;
      const barW = Math.max(2, (s.cost_usd / maxCost) * barAreaW);

      // Label
      ctx.fillStyle = "#8b949e";
      ctx.font = "11px -apple-system, BlinkMacSystemFont, sans-serif";
      ctx.textAlign = "right";
      ctx.fillText(s.stage.length > 14 ? s.stage.slice(0, 13) + "…" : s.stage, labelW - 6, y + 14);

      // Bar
      const gradient = ctx.createLinearGradient(labelW, 0, labelW + barW, 0);
      gradient.addColorStop(0, "#58a6ff");
      gradient.addColorStop(1, "#388bfd");
      ctx.fillStyle = gradient;
      ctx.beginPath();
      ctx.roundRect(labelW, y + 2, barW, rowH - 8, 3);
      ctx.fill();

      // Value
      ctx.fillStyle = "#e6edf3";
      ctx.textAlign = "left";
      ctx.fillText("$" + s.cost_usd.toFixed(4), labelW + barW + 6, y + 14);
    });
  }

  window.closeCost = function () {
    document.getElementById("cost-modal").classList.remove("open");
  };

  // ── Analytics ─────────────────────────────────────────────────
  window.showAnalytics = async function () {
    const modal = document.getElementById("analytics-modal");
    modal.classList.add("open");
    showAnalyticsTab("throughput");
  };

  window.closeAnalytics = function () {
    document.getElementById("analytics-modal").classList.remove("open");
  };

  window.showAnalyticsTab = function (tab) {
    document.querySelectorAll(".analytics-tab").forEach(t => t.classList.remove("active"));
    document.querySelectorAll(".analytics-panel").forEach(p => p.style.display = "none");
    if (event && event.target) event.target.classList.add("active");
    const panelId = { throughput: "analytics-throughput", errors: "analytics-errors", providers: "analytics-providers" }[tab];
    document.getElementById(panelId).style.display = "block";
    if (tab === "throughput") loadThroughputChart();
    if (tab === "errors") loadErrorChart();
    if (tab === "providers") loadProviderChart();
  };

  async function loadThroughputChart() {
    const data = await GET("/analytics/throughput");
    const days = data.days || [];
    const canvas = document.getElementById("throughput-chart");
    if (!canvas || days.length === 0) {
      document.getElementById("throughput-summary").textContent = "No data yet.";
      return;
    }
    const container = canvas.parentElement;
    const W = container.offsetWidth || 600;
    const H = 220;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = W * dpr;
    canvas.height = H * dpr;
    canvas.style.width = W + "px";
    canvas.style.height = H + "px";
    const ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, W, H);

    const padL = 40, padR = 20, padT = 20, padB = 40;
    const chartW = W - padL - padR;
    const chartH = H - padT - padB;
    const maxEp = Math.max(...days.map(d => d.episodes), 1);
    const barW = Math.max(4, Math.min(30, chartW / days.length - 2));

    ctx.strokeStyle = "#30363d";
    ctx.beginPath();
    ctx.moveTo(padL, padT);
    ctx.lineTo(padL, padT + chartH);
    ctx.lineTo(padL + chartW, padT + chartH);
    ctx.stroke();

    ctx.fillStyle = "#8b949e";
    ctx.font = "10px sans-serif";
    ctx.textAlign = "right";
    for (let i = 0; i <= 4; i++) {
      const val = Math.round(maxEp * i / 4);
      const y = padT + chartH - (chartH * i / 4);
      ctx.fillText(String(val), padL - 4, y + 3);
    }

    days.forEach((d, i) => {
      const x = padL + (i / days.length) * chartW + 1;
      const barH = (d.episodes / maxEp) * chartH;
      const y = padT + chartH - barH;
      const gradient = ctx.createLinearGradient(x, y, x, y + barH);
      gradient.addColorStop(0, "#3fb950");
      gradient.addColorStop(1, "#238636");
      ctx.fillStyle = gradient;
      ctx.beginPath();
      ctx.roundRect(x, y, barW, barH, 2);
      ctx.fill();

      if (days.length <= 14 || i % Math.ceil(days.length / 10) === 0) {
        ctx.fillStyle = "#8b949e";
        ctx.font = "9px sans-serif";
        ctx.textAlign = "center";
        const label = d.date ? d.date.slice(5) : "";
        ctx.fillText(label, x + barW / 2, padT + chartH + 14);
      }
    });

    const totalEp = days.reduce((s, d) => s + d.episodes, 0);
    const totalCost = days.reduce((s, d) => s + d.cost_usd, 0);
    document.getElementById("throughput-summary").textContent =
      `${totalEp} episodes over ${days.length} days | Total cost: $${totalCost.toFixed(2)}`;
  }

  async function loadErrorChart() {
    const data = await GET("/analytics/error-rate");
    const stages = data.stages || [];
    const canvas = document.getElementById("error-chart");
    if (!canvas || stages.length === 0) return;
    const container = canvas.parentElement;
    const W = container.offsetWidth || 600;
    const rowH = 28;
    const H = stages.length * rowH + 16;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = W * dpr;
    canvas.height = H * dpr;
    canvas.style.width = W + "px";
    canvas.style.height = H + "px";
    const ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, W, H);

    const labelW = 120;
    const barAreaW = W - labelW - 80;

    stages.forEach((s, i) => {
      const y = i * rowH + 8;
      const total = s.success + s.failed;
      if (total === 0) return;
      const successW = (s.success / total) * barAreaW;
      const failW = (s.failed / total) * barAreaW;

      ctx.fillStyle = "#8b949e";
      ctx.font = "11px sans-serif";
      ctx.textAlign = "right";
      ctx.fillText(s.stage.length > 14 ? s.stage.slice(0, 13) + "\u2026" : s.stage, labelW - 6, y + 14);

      ctx.fillStyle = "#238636";
      ctx.beginPath();
      ctx.roundRect(labelW, y + 2, successW, rowH - 8, [3, 0, 0, 3]);
      ctx.fill();

      if (failW > 0) {
        ctx.fillStyle = "#f85149";
        ctx.beginPath();
        ctx.roundRect(labelW + successW, y + 2, failW, rowH - 8, [0, 3, 3, 0]);
        ctx.fill();
      }

      ctx.fillStyle = "#e6edf3";
      ctx.textAlign = "left";
      const pct = total > 0 ? ((s.failed / total) * 100).toFixed(1) : "0";
      ctx.fillText(`${pct}% err (${s.failed}/${total})`, labelW + successW + failW + 6, y + 14);
    });
  }

  async function loadProviderChart() {
    const data = await GET("/analytics/provider-cost");
    const providers = data.providers || [];
    const canvas = document.getElementById("provider-chart");
    if (!canvas || providers.length === 0) {
      document.getElementById("provider-summary").textContent = "No data yet.";
      return;
    }
    const container = canvas.parentElement;
    const W = container.offsetWidth || 600;
    const rowH = 32;
    const H = providers.length * rowH + 16;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = W * dpr;
    canvas.height = H * dpr;
    canvas.style.width = W + "px";
    canvas.style.height = H + "px";
    const ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, W, H);

    const labelW = 140;
    const barAreaW = W - labelW - 80;
    const maxCost = Math.max(...providers.map(p => p.cost_usd), 0.001);
    const colors = ["#58a6ff", "#3fb950", "#f0883e", "#f85149", "#bc8cff", "#79c0ff"];

    providers.forEach((p, i) => {
      const y = i * rowH + 8;
      const barW = Math.max(2, (p.cost_usd / maxCost) * barAreaW);

      ctx.fillStyle = "#8b949e";
      ctx.font = "11px sans-serif";
      ctx.textAlign = "right";
      ctx.fillText(p.provider, labelW - 6, y + 16);

      ctx.fillStyle = colors[i % colors.length];
      ctx.beginPath();
      ctx.roundRect(labelW, y + 4, barW, rowH - 10, 3);
      ctx.fill();

      ctx.fillStyle = "#e6edf3";
      ctx.textAlign = "left";
      ctx.fillText(`$${p.cost_usd.toFixed(4)} (${p.runs} runs)`, labelW + barW + 6, y + 16);
    });

    const totalCost = providers.reduce((s, p) => s + p.cost_usd, 0);
    document.getElementById("provider-summary").textContent =
      `Total: $${totalCost.toFixed(4)} across ${providers.length} providers`;
  }

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
      // Build query with optional channel and profile filters
      let url = "/episodes";
      const params = [];
      if (selectedChannelId) {
        params.push(`channel_id=${encodeURIComponent(selectedChannelId)}`);
      }
      if (selectedProfile) {
        params.push(`profile=${encodeURIComponent(selectedProfile)}`);
      }
      if (params.length > 0) {
        url += "?" + params.join("&");
      }

      const data = await GET(url);
      if (data.error) {
        showError("API error: " + data.error);
        return;
      }
      episodes = Array.isArray(data) ? data : [];
      applyFilters();
      loadWhatsNew();
      updateReviewBadge();
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

  function showSuccess(msg) {
    toast(msg, true);
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

  // ── Batch Processing ─────────────────────────────────────────────
  let activeBatchId = null;
  let batchPollTimer = null;

  async function toggleBatch() {
    const btn = document.getElementById("batch-btn");

    if (activeBatchId) {
      // Stop the batch
      const r = await POST(`/batch/${activeBatchId}/stop`);
      if (r.error) {
        toast("Failed to stop: " + r.error, false);
      } else {
        toast("Stopping batch after current episode...");
      }
    } else {
      // Start a new batch
      const body = {};
      if (selectedChannelId) {
        body.channel_id = selectedChannelId;
      }
      if (selectedProfile) {
        body.profile = selectedProfile;
      }
      const r = await POST("/batch/start", body);
      if (r.error) {
        toast(r.error, false);
        return;
      }
      activeBatchId = r.batch_id;
      btn.textContent = "Stop";
      btn.classList.remove("btn-primary");
      btn.classList.add("btn-danger");
      showBatchProgress();
      pollBatch();
    }
  }
  window.toggleBatch = toggleBatch;

  function showBatchProgress() {
    document.getElementById("batch-progress").style.display = "block";
  }

  function hideBatchProgress() {
    document.getElementById("batch-progress").style.display = "none";
  }

  function updateBatchUI(batch) {
    document.getElementById("batch-status").textContent = batch.state;
    document.getElementById("batch-completed").textContent = batch.completed_episodes;
    document.getElementById("batch-failed").textContent = batch.failed_episodes;
    document.getElementById("batch-remaining").textContent = batch.remaining_episodes;
    document.getElementById("batch-cost").textContent = batch.total_cost_usd.toFixed(4);

    // Update progress bar
    const progressPct = batch.progress_pct || 0;
    document.getElementById("batch-progress-fill").style.width = progressPct + "%";
    document.getElementById("batch-progress-text").textContent = progressPct + "%";

    if (batch.current_episode_id) {
      const episode = episodes.find(e => e.episode_id === batch.current_episode_id);
      const title = batch.current_episode_title || (episode ? trunc(episode.title, 50) : batch.current_episode_id);
      document.getElementById("batch-episode").textContent = `Current: ${title}`;
    } else {
      document.getElementById("batch-episode").textContent = "";
    }

    if (batch.current_stage) {
      document.getElementById("batch-stage").textContent = `Stage: ${batch.current_stage}`;
    } else {
      document.getElementById("batch-stage").textContent = "";
    }
  }

  function pollBatch() {
    clearInterval(batchPollTimer);
    batchPollTimer = setInterval(async () => {
      if (!activeBatchId) {
        clearInterval(batchPollTimer);
        return;
      }

      try {
        const batch = await GET(`/batch/${activeBatchId}`);
        if (batch.error) {
          clearInterval(batchPollTimer);
          resetBatchUI();
          toast("Batch error: " + batch.error, false);
          return;
        }

        updateBatchUI(batch);

        if (batch.state === "success") {
          clearInterval(batchPollTimer);
          const msg = `Batch complete: ${batch.completed_episodes} succeeded, ${batch.failed_episodes} failed ($${batch.total_cost_usd.toFixed(4)})`;
          toast(msg);
          resetBatchUI();
          refresh();
        } else if (batch.state === "stopped") {
          clearInterval(batchPollTimer);
          toast(`Batch stopped: ${batch.message}`);
          resetBatchUI();
          refresh();
        } else if (batch.state === "error") {
          clearInterval(batchPollTimer);
          toast("Batch failed: " + batch.message, false);
          resetBatchUI();
          refresh();
        }
      } catch (err) {
        clearInterval(batchPollTimer);
        resetBatchUI();
        toast("Batch polling error: " + err.message, false);
      }
    }, 2000);
  }

  function resetBatchUI() {
    activeBatchId = null;
    hideBatchProgress();
    const btn = document.getElementById("batch-btn");
    btn.textContent = "Process All";
    btn.classList.remove("btn-danger");
    btn.classList.add("btn-primary");
  }

  // Check for active batch on load
  async function checkActiveBatch() {
    const r = await GET("/batch/active");
    if (r.active) {
      activeBatchId = r.batch_id;
      const btn = document.getElementById("batch-btn");
      btn.textContent = "Stop";
      btn.classList.remove("btn-primary");
      btn.classList.add("btn-danger");
      showBatchProgress();
      updateBatchUI(r);
      pollBatch();
    }
  }

  // ── Channel Management ───────────────────────────────────────────
  async function loadChannels() {
    try {
      const r = await GET("/channels");
      if (r.error) {
        console.error("Failed to load channels:", r.error);
        return;
      }
      channels = r.channels || [];
      updateChannelSelector();
    } catch (err) {
      console.error("Failed to load channels:", err);
    }
  }

  function updateChannelSelector() {
    const select = document.getElementById("channel-select");
    const currentValue = select.value;

    // Clear and rebuild options
    select.innerHTML = '<option value="">All Channels</option>';

    channels.forEach(ch => {
      if (ch.is_active) {
        const opt = document.createElement("option");
        opt.value = ch.channel_id;
        opt.textContent = ch.name;
        select.appendChild(opt);
      }
    });

    // Restore selection if still valid
    if (currentValue && Array.from(select.options).some(o => o.value === currentValue)) {
      select.value = currentValue;
    }

    // Update selectedChannelId
    selectedChannelId = select.value || null;
  }

  async function onChannelChange() {
    const select = document.getElementById("channel-select");
    selectedChannelId = select.value || null;

    // Auto-select the channel's content_profile in the profile dropdown
    const profileSelect = document.getElementById("profile-select");
    if (profileSelect) {
      if (selectedChannelId) {
        const ch = channels.find(c => c.channel_id === selectedChannelId);
        const prof = ch && ch.content_profile ? ch.content_profile : "";
        if (prof && Array.from(profileSelect.options).some(o => o.value === prof)) {
          profileSelect.value = prof;
          selectedProfile = prof;
        }
      } else {
        // "All Channels" → clear profile filter
        profileSelect.value = "";
        selectedProfile = null;
      }
    }
    refresh();
  }

  async function onProfileChange() {
    const select = document.getElementById("profile-select");
    if (select) {
      selectedProfile = select.value || null;
      refresh();
    }
  }
  window.onProfileChange = onProfileChange;

  function showChannelManager() {
    document.getElementById("channel-modal").style.display = "flex";
    loadChannelList();
  }
  window.showChannelManager = showChannelManager;

  function closeChannelManager() {
    document.getElementById("channel-modal").style.display = "none";
    // Clear form
    document.getElementById("new-channel-name").value = "";
    document.getElementById("new-channel-youtube-id").value = "";
    document.getElementById("new-channel-rss").value = "";
  }
  window.closeChannelManager = closeChannelManager;

  async function loadChannelList() {
    const tbody = document.getElementById("channel-list-body");

    try {
      const r = await GET("/channels");
      if (r.error) {
        tbody.innerHTML = `<tr><td colspan="4" class="empty" style="color:var(--red)">${esc(r.error)}</td></tr>`;
        return;
      }

      channels = r.channels || [];

      if (channels.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5" class="empty">No channels configured</td></tr>';
        return;
      }

      const profileOptions = ["bitcoin_podcast", "tagesschau_tr"];
      tbody.innerHTML = channels.map(ch => `
        <tr>
          <td>${esc(ch.name)}</td>
          <td>${esc(ch.youtube_channel_id || ch.rss_url || ch.channel_id)}</td>
          <td>
            <select onchange="updateChannelProfile(${ch.id}, this.value)">
              ${profileOptions.map(p => `<option value="${p}" ${ch.content_profile === p ? "selected" : ""}>${esc(p)}</option>`).join("")}
            </select>
          </td>
          <td>
            <span style="color: ${ch.is_active ? 'var(--green)' : 'var(--text-dim)'}">
              ${ch.is_active ? '✓' : '✗'}
            </span>
          </td>
          <td>
            <button class="btn btn-sm" onclick="toggleChannelActive(${ch.id})">
              ${ch.is_active ? 'Deactivate' : 'Activate'}
            </button>
            <button class="btn btn-sm btn-danger" onclick="deleteChannel(${ch.id})">Delete</button>
          </td>
        </tr>
      `).join("");
    } catch (err) {
      tbody.innerHTML = `<tr><td colspan="5" class="empty" style="color:var(--red)">Error: ${esc(err.message)}</td></tr>`;
    }
  }

  async function updateChannelProfile(channelId, profile) {
    const r = await api("PATCH", `/channels/${channelId}`, { content_profile: profile });
    if (r.error) {
      toast("Failed to update profile: " + r.error, false);
      return;
    }
    toast("Profile updated");
    loadChannels();
  }
  window.updateChannelProfile = updateChannelProfile;

  async function addChannel() {
    const name = document.getElementById("new-channel-name").value.trim();
    const youtubeId = document.getElementById("new-channel-youtube-id").value.trim();
    const rssUrl = document.getElementById("new-channel-rss").value.trim();
    const profile = document.getElementById("new-channel-profile").value || "bitcoin_podcast";

    if (!name) {
      toast("Channel name is required", false);
      return;
    }

    if (!youtubeId && !rssUrl) {
      toast("Either YouTube Channel ID or RSS URL is required", false);
      return;
    }

    const r = await POST("/channels", {
      name,
      youtube_channel_id: youtubeId || null,
      rss_url: rssUrl || null,
      content_profile: profile,
    });

    if (r.error) {
      toast("Failed to add channel: " + r.error, false);
      return;
    }

    toast("Channel added successfully");
    loadChannels();
    loadChannelList();

    // Clear form
    document.getElementById("new-channel-name").value = "";
    document.getElementById("new-channel-youtube-id").value = "";
    document.getElementById("new-channel-rss").value = "";
  }
  window.addChannel = addChannel;

  async function deleteChannel(channelId) {
    if (!confirm("Are you sure you want to delete this channel?")) {
      return;
    }

    const r = await api("DELETE", `/channels/${channelId}`);

    if (r.error) {
      toast("Failed to delete channel: " + r.error, false);
      return;
    }

    toast("Channel deleted");
    loadChannels();
    loadChannelList();
  }
  window.deleteChannel = deleteChannel;

  async function toggleChannelActive(channelId) {
    const r = await POST(`/channels/${channelId}/toggle`);

    if (r.error) {
      toast("Failed to toggle channel: " + r.error, false);
      return;
    }

    toast(r.is_active ? "Channel activated" : "Channel deactivated");
    loadChannels();
    loadChannelList();
  }
  window.toggleChannelActive = toggleChannelActive;

  // ── Review System ────────────────────────────────────────────
  let reviewTasks = [];
  let selectedReview = null;

  async function updateReviewBadge() {
    try {
      const data = await GET("/reviews/count");
      const badge = document.getElementById("review-badge");
      if (data.pending_count > 0) {
        badge.textContent = data.pending_count;
        badge.style.display = "inline";
      } else {
        badge.textContent = "";
        badge.style.display = "none";
      }
    } catch (err) {
      // Non-critical
    }
  }

  function showReviews() {
    document.querySelector(".main").style.display = "none";
    document.getElementById("review-panel").style.display = "block";
    loadReviewList();
  }
  window.showReviews = showReviews;

  function hideReviews() {
    document.getElementById("review-panel").style.display = "none";
    document.querySelector(".main").style.display = "";
  }
  window.hideReviews = hideReviews;

  async function loadReviewList() {
    const container = document.getElementById("review-list");
    container.innerHTML = '<div class="empty">Loading...</div>';

    const data = await GET("/reviews");
    if (data.error) {
      container.innerHTML = `<div class="empty" style="color:var(--red)">${esc(data.error)}</div>`;
      return;
    }

    reviewTasks = data.tasks || [];

    if (reviewTasks.length === 0) {
      container.innerHTML = '<div class="empty">No reviews found.</div>';
      return;
    }

    let html = "";
    let lastSection = "";
    reviewTasks.forEach((t) => {
      const isPending = t.status === "pending" || t.status === "in_review";
      const section = isPending ? "Pending" : "Resolved";
      if (section !== lastSection) {
        html += `<div class="review-section-header">${section}</div>`;
        lastSection = section;
      }

      const statusClass = "review-status-" + t.status.replace("_", "-");
      const time = t.reviewed_at || t.created_at;
      const timeLabel = time ? timeAgo(time) : "";
      const isSelected = selectedReview && selectedReview.id === t.id;

      html += `<div class="review-item ${statusClass} ${isSelected ? "selected" : ""}" onclick="selectReview(${t.id})">
        <div class="review-item-top">
          <span class="badge badge-review-${t.status.replace("_", "-")}">${t.status}</span>
          <span class="review-item-stage">${esc(t.stage)}</span>
        </div>
        <div class="review-item-title">${esc(t.episode_title || t.episode_id)}</div>
        <div class="review-item-time">${timeLabel}</div>
      </div>`;
    });

    container.innerHTML = html;
  }

  async function selectReview(id) {
    const detail = document.getElementById("review-detail");
    detail.innerHTML = '<div class="empty">Loading...</div>';

    const data = await GET("/reviews/" + id);
    if (data.error) {
      detail.innerHTML = `<div class="empty" style="color:var(--red)">${esc(data.error)}</div>`;
      return;
    }
    selectedReview = data;

    // Re-render list to show selection
    loadReviewList();

    let html = "";

    // Header
    html += `<div class="review-detail-header">
      <h3>${esc(data.episode_title || data.episode_id)}</h3>
      <div class="review-detail-meta">
        <span class="badge badge-review-${data.status.replace("_", "-")}">${data.status}</span>
        Stage: ${esc(data.stage)} &middot; ${data.episode_id}
        ${data.created_at ? " &middot; Created: " + new Date(data.created_at).toLocaleString() : ""}
        ${data.reviewed_at ? " &middot; Reviewed: " + new Date(data.reviewed_at).toLocaleString() : ""}
      </div>
    </div>`;

    // Video player for render reviews (Sprint 10)
    if (data.stage === "render" && data.video_url) {
      html += `<div class="review-video-player">
        <video controls preload="metadata">
          <source src="${data.video_url}" type="video/mp4">
          Your browser does not support video playback.
        </video>
      </div>`;

      if (data.render_manifest) {
        const m = data.render_manifest;
        const dur = (m.total_duration_seconds || 0).toFixed(1);
        const size = ((m.total_size_bytes || 0) / 1024 / 1024).toFixed(1);
        html += `<div class="review-render-info">
          <strong>Render Info:</strong> ${(m.segments || []).length} segments &middot; ${dur}s &middot; ${size} MB &middot;
          ${m.resolution || '1920x1080'} @ ${m.fps || 30}fps
        </div>`;
      }

      if (data.chapter_script && data.chapter_script.length > 0) {
        const chaptersHtml = data.chapter_script.map((ch) => {
          const title = ch.title ? ` — ${esc(ch.title)}` : "";
          const text = ch.text ? esc(ch.text) : "(no narration text)";
          return `
            <div class="review-script-chapter">
              <div class="review-script-title">${esc(ch.chapter_id || "")}${title}</div>
              <div class="review-script-text">${text}</div>
            </div>`;
        }).join("");
        html += `<div class="review-script-panel">
          <strong>Chapter Script:</strong>
          <div class="review-script-list">${chaptersHtml}</div>
        </div>`;
      }
    }

    // TTS audio preview for tts-stage reviews
    if (data.stage === "tts" || data.stage === "review_gate_tts") {
      try {
        const ttsData = await GET(`/episodes/${data.episode_id}/tts`);
        if (!ttsData.error && ttsData.segments && ttsData.segments.length > 0) {
          let audioRows = ttsData.segments.map(s => {
            const dur = (s.duration_seconds || 0).toFixed(1);
            const audioUrl = `api/episodes/${data.episode_id}/tts/${s.chapter_id}.mp3`;
            return `<div class="review-tts-row">
              <span class="review-tts-label">${esc(s.chapter_id)}</span>
              <span style="font-size:11px;color:var(--text-dim)">${dur}s</span>
              <audio class="review-tts-player" controls preload="none">
                <source src="${audioUrl}" type="audio/mpeg">
              </audio>
            </div>`;
          }).join("");
          html += `<div class="review-tts-audio">
            <h4>TTS Audio Preview (${ttsData.segments.length} chapters)</h4>
            ${audioRows}
          </div>`;
        }
      } catch (e) { /* non-critical */ }
    }

    // Diff viewer (for correct/adapt stages)
    const isActionable = (data.status === "pending" || data.status === "in_review");
    if (data.diff) {
      html += renderDiffViewer(
        data.diff, data.original_text, data.corrected_text,
        data.item_decisions || {}, isActionable, data.id
      );
    } else if (data.stage !== "render") {
      html += '<div class="empty">No diff data available.</div>';
    }

    // Decision history
    if (data.decisions && data.decisions.length > 0) {
      html += '<div class="review-decisions"><h4>Decision History</h4>';
      data.decisions.forEach((d) => {
        const decClass = "badge-review-" + d.decision.replace("_", "-");
        const stars = d.quality_rating ? " " + "\u2605".repeat(d.quality_rating) + "\u2606".repeat(5 - d.quality_rating) : "";
        html += `<div class="review-decision-entry">
          <span class="badge ${decClass}">${d.decision}</span>
          ${stars ? `<span class="decision-stars">${stars}</span>` : ""}
          <span class="review-decision-time">${d.decided_at ? new Date(d.decided_at).toLocaleString() : ""}</span>
          ${d.notes ? `<div class="review-decision-notes">${esc(d.notes)}</div>` : ""}
        </div>`;
      });
      html += "</div>";
    }

    // Action buttons + feedback form (only if pending/in_review)
    const isPending = data.status === "pending" || data.status === "in_review";
    if (isPending) {
      html += `<div class="review-feedback-panel">
        <h4>Your Feedback</h4>
        <div class="star-rating" id="star-rating">
          <span class="star-label">Quality:</span>
          ${[1,2,3,4,5].map(n => `<span class="star" data-value="${n}" onclick="setRating(${n})">&#9734;</span>`).join("")}
          <span class="star-value" id="star-value"></span>
        </div>
        <textarea class="review-notes-textarea" id="review-notes" placeholder="Comments on quality, issues, suggestions for improvement..."></textarea>
      </div>
      <div class="review-actions">
        <button class="btn btn-primary" onclick="approveReview(${data.id})">&#10004; Approve</button>
        <button class="btn btn-danger" onclick="rejectReview(${data.id})">&#10008; Reject</button>
        <button class="btn btn-warning" onclick="submitRequestChanges(${data.id})">&#8635; Request Changes</button>
        ${(data.stage === "correct" || data.stage === "adapt") ? `<button class="btn btn-secondary" onclick="applyReviewItems(${data.id})">Apply Accepted Changes</button>` : ""}
      </div>`;
    }

    detail.innerHTML = html;
  }
  window.selectReview = selectReview;

  function renderDiffViewer(diff, originalText, correctedText, itemDecisions, isActionable, reviewId) {
    if (diff.error) {
      return `<div class="empty">${esc(diff.error)}</div>`;
    }

    itemDecisions = itemDecisions || {};
    isActionable = !!isActionable;

    let html = "";
    const summary = diff.summary || {};
    const changes = diff.changes || diff.adaptations || [];

    // Check if this is an adaptation diff (has tier info)
    const isAdaptation = changes.length > 0 && changes[0].tier !== undefined;

    // Summary bar
    if (isAdaptation) {
      // Adaptation-specific summary
      const total = summary.total_adaptations || 0;
      const tier1 = summary.tier1_count || 0;
      const tier2 = summary.tier2_count || 0;
      html += `<div class="diff-summary">
        <span class="diff-summary-total">${total} adaptations</span>
        <span class="diff-type-badge tier1">${tier1} T1 (mechanical)</span>
        <span class="diff-type-badge tier2">${tier2} T2 (editorial)</span>
      </div>`;

      // Category breakdown
      const byCategory = summary.by_category || {};
      if (Object.keys(byCategory).length > 0) {
        html += '<div class="diff-category-summary">';
        Object.entries(byCategory).forEach(([cat, count]) => {
          html += `<span class="diff-category-badge">${cat}: ${count}</span>`;
        });
        html += '</div>';
      }
    } else {
      // Correction-specific summary
      html += `<div class="diff-summary">
        <span class="diff-summary-total">${summary.total_changes || 0} changes</span>`;
      const byType = summary.by_type || {};
      if (byType.replace) html += `<span class="diff-type-badge replace">${byType.replace} replace</span>`;
      if (byType.insert) html += `<span class="diff-type-badge insert">${byType.insert} insert</span>`;
      if (byType.delete) html += `<span class="diff-type-badge delete">${byType.delete} delete</span>`;
      html += "</div>";
    }

    // Item summary bar (Phase 5)
    html += renderItemSummary(changes, itemDecisions, isAdaptation);

    // Change/Adaptation list
    if (changes.length > 0) {
      html += '<div class="diff-changes">';
      changes.forEach((c, idx) => {
        // Determine item_id (new format or backward-compat generated from index)
        const itemId = c.item_id || (isAdaptation
          ? `adap-${String(idx).padStart(4, "0")}`
          : `corr-${String(idx).padStart(4, "0")}`);

        const decision = itemDecisions[itemId] || { action: "pending", edited_text: null };
        const currentAction = decision.action || "pending";
        const actionClass = currentAction !== "pending" ? ` diff-item-${currentAction}` : "";

        if (isAdaptation) {
          // Render adaptation with tier highlighting
          const tierClass = c.tier === "T1" ? "tier1" : "tier2";
          const tierLabel = c.tier === "T1" ? "T1 (mechanical)" : "T2 (editorial)";
          html += `<div class="diff-change adaptation ${tierClass}${actionClass}" data-item-id="${esc(itemId)}">
            <span class="diff-tier-label">${tierLabel}</span>
            <span class="diff-category-label">${c.category || "other"}</span>`;
          if (c.original) html += `<span class="diff-original">${esc(c.original)}</span>`;
          if (c.original && c.adapted) html += ' <span class="diff-arrow">&rarr;</span> ';
          if (c.adapted) html += `<span class="diff-adapted">${esc(c.adapted)}</span>`;
          if (c.context) html += `<div class="diff-context">${esc(c.context)}</div>`;
          if (isActionable) {
            html += _renderItemActions(reviewId, itemId, currentAction, decision.edited_text, c.adapted || "", isAdaptation);
          }
          html += "</div>";
        } else {
          // Render correction (existing format)
          html += `<div class="diff-change ${c.type}${actionClass}" data-item-id="${esc(itemId)}">
            <span class="diff-type-label">${c.type}</span>`;
          if (c.original) html += `<span class="diff-original">${esc(c.original)}</span>`;
          if (c.original && c.corrected) html += ' <span class="diff-arrow">&rarr;</span> ';
          if (c.corrected) html += `<span class="diff-corrected">${esc(c.corrected)}</span>`;
          if (c.context) html += `<div class="diff-context">${esc(c.context)}</div>`;
          if (isActionable) {
            html += _renderItemActions(reviewId, itemId, currentAction, decision.edited_text, c.corrected || "", isAdaptation);
          }
          html += "</div>";
        }
      });
      html += "</div>";
    }

    // Side-by-side panels
    if (originalText || correctedText) {
      const leftLabel = isAdaptation ? "Translation" : "Original";
      const rightLabel = isAdaptation ? "Adapted" : "Corrected";
      html += `<div class="diff-sidebyside">
        <div class="diff-side">
          <h4>${leftLabel}</h4>
          <pre class="diff-text">${esc(originalText || "(not available)")}</pre>
        </div>
        <div class="diff-side">
          <h4>${rightLabel}</h4>
          <pre class="diff-text">${esc(correctedText || "(not available)")}</pre>
        </div>
      </div>`;
    }

    return html;
  }

  async function batchApproveAll() {
    const pending = reviewTasks.filter(t => t.status === "pending" || t.status === "in_review");
    if (pending.length === 0) {
      toast("No pending reviews to approve", false);
      return;
    }
    if (!confirm(`Approve all ${pending.length} pending review(s)?`)) return;
    const ids = pending.map(t => t.id);
    const r = await POST("/reviews/batch-approve", { review_ids: ids });
    if (r.error) {
      toast(r.error, false);
    } else {
      toast(`Approved ${r.approved.length} reviews${r.errors.length ? ", " + r.errors.length + " errors" : ""}`);
      selectedReview = null;
      loadReviewList();
      updateReviewBadge();
      document.getElementById("review-detail").innerHTML = '<div class="empty">Reviews approved.</div>';
    }
  }
  window.batchApproveAll = batchApproveAll;

  let currentRating = null;
  function setRating(n) {
    currentRating = n;
    document.querySelectorAll("#star-rating .star").forEach((s) => {
      s.innerHTML = parseInt(s.dataset.value) <= n ? "\u2605" : "\u2606";
    });
    const sv = document.getElementById("star-value");
    if (sv) sv.textContent = n + "/5";
  }
  window.setRating = setRating;

  function _getReviewFeedback() {
    const notes = (document.getElementById("review-notes") || {}).value || "";
    return { notes: notes.trim() || undefined, quality_rating: currentRating };
  }

  async function approveReview(id) {
    const fb = _getReviewFeedback();
    const r = await POST("/reviews/" + id + "/approve", fb);
    if (r.error) {
      toast(r.error, false);
    } else {
      toast("Review approved");
      currentRating = null;
      selectedReview = null;
      loadReviewList();
      updateReviewBadge();
      document.getElementById("review-detail").innerHTML = '<div class="empty">Review approved.</div>';
    }
  }
  window.approveReview = approveReview;

  async function rejectReview(id) {
    const isRender = selectedReview && selectedReview.stage === "render";
    if (!confirm("Reject this review?")) return;

    const fb = _getReviewFeedback();
    if (isRender && !fb.notes) {
      toast("Notes are required to reject render review", false);
      return;
    }

    const r = await POST("/reviews/" + id + "/reject", fb);
    if (r.error) {
      toast(r.error, false);
    } else {
      toast("Review rejected");
      currentRating = null;
      selectedReview = null;
      loadReviewList();
      updateReviewBadge();
      document.getElementById("review-detail").innerHTML = '<div class="empty">Review rejected.</div>';
    }
  }
  window.rejectReview = rejectReview;

  async function submitRequestChanges(id) {
    const fb = _getReviewFeedback();
    if (!fb.notes) {
      toast("Please provide notes describing the changes needed", false);
      return;
    }
    const r = await POST("/reviews/" + id + "/request-changes", fb);
    if (r.error) {
      toast(r.error, false);
    } else {
      toast("Changes requested");
      currentRating = null;
      selectedReview = null;
      loadReviewList();
      updateReviewBadge();
      document.getElementById("review-detail").innerHTML = '<div class="empty">Changes requested.</div>';
    }
  }
  window.submitRequestChanges = submitRequestChanges;

  // ── Phase 5: Granular item-level review functions ─────────────────────
  function _renderItemActions(reviewId, itemId, currentAction, editedText, proposedText, isAdaptation) {
    const prefill = esc(editedText || proposedText);
    return `<div class="diff-item-actions" data-item-id="${esc(itemId)}" data-review-id="${reviewId}">
      <button class="diff-item-btn accept ${currentAction === "accepted" ? "active" : ""}"
        onclick="itemAction(${reviewId}, '${esc(itemId)}', 'accept')">&#10003; Accept</button>
      <button class="diff-item-btn reject ${currentAction === "rejected" ? "active" : ""}"
        onclick="itemAction(${reviewId}, '${esc(itemId)}', 'reject')">&#10007; Reject</button>
      <button class="diff-item-btn edit ${currentAction === "edited" ? "active" : ""}"
        onclick="toggleEditInline('${esc(itemId)}')">&#9998; Edit</button>
      <button class="diff-item-btn reset ${currentAction === "pending" ? "active" : ""}"
        onclick="itemAction(${reviewId}, '${esc(itemId)}', 'reset')">&#9675; Reset</button>
    </div>
    <div class="diff-edit-inline" id="edit-inline-${esc(itemId)}" style="display:none">
      <textarea class="diff-edit-textarea" id="edit-text-${esc(itemId)}">${prefill}</textarea>
      <div class="diff-edit-actions">
        <button class="btn btn-sm btn-primary" onclick="saveEditInline(${reviewId}, '${esc(itemId)}')">Save</button>
        <button class="btn btn-sm" onclick="cancelEditInline('${esc(itemId)}')">Cancel</button>
      </div>
    </div>`;
  }

  function renderItemSummary(changes, itemDecisions, isAdaptation) {
    const counts = { accepted: 0, rejected: 0, edited: 0, unchanged: 0, pending: 0 };
    changes.forEach((c, idx) => {
      const itemId = c.item_id || (isAdaptation
        ? `adap-${String(idx).padStart(4, "0")}`
        : `corr-${String(idx).padStart(4, "0")}`);
      const action = (itemDecisions[itemId] || {}).action || "pending";
      counts[action] = (counts[action] || 0) + 1;
    });
    return `<div class="diff-item-summary" id="diff-item-summary">
      <span class="dim-count accepted">&#10003; ${counts.accepted} accepted</span>
      <span class="dim-count rejected">&#10007; ${counts.rejected} rejected</span>
      <span class="dim-count edited">&#9998; ${counts.edited} edited</span>
      <span class="dim-count unchanged">&mdash; ${counts.unchanged} unchanged</span>
      <span class="dim-count pending">&#8943; ${counts.pending} pending</span>
    </div>`;
  }

  async function itemAction(reviewId, itemId, action) {
    const r = await POST(`/reviews/${reviewId}/items/${itemId}/${action}`);
    if (r.error) {
      toast(r.error, false);
    } else {
      _updateItemVisual(itemId, r.action);
      _updateItemSummary();
    }
  }
  window.itemAction = itemAction;

  function toggleEditInline(itemId) {
    const el = document.getElementById(`edit-inline-${itemId}`);
    if (el) el.style.display = el.style.display === "none" ? "block" : "none";
  }
  window.toggleEditInline = toggleEditInline;

  async function saveEditInline(reviewId, itemId) {
    const textarea = document.getElementById(`edit-text-${itemId}`);
    if (!textarea) return;
    const text = textarea.value.trim();
    if (!text) { toast("Edited text cannot be empty", false); return; }
    const r = await POST(`/reviews/${reviewId}/items/${itemId}/edit`, { text });
    if (r.error) {
      toast(r.error, false);
    } else {
      _updateItemVisual(itemId, "edited");
      _updateItemSummary();
      const panel = document.getElementById(`edit-inline-${itemId}`);
      if (panel) panel.style.display = "none";
      toast("Edit saved");
    }
  }
  window.saveEditInline = saveEditInline;

  function cancelEditInline(itemId) {
    const el = document.getElementById(`edit-inline-${itemId}`);
    if (el) el.style.display = "none";
  }
  window.cancelEditInline = cancelEditInline;

  function _updateItemVisual(itemId, action) {
    const container = document.querySelector(`[data-item-id="${itemId}"].diff-change`);
    if (!container) return;
    const actionClasses = ["diff-item-accepted", "diff-item-rejected", "diff-item-edited",
                           "diff-item-unchanged"];
    actionClasses.forEach(cls => container.classList.remove(cls));
    if (action && action !== "pending") {
      container.classList.add(`diff-item-${action}`);
    }
    container.querySelectorAll(".diff-item-btn").forEach(btn => btn.classList.remove("active"));
    const activeBtn = container.querySelector(`.diff-item-btn.${action}`);
    if (activeBtn) activeBtn.classList.add("active");
  }

  function _updateItemSummary() {
    const counts = { accepted: 0, rejected: 0, edited: 0, unchanged: 0, pending: 0 };
    document.querySelectorAll(".diff-change").forEach(el => {
      if (el.classList.contains("diff-item-accepted")) counts.accepted++;
      else if (el.classList.contains("diff-item-rejected")) counts.rejected++;
      else if (el.classList.contains("diff-item-edited")) counts.edited++;
      else if (el.classList.contains("diff-item-unchanged")) counts.unchanged++;
      else counts.pending++;
    });
    const bar = document.getElementById("diff-item-summary");
    if (bar) {
      bar.innerHTML = `
        <span class="dim-count accepted">&#10003; ${counts.accepted} accepted</span>
        <span class="dim-count rejected">&#10007; ${counts.rejected} rejected</span>
        <span class="dim-count edited">&#9998; ${counts.edited} edited</span>
        <span class="dim-count unchanged">&mdash; ${counts.unchanged} unchanged</span>
        <span class="dim-count pending">&#8943; ${counts.pending} pending</span>`;
    }
  }

  async function applyReviewItems(reviewId) {
    const r = await POST(`/reviews/${reviewId}/apply`);
    if (r.error) {
      toast(r.error, false);
    } else {
      const msg = r.pending_count > 0
        ? `Reviewed file saved. ${r.pending_count} of ${r.total_items} items still pending — pending items treated as accepted.`
        : `Reviewed file saved. All ${r.total_items} items decided.`;
      toast(msg);
    }
  }
  window.applyReviewItems = applyReviewItems;

  function timeAgo(dateStr) {
    const now = new Date();
    const then = new Date(dateStr);
    const diffMs = now - then;
    const mins = Math.floor(diffMs / 60000);
    if (mins < 1) return "just now";
    if (mins < 60) return mins + "m ago";
    const hours = Math.floor(mins / 60);
    if (hours < 24) return hours + "h ago";
    const days = Math.floor(hours / 24);
    return days + "d ago";
  }

  // ── SSE Live Updates ─────────────────────────────────────────
  let _sseSource = null;
  let _sseReconnectTimer = null;

  function initSSE() {
    if (!window.EventSource) return; // Fallback: keep existing polling
    if (_sseSource) { _sseSource.close(); _sseSource = null; }

    const indicator = document.getElementById("sse-indicator");
    _sseSource = new EventSource("api/stream");

    _sseSource.addEventListener("connected", () => {
      if (indicator) { indicator.className = "sse-indicator sse-connected"; }
      if (_sseReconnectTimer) { clearTimeout(_sseReconnectTimer); _sseReconnectTimer = null; }
    });

    _sseSource.addEventListener("job_update", (e) => {
      const data = JSON.parse(e.data);
      // If this matches our active job, stop polling and handle result
      if (data.job_id && data.job_id === activeJobId) {
        if (data.state === "success") {
          clearInterval(pollTimer);
          hideSpinner();
          disableActions(false);
          activeJobId = null;
          const cost = data.result && data.result.cost_usd
            ? ` ($${data.result.cost_usd.toFixed(4)})` : "";
          toast(data.action + " complete" + cost);
          refresh();
        } else if (data.state === "error") {
          clearInterval(pollTimer);
          hideSpinner();
          disableActions(false);
          activeJobId = null;
          toast(data.message || "Job failed", false);
          refresh();
        } else if (data.state === "running") {
          updateSpinner(data.action + ": " + (data.stage || "running"));
        }
      }
    });

    _sseSource.addEventListener("batch_update", (e) => {
      const data = JSON.parse(e.data);
      if (activeBatchId && data.batch_id === activeBatchId) {
        updateBatchUI({
          state: data.state,
          progress_pct: data.progress_pct || 0,
          completed_episodes: data.completed_episodes || 0,
          failed_episodes: data.failed_episodes || 0,
          remaining_episodes: 0,
          total_cost_usd: data.total_cost_usd || 0,
          current_episode_id: data.current_episode_id || null,
          current_episode_title: data.current_episode_id || "",
          current_stage: data.current_stage || "",
          message: data.message || "",
        });
        if (data.state === "success" || data.state === "stopped" || data.state === "error") {
          clearInterval(batchPollTimer);
          const msg = data.state === "success"
            ? `Batch complete: ${data.completed_episodes || 0} done ($${(data.total_cost_usd || 0).toFixed(4)})`
            : `Batch ${data.state}${data.message ? ": " + data.message : ""}`;
          toast(msg, data.state === "success");
          resetBatchUI();
          refresh();
        }
      }
    });

    _sseSource.onerror = () => {
      if (indicator) { indicator.className = "sse-indicator sse-error"; }
      _sseSource.close();
      _sseSource = null;
      // Reconnect after 5s
      _sseReconnectTimer = setTimeout(initSSE, 5000);
    };
  }

  // ── Init ─────────────────────────────────────────────────────
  document.addEventListener("DOMContentLoaded", () => {
    document.getElementById("filter-status").onchange = applyFilters;
    document.getElementById("filter-search").oninput = applyFilters;
    document.getElementById("channel-select").onchange = onChannelChange;

    // Mobile view detection
    updateMobileView();
    window.addEventListener("resize", updateMobileView);

    // iOS viewport height fix
    setViewportHeight();
    window.addEventListener("resize", setViewportHeight);
    window.addEventListener("orientationchange", setViewportHeight);

    // Start in list view on mobile
    if (isMobileView) {
      document.body.classList.add("mobile-list-view");
    }

    loadChannels();
    refresh();
    checkActiveBatch();
    initSSE();
  });
})();
