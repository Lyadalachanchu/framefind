"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const icons = {
    play: '<svg viewBox="0 0 24 24"><path d="m8 5 11 7-11 7V5Z"/></svg>',
    pause: '<svg viewBox="0 0 24 24"><path d="M9 5v14M15 5v14"/></svg>',
    trash: '<svg viewBox="0 0 24 24"><path d="M4 6h16M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7m4-7v7"/></svg>',
    film: '<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 4v16M17 4v16M3 9h4m-4 6h4M17 9h4m-4 6h4"/></svg>',
    check: '<svg viewBox="0 0 24 24"><path d="m5 12 4 4L19 6"/></svg>',
    search: '<svg viewBox="0 0 24 24"><circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4 4"/></svg>',
    arrow: '<svg viewBox="0 0 24 24"><path d="M5 12h14m-6-6 6 6-6 6"/></svg>',
    spinner: '<span class="spinner" aria-hidden="true"></span>'
  };
  const ACTIVE = new Set(["queued", "downloading", "probing", "extracting", "indexing", "processing", "pausing"]);
  const TERMINAL_SEARCH = new Set(["complete", "failed", "cancelled"]);
  const state = { videos: [], status: null, selected: new Set(), search: null, lastQuery: "", results: [], pending: new Set(), uploading: false, polling: false, offline: false, librarySignature: "", resultsSignature: "", searchTimer: null, refreshTimer: null, snippet: null };
  const defaultEmptyVisual = $("empty-state").innerHTML;
  const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[character]));
  const number = (value) => new Intl.NumberFormat().format(Number(value) || 0);
  const percent = (value) => Math.round(Math.max(0, Math.min(1, Number(value) || 0)) * 100);
  const time = (seconds) => { const value = Math.max(0, Math.floor(Number(seconds) || 0)); const hours = Math.floor(value / 3600); const minutes = Math.floor((value % 3600) / 60); return `${hours ? hours + ":" : ""}${String(minutes).padStart(2, "0")}:${String(value % 60).padStart(2, "0")}`; };
  const videoUrl = (video, suffix = "") => `/api/videos/${encodeURIComponent(video.id || video.video_id)}${suffix}`;
  const safeMedia = (url) => { if (typeof url !== "string") return ""; try { const parsed = new URL(url, window.location.origin); return parsed.origin === window.location.origin && ["http:", "https:"].includes(parsed.protocol) ? parsed.href : ""; } catch { return ""; } };

  async function api(path, options = {}) {
    const response = await fetch(path, { ...options, headers: { ...(options.body && !(options.body instanceof FormData) ? { "Content-Type": "application/json" } : {}), ...options.headers } });
    let data;
    try { data = await response.json(); } catch { data = {}; }
    if (!response.ok) {
      const detail = typeof data.detail === "string" ? data.detail : typeof data.error === "string" ? data.error : Array.isArray(data.detail) ? data.detail.map((item) => item.msg).join("; ") : `Request failed (${response.status}).`;
      throw new Error(detail);
    }
    return data;
  }

  function toast(message, error = false) {
    const element = document.createElement("div");
    element.className = "toast" + (error ? " error" : "");
    element.textContent = message;
    element.setAttribute("role", error ? "alert" : "status");
    $("toast-region").append(element);
    setTimeout(() => element.remove(), error ? 8500 : 5000);
  }

  function labelFor(video) {
    if (video.status === "ready") return "Ready to search";
    if (video.status === "partial") return "Partially prepared";
    if (video.status === "paused") return "Preparation paused";
    if (video.status === "failed") return "Needs attention";
    if (video.status === "pausing") return "Pausing preparation";
    if (video.status === "indexing") return "Preparing video";
    const stage = video.stage || video.status || "queued";
    return stage.charAt(0).toUpperCase() + stage.slice(1).replace(/_/g, " ");
  }

  function renderLibrary() {
    const signature = JSON.stringify([state.videos, [...state.selected], [...state.pending]]);
    if (signature === state.librarySignature) return;
    state.librarySignature = signature;
    const focused = $("video-list").contains(document.activeElement) ? document.activeElement : null;
    const focusKey = focused?.dataset.select ? ["data-select", focused.dataset.select] : focused?.dataset.preview ? ["data-preview", focused.dataset.preview] : focused?.dataset.action ? ["data-action", focused.dataset.action, focused.dataset.id] : null;
    const count = state.videos.length;
    $("library-count").textContent = count;
    $("all-count").textContent = count;
    $("all-videos").classList.toggle("active", state.selected.size === 0);
    $("all-videos").setAttribute("aria-pressed", state.selected.size === 0 ? "true" : "false");
    $("search-scope").textContent = state.selected.size ? `Searching ${state.selected.size} selected video${state.selected.size === 1 ? "" : "s"}` : "Searching all videos";
    if (!count) {
      $("video-list").innerHTML = `<div class="library-empty">${icons.film}<p>No videos yet</p></div>`;
    } else {
      $("video-list").innerHTML = state.videos.map((video) => {
        const active = ACTIVE.has(video.status);
        const resume = ["paused", "partial", "failed"].includes(video.status);
        const downloadMatch = video.status === "downloading" ? String(video.stage || "").match(/(\d+(?:\.\d+)?)%/) : null;
        const progress = downloadMatch ? Math.max(0, Math.min(100, Number(downloadMatch[1]))) : video.status === "ready" ? 100 : 0;
        const indeterminate = active && !downloadMatch;
        const pending = state.pending.has(video.id);
        const selected = state.selected.has(video.id);
        return `<article class="video-item${selected ? " selected" : ""}" data-video-id="${escape(video.id)}"><div class="video-top"><input class="video-select" type="checkbox" ${selected ? "checked" : ""} data-select="${escape(video.id)}" aria-label="Select ${escape(video.name)} for search"><button type="button" class="video-title-button" data-preview="${escape(video.id)}" title="Preview ${escape(video.name)}"><span class="video-title">${escape(video.name)}</span><span class="video-duration">${video.duration ? time(video.duration) : "Preparing video"}${video.width && video.height ? `<span class="divider">·</span>${video.width} × ${video.height}` : ""}</span></button></div><div class="video-info-row"><span class="video-status ${escape(video.status)}${active ? " active" : ""}"><span class="status-dot"></span>${escape(labelFor(video))}</span><div class="video-actions">${active && video.status !== "pausing" ? `<button type="button" class="icon-button small" data-action="pause" data-id="${escape(video.id)}" aria-label="Pause preparation ${escape(video.name)}" title="Pause preparation" ${pending ? "disabled" : ""}>${icons.pause}</button>` : ""}${resume ? `<button type="button" class="icon-button small" data-action="resume" data-id="${escape(video.id)}" aria-label="Resume preparation ${escape(video.name)}" title="Resume preparation" ${pending ? "disabled" : ""}>${icons.play}</button>` : ""}<button type="button" class="icon-button small delete-video" data-action="delete" data-id="${escape(video.id)}" aria-label="Delete ${escape(video.name)}" title="Delete video" ${pending ? "disabled" : ""}>${icons.trash}</button></div></div>${active || video.status === "paused" || video.status === "partial" ? `<div class="video-progress-track${indeterminate ? " indeterminate" : ""}" role="progressbar" aria-label="${video.status === "downloading" ? "Download" : "Preparation"} progress" ${indeterminate ? 'aria-valuetext="Waiting for progress"' : `aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(progress)}"`}><span style="width:${progress}%"></span></div>` : ""}${video.error ? `<p class="video-error">${escape(video.error)}</p>` : ""}</article>`;
      }).join("");
    }
    if (focusKey) {
      const selector = `[${focusKey[0]}="${CSS.escape(focusKey[1])}"]${focusKey[2] ? `[data-id="${CSS.escape(focusKey[2])}"]` : ""}`;
      $("video-list").querySelector(selector)?.focus({ preventScroll: true });
    }
    $("upload-zone").classList.toggle("compact", count > 0);
    $("upload-title").textContent = "Add videos";
    $("upload-description").textContent = "Drop files or paste a link.";
    updateSearchButton();
    if (!state.search) renderEmpty();
  }

  function renderStatus() {
    const status = state.status;
    if (!status) return;
    const configured = status.configured !== false;
    const ffmpeg = status.ffmpeg_available !== false;
    $("configuration-alert").hidden = configured && ffmpeg;
    if (!ffmpeg) {
      $("configuration-title").textContent = "FFmpeg is needed to read your videos";
      $("configuration-message").textContent = "Install FFmpeg and make sure ffmpeg and ffprobe are on the server’s PATH, then restart the app.";
    } else if (!configured) {
      $("configuration-title").textContent = "Connect Jev to search";
      $("configuration-message").textContent = "Run start.command and enter your TypeSafe API key when prompted. Your key stays on the server.";
    }
    $("connection-dot").className = `connection-dot ${configured && ffmpeg ? "connected" : "error"}`;
    $("connection-label").textContent = configured && ffmpeg ? "Connected" : !configured ? "Jev API key needed" : "FFmpeg needed";
    updateSearchButton();
  }

  function isSearching() { return Boolean(state.search && !TERMINAL_SEARCH.has(state.search.status)); }
  function updateSearchButton() {
    const button = $("search-button");
    const running = isSearching();
    button.disabled = running;
    button.innerHTML = running ? icons.spinner : icons.arrow;
    button.setAttribute("aria-label", running ? "Search in progress" : "Search videos");
    $("search-form").setAttribute("aria-busy", running ? "true" : "false");
  }

  function renderEmpty(kind = "initial", message = "") {
    const empty = $("empty-state");
    empty.hidden = state.results.length > 0;
    if (state.results.length) return;
    if (kind === "initial") {
      if (!$("empty-title")) empty.innerHTML = defaultEmptyVisual;
      const frames = state.videos.reduce((sum, video) => sum + (Number(video.frames_ready) || 0), 0);
      empty.hidden = frames > 0;
      $("empty-title").textContent = state.videos.length ? "Preparing video…" : "Add a video to search";
      $("empty-description").textContent = state.videos.length ? "Your video will be ready shortly." : "";
      $("empty-description").hidden = !state.videos.length;
      return;
    }
    if (kind === "searching") { empty.hidden = true; return; }
    const titles = { none: "No matches", failed: "Search failed", cancelled: "Search stopped" };
    const descriptions = { none: "Try a broader query.", failed: escape(message || "Check your connection and try again."), cancelled: "" };
    empty.innerHTML = `<h3>${titles[kind] || titles.none}</h3>${descriptions[kind] ? `<p>${descriptions[kind]}</p>` : ""}`;
  }

  async function refresh() {
    if (state.polling) return;
    state.polling = true;
    try {
      const results = await Promise.allSettled([api("/api/status"), api("/api/videos")]);
      const failed = results.find((result) => result.status === "rejected");
      if (results[0].status === "fulfilled") { state.status = results[0].value; renderStatus(); }
      if (results[1].status === "fulfilled") {
        state.videos = results[1].value.videos || [];
        const existing = new Set(state.videos.map((video) => video.id));
        state.selected = new Set([...state.selected].filter((id) => existing.has(id)));
        renderLibrary();
      }
      if (failed) throw failed.reason;
      state.offline = false;
    } catch (error) {
      $("connection-dot").className = "connection-dot error";
      $("connection-label").textContent = "Connection lost · retrying";
      if (!state.offline) toast("Could not reach the local server. Reconnecting automatically.", true);
      state.offline = true;
    } finally {
      state.polling = false;
      clearTimeout(state.refreshTimer);
      const active = state.videos.some((video) => ACTIVE.has(video.status)) || state.uploading || isSearching();
      state.refreshTimer = setTimeout(refresh, active ? 1500 : 6000);
    }
  }

  async function videoAction(action, id) {
    const video = state.videos.find((item) => item.id === id);
    if (!video || state.pending.has(id)) return;
    if (action === "delete" && !window.confirm(`Delete “${video.name}” and its saved frames from this library? This removes the local copy stored by FrameFind.`)) return;
    state.pending.add(id); renderLibrary();
    try {
      await api(videoUrl(video, action === "delete" ? "" : `/${action}`), { method: action === "delete" ? "DELETE" : "POST" });
      if (action === "delete") {
        state.selected.delete(id);
        state.videos = state.videos.filter((item) => item.id !== id);
        state.results = state.results.filter((item) => item.video_id !== id);
        if (state.search) { state.search.results = state.results; renderSearch(); }
        toast("Video removed from your library.");
      }
      await refresh();
    } catch (error) { toast(error.message, true); }
    finally { state.pending.delete(id); renderLibrary(); }
  }

  function uploadFile(file, position, total) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/videos");
      xhr.responseType = "json";
      const base = `${total > 1 ? `${position} of ${total} · ` : ""}${file.name}`;
      const show = (description) => { $("upload-progress").innerHTML = `${icons.spinner}<span>${escape(description)}</span>`; };
      show(`Uploading ${base}`);
      xhr.upload.onprogress = (event) => { if (event.lengthComputable) show(`Uploading ${base} · ${Math.round(event.loaded / event.total * 100)}%`); };
      xhr.onload = () => xhr.status >= 200 && xhr.status < 300 ? resolve(xhr.response) : reject(new Error(typeof xhr.response?.detail === "string" ? xhr.response.detail : `Could not upload ${file.name} (${xhr.status}).`));
      xhr.onerror = () => reject(new Error("Upload interrupted. Check that the local server is running."));
      xhr.upload.onload = () => show(`Preparing ${base}…`);
      const form = new FormData(); form.append("file", file); xhr.send(form);
    });
  }

  async function upload(files) {
    if (!files?.length) return;
    if (state.uploading) { toast("An upload is in progress. Add more videos when it finishes."); return; }
    state.uploading = true;
    $("upload-progress").hidden = false;
    document.querySelectorAll(".upload-trigger").forEach((button) => { button.disabled = true; });
    let completed = 0;
    try {
      for (const [index, file] of [...files].entries()) {
        try { await uploadFile(file, index + 1, files.length); completed++; await refresh(); }
        catch (error) { toast(error.message, true); }
      }
      if (completed) toast(`${completed === 1 ? "Video added" : `${completed} videos added`}. Preparing your video.`);
    } finally {
      state.uploading = false; $("upload-progress").hidden = true; $("file-input").value = "";
      document.querySelectorAll(".upload-trigger").forEach((button) => { button.disabled = false; });
      await refresh();
    }
  }

  async function startSearch(event) {
    event.preventDefault();
    const query = $("search-query").value.trim();
    if (!query || isSearching()) return;
    if (!state.videos.length) { toast("Add a video to start searching."); return; }
    if (state.status?.configured === false) { toast("Set your TypeSafe API key on the server to enable search.", true); return; }
    const scoped = state.selected.size ? state.videos.filter((video) => state.selected.has(video.id)) : state.videos;
    if (!scoped.some((video) => Number(video.frames_ready) > 0)) { toast("Your selected videos are still being prepared."); return; }
    state.lastQuery = query;
    state.results = []; state.resultsSignature = "";
    state.search = { status: "running", progress: 0, total: 0, results: [] };
    renderSearch();
    try {
      const data = await api("/api/search", { method: "POST", body: JSON.stringify({ query, video_ids: [...state.selected] }) });
      state.search = { ...state.search, ...data, status: data.status || "running" };
      await pollSearch();
    } catch (error) { state.search = { ...state.search, status: "failed", error: error.message }; renderSearch(); }
  }

  async function pollSearch() {
    clearTimeout(state.searchTimer);
    if (!state.search?.id || !isSearching()) return;
    const id = state.search.id;
    try {
      const result = await api(`/api/search/${encodeURIComponent(id)}`);
      if (state.search?.id !== id) return;
      state.search = { ...state.search, ...result };
      state.results = result.results || [];
      renderSearch();
      if (!isSearching()) { await refresh(); return; }
    } catch (error) {
      $("search-progress").innerHTML = `<div class="search-progress-inner">${icons.spinner}<div class="progress-copy"><strong>Reconnecting to your search…</strong><span>${escape(error.message)}</span></div><button type="button" class="cancel-search">Cancel</button></div>`;
    }
    state.searchTimer = setTimeout(pollSearch, 1500);
  }

  function renderSearch() {
    const search = state.search;
    if (!search) return;
    const running = isSearching();
    const progress = Math.max(0, Number(search.progress) || 0);
    const total = Math.max(0, Number(search.total) || 0);
    const checked = Number(search.frames_checked) || progress;
    const totalFrames = Number(search.total_frames) || total;
    const pct = total ? Math.min(100, progress / total * 100) : 0;
    $("search-progress").hidden = !running;
    if (running) $("search-progress").innerHTML = `<div class="search-progress-inner">${icons.spinner}<div class="progress-copy"><strong>${total ? "Finding matching moments" : "Preparing your search"}</strong><span>${total ? `${number(progress)} of ${number(total)} frames checked` : "Preparing your search…"}</span></div><button type="button" class="cancel-search">Cancel</button></div><span class="progress-bar" style="width:${pct}%"></span>`;
    $("results-heading").textContent = "Results";
    $("results-subtitle").textContent = `“${state.lastQuery}”`;
    $("results-subtitle").hidden = !state.lastQuery;
    $("results-summary").hidden = running && !state.results.length;
    const coverage = typeof search.coverage === "number" ? ` · ${percent(search.coverage)}% frame coverage` : "";
    const count = state.results.length;
    $("results-summary").innerHTML = `<span>${count} moment${count === 1 ? "" : "s"}${search.status === "cancelled" ? " · search stopped" : running ? " found so far" : " found"}${search.cached ? ` · ${number(search.cached)} cached checks` : ""}</span><small>${checked || totalFrames ? `${number(checked)} / ${number(totalFrames)} frames checked${coverage}` : ""}</small>`;
    const resultSignature = JSON.stringify(state.results);
    if (resultSignature !== state.resultsSignature) {
      state.resultsSignature = resultSignature;
      $("results-grid").innerHTML = state.results.map((result, index) => {
        const labels = (result.labels || []).map((label) => typeof label === "string" ? label : label.label).filter(Boolean).slice(0, 3);
        const thumbnail = safeMedia(result.thumbnail_url);
        return `<button type="button" class="result-card" data-result="${index}" aria-label="Play ${escape(result.video_name)} from ${time(result.start)} to ${time(result.end)}, ${percent(result.score)} percent match"><div class="result-image">${thumbnail ? `<img src="${escape(thumbnail)}" alt="Matched video frame" loading="lazy">` : `<span class="image-placeholder">${icons.film}</span>`}<span class="result-score">${percent(result.score)}% MATCH</span><span class="result-time">${time(result.start)} — ${time(result.end)}</span><span class="result-play">${icons.play}</span></div><div class="result-copy"><h3 title="${escape(result.video_name)}">${escape(result.video_name || "Video")}</h3><div class="result-labels">${labels.map((label) => `<span>${escape(label)}</span>`).join("")}</div><div class="result-bottom"><span>${number(result.evidence_count || 1)} matching frame${Number(result.evidence_count || 1) === 1 ? "" : "s"}</span><span class="verified">${result.verified ? icons.check + " Jev checked" : "Match"}</span></div></div></button>`;
      }).join("");
    }
    renderEmpty(running ? "searching" : search.status === "failed" ? "failed" : search.status === "cancelled" ? "cancelled" : "none", search.error);
    updateSearchButton();
  }

  function openPlayer(result, wholeVideo = false) {
    const media = safeMedia(result.media_url);
    if (!media) { toast("This video is still being prepared. Try again in a moment."); return; }
    const start = Math.max(0, Number(result.start) || 0);
    const end = Math.max(start, Number(result.end) || 0);
    state.snippet = { ...result, start, end, wholeVideo };
    $("player-title").textContent = result.video_name || "Video";
    $("player-kicker").textContent = wholeVideo ? "YOUR VIDEO" : "MATCHED MOMENT";
    $("player-time").textContent = wholeVideo ? `${time(end)} duration` : `${time(start)} — ${time(end)} · ${percent(result.score)}% match`;
    $("player-evidence").textContent = wholeVideo ? "Preview your original footage." : `${number(result.evidence_count || 1)} matching sampled frame${Number(result.evidence_count || 1) === 1 ? "" : "s"}${result.verified ? " · Checked with Jev" : ""}`;
    $("player-labels").innerHTML = (result.labels || []).map((label) => `<span>${escape(typeof label === "string" ? label : label.label)}</span>`).join("");
    $("loop-snippet").checked = false;
    $("download-snippet").hidden = wholeVideo;
    $("download-snippet").href = `/api/videos/${encodeURIComponent(result.video_id)}/clip?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`;
    $("video-player").src = media;
    $("player-dialog").showModal();
    $("video-player").load();
  }

  $("search-form").addEventListener("submit", startSearch);
  $("all-videos").addEventListener("click", () => { state.selected.clear(); renderLibrary(); });
  $("video-list").addEventListener("change", (event) => { const id = event.target.dataset.select; if (!id) return; event.target.checked ? state.selected.add(id) : state.selected.delete(id); renderLibrary(); });
  $("video-list").addEventListener("click", (event) => {
    const action = event.target.closest("[data-action]");
    if (action) { videoAction(action.dataset.action, action.dataset.id); return; }
    const preview = event.target.closest("[data-preview]");
    if (preview) { const video = state.videos.find((item) => item.id === preview.dataset.preview); if (video) openPlayer({ video_id: video.id, video_name: video.name, media_url: video.media_url, start: 0, end: video.duration, labels: [] }, true); }
  });
  document.querySelectorAll(".upload-trigger").forEach((button) => button.addEventListener("click", () => $("file-input").click()));
  $("file-input").addEventListener("change", () => upload($("file-input").files));
  let dragCounter = 0;
  $("upload-zone").addEventListener("dragenter", (event) => { event.preventDefault(); dragCounter++; $("upload-zone").classList.add("drag-over"); });
  $("upload-zone").addEventListener("dragover", (event) => { event.preventDefault(); if (event.dataTransfer) event.dataTransfer.dropEffect = "copy"; });
  $("upload-zone").addEventListener("dragleave", () => { dragCounter--; if (dragCounter <= 0) $("upload-zone").classList.remove("drag-over"); });
  $("upload-zone").addEventListener("drop", (event) => { event.preventDefault(); dragCounter = 0; $("upload-zone").classList.remove("drag-over"); upload(event.dataTransfer?.files); });
  document.addEventListener("dragover", (event) => event.preventDefault());
  document.addEventListener("drop", (event) => event.preventDefault());
  $("help-toggle").addEventListener("click", () => { $("help-panel").hidden = !$("help-panel").hidden; $("help-toggle").setAttribute("aria-expanded", String(!$("help-panel").hidden)); });
  $("example-queries").addEventListener("click", (event) => { const button = event.target.closest("[data-query]"); if (button) { $("search-query").value = button.dataset.query; $("search-query").focus(); } });
  $("import-toggle").addEventListener("click", () => { $("import-form").hidden = !$("import-form").hidden; $("import-toggle").setAttribute("aria-expanded", String(!$("import-form").hidden)); if (!$("import-form").hidden) $("import-url").focus(); });
  $("import-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const url = $("import-url").value.trim();
    if (!url || $("import-submit").disabled) return;
    try { if (!["http:", "https:"].includes(new URL(url).protocol)) throw new Error(); } catch { toast("Enter a public HTTP or HTTPS video URL.", true); return; }
    $("import-submit").disabled = true;
    $("import-submit").textContent = "Importing…";
    try {
      await api("/api/videos/import", { method: "POST", body: JSON.stringify({ url }) });
      $("import-url").value = "";
      $("import-form").hidden = true;
      $("import-toggle").setAttribute("aria-expanded", "false");
      toast("Import started. Progress appears in your library.");
      await refresh();
    } catch (error) { toast(error.message, true); }
    finally { $("import-submit").disabled = false; $("import-submit").innerHTML = 'Import video <span aria-hidden="true">↗</span>'; }
  });
  $("search-progress").addEventListener("click", async (event) => {
    const button = event.target.closest(".cancel-search");
    if (!button || !state.search?.id) return;
    button.disabled = true;
    button.textContent = "Stopping…";
    try { await api(`/api/search/${encodeURIComponent(state.search.id)}/cancel`, { method: "POST" }); await pollSearch(); }
    catch (error) { toast(error.message, true); button.disabled = false; button.textContent = "Cancel"; }
  });
  $("results-grid").addEventListener("click", (event) => { const card = event.target.closest("[data-result]"); if (card && state.results[Number(card.dataset.result)]) openPlayer(state.results[Number(card.dataset.result)]); });
  $("close-player").addEventListener("click", () => $("player-dialog").close());
  $("player-dialog").addEventListener("click", (event) => { if (event.target === $("player-dialog")) { const rect = $("player-dialog").getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) $("player-dialog").close(); } });
  $("player-dialog").addEventListener("close", () => { $("video-player").pause(); $("video-player").removeAttribute("src"); $("video-player").load(); state.snippet = null; });
  $("video-player").addEventListener("loadedmetadata", () => { if (!state.snippet) return; $("video-player").currentTime = state.snippet.start; $("video-player").play().catch(() => {}); });
  $("video-player").addEventListener("play", () => { const clip = state.snippet; if (clip && !clip.wholeVideo && clip.end > clip.start && ($("video-player").currentTime >= clip.end - 0.1 || $("video-player").currentTime < clip.start)) $("video-player").currentTime = clip.start; });
  $("video-player").addEventListener("timeupdate", () => {
    const clip = state.snippet;
    if (!clip || clip.wholeVideo || clip.end <= clip.start || $("video-player").paused) return;
    if ($("video-player").currentTime >= clip.end) {
      if ($("loop-snippet").checked) { $("video-player").currentTime = clip.start; $("video-player").play().catch(() => {}); }
      else { $("video-player").pause(); $("video-player").currentTime = clip.end; }
    }
  });
  $("video-player").addEventListener("ended", () => { if (state.snippet && $("loop-snippet").checked) { $("video-player").currentTime = state.snippet.start; $("video-player").play().catch(() => {}); } });
  $("video-player").addEventListener("error", () => { if (state.snippet && $("video-player").getAttribute("src")) toast("This video could not be played in your browser. Download the snippet to open it in another player.", true); });
  window.addEventListener("pagehide", () => { clearTimeout(state.refreshTimer); clearTimeout(state.searchTimer); });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) { refresh(); if (isSearching()) pollSearch(); } });
  renderLibrary();
  refresh();
})();
