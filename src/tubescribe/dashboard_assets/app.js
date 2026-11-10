"use strict";

const $ = (id) => document.getElementById(id);
let archive = null;
let rendered = "";
let refreshing = false;
let selectedVideo = null;
let selectedProfile = null;
let readerSignature = "";
let readerController = null;
let readerIdentity = "";
let readerTab = "summary";
let libraryScope = "";
let readerLevel = "video";
let digests = null;
let digestRefreshing = false;
const readingCache = new Map();
const rowCache = new Map();
const feedRecords = new Map();
let signedInUser = null;
let authEnabled = true;
let identityGeneration = 0;
let feedRevision = "";
let feedLimit = 50;
let feedSearch = "";
let feedObserver = null;
let realtimeSettings = {enabled: false};
let realtimeLoader = null;
let liveFeed = null;
let ownsLibrary = true;
let processingSettings = null;
let unprocessedOnly = false;
let processingRecords = null;
let processingRevision = 0;
const pendingProcessing = new Set();
const selectedForProcessing = new Set();
const dateFormatter = new Intl.DateTimeFormat(undefined, {day: "numeric", month: "short", year: "numeric"});
const narrowReader = window.matchMedia("(max-width: 900px)");

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}
function human(value) {
  if (value === "asr") return "ASR";
  if (value === "not_processed") return "Not Processed";
  return value ? (value[0].toUpperCase() + value.slice(1)).replaceAll("_", " ") : "—";
}
function duration(seconds) {
  if (seconds == null) return "";
  const minutes = Math.floor(seconds / 60);
  return minutes >= 60 ? `${Math.floor(minutes / 60)}h ${minutes % 60}m` : `${minutes}m ${Math.floor(seconds % 60)}s`;
}
function parsedDate(value) {
  if (!value) return new Date(NaN);
  const timestamp = /^\d{8}$/.test(String(value))
    ? `${String(value).slice(0, 4)}-${String(value).slice(4, 6)}-${String(value).slice(6, 8)}T00:00:00`
    : /^\d+(\.\d+)?$/.test(String(value)) ? Number(value) * 1000 : value;
  return new Date(timestamp);
}
function date(value, dateOnly = false) {
  if (!value) return "";
  const parsed = parsedDate(value);
  return Number.isNaN(parsed.getTime()) ? String(value)
    : dateOnly ? dateFormatter.format(parsed) : parsed.toLocaleString();
}
function externalLink(label, url, className) {
  const link = node("a", label, className);
  try {
    if (new URL(url).protocol === "https:") {
      link.href = url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
    }
  } catch { /* Unknown source addresses remain plain text. */ }
  return link;
}
function icon(name) {
  const image = node("img", undefined, "icon");
  image.src = `/icons/${name}.svg`;
  image.alt = "";
  return image;
}
function resourceLink(label, url, iconName, className = "") {
  const link = node("a", undefined, className);
  link.href = url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  if (iconName) link.append(icon(iconName));
  link.append(document.createTextNode(label));
  return link;
}
function bestJob(jobs) {
  return [...jobs].sort((a, b) => quality(b) - quality(a)
    || Number(b.job_id || 0) - Number(a.job_id || 0))[0];
}
function quality(job) {
  return job.summary_url ? 4 : job.processed_url ? 3 : job.transcript_url ? 2 : job.job_id != null ? 1 : 0;
}
function displayStage(job) {
  return ["queued", "indexed"].includes(job.stage) && !job.scheduled ? "not_processed" : job.stage;
}
function statusBadge(job, jobs = [job]) {
  const {stage, label} = processingStatus(jobs, job);
  return node("span", label, `badge ${stage}`);
}
function syncSelectedRows() {
  for (const row of document.querySelectorAll(".video-row")) {
    const selected = readerLevel === "video" && row.dataset.video === selectedVideo;
    row.classList.toggle("selected", selected);
    for (const button of row.querySelectorAll("[aria-pressed]")) button.setAttribute("aria-pressed", String(selected));
  }
}
function selectVideo(jobs, job = bestJob(jobs), open = true) {
  readerLevel = "video";
  $("rolling-summary").setAttribute("aria-pressed", "false");
  selectedVideo = jobs[0].video_id;
  selectedProfile = job.profile;
  syncSelectedRows();
  renderReader(jobs, job);
  if (open) setReaderOpen(true);
}
function processingRecord(jobs) {
  return processingRecords?.get(jobs[0].video_id);
}
function processingStatus(jobs, job = bestJob(jobs)) {
  const stage = displayStage(job);
  const record = processingRecord(jobs);
  if (stage !== "complete") {
    if (record?.state === "queued") {
      return {stage: "queued", label: record.retry_at ? "Retry scheduled" : "Queued"};
    }
    if (record?.state === "active" && ["not_processed", "queued", "failed", "interrupted"].includes(stage)) {
      return {stage: "active", label: "Processing"};
    }
    if (["failed", "interrupted"].includes(record?.state)) {
      return {stage: record.state, label: human(record.state)};
    }
  }
  return {stage, label: human(stage)};
}
function canQueue(jobs) {
  const state = processingRecord(jobs)?.state;
  return ownsLibrary && Boolean(processingSettings && processingRecords) &&
    (!state || state === "cancelled") && !jobs.some(job => job.transcript_url || job.scheduled);
}
function canRetry(jobs) {
  return ownsLibrary && Boolean(processingSettings && processingRecords) &&
    ["failed", "interrupted"].includes(processingRecord(jobs)?.state);
}
function transcriptionEstimate(durations) {
  const price = processingSettings?.price_per_second || 0;
  const chunk = processingSettings?.chunk_seconds || Infinity;
  const references = processingSettings?.reference_seconds || 0;
  const base = durations.reduce((sum, seconds) => sum + seconds * price, 0);
  const extra = durations.reduce((sum, seconds) => sum + Math.max(0, Math.ceil(seconds / chunk) - 1) * references * price, 0);
  return `Estimated transcription $${base.toFixed(3)}${extra ? `–$${(base + extra).toFixed(3)} (includes speaker matching)` : ""}`;
}
function updateProcessingEstimate() {
  $("processing-controls").hidden = !ownsLibrary || !processingSettings || !processingRecords;
  const selected = [...selectedForProcessing];
  const durations = selected.map(id => archive?.jobs.find(job => job.video_id === id)?.duration);
  const known = durations.filter(seconds => Number.isFinite(seconds) && seconds > 0);
  const missing = known.length < selected.length ? " · Some durations unknown" : "";
  const cost = known.length ? `${transcriptionEstimate(known)}${missing}` : "Duration unavailable";
  $("processing-estimate").textContent = selected.length ? `${selected.length} selected · ${cost}` : "";
  $("queue-selected").disabled = !selected.length || selected.some(id => pendingProcessing.has(id));
}
async function refreshProcessing() {
  if (!ownsLibrary || !processingSettings || !signedInUser) return;
  const revision = ++processingRevision;
  try {
    const response = await libraryFetch("/api/processing");
    if (!response.ok) throw new Error("Processing status is unavailable. Refresh to retry.");
    const data = await response.json();
    if (revision !== processingRevision) return;
    processingRecords = new Map(data.videos.map(record => [record.video_id, record]));
    for (const id of selectedForProcessing) {
      const jobs = archive?.jobs.filter(job => job.video_id === id) || [];
      if (!jobs.length || !canQueue(jobs)) selectedForProcessing.delete(id);
    }
    renderFeed();
    updateProcessingEstimate();
  } catch (error) {
    if (revision === processingRevision) $("processing-message").textContent = error.message;
  }
}
async function changeProcessing(action, ids) {
  if (!ownsLibrary || !processingSettings || !ids.length || ids.some(id => pendingProcessing.has(id))) return;
  const generation = identityGeneration;
  for (const id of ids) pendingProcessing.add(id);
  $("processing-message").textContent = action === "cancel" ? "Removing from the queue…" : "Queueing videos…";
  updateProcessingEstimate();
  renderFeed();
  try {
    const response = await libraryFetch("/api/processing", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({action, video_ids: ids}),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Could not update the processing queue");
    if (generation !== identityGeneration) return;
    for (const id of ids) selectedForProcessing.delete(id);
    $("processing-message").textContent = result.changed ? `${result.changed} video${result.changed === 1 ? "" : "s"} ${action === "cancel" ? "removed from the queue" : "queued"}.` : "No videos changed. The queue may have already advanced.";
    await refreshProcessing();
  } catch (error) {
    if (generation === identityGeneration) $("processing-message").textContent = error.message;
  } finally {
    if (generation === identityGeneration) {
      for (const id of ids) pendingProcessing.delete(id);
      updateProcessingEstimate();
      renderFeed();
    }
  }
}
function videoRow(jobs) {
  const job = bestJob(jobs);
  const video = jobs[0];
  const row = node("li", undefined, "video-row");
  row.dataset.video = video.video_id;
  row.classList.toggle("selected", video.video_id === selectedVideo);
  row.addEventListener("click", (event) => {
    if (event.defaultPrevented || event.target.closest("a, button, input, select, textarea, details")) return;
    selectVideo(jobs);
  });
  const main = node("div", undefined, "video-main");
  const art = node("button", undefined, "video-art");
  art.setAttribute("aria-label", `Preview ${video.title}`);
  art.dataset.focus = `art-${video.video_id}`;
  art.setAttribute("aria-pressed", String(readerLevel === "video" && video.video_id === selectedVideo));
  const placeholder = node("span", "▶", "video-placeholder");
  placeholder.setAttribute("aria-hidden", "true");
  art.append(placeholder);
  if (job.cover_url) {
    const image = node("img", undefined, "video-thumbnail");
    image.src = job.cover_url;
    image.alt = "";
    image.loading = "lazy";
    image.addEventListener("error", () => image.remove(), {once: true});
    art.append(image);
  }
  if (video.duration != null) art.append(node("span", duration(video.duration), "duration"));
  if (displayStage(job) === "complete") {
    const badge = node("span", "✓", "completion-badge");
    badge.setAttribute("role", "img");
    badge.setAttribute("aria-label", "Complete");
    badge.title = "Complete";
    art.append(badge);
  }
  art.addEventListener("click", () => selectVideo(jobs));
  main.append(art);
  const description = node("div", undefined, "video-description");
  const title = node("button", video.title || video.video_id, "video-title");
  title.dataset.focus = `title-${video.video_id}`;
  title.setAttribute("aria-pressed", String(video.video_id === selectedVideo));
  title.addEventListener("click", () => selectVideo(jobs));
  const channel = video.channel_title || archive.channels.find((source) => source.id === video.channel_id)?.title || video.channel_id;
  const meta = [channel, date(video.published_at, true)].filter(Boolean);
  const bottom = node("div", undefined, "video-bottom");
  description.append(title, node("p", meta.join(" · "), "video-meta"));
  const record = processingRecord(jobs);
  const queueable = canQueue(jobs);
  const retryable = canRetry(jobs);
  const removable = ownsLibrary && processingSettings && record?.state === "queued";
  const pending = pendingProcessing.has(video.video_id);
  if (displayStage(job) !== "complete" || retryable || removable) {
    bottom.append(statusBadge(job, jobs));
    if (queueable) {
      const checkbox = node("input");
      checkbox.type = "checkbox";
      checkbox.dataset.focus = `select-${video.video_id}`;
      checkbox.checked = selectedForProcessing.has(video.video_id);
      checkbox.disabled = pending;
      checkbox.setAttribute("aria-label", `Select ${video.title || video.video_id} for processing`);
      checkbox.addEventListener("change", () => {
        if (checkbox.checked && selectedForProcessing.size >= 50) {
          checkbox.checked = false;
          $("processing-message").textContent = "Select up to 50 videos at a time.";
          return;
        }
        if (checkbox.checked) selectedForProcessing.add(video.video_id);
        else selectedForProcessing.delete(video.video_id);
        updateProcessingEstimate();
      });
      const label = node("label");
      label.append(checkbox, node("span", "Select"));
      bottom.append(label);
    }
    const {stage, label} = processingStatus(jobs, job);
    const busy = pending || removable || ["queued", "active", "downloading", "transcribing",
      "awaiting_diarization", "diarizing", "awaiting_postprocessing", "postprocessing",
      "awaiting_summary", "summarizing"].includes(stage);
    if (busy || queueable || retryable) {
      const actionLabel = busy ? "Processing" : retryable ? "Retry" : "Process";
      const button = node("button", undefined, "button process-button");
      button.dataset.focus = `process-${video.video_id}`;
      button.setAttribute("aria-label", `${actionLabel} ${video.title || video.video_id}`);
      button.disabled = busy;
      if (busy) {
        button.setAttribute("aria-busy", "true");
        button.title = pending ? "Updating processing queue" : label;
        const spinner = node("span", undefined, "process-spinner");
        spinner.setAttribute("aria-hidden", "true");
        button.append(spinner, node("span", "Processing"));
      } else {
        button.textContent = actionLabel;
      }
      if (!busy && Number.isFinite(video.duration) && video.duration > 0) {
        button.title = transcriptionEstimate([video.duration]);
      }
      button.addEventListener("click", () => changeProcessing(retryable ? "retry" : "queue", [video.video_id]));
      bottom.append(button);
    }
    if (removable) {
      const button = node("button", "Remove", "button");
      button.dataset.focus = `remove-${video.video_id}`;
      button.setAttribute("aria-label", `Remove ${video.title || video.video_id} from queue`);
      button.disabled = pending;
      button.addEventListener("click", () => changeProcessing("cancel", [video.video_id]));
      bottom.append(button);
    }
    description.append(bottom);
  }
  const current = jobs.find(job => job.profile === processingSettings?.profile);
  const notes = [];
  if (record?.cost != null) notes.push(`Transcription $${record.cost.toFixed(3)}`);
  if (record?.retry_at) notes.push(`Retries ${date(record.retry_at)}`);
  if (record && current?.transcript_url && current.diarization_state !== "complete") notes.push("Speaker labels unavailable");
  if (notes.length) description.append(node("p", notes.join(" · "), "muted video-processing-note"));
  main.append(description);
  row.append(main);
  return row;
}
function renderFeed() {
  if (!archive) return;
  const search = $("video-search").value.trim().toLowerCase();
  const signature = JSON.stringify([libraryScope, search, archive.channels, archive.jobs, feedRevision, unprocessedOnly, processingRecords && [...processingRecords], [...pendingProcessing], [...selectedForProcessing]]);
  if (signature === rendered) return;
  rendered = signature;
  const focused = document.activeElement?.dataset.focus;
  const allVideos = new Map();
  for (const job of archive.jobs) {
    if (!allVideos.has(job.video_id)) allVideos.set(job.video_id, []);
    allVideos.get(job.video_id).push(job);
  }
  const videos = Array.from(allVideos.values(), (jobs) => ({
    jobs,
    published: parsedDate(jobs[0].published_at).getTime(),
  })).sort((a, b) => {
    const first = Number.isFinite(a.published) ? a.published : -Infinity;
    const second = Number.isFinite(b.published) ? b.published : -Infinity;
    return (first === second ? 0 : second - first) || a.jobs[0].video_id.localeCompare(b.jobs[0].video_id);
  });
  const matches = videos.filter(({jobs}) => (!unprocessedOnly || !jobs.some(job => job.transcript_url || displayStage(job) === "complete")) &&
    (!libraryScope || jobs[0].channel_id === libraryScope) && (!search || jobs.some((job) =>
    `${job.channel_title} ${job.channel_id} ${job.title} ${job.video_id}`.toLowerCase().includes(search))));
  if (!matches.some(({jobs}) => jobs[0].video_id === selectedVideo)) {
    const initial = matches[0]?.jobs;
    selectedVideo = initial?.[0].video_id || null;
    selectedProfile = initial ? bestJob(initial).profile : null;
  }
  if (search !== feedSearch) {
    feedSearch = search;
    feedLimit = 50;
  }
  const feed = $("feed");
  if (!$("feed-list")) {
    const errors = node("div");
    errors.id = "feed-errors";
    const list = node("ul", undefined, "video-list");
    list.id = "feed-list";
    list.setAttribute("aria-label", "Videos, newest first");
    const empty = node("p", undefined, "empty");
    empty.id = "feed-empty";
    const more = node("button", "Load older videos", "feed-more");
    more.id = "feed-more";
    more.addEventListener("click", () => {
      feedLimit += 50;
      rendered = "";
      renderFeed();
    });
    feed.replaceChildren(errors, list, empty, more);
    if ("IntersectionObserver" in window) {
      feedObserver = new IntersectionObserver((entries) => {
        if (entries.some((entry) => entry.isIntersecting) && !more.hidden) more.click();
      }, {rootMargin: "600px"});
      feedObserver.observe(more);
    }
  }
  const list = $("feed-list");
  // Keep a preserved selection in the rendered feed even when newer uploads arrive.
  const selectedIndex = matches.findIndex(({jobs}) => jobs[0].video_id === selectedVideo);
  const visible = matches.slice(0, Math.max(feedLimit, selectedIndex + 1));
  const rows = visible.map(({jobs}) => {
    const id = jobs[0].video_id;
    const signature = JSON.stringify([jobs, ownsLibrary, Boolean(processingSettings && processingRecords), processingRecord(jobs), pendingProcessing.has(id), selectedForProcessing.has(id)]);
    let cached = rowCache.get(id);
    if (!cached || cached.signature !== signature) {
      cached = {signature, row: videoRow(jobs)};
      rowCache.set(id, cached);
    }
    const selected = readerLevel === "video" && id === selectedVideo;
    cached.row.classList.toggle("selected", selected);
    for (const button of cached.row.querySelectorAll("[aria-pressed]")) button.setAttribute("aria-pressed", String(selected));
    return cached.row;
  });
  let cursor = list.firstElementChild;
  for (const row of rows) {
    if (row === cursor) cursor = cursor.nextElementSibling;
    else list.insertBefore(row, cursor);
  }
  while (cursor) {
    const next = cursor.nextElementSibling;
    cursor.remove();
    cursor = next;
  }
  for (const id of rowCache.keys()) if (!allVideos.has(id)) rowCache.delete(id);
  $("feed-empty").hidden = matches.length > 0;
  $("feed-empty").textContent = search ? "No videos match this search. Try a different title or channel." :
    unprocessedOnly ? "No unprocessed videos" : "No videos yet";
  $("feed-more").hidden = visible.length >= matches.length;
  $("feed-more").textContent = `Load older videos (${visible.length} of ${matches.length})`;
  $("result-count").textContent = `${matches.length} video${matches.length === 1 ? "" : "s"}${search ? " found" : ""}`;
  if (focused) Array.from(list.querySelectorAll("[data-focus]")).find((element) => element.dataset.focus === focused)?.focus({preventScroll:true});
  const jobs = allVideos.get(selectedVideo);
  if (readerLevel === "digest") renderDigest();
  else if (jobs) renderReader(jobs, jobs.find((job) => job.profile === selectedProfile) || bestJob(jobs));
  else resetReader();
}
// Rebuild allowed text elements rather than injecting server HTML. Source content cannot
// bring scripts, inline styles, event handlers, or external media into the dashboard.
function copyReadingContent(source, baseUrl) {
  if (source.nodeType === Node.TEXT_NODE) return document.createTextNode(source.textContent);
  if (source.nodeType !== Node.ELEMENT_NODE) return document.createDocumentFragment();
  const tag = source.tagName.toLowerCase();
  if (["script", "style", "iframe", "object", "form", "input", "button", "h1"].includes(tag)) return document.createDocumentFragment();
  const allowed = ["h2", "h3", "h4", "p", "div", "span", "ul", "ol", "li", "strong", "em", "code", "pre", "blockquote", "br", "a", "figure", "figcaption", "img"];
  const target = allowed.includes(tag) ? node(tag) : document.createDocumentFragment();
  if (allowed.includes(tag)) {
    for (const name of ["srt-cues", "srt-cue", "srt-time", "srt-text"]) {
      if (source.classList.contains(name)) target.classList.add(name);
    }
    if (source.hasAttribute("aria-label")) target.setAttribute("aria-label", source.getAttribute("aria-label"));
  }
  if (tag === "a" || tag === "img") {
    try {
      const url = new URL(source.getAttribute(tag === "a" ? "href" : "src"), baseUrl);
      const youtube = tag === "a" && url.protocol === "https:" && ["youtube.com", "www.youtube.com"].includes(url.hostname) && url.pathname === "/watch";
      if ((!youtube && url.origin !== location.origin) || !["http:", "https:"].includes(url.protocol)) {
        if (tag === "img") return document.createDocumentFragment();
      } else if (tag === "a") {
        target.href = url.href;
        target.target = "_blank";
        target.rel = "noopener noreferrer";
      } else {
        target.src = url.href;
        target.alt = source.getAttribute("alt") || "Transcript figure";
        target.loading = "lazy";
      }
    } catch { /* Malformed references remain text. */ }
  }
  for (const child of source.childNodes) target.append(copyReadingContent(child, baseUrl));
  return target;
}
function setReaderOpen(open) {
  const pane = $("reader");
  pane.classList.toggle("is-open", open);
  const modal = open && narrowReader.matches;
  document.body.classList.toggle("reading-modal", modal);
  document.querySelector(".library-column").inert = modal;
  $("channel-sidebar").inert = modal;
  document.querySelector(".app-bar").inert = modal;
  if (modal) {
    pane.setAttribute("role", "dialog");
    pane.setAttribute("aria-modal", "true");
    pane.querySelector(".reader-close button")?.focus();
  } else {
    pane.removeAttribute("role");
    pane.removeAttribute("aria-modal");
  }
}
function backToLibrary() {
  setReaderOpen(false);
  Array.from(document.querySelectorAll(".video-title")).find((button) => button.dataset.focus === `title-${selectedVideo}`)?.focus({preventScroll:true});
}
function resetReader() {
  readerController?.abort();
  const pane = $("reader");
  delete pane.readerView;
  readerSignature = "";
  readerIdentity = "";
  const empty = node("div", undefined, "reader-empty");
  const welcome = node("div", undefined, "reader-welcome");
  const art = node("div", undefined, "reader-welcome-art");
  art.setAttribute("aria-hidden", "true");
  art.append(icon("document-text"), node("span", undefined, "reader-welcome-play"));
  const title = node("h2", "Select a video to start reading");
  title.id = "reader-title";
  const intro = node("p", "Choose a video from your library to revisit the ideas worth keeping.", "reader-welcome-intro");
  const features = node("dl", undefined, "reader-welcome-features");
  for (const [label, description] of [
    ["Summary", "Catch up on the key ideas at a glance."],
    ["Transcript", "Read what was said and jump to a moment with timestamps."],
  ]) {
    const feature = node("div");
    feature.append(node("dt", label), node("dd", description));
    features.append(feature);
  }
  welcome.append(art, node("p", "Your reading space", "eyebrow"), title, intro, features);
  empty.append(welcome);
  pane.replaceChildren(empty);
  setReaderOpen(false);
}
async function renderReader(jobs, job) {
  const tabs = [
    {key: "summary", label: "Summary", url: job.summary_url},
    {key: "transcript", label: "Transcript", url: job.processed_url || job.transcript_url},
    {key: "srt", label: "SRT timestamps", url: job.srt_url},
  ].filter(tab => tab.url);
  tabs.push({key: "pipeline", label: "Pipeline"});
  const identity = job.video_id + "|" + job.profile;
  if (identity !== readerIdentity || !tabs.some(tab => tab.key === readerTab)) readerTab = tabs[0].key;
  readerIdentity = identity;
  const activeTab = tabs.find(tab => tab.key === readerTab);
  const sourceUrl = activeTab.url;
  const status = processingStatus(jobs, job);
  const pane = $("reader");
  const frame = JSON.stringify([identity, readerTab, jobs.map(run => run.profile)]);
  let view = pane.readerView;
  // Discovery has no profile yet. Starting its first job keeps the same panel,
  // but cancels any profile-less detail request before reading the new job.
  if (view && !view.controller.signal.aborted && view.tab === "pipeline" && readerTab === "pipeline"
      && view.job.video_id === job.video_id && !view.job.profile && job.profile && jobs.length === 1) {
    view.frame = frame;
    view.controller.abort();
    view.controller = new AbortController();
    readerController = view.controller;
    view.pipelineRequest = null;
  }
  const fresh = !view || view.frame !== frame || view.controller.signal.aborted;
  const force = !readerSignature;
  if (fresh) {
    readerController?.abort();
    view = {frame, tab: readerTab, job, jobs, tabs: [], tabButtons: new Map(), controller: new AbortController()};
    pane.readerView = view;
    readerController = view.controller;
    const closeBar = node("div", undefined, "reader-close");
    const close = node("button", undefined, "icon-button");
    close.setAttribute("aria-label", "Back to library");
    close.append(icon("x-mark"));
    close.addEventListener("click", backToLibrary);
    closeBar.append(node("span", "Your library"), close);
    view.title = node("h2");
    view.title.id = "reader-title";
    view.meta = node("p", undefined, "reader-meta");
    const statusRow = node("div", undefined, "reader-status-row");
    view.badge = statusBadge(job, jobs);
    statusRow.append(view.badge);
    const parts = [view.title, view.meta, statusRow];
    const readerToolbar = node("div", undefined, "reader-toolbar");
    if (jobs.length > 1) {
      const label = node("label", "Transcript version", "profile-picker");
      const select = node("select");
      select.setAttribute("aria-label", "Transcript version");
      select.replaceChildren(...jobs.map(run => new Option(run.profile, run.profile)));
      select.value = job.profile;
      select.addEventListener("change", () => {
        selectVideo(view.jobs, view.jobs.find(run => run.profile === select.value), false);
        pane.querySelector(".profile-picker select")?.focus({preventScroll: true});
      });
      label.append(select);
      parts.push(label);
    }
    view.chooseTab = key => {
      readerTab = key;
      renderReader(view.jobs, view.job);
      $("reader-tab-" + readerTab)?.focus({preventScroll: true});
    };
    view.youtube = externalLink("", job.url, "document-button");
    view.youtube.title = "View on YouTube";
    view.youtube.setAttribute("aria-label", view.youtube.title);
    const play = node("span", "▶", "youtube-icon");
    play.setAttribute("aria-hidden", "true");
    view.youtube.append(play);
    const actions = node("div", undefined, "reader-actions");
    actions.append(view.youtube);
    const shareStatus = node("div", undefined, "reader-share-status");
    shareStatus.setAttribute("role", "status");
    if (sourceUrl) {
      const tools = node("div", undefined, "reader-document-actions");
      view.fullPage = resourceLink("", sourceUrl, "arrow-top-right-on-square", "document-button");
      view.fullPage.title = "Open " + activeTab.label + " full page";
      view.fullPage.setAttribute("aria-label", view.fullPage.title);
      const share = node("button", undefined, "document-button");
      share.title = "Copy " + activeTab.label + " link";
      share.setAttribute("aria-label", share.title);
      share.append(icon("clipboard-document"));
      share.addEventListener("click", async () => {
        const url = new URL(view.sourceUrl, location.href).href;
        share.disabled = true;
        try {
          await navigator.clipboard.writeText(url);
          shareStatus.textContent = "Link copied.";
        } catch {
          const field = node("input");
          field.type = "url";
          field.readOnly = true;
          field.value = url;
          field.setAttribute("aria-label", "Full-page link");
          shareStatus.replaceChildren(node("span", "Copy this link: "), field);
          field.select();
        } finally {
          share.disabled = false;
        }
      });
      tools.append(view.fullPage, share);
      actions.append(tools);
    }
    view.tabList = node("div", undefined, "reader-tabs");
    view.tabList.setAttribute("role", "tablist");
    view.tabList.setAttribute("aria-label", "Video content");
    const body = node("div", undefined, "reader-body");
    body.id = "reader-panel";
    body.setAttribute("aria-live", readerTab === "pipeline" ? "off" : "polite");
    body.setAttribute("role", "tabpanel");
    body.setAttribute("aria-labelledby", "reader-tab-" + readerTab);
    body.tabIndex = 0;
    view.content = node("div", undefined, "reading-content");
    body.append(view.content);
    view.content.addEventListener("click", event => {
      const link = event.target.closest("a[href]");
      if (!link || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      const url = new URL(link.href);
      const tab = view.tabs.find(tab => tab.url && new URL(tab.url, location.href).href === url.href);
      if (tab) {event.preventDefault(); view.chooseTab(tab.key);}
    });
    readerToolbar.append(view.tabList, actions);
    parts.push(shareStatus, readerToolbar, body);
    const documentView = node("div", undefined, "reader-document");
    documentView.append(...parts);
    pane.replaceChildren(closeBar, documentView);
    pane.scrollTop = 0;
  }
  view.jobs = jobs;
  view.job = job;
  view.status = status;
  view.sourceUrl = sourceUrl;
  setReaderText(view.title, job.title || job.video_id);
  setReaderText(view.meta, [job.channel_title, date(job.published_at, true), duration(job.duration)].filter(Boolean).join(" · "));
  const badge = statusBadge(job, jobs);
  setReaderText(view.badge, badge.textContent);
  view.badge.className = badge.className;
  view.youtube.href = job.url;
  if (view.fullPage) view.fullPage.href = sourceUrl;
  updateReaderTabs(view, tabs);
  if (view.pipelineData) renderPipeline(view.content, view.pipelineData, status);
  // Queue/status updates affect the chrome, never a saved document's cache key.
  const version = readerTab === "pipeline" ? [feedRevision, job] :
    readerTab === "summary" ? [job.summary_updated_at, job.summary_dir] : [job.postprocessing_dir];
  const signature = JSON.stringify([identity, readerTab, sourceUrl, version]);
  if (!fresh && !force && signature === readerSignature) return;
  readerSignature = signature;
  if (readerTab === "pipeline") return refreshReaderPipeline(view, signature);
  if (!fresh) {
    view.controller.abort();
    view.controller = new AbortController();
    readerController = view.controller;
  }
  const controller = view.controller;
  const content = view.content;
  if (!sourceUrl) {
    content.append(node("h3", "The transcript is not ready yet."), node("p", ["failed", "interrupted"].includes(status.stage) ? "The transcript is currently unavailable." : "This video is in your library. Refresh after processing finishes to read its transcript."));
    return;
  }
  if (!view.documentLoaded) content.replaceChildren(node("p", "Loading " + activeTab.label.toLowerCase() + "…", "muted"));
  try {
    const cacheKey = sourceUrl + "|" + signature;
    let documentData = readingCache.get(cacheKey);
    if (!documentData) {
      const response = await libraryFetch(sourceUrl, {signal: controller.signal});
      if (!response.ok) throw new Error("This document is temporarily unavailable.");
      documentData = {text: await response.text(), type: response.headers.get("Content-Type") || ""};
    }
    if (controller.signal.aborted || readerSignature !== signature) return;
    if (documentData.type.includes("text/html")) {
      const documentSource = new DOMParser().parseFromString(documentData.text, "text/html");
      const source = documentSource.querySelector(readerTab === "srt" ? ".srt-cues" : "article");
      if (!source) throw new Error("This document could not be displayed. Use the full-page button to open it.");
      if (readerTab === "summary") {
        for (const link of source.querySelectorAll("a")) {
          if (!/^Full transcript$/i.test(link.textContent.trim())) continue;
          const paragraph = link.closest("p");
          link.remove();
          if (paragraph && !paragraph.textContent.trim() && !paragraph.querySelector("img")) paragraph.remove();
        }
      }
      const baseUrl = new URL(sourceUrl, location.href).href;
      if (readerTab === "srt") content.replaceChildren(copyReadingContent(source, baseUrl));
      else content.replaceChildren(...Array.from(source.children).filter((element) =>
        element.tagName !== "H1" && !(readerTab === "transcript" && element.tagName === "P"
          && /^Source:\s*YouTube\s*$/i.test(element.textContent.trim())))
        .map((element) => copyReadingContent(element, baseUrl)));
    } else {
      content.replaceChildren(node("pre", documentData.text, "raw-transcript"));
    }
    if (!readingCache.has(cacheKey)) {
      readingCache.set(cacheKey, documentData);
      if (readingCache.size > 8) readingCache.delete(readingCache.keys().next().value);
    }
    view.documentLoaded = true;
    if (!content.textContent.trim()) content.append(node("p", "This document has no content."));
  } catch (error) {
    if (controller.signal.aborted || readerSignature !== signature) return;
    content.replaceChildren(node("p", error.message, "muted"));
    const retry = node("button", "Try again");
    retry.addEventListener("click", () => {readerSignature = ""; renderReader(view.jobs, view.job);});
    content.append(retry);
  }
}

function setReaderText(element, value) {
  if (element.textContent !== value) element.textContent = value;
}

function updateReaderTabs(view, tabs) {
  const changed = JSON.stringify(view.tabs) !== JSON.stringify(tabs);
  view.tabs = tabs;
  if (!changed) return;
  const buttons = tabs.map(tab => {
    let button = view.tabButtons.get(tab.key);
    if (!button) {
      button = node("button", tab.label, "reader-tab");
      button.id = "reader-tab-" + tab.key;
      button.setAttribute("role", "tab");
      button.setAttribute("aria-controls", "reader-panel");
      button.addEventListener("click", () => view.chooseTab(tab.key));
      button.addEventListener("keydown", event => {
        const index = view.tabs.findIndex(value => value.key === tab.key);
        let next;
        if (event.key === "ArrowRight") next = (index + 1) % view.tabs.length;
        else if (event.key === "ArrowLeft") next = (index + view.tabs.length - 1) % view.tabs.length;
        else if (event.key === "Home") next = 0;
        else if (event.key === "End") next = view.tabs.length - 1;
        if (next !== undefined) {event.preventDefault(); view.chooseTab(view.tabs[next].key);}
      });
      view.tabButtons.set(tab.key, button);
    }
    button.setAttribute("aria-selected", String(tab.key === readerTab));
    button.tabIndex = tab.key === readerTab ? 0 : -1;
    return button;
  });
  for (let index = 0; index < buttons.length; index++) {
    const current = view.tabList.children[index];
    if (!current) view.tabList.append(buttons[index]);
    else if (current !== buttons[index]) view.tabList.insertBefore(buttons[index], current);
  }
  for (const [key, button] of view.tabButtons) {
    if (!tabs.some(tab => tab.key === key)) {button.remove(); view.tabButtons.delete(key);}
  }
}

function refreshReaderPipeline(view, signature) {
  // A push and a queue response can arrive together. Keep one request in flight,
  // then fetch only the newest pending revision; never blank a saved result.
  view.pipelineNext = signature;
  if (view.pipelineRequest) return view.pipelineRequest;
  if (!view.pipelineData && !view.pipelineError) view.content.replaceChildren(node("p", "Loading pipeline…", "muted"));
  const controller = view.controller;
  const request = Promise.resolve().then(async () => {
    try {
      while (view.pipelineNext && !controller.signal.aborted) {
        view.pipelineNext = null;
        try {
          const query = view.job.profile ? "?profile=" + encodeURIComponent(view.job.profile) : "";
          const response = await libraryFetch("/api/videos/" + encodeURIComponent(view.job.video_id) + "/pipeline" + query,
            {signal: controller.signal});
          if (!response.ok) throw new Error("Pipeline details are temporarily unavailable.");
          const data = await response.json();
          if (controller.signal.aborted) return;
          if (view.pipelineNext) continue;
          view.pipelineData = data;
          renderPipeline(view.content, data, view.status);
          view.pipelineError?.remove();
          view.pipelineError = null;
        } catch (error) {
          if (controller.signal.aborted) return;
          if (view.pipelineNext) continue;
          if (!view.pipelineError) {
            view.pipelineError = node("div", undefined, "pipeline-refresh-error");
            view.pipelineError.setAttribute("role", "status");
            view.pipelineErrorMessage = node("p", undefined, "muted");
            const retry = node("button", "Try again");
            retry.addEventListener("click", () => {readerSignature = ""; renderReader(view.jobs, view.job);});
            view.pipelineError.append(view.pipelineErrorMessage, retry);
            if (!view.pipelineData) view.content.replaceChildren(view.pipelineError);
            else view.content.append(view.pipelineError);
          }
          setReaderText(view.pipelineErrorMessage, error.message);
        }
      }
    } finally {if (view.pipelineRequest === request) view.pipelineRequest = null;}
  });
  view.pipelineRequest = request;
  return request;
}

function renderPipeline(content, data, status) {
  let view = content.pipelineView;
  const shape = JSON.stringify(data.steps.map(step => step.key));
  if (!view || view.shape !== shape) {
    const heading = node("div", undefined, "pipeline-progress");
    const caption = node("div", undefined, "pipeline-progress-caption");
    caption.setAttribute("role", "status");
    caption.setAttribute("aria-live", "polite");
    view = {shape, status: node("span"), count: node("span"), progress: node("progress"),
      stats: [], steps: [], provenance: []};
    view.progress.setAttribute("aria-label", "Video processing progress");
    caption.append(view.status, view.count);
    heading.append(caption, view.progress);
    const stats = node("dl", undefined, "pipeline-stats");
    for (let index = 0; index < 6; index++) {
      const field = {label: node("dt"), value: node("dd")};
      const item = node("div");
      item.append(field.label, field.value);
      stats.append(item);
      view.stats.push(field);
    }
    const steps = node("ol", undefined, "pipeline-steps");
    for (const step of data.steps) {
      const item = node("li", undefined, "pipeline-step");
      const title = node("div", undefined, "pipeline-step-heading");
      const field = {label: node("strong"), badge: node("span"), description: node("p"),
        retry: node("p", undefined, "pipeline-step-meta")};
      title.append(field.label, field.badge);
      item.append(title, field.description, field.retry);
      steps.append(item);
      view.steps.push(field);
    }
    const provenance = node("dl", undefined, "pipeline-provenance");
    for (const label of ["Profile", "Transcription model", "Language", "First recorded attempt", "Finished"]) {
      const field = {label: node("dt", label), value: node("dd")};
      provenance.append(field.label, field.value);
      view.provenance.push(field);
    }
    content.replaceChildren(heading, stats, steps, provenance);
    content.pipelineView = view;
  }
  const money = value => value == null ? "Not recorded" : "$" + value.toFixed(4);
  const elapsed = value => value == null ? "Not recorded" : value < 60 ? value.toFixed(1) + "s" : duration(value);
  const count = value => value == null ? "Not recorded" : value.toLocaleString();
  // Hosted speaker attribution happens inside transcription, not as another
  // completed phase before downloading has even started.
  const required = data.steps.filter(step => step.state !== "included");
  const completed = required.filter(step => step.state === "complete").length;
  const progressText = completed + " of " + required.length + " steps complete";
  setReaderText(view.status, data.complete ? "Pipeline complete" : status.label);
  setReaderText(view.count, progressText);
  view.progress.max = Math.max(required.length, 1);
  view.progress.value = completed;
  view.progress.setAttribute("aria-valuetext", (data.complete ? "Pipeline complete. " : status.label + ". ") + progressText);
  const stats = [
    [data.cost_complete ? "Processing cost" : data.complete ? "Processing cost · partial" : "Processing cost so far", money(data.cost_usd)],
    ["Processing time", elapsed(data.processing_seconds)],
    ["Video duration", data.duration_seconds == null ? "Unknown" : duration(data.duration_seconds)],
    ["Recorded tokens", count(data.total_tokens)],
    ["Speakers", count(data.speakers)], ["Figures", count(data.figures)],
  ];
  stats.forEach(([label, value], index) => {
    setReaderText(view.stats[index].label, label);
    setReaderText(view.stats[index].value, value);
  });
  const descriptions = {
    download: "Downloads the video's audio for transcription.",
    transcription: "Turns speech into text with word-level timestamps.",
    diarization: "Identifies speakers and labels their turns in the conversation.",
    postprocessing: "Cleans up the transcript and adds speaker and visual context.",
    summary: "Creates a concise summary of the video's main ideas.",
  };
  data.steps.forEach((step, index) => {
    const field = view.steps[index];
    setReaderText(field.label, step.label);
    setReaderText(field.badge, ({pending: "Waiting", active: "In progress", included: "Included"})[step.state] || human(step.state));
    field.badge.className = "badge " + step.state;
    setReaderText(field.description, step.state === "included" ? "Identifies speakers as part of transcription." :
      descriptions[step.key] || step.note || "Processes this part of the video.");
    field.retry.hidden = !step.retry_at;
    setReaderText(field.retry, step.retry_at ? "Retry scheduled: " + date(step.retry_at) : "");
  });
  [data.profile, data.model, data.language, date(data.started_at), date(data.completed_at)].forEach((value, index) => {
    const field = view.provenance[index];
    field.label.hidden = field.value.hidden = !value;
    setReaderText(field.value, value || "");
  });
}

function applyFeed(data) {
  if (data.full) feedRecords.clear();
  else if (!archive) throw new Error("The library needs a fresh snapshot.");
  for (const key of data.removed || []) feedRecords.delete(key);
  for (const job of data.jobs) feedRecords.set(job.key, job);
  archive = {
    channels: data.channels,
    summary: data.summary,
    generated_at: data.generated_at,
    jobs: Array.from(feedRecords.values()),
  };
  feedRevision = data.revision;
}
function showArchive(cached = false) {
  $("archive-summary").textContent = `${archive.summary.channels} channel${archive.summary.channels === 1 ? "" : "s"} · ${archive.summary.videos} video${archive.summary.videos === 1 ? "" : "s"}`;
  $("worker-status").textContent = cached ? "Checking for updates…" : "Library up to date";
  $("updated").textContent = cached ? "Cached library" : `Updated ${new Date(archive.generated_at).toLocaleTimeString()}`;
  syncScopes();
  renderFeed();
  void refreshProcessing();
}
function loadRealtime() {
  if (window.TubeScribeRealtime?.connect) return Promise.resolve(window.TubeScribeRealtime);
  if (!realtimeLoader) {
    realtimeLoader = new Promise((resolve) => {
      const script = document.createElement("script");
      const unavailable = () => {
        script.remove();
        realtimeLoader = null;
        resolve(null);
      };
      script.src = "/realtime.js";
      script.onload = () => {
        if (window.TubeScribeRealtime?.connect) resolve(window.TubeScribeRealtime);
        else unavailable();
      };
      script.onerror = unavailable;
      document.head.append(script);
    });
  }
  return realtimeLoader;
}
function showLiveUnavailable() {
  $("worker-status").textContent = "Live connection unavailable";
  $("error").textContent = "Live updates unavailable. Refresh to reconnect. Your last snapshot is preserved.";
  $("error").classList.remove("hidden");
}
async function refresh(event) {
  if (refreshing || !signedInUser) return;
  if (readerTab === "pipeline") {
    readerSignature = "";
    rendered = "";
    const view = $("reader").readerView;
    if (view && !view.controller.signal.aborted) void renderReader(view.jobs, view.job);
  }
  if (realtimeSettings.enabled && ownsLibrary) {
    const generation = identityGeneration;
    const pendingRealtime = loadRealtime();
    if (!archive || (event?.type === "click" && (!liveFeed || liveFeed.status === "error"))) {
      await refreshSnapshot();
    }
    const realtime = await pendingRealtime;
    if (generation !== identityGeneration) return;
    if (!realtime) {
      if (archive) showLiveUnavailable();
      return;
    }
    if (liveFeed && liveFeed.status !== "error") {
      await liveFeed.wake();
      if (event?.type === "click") refreshDigests();
      return;
    }
    liveFeed?.close();
    let initial = !archive;
    liveFeed = realtime.connect({
      getCapabilities: async () => {
        const response = await libraryFetch(realtimeSettings.endpoint);
        if (!response.ok) {
          throw Object.assign(new Error("Live updates are unavailable. Refresh to retry."),
            {code: "authorization_http_" + response.status});
        }
        return response.json();
      },
      onError: failure => console.warn("TubeScribe live update failure " + JSON.stringify(failure)),
      getFeed: async () => {
        const response = await libraryFetch(`/api/feed${feedRevision ? "?revision=" + encodeURIComponent(feedRevision) : ""}`);
        const data = await response.json();
        if (!response.ok) throw new Error("The library update is unavailable");
        return data;
      },
      onFeed: data => {
        if (generation !== identityGeneration) return;
        applyFeed(data);
        $("error").classList.add("hidden");
        showArchive();
        $("worker-status").textContent = liveFeed.status === "live" ? "Library live" : "Reconnecting…";
        if (initial) {initial = false; refreshDigests();}
      },
      onState: state => {
        if (generation !== identityGeneration) return;
        if (state === "error") showLiveUnavailable();
        else $("worker-status").textContent = state === "live" ? "Library live" : "Reconnecting…";
      },
    });
    await liveFeed.start();
    return;
  }
  await refreshSnapshot();
}
function showLibraryLoading() {
  const feed = $("feed");
  if (feed.firstElementChild?.classList.contains("feed-loading")) return;
  const status = node("p", undefined, "empty feed-loading");
  status.setAttribute("role", "status");
  const spinner = node("span", undefined, "loading-spinner");
  spinner.setAttribute("aria-hidden", "true");
  status.append(spinner, node("span", "Loading your videos…"));
  feed.replaceChildren(status);
}
async function refreshSnapshot() {
  const generation = identityGeneration;
  refreshing = true;
  $("refresh").disabled = true;
  if (!archive) {
    showLibraryLoading();
    $("error").classList.add("hidden");
  }
  try {
    const response = await libraryFetch(`/api/feed${feedRevision ? "?revision=" + encodeURIComponent(feedRevision) : ""}`);
    const data = await response.json();
    if (generation !== identityGeneration) return;
    if (!response.ok) throw new Error(data.error || "Archive unavailable");
    applyFeed(data);
    $("error").classList.add("hidden");
    showArchive();
    refreshDigests();
  } catch (error) {
    if (generation !== identityGeneration) return;
    $("error").textContent = `${error.message}. ${archive ? "Showing the last available snapshot." : "Could not load the archive."} Refresh will retry.`;
    $("error").classList.remove("hidden");
    $("updated").textContent = "Connection unavailable";
    $("worker-status").textContent = "Library unavailable";
    if (!archive) $("feed").replaceChildren(node("p", "Videos are unavailable until the archive reconnects.", "empty"));
  } finally {
    if (generation === identityGeneration) {
      refreshing = false;
      $("refresh").disabled = false;
    }
  }
}
document.addEventListener("keydown", (event) => {
  if (!narrowReader.matches || !$("reader").classList.contains("is-open")) return;
  if (event.key === "Escape") backToLibrary();
  if (event.key === "Tab") {
    const controls = Array.from($("reader").querySelectorAll('a[href], button:not([disabled]), select'));
    const first = controls[0];
    const last = controls[controls.length - 1];
    if (event.shiftKey && document.activeElement === first) {event.preventDefault(); last?.focus();}
    else if (!event.shiftKey && document.activeElement === last) {event.preventDefault(); first?.focus();}
  }
});
narrowReader.addEventListener("change", () => setReaderOpen($("reader").classList.contains("is-open")));
$("video-search").addEventListener("input", () => {
  readerLevel = "video";
  $("rolling-summary").setAttribute("aria-pressed", "false");
  rendered = "";
  renderFeed();
});
$("refresh").addEventListener("click", refresh);
// Refresh on entry, explicit request, or returning to the tab; no background polling.
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && signedInUser) {
    if (realtimeSettings.enabled && ownsLibrary) liveFeed?.wake();
    else refresh();
  }
});
window.addEventListener("pagehide", () => {liveFeed?.close(); liveFeed = null;});
window.addEventListener("pageshow", event => {if (event.persisted && signedInUser) refresh();});

function syncScopes() {
  const picker = $("channel-scope");
  const signature = JSON.stringify(archive.channels.map((c) => [c.id, c.title]));
  if (picker.dataset.signature === signature) return;
  picker.dataset.signature = signature;
  picker.replaceChildren(new Option("All channels", ""), ...archive.channels.map((c) => new Option(c.title, c.id)));
  if (!archive.channels.some((c) => c.id === libraryScope)) libraryScope = "";
  picker.value = libraryScope;
  const makeChannel = (id, title) => {
    const button = node("button", undefined, "channel-link");
    button.type = "button";
    button.dataset.channel = id;
    button.title = title;
    const mark = node("span", undefined, "channel-mark");
    mark.setAttribute("aria-hidden", "true");
    if (id) mark.textContent = title.trim().slice(0, 1).toUpperCase();
    else mark.append(icon("document-text"));
    button.append(mark, node("span", title, "channel-name"));
    button.addEventListener("click", () => chooseScope(id));
    return button;
  };
  $("library-navigation").replaceChildren(makeChannel("", "All videos"));
  $("channel-navigation").replaceChildren(...archive.channels.map(c => makeChannel(c.id, c.title)));
  syncChannelSelection();
}
function syncChannelSelection() {
  for (const button of $("channel-sidebar").querySelectorAll("[data-channel]")) {
    button.setAttribute("aria-current", button.dataset.channel === libraryScope ? "page" : "false");
  }
}
function chooseScope(channel = "", overview = false) {
  libraryScope = channel;
  $("channel-scope").value = channel;
  syncChannelSelection();
  feedLimit = 50;
  readerLevel = overview ? "digest" : "video";
  readerSignature = "";
  rendered = "";
  $("rolling-summary").setAttribute("aria-pressed", String(overview));
  renderFeed();
}
function digestBreadcrumbs(channel) {
  const nav = node("nav", undefined, "digest-breadcrumbs");
  nav.setAttribute("aria-label", "Summary levels");
  const all = node("button", "All channels");
  all.addEventListener("click", () => chooseScope("", true));
  nav.append(all);
  if (channel) {
    const name = archive?.channels.find((c) => c.id === channel)?.title || channel;
    const button = node("button", name);
    button.addEventListener("click", () => chooseScope(channel, true));
    nav.append(node("span", "/"), button);
  }
  return nav;
}
async function refreshDigests() {
  if (digestRefreshing || !signedInUser) return;
  const generation = identityGeneration;
  digestRefreshing = true;
  try {
    const response = await libraryFetch("/api/summaries");
    if (!response.ok) throw new Error("Seven-day summaries are temporarily unavailable.");
    const updatedDigests = await response.json();
    if (generation !== identityGeneration) return;
    digests = updatedDigests;
    if (readerLevel === "digest") renderDigest();
  } catch (error) {
    if (generation !== identityGeneration) return;
    if (readerLevel === "digest") {
      readerSignature = "";
      const heading = node("h2", "Seven-day summary");
      heading.id = "reader-title";
      $("reader").replaceChildren(heading, node("p", error.message, "error"));
    }
  } finally { digestRefreshing = false; }
}
async function renderDigest() {
  const scope = libraryScope ? "channel:" + libraryScope : "all";
  const entry = digests?.items.find((e) => e.scope === scope);
  if (!entry) {
    if (!digests) $("reader").replaceChildren(node("h2", "Seven-day summary"), node("p", "Loading summaries…"));
    return;
  }
  const signature = JSON.stringify([scope, entry, digests.waiting_for_videos]);
  if (readerSignature === signature) return;
  readerSignature = signature;
  readerController?.abort();
  const controller = new AbortController();
  readerController = controller;
  const title = node("h2", entry.title);
  title.id = "reader-title";
  const closeBar = node("div", undefined, "reader-close");
  const close = node("button", undefined, "icon-button");
  close.setAttribute("aria-label", "Back to library");
  close.append(icon("x-mark"));
  close.addEventListener("click", backToLibrary);
  closeBar.append(node("span", "Seven-day summary"), close);
  const coverage = entry.coverage;
  const meta = node("p", `Last 7 days · ${date(digests.since, true)} – ${date(digests.until, true)}`, "reader-meta");
  const note = node("p", `${coverage.summarized} of ${coverage.videos} indexed videos summarized${coverage.undated ? ` · ${coverage.undated} undated videos excluded` : ""}.`, "digest-coverage");
  const body = node("div", undefined, "reader-body");
  const pane = $("reader");
  pane.replaceChildren(closeBar, digestBreadcrumbs(libraryScope), node("p", "ROLLING OVERVIEW", "eyebrow"), title, meta, note, body);
  pane.scrollTop = 0;
  const browse = node("div", undefined, "digest-browse");
  if (!libraryScope) {
    browse.append(node("h3", "Explore channels"));
    for (const channel of archive?.channels || []) {
      const button = node("button", channel.title, "button");
      button.addEventListener("click", () => chooseScope(channel.id, true));
      browse.append(button);
    }
  } else {
    browse.append(node("h3", "Videos in this window"));
    const seen = new Set();
    const recent = (archive?.jobs || []).filter((job) => {
      const stamp = parsedDate(job.published_at).getTime();
      if (seen.has(job.video_id) || job.channel_id !== libraryScope || stamp < Date.parse(digests.since) || stamp > Date.parse(digests.until) || !Number.isFinite(stamp)) return false;
      seen.add(job.video_id);
      return true;
    }).sort((a, b) => parsedDate(b.published_at) - parsedDate(a.published_at));
    for (const job of recent) {
      const button = node("button", job.title, "digest-video");
      button.addEventListener("click", () => selectVideo(archive.jobs.filter((j) => j.video_id === job.video_id)));
      browse.append(button);
    }
    if (!recent.length) browse.append(node("p", "No indexed videos were published in this window.", "muted"));
  }
  pane.append(browse);
  if (entry.state !== "ready") {
    body.append(node("p", "The current seven-day summary is not available yet.", "muted"));
    return;
  }
  const actions = node("div", undefined, "reader-actions");
  const full = node("a", undefined, "document-button");
  full.href = entry.url;
  full.target = "_blank";
  full.rel = "noopener noreferrer";
  full.title = "Open summary full page";
  full.setAttribute("aria-label", full.title);
  full.append(icon("arrow-top-right-on-square"));
  const share = node("button", undefined, "document-button");
  share.title = "Copy summary link";
  share.setAttribute("aria-label", share.title);
  share.append(icon("clipboard-document"));
  const status = node("span", "", "reader-share-status");
  status.setAttribute("role", "status");
  share.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(new URL(entry.url, location.href).href); status.textContent = "Link copied."; }
    catch { status.textContent = new URL(entry.url, location.href).href; }
  });
  actions.append(full, share, status);
  body.append(actions);
  const content = node("div");
  body.append(content);
  try {
    const key = entry.url + entry.input_hash + entry.generated_at;
    let text = readingCache.get(key)?.text;
    if (!text) {
      const response = await libraryFetch(entry.url, {signal: controller.signal});
      if (!response.ok) throw new Error("This summary is being refreshed. It will appear automatically.");
      text = await response.text();
      readingCache.set(key, {text});
      if (readingCache.size > 8) readingCache.delete(readingCache.keys().next().value);
    }
    if (controller.signal.aborted || readerSignature !== signature || readerLevel !== "digest") return;
    const article = new DOMParser().parseFromString(text, "text/html").querySelector("article");
    if (!article) throw new Error("This summary could not be displayed.");
    content.replaceChildren(...Array.from(article.children).filter((e) => e.tagName !== "H1").map((e) => copyReadingContent(e, new URL(entry.url, location.href).href)));
    content.addEventListener("click", (event) => {
      const anchor = event.target.closest("a");
      if (!anchor) return;
      const target = new URL(anchor.href);
      if (target.origin !== location.origin) return;
      const channel = target.pathname.match(/^\/channels\/([^/]+)\/summary\/$/);
      const video = target.pathname.match(/^\/videos\/([^/]+)\/transcripts\/([^/]+)\/artifacts\/summary\/$/);
      if (channel) { event.preventDefault(); chooseScope(decodeURIComponent(channel[1]), true); }
      else if (video) {
        const jobs = archive.jobs.filter((j) => j.video_id === decodeURIComponent(video[1]));
        if (jobs.length) { event.preventDefault(); selectVideo(jobs, jobs.find((j) => j.profile === decodeURIComponent(video[2])) || bestJob(jobs)); }
      }
    });
    body.append(node("p", `Generated ${new Date(entry.generated_at).toLocaleString()} from saved summaries. Speaker opinions and forecasts retain their original uncertainty.`, "reader-note"));
  } catch (error) {
    if (!controller.signal.aborted) { content.textContent = error.message; readerSignature = ""; }
  }
}
$("channel-scope").addEventListener("change", (event) => chooseScope(event.target.value));
$("rolling-summary").addEventListener("click", () => { chooseScope(libraryScope, true); setReaderOpen(true); });
$("unprocessed-filter").addEventListener("click", () => {
  unprocessedOnly = !unprocessedOnly;
  $("unprocessed-filter").setAttribute("aria-pressed", String(unprocessedOnly));
  feedLimit = 50;
  renderFeed();
});
$("queue-selected").addEventListener("click", () => changeProcessing("queue", [...selectedForProcessing]));

function clearPrivateLibrary() {
  identityGeneration += 1;
  refreshing = false;
  liveFeed?.close();
  liveFeed = null;
  ownsLibrary = true;
  unprocessedOnly = false;
  processingRecords = null;
  processingRevision += 1;
  pendingProcessing.clear();
  selectedForProcessing.clear();
  $("processing-controls").hidden = true;
  $("processing-message").textContent = "";
  $("unprocessed-filter").setAttribute("aria-pressed", "false");
  readerController?.abort();
  feedObserver?.disconnect();
  readingCache.clear(); rowCache.clear(); feedRecords.clear();
  archive = null; digests = null; feedRevision = ""; rendered = "";
  selectedVideo = null; selectedProfile = null; libraryScope = "";
  readerSignature = ""; readerIdentity = ""; readerLevel = "video";
  $("rolling-summary").setAttribute("aria-pressed", "false");
  feedLimit = 50; feedSearch = "";
  $("video-search").value = "";
  $("channel-scope").replaceChildren(new Option("All channels", ""));
  $("library-navigation").replaceChildren();
  $("channel-navigation").replaceChildren();
  delete $("channel-scope").dataset.signature;
  $("archive-summary").textContent = "Your private library";
  $("updated").textContent = "";
  $("result-count").textContent = "";
  $("error").classList.add("hidden");
  $("feed").replaceChildren();
  resetReader();
  // Discard the former anonymous feed cache. Private data stays in memory only.
  try { sessionStorage.removeItem("tubescribe.feed.v2"); } catch { /* Storage disabled. */ }
}
async function libraryFetch(url, options = {}) {
  const generation = identityGeneration;
  if (!signedInUser) throw new Error("Sign in to read your library");
  const headers = new Headers(options.headers);
  if (authEnabled) {
    const token = await window.Clerk.session?.getToken();
    if (!token) throw new Error("Your session has ended. Please sign in again");
    headers.set("Authorization", `Bearer ${token}`);
  }
  const response = await fetch(url, {...options, headers, cache: "no-store", credentials: "same-origin"});
  if (generation !== identityGeneration) throw new DOMException("Account changed", "AbortError");
  if (response.status === 401) {
    await syncIdentity({user: null, session: null});
    $("auth-message").textContent = "Your session has ended. Please sign in again.";
    throw new Error("Sign in required");
  }
  return response;
}
// Older documents may finish loading across a deployment.
let setSignInVisible = visible => {
  if (visible) {
    window.Clerk.mountSignIn($("sign-in"), {routing: "hash", forceRedirectUrl: "/" + window.location.search});
  } else {
    window.Clerk.unmountSignIn($("sign-in"));
  }
};
async function syncIdentity({user, session}) {
  const next = user && session ? user.id : null;
  if (next === signedInUser) return;
  signedInUser = next;
  clearPrivateLibrary();
  const generation = identityGeneration;
  if (next) showLibraryLoading();
  $("refresh").disabled = Boolean(next);
  document.querySelector(".workspace").hidden = !next;
  $("auth-panel").hidden = Boolean(next);
  $("library-status").hidden = !next;
  if (!next) {
    window.Clerk.unmountUserButton($("user-button"));
    $("worker-status").textContent = "";
    $("auth-message").textContent = "";
    setSignInVisible(true);
    return;
  }
  setSignInVisible(false);
  window.Clerk.mountUserButton($("user-button"));
  try {
    const response = await libraryFetch("/api/session");
    const verified = await response.json();
    if (generation !== identityGeneration) return;
    if (!response.ok || verified.user_id !== next) throw new Error("Could not verify your account. Refresh to retry.");
    ownsLibrary = verified.has_library;
    const destination = new URLSearchParams(window.location.search).get("return_to");
    if (destination && destination.startsWith("/") && !destination.startsWith("//")) {
      const target = new URL(destination, window.location.origin);
      if (target.origin === window.location.origin && target.pathname !== "/") {
        window.location.replace(target.href);
        return;
      }
    }
    await refresh();
  } catch (error) {
    if (generation !== identityGeneration) return;
    if (!archive) $("feed").replaceChildren();
    $("refresh").disabled = false;
    $("error").textContent = error.message;
    $("error").classList.remove("hidden");
  }
}
async function initializeAuthentication() {
  clearPrivateLibrary();
  try {
    const bootstrap = document.querySelector('meta[name="tubescribe-config"]');
    let settings;
    if (bootstrap) {
      settings = JSON.parse(bootstrap.content);
    } else {
      const response = await fetch("/auth/config", {cache: "no-store"});
      if (!response.ok) throw new Error("Sign-in configuration is unavailable. Refresh to retry.");
      settings = await response.json();
    }
    authEnabled = settings.enabled;
    processingSettings = settings.processing || null;
    realtimeSettings = settings.realtime || {enabled: false};
    if (!authEnabled) {
      signedInUser = "local";
      $("auth-panel").hidden = true;
      $("library-status").hidden = false;
      document.querySelector(".workspace").hidden = false;
      await refresh();
      return;
    }
    // An older document may have fetched this reader across a deployment.
    if (!window.tubeScribeClerkReady) {
      await new Promise((resolve, reject) => {
        const script = document.createElement("script");
        script.src = "/clerk-bootstrap.js";
        script.onload = resolve;
        script.onerror = () => reject(new Error("Sign-in could not load. Refresh to retry."));
        document.head.append(script);
      });
    }
    setSignInVisible = (await window.tubeScribeClerkReady) ?? setSignInVisible;
    // Force initial signed-out rendering, then clear every cache on account changes.
    signedInUser = undefined;
    window.Clerk.addListener((state) => { void syncIdentity(state); });
  } catch (error) {
    $("auth-message").textContent = error.message;
    $("worker-status").textContent = "Sign-in unavailable";
  }
}
void initializeAuthentication();
