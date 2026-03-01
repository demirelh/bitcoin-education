/* btcedu dashboard - vanilla JS */
(function () {
  "use strict";

  let episodes = [];
  let selected = null;
  let channels = [];
  let selectedChannelId = null;
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
    "outline_v2", "script_v2", "publishing_v2", "chapters"
  ];
  const FILE_LABELS = [
    "Audio", "Transcript DE", "Transcript Clean", "Chunks",
    "Outline TR", "Script TR", "Shorts", "Visuals", "QA", "Publishing",
    "Outline v2", "Script v2", "Publishing v2", "Chapters"
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
          <span class="badge badge-${ep.status}">${ep.status}</span>
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

    // Show detail view on mobile
    mobileShowDetail();

    const det = document.getElementById("detail");
    det.innerHTML = `
      <div class="detail-header">
        <h2>${esc(ep.title)}</h2>
        <div class="detail-meta">
          <span class="badge badge-${ep.status}">${ep.status}</span>
          ${ep.episode_id} &middot; ${ep.published_at ? ep.published_at.slice(0, 10) : "\u2014"}
          &middot; <a href="${esc(ep.url)}" target="_blank" style="color:var(--accent)">source</a>
          ${ep.youtube_video_id ? `&middot; <a href="https://youtu.be/${esc(ep.youtube_video_id)}" target="_blank" style="color:#f90">▶ YouTube</a>` : ""}
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
          <button class="btn btn-sm btn-success" onclick="actions.publish()" title="Publish approved video to YouTube">Publish</button>
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
        <div class="tab" data-tab="chapters">Chapters</div>
        <div class="tab" data-tab="images">Images</div>
        <div class="tab" data-tab="tts_audio">TTS Audio</div>
        <div class="tab" data-tab="video">Video</div>
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
      // Build query with optional channel filter
      let url = "/episodes";
      if (selectedChannelId) {
        url += `?channel_id=${encodeURIComponent(selectedChannelId)}`;
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
    refresh();
  }

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
        tbody.innerHTML = '<tr><td colspan="4" class="empty">No channels configured</td></tr>';
        return;
      }

      tbody.innerHTML = channels.map(ch => `
        <tr>
          <td>${esc(ch.name)}</td>
          <td>${esc(ch.youtube_channel_id || ch.rss_url || ch.channel_id)}</td>
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
      tbody.innerHTML = `<tr><td colspan="4" class="empty" style="color:var(--red)">Error: ${esc(err.message)}</td></tr>`;
    }
  }

  async function addChannel() {
    const name = document.getElementById("new-channel-name").value.trim();
    const youtubeId = document.getElementById("new-channel-youtube-id").value.trim();
    const rssUrl = document.getElementById("new-channel-rss").value.trim();

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

    // Diff viewer (for correct/adapt stages)
    if (data.diff) {
      html += renderDiffViewer(data.diff, data.original_text, data.corrected_text);
    } else if (data.stage !== "render") {
      html += '<div class="empty">No diff data available.</div>';
    }

    // Decision history
    if (data.decisions && data.decisions.length > 0) {
      html += '<div class="review-decisions"><h4>Decision History</h4>';
      data.decisions.forEach((d) => {
        const decClass = "badge-review-" + d.decision.replace("_", "-");
        html += `<div class="review-decision-entry">
          <span class="badge ${decClass}">${d.decision}</span>
          <span class="review-decision-time">${d.decided_at ? new Date(d.decided_at).toLocaleString() : ""}</span>
          ${d.notes ? `<div class="review-decision-notes">${esc(d.notes)}</div>` : ""}
        </div>`;
      });
      html += "</div>";
    }

    // Action buttons (only if pending/in_review)
    const isPending = data.status === "pending" || data.status === "in_review";
    if (isPending) {
      html += `<div class="review-actions">
        <button class="btn btn-primary" onclick="approveReview(${data.id})">Approve</button>
        <button class="btn btn-danger" onclick="rejectReview(${data.id})">Reject</button>
        <button class="btn btn-warning" onclick="toggleChangesForm()">Request Changes</button>
      </div>
      <div class="review-changes-form" id="review-changes-form" style="display:none;">
        <textarea class="review-notes-textarea" id="review-notes" placeholder="Describe the changes needed..."></textarea>
        <button class="btn btn-warning" onclick="submitRequestChanges(${data.id})">Submit Feedback</button>
      </div>`;
    }

    detail.innerHTML = html;
  }
  window.selectReview = selectReview;

  function renderDiffViewer(diff, originalText, correctedText) {
    if (diff.error) {
      return `<div class="empty">${esc(diff.error)}</div>`;
    }

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

    // Change/Adaptation list
    if (changes.length > 0) {
      html += '<div class="diff-changes">';
      changes.forEach((c) => {
        if (isAdaptation) {
          // Render adaptation with tier highlighting
          const tierClass = c.tier === "T1" ? "tier1" : "tier2";
          const tierLabel = c.tier === "T1" ? "T1 (mechanical)" : "T2 (editorial)";
          html += `<div class="diff-change adaptation ${tierClass}">
            <span class="diff-tier-label">${tierLabel}</span>
            <span class="diff-category-label">${c.category || "other"}</span>`;
          if (c.original) html += `<span class="diff-original">${esc(c.original)}</span>`;
          if (c.original && c.adapted) html += ' <span class="diff-arrow">&rarr;</span> ';
          if (c.adapted) html += `<span class="diff-adapted">${esc(c.adapted)}</span>`;
          if (c.context) html += `<div class="diff-context">${esc(c.context)}</div>`;
          html += "</div>";
        } else {
          // Render correction (existing format)
          html += `<div class="diff-change ${c.type}">
            <span class="diff-type-label">${c.type}</span>`;
          if (c.original) html += `<span class="diff-original">${esc(c.original)}</span>`;
          if (c.original && c.corrected) html += ' <span class="diff-arrow">&rarr;</span> ';
          if (c.corrected) html += `<span class="diff-corrected">${esc(c.corrected)}</span>`;
          if (c.context) html += `<div class="diff-context">${esc(c.context)}</div>`;
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

  async function approveReview(id) {
    const r = await POST("/reviews/" + id + "/approve");
    if (r.error) {
      toast(r.error, false);
    } else {
      toast("Review approved");
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

    let notes = "";
    if (isRender) {
      notes = prompt("Reject notes (required):", "") || "";
      if (!notes.trim()) {
        toast("Notes are required to reject render review", false);
        return;
      }
    }

    const r = await POST("/reviews/" + id + "/reject", isRender ? { notes } : null);
    if (r.error) {
      toast(r.error, false);
    } else {
      toast("Review rejected");
      selectedReview = null;
      loadReviewList();
      updateReviewBadge();
      document.getElementById("review-detail").innerHTML = '<div class="empty">Review rejected.</div>';
    }
  }
  window.rejectReview = rejectReview;

  function toggleChangesForm() {
    const form = document.getElementById("review-changes-form");
    form.style.display = form.style.display === "none" ? "block" : "none";
  }
  window.toggleChangesForm = toggleChangesForm;

  async function submitRequestChanges(id) {
    const notes = document.getElementById("review-notes").value.trim();
    if (!notes) {
      toast("Please provide notes describing the changes needed", false);
      return;
    }
    const r = await POST("/reviews/" + id + "/request-changes", { notes });
    if (r.error) {
      toast(r.error, false);
    } else {
      toast("Changes requested");
      selectedReview = null;
      loadReviewList();
      updateReviewBadge();
      document.getElementById("review-detail").innerHTML = '<div class="empty">Changes requested.</div>';
    }
  }
  window.submitRequestChanges = submitRequestChanges;

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
  });
})();
