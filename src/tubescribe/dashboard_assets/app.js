"use strict";

const $ = (id) => document.getElementById(id);
let archive = null;
let page = 0;
let currentView = "overview";
let refreshing = false;
let previewRequest = 0;
let detailKey = null;
const pageSize = 20;
const titles = {
  overview: ["Archive overview", "A live view of your channels, queue, and saved transcripts."],
  videos: ["Video library", "Track every detected video from discovery through saved outputs."],
  channels: ["Registered channels", "Channel history, discovery results, and import progress."],
  files: ["Archive files", "Browse transcript formats, review artifacts, and retained audio."],
};

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}
function replace(id, children) { $(id).replaceChildren(...children); }
function badge(text, status) { return node("span", text, `badge ${status || text}`); }
function humanStage(value) { return value ? value[0].toUpperCase() + value.slice(1) : "—"; }
function duration(seconds) {
  if (seconds == null) return "—";
  const minutes = Math.floor(seconds / 60);
  return minutes >= 60 ? `${Math.floor(minutes / 60)}h ${minutes % 60}m` : `${minutes}m`;
}
function bytes(size) {
  if (size < 1024) return `${size} B`;
  if (size < 1048576) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1048576).toFixed(1)} MB`;
}
function date(value) {
  if (!value) return "—";
  const timestamp = /^\d{8}$/.test(String(value))
    ? `${String(value).slice(0,4)}-${String(value).slice(4,6)}-${String(value).slice(6,8)}`
    : /^\d+(\.\d+)?$/.test(String(value)) ? Number(value) * 1000 : value;
  const parsed = new Date(timestamp);
  return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleString();
}
function externalLink(label, url) {
  const a = node("a", label);
  try {
    if (new URL(url).protocol === "https:") {
      a.href = url; a.target = "_blank"; a.rel = "noopener noreferrer";
    }
  } catch { /* Unknown source addresses remain plain text. */ }
  return a;
}
function download(file, label = "Download ↓") {
  const a = node("a", label, "button");
  a.href = `${file.url}&download=1`;
  a.download = file.name;
  return a;
}
function fileButtons(files) {
  const group = node("div", undefined, "output-links");
  for (const file of files) {
    if (file.previewable) {
      const button = node("button", file.format, "file-button");
      button.title = `Preview ${file.path}`;
      button.addEventListener("click", () => preview(file));
      group.append(button);
    } else { group.append(download(file, "Audio ↓")); }
  }
  if (!files.length) group.append(node("span", "Not saved yet", "muted"));
  return group;
}
function setView(view) {
  if (!titles[view]) view = "overview";
  currentView = view;
  for (const key of Object.keys(titles)) $(`${key}-view`).classList.toggle("hidden", key !== view);
  document.querySelectorAll("[data-view]").forEach((el) => {
    el.classList.toggle("active", el.dataset.view === view);
    el.setAttribute("aria-current", el.dataset.view === view ? "page" : "false");
  });
  $("breadcrumb").textContent = humanStage(view);
  $("page-title").textContent = titles[view][0];
  $("page-description").textContent = titles[view][1];
  history.replaceState(null, "", `#${view}`);
  if (archive) renderView();
}
function showVideos(channel = "", status = "") {
  $("channel-filter").value = channel;
  $("stage-filter").value = status;
  $("video-search").value = "";
  $("profile-filter").value = "";
  page = 0; setView("videos");
}
function options(id, items, title) {
  const select = $(id);
  const signature = JSON.stringify(items);
  if (select.dataset.signature === signature) return;
  const value = select.value;
  select.replaceChildren(new Option(title, ""), ...items.map(([value, label]) => new Option(label, value)));
  select.value = value;
  select.dataset.signature = signature;
}
function renderStats() {
  const s = archive.summary;
  const processing = s.downloading + s.transcribing;
  const items = [
    ["Detected videos", s.videos, `${s.channels} registered channel${s.channels === 1 ? "" : "s"}`, ""],
    ["Queued", s.queued, `${s.failed} failed · inspect Videos for details`, "queued"],
    ["Processing", processing, `${s.downloading} downloading · ${s.transcribing} transcribing`, "processing"],
    ["Completed jobs", s.complete, `${s.jobs ? Math.round(s.complete / s.jobs * 100) : 0}% of ${s.jobs} indexed jobs`, "complete"],
  ];
  replace("stats", items.map(([label, value, note, status]) => {
    const card = node("div", undefined, "stat");
    card.append(node("div", label, "stat-label"), node("div", value.toLocaleString(), `stat-number ${status === "complete" ? "stat-accent" : ""}`), node("div", note, "stat-note"));
    return card;
  }));
  $("nav-videos").textContent = s.videos;
  $("nav-channels").textContent = s.channels;
  $("nav-files").textContent = s.files;
}
function channelCard(channel, full = false) {
  const card = node("div", undefined, full ? "panel" : "channel-row");
  const body = full ? node("div", undefined, "channel-row") : card;
  const line = node("div", undefined, "channel-line");
  const names = node("div");
  names.append(node("div", channel.title, "channel-name"), externalLink("YouTube channel ↗", channel.url));
  line.append(node("span", channel.title.split(/\s+/).slice(0,2).map((w) => w[0]).join(""), "channel-initial"), names);
  const counts = node("div", undefined, "channel-counts");
  for (const [label, value] of [["videos", channel.videos], ["completed jobs", channel.complete], ["failed", channel.failed]]) {
    const count = node("div"); count.append(node("strong", value), node("span", label)); counts.append(count);
  }
  const button = node("button", "Browse videos →", "channel-button");
  button.addEventListener("click", () => showVideos(channel.id));
  body.append(line, counts, button);
  if (full) {
    card.append(body);
    const discovery = node("div", undefined, "discovery");
    discovery.append(node("div", channel.id, "channel-id"));
    for (const kind of ["upload", "short", "stream"]) {
      const info = channel.discovery.find((d) => d.kind === kind);
      const row = node("div", undefined, "discovery-row");
      row.append(node("span", {upload:"Uploads", short:"Shorts", stream:"Livestreams"}[kind]), badge(info ? `${info.indexed_count} · ${info.state}` : "Not synced", info?.state));
      discovery.append(row);
      if (info?.error) discovery.append(node("div", info.error, "detail-error"));
      if (info?.updated_at) discovery.append(node("div", `Last checked ${date(info.updated_at)}`, "muted"));
    }
    card.append(discovery);
  }
  return card;
}
function renderOverview() {
  const s = archive.summary;
  const ratio = s.jobs ? s.complete / s.jobs : 0;
  const progress = node("div", undefined, "pipeline-progress");
  const label = node("div", undefined, "progress-label");
  label.append(node("span", "Archive completion"), node("strong", `${s.complete} / ${s.jobs} jobs`));
  const track = node("div", undefined, "progress-track");
  track.setAttribute("role", "progressbar"); track.setAttribute("aria-label", "Completed jobs");
  track.setAttribute("aria-valuenow", s.complete); track.setAttribute("aria-valuemin", 0); track.setAttribute("aria-valuemax", Math.max(1,s.jobs));
  const fill = node("div", undefined, "progress-fill"); fill.style.width = `${ratio * 100}%`;
  const active = node("div", undefined, "progress-active"); active.style.width = `${s.jobs ? (s.downloading + s.transcribing) / s.jobs * 100 : 0}%`;
  track.append(fill, active);
  const steps = node("div", undefined, "pipeline-steps");
  for (const key of ["queued", "downloading", "transcribing", "failed"]) steps.append(node("span", `${s[key]} ${key}`));
  progress.append(label, track, steps); replace("pipeline", [progress]);
  $("worker-status").textContent = archive.worker.running ? "Worker running" : "Worker stopped";
  $("worker-status").className = `badge ${archive.worker.running ? "running" : "queued"}`;
  const activeJobs = archive.jobs.filter((j) => ["transcribing", "downloading"].includes(j.stage));
  replace("active-work", activeJobs.length ? activeJobs.map((job) => {
    const card = node("div", undefined, "active-card");
    const button = node("button", job.title, "video-title active-title"); button.addEventListener("click", () => details(job));
    card.append(badge(humanStage(job.stage), job.stage), node("div", undefined), button, node("div", `${job.channel_title} · ${job.profile} · stage updated ${date(job.updated_at)}${archive.worker.running ? "" : " · worker stopped; recovery needed"}`, "active-meta"));
    return card;
  }) : [node("div", archive.worker.running ? "No active stage. The worker may be waiting for a retry." : "Start or resume the worker with the CLI when you are ready.", "empty")]);
  const models = archive.profiles.map((p) => `${p.name} · ${p.identity.model || "Unbound model"}`);
  replace("model", [node("span", models.join(" / ") || "No inference profile recorded"), node("span", "Download → ASR → Save")]);
  replace("channel-summary", archive.channels.length ? archive.channels.map((c) => channelCard(c)) : [node("div", "No channels registered yet. Add one with the CLI.", "empty")]);
  const eventNames = {complete:"Transcript saved", download_start:"Audio download started", download_complete:"Audio downloaded", asr_start:"Transcription started", failed:"Job failed", backoff:"Queue backing off", queue_paused:"Queue paused", worker_finished:"Worker finished", reconnect_start:"Reconnecting inference", reconnect_complete:"Inference reconnected", skip_completed:"Saved transcripts skipped"};
  const jobs = new Map(archive.jobs.map((j) => [j.video_id, j]));
  replace("events", archive.events.length ? archive.events.slice(0,8).map((event) => {
    const row = node("div", undefined, "event");
    const body = node("div", undefined, "event-main");
    body.append(node("div", eventNames[event.event] || event.event.replaceAll("_", " ")), node("small", jobs.get(event.video_id)?.title || event.reason || (event.count != null ? `${event.count} completed jobs` : event.video_id || "Queue status")));
    row.append(node("span", undefined, `event-marker ${event.event}`), body, node("time", date(event.time)));
    return row;
  }) : [node("div", "No backfill events recorded yet.", "empty")]);
}
function videoRows() {
  const search = $("video-search").value.trim().toLowerCase();
  const rows = archive.jobs.filter((j) => (!search || `${j.title} ${j.video_id}`.toLowerCase().includes(search)) && (!$("channel-filter").value || j.channel_id === $("channel-filter").value) && (!$("stage-filter").value || j.stage === $("stage-filter").value) && (!$("profile-filter").value || j.profile === $("profile-filter").value));
  const pages = Math.max(1, Math.ceil(rows.length / pageSize)); page = Math.min(page, pages - 1);
  replace("video-rows", rows.slice(page * pageSize, (page + 1) * pageSize).map((job) => {
    const row = node("tr"), title = node("td");
    const button = node("button", job.title, "video-title"); button.addEventListener("click", () => details(job));
    title.append(button, node("div", `${job.channel_title} · ${job.kind} · ${job.video_id} · ${job.profile || "No job"}`, "subline"));
    const status = node("td"); status.append(badge(humanStage(job.stage), job.stage));
    const stages = node("td", undefined, "stage-pair");
    stages.append(node("div", `Download: ${job.download_state || "—"}`), node("div", `ASR: ${job.asr_state || "—"}`));
    const files = node("td"); files.append(fileButtons(job.files));
    row.append(title, status, stages, node("td", duration(job.duration)), files); return row;
  }));
  if (!rows.length) {
    const row = node("tr"), td = node("td", "No videos match these filters.", "empty"); td.colSpan = 5; row.append(td); $("video-rows").append(row);
  }
  $("video-count").textContent = `${rows.length} video/profile entries`;
  $("page-number").textContent = `${page + 1} / ${pages}`;
  $("previous-page").disabled = page === 0; $("next-page").disabled = page + 1 >= pages;
}
function fileRows() {
  const search = $("file-search").value.trim().toLowerCase(), format = $("format-filter").value;
  const files = archive.files.filter((f) => (!search || f.path.toLowerCase().includes(search)) && (!format || (format === "audio" ? f.category === "audio" : f.format === format)));
  replace("file-rows", files.map((file) => {
    const row = node("tr"), title = node("td"), actions = node("td");
    title.append(node("strong", file.name), node("span", file.path, "file-path"));
    if (file.previewable) { const button = node("button", "Preview", "file-button"); button.addEventListener("click", () => preview(file)); actions.append(button, document.createTextNode(" ")); }
    actions.append(download(file)); row.append(title, node("td", file.format.toUpperCase()), node("td", bytes(file.bytes)), actions); return row;
  }));
  if (!files.length) {
    const row = node("tr"), td = node("td", "No output files match these filters.", "empty"); td.colSpan = 4; row.append(td); $("file-rows").append(row);
  }
  $("file-count").textContent = `${files.length} files`;
}
function details(job, open = true) {
  detailKey = job.job_id ?? job.video_id;
  $("detail-title").textContent = job.title;
  const grid = node("div", undefined, "detail-grid");
  const fields = [["Status", humanStage(job.stage)], ["Channel", job.channel_title], ["Video ID", job.video_id], ["Profile", job.profile || "—"], ["Download / ASR", `${job.download_state || "—"} / ${job.asr_state || "—"}`], ["Attempt counters · download / ASR", `${job.download_attempts || 0} / ${job.asr_attempts || 0}`], ["Duration", duration(job.duration)], ["Model / language", `${job.model || "—"} / ${job.language || "—"}`], ["ASR processing time", job.processing_seconds == null ? "—" : `${job.processing_seconds.toFixed(2)} seconds`], ["Stage updated", date(job.updated_at)], ["Completed", date(job.completed_at)], ["Next retry", job.stage === "failed" ? date(job.next_retry) : "—"]];
  for (const [label, value] of fields) { const item = node("div", undefined, "detail-item"); item.append(node("span", label), node("div", value)); grid.append(item); }
  const files = node("div", undefined, "detail-files"); files.append(fileButtons(job.files));
  const source = node("div", undefined, "detail-source"); source.append(externalLink("Open source on YouTube ↗", job.url));
  replace("detail-content", [grid, files, source]);
  if (job.error) $("detail-content").append(node("div", `${job.failed_stage || "Job"}: ${job.error}`, "detail-error"));
  if (open) $("video-detail").showModal();
}
async function preview(file) {
  const request = ++previewRequest;
  $("preview-title").textContent = file.name;
  $("preview-path").textContent = file.path;
  $("preview-download").href = `${file.url}&download=1`;
  $("preview-download").download = file.name;
  replace("preview-content", [node("div", "Loading transcript…", "empty")]);
  $("file-preview").showModal();
  try {
    const response = await fetch(`/api/preview?path=${encodeURIComponent(file.path)}`);
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Preview unavailable");
    if (request !== previewRequest) return;
    if (file.format === "md") {
      replace("preview-content", result.content.split(/\n\s*\n/).map((paragraph) => {
        const element = node("p", undefined, "markdown-turn");
        const turn = paragraph.match(/^\*\*(.+?):\*\*\s(.*)$/s);
        if (turn) { element.append(node("strong", `${turn[1].replace(/\\([\\*_\[\]])/g, "$1")}: `), document.createTextNode(turn[2])); }
        else element.textContent = paragraph;
        return element;
      }));
    } else replace("preview-content", [node("pre", result.content, "preview-body")]);
    if (result.truncated) $("preview-content").prepend(node("div", "Preview limited to 2 MiB. Download the file for its complete contents.", "preview-warning"));
  } catch (error) { if (request === previewRequest) replace("preview-content", [node("div", error.message, "empty")]); }
}
function renderView() {
  if (currentView === "overview") renderOverview();
  if (currentView === "videos") videoRows();
  if (currentView === "channels") replace("channel-cards", archive.channels.length ? archive.channels.map((c) => channelCard(c, true)) : [node("div", "No registered channels. Add one with the CLI.", "empty")]);
  if (currentView === "files") fileRows();
}
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    const response = await fetch("/api/snapshot", {cache:"no-store"});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Archive unavailable");
    archive = data;
    options("channel-filter", archive.channels.map((c) => [c.id,c.title]), "All channels");
    options("profile-filter", [...new Set(archive.jobs.map((j) => j.profile).filter(Boolean))].map((p) => [p,p]), "All profiles");
    $("root-label").textContent = archive.archive_root;
    $("updated").textContent = `Updated ${new Date(data.generated_at).toLocaleTimeString()}`;
    $("live-dot").classList.remove("offline");
    $("error").classList.add("hidden");
    renderStats(); renderView();
    if ($("video-detail").open) {
      const selected = archive.jobs.find((job) => (job.job_id ?? job.video_id) === detailKey);
      if (selected) details(selected, false);
    }
  } catch (error) {
    $("error").textContent = `${error.message}. Showing the last available snapshot; refresh will retry.`;
    $("error").classList.remove("hidden"); $("updated").textContent = "Connection unavailable";
    $("live-dot").classList.add("offline");
  } finally { refreshing = false; }
}
document.querySelectorAll("[data-view]").forEach((el) => el.addEventListener("click", () => setView(el.dataset.view)));
document.querySelector(".brand").addEventListener("click", (event) => { event.preventDefault(); setView("overview"); });
document.querySelectorAll("[data-open-view]").forEach((el) => el.addEventListener("click", () => setView(el.dataset.openView)));
document.querySelectorAll("dialog .close").forEach((el) => el.addEventListener("click", () => el.closest("dialog").close()));
$("file-preview").addEventListener("close", () => previewRequest++);
for (const id of ["video-search", "channel-filter", "stage-filter", "profile-filter"]) $(id).addEventListener(id === "video-search" ? "input" : "change", () => { page = 0; if (archive) videoRows(); });
for (const id of ["file-search", "format-filter"]) $(id).addEventListener(id === "file-search" ? "input" : "change", () => { if (archive) fileRows(); });
$("previous-page").addEventListener("click", () => { page--; videoRows(); });
$("next-page").addEventListener("click", () => { page++; videoRows(); });
$("refresh").addEventListener("click", refresh);
setView(location.hash.slice(1) || "overview");
refresh();
setInterval(() => { if (!document.hidden) refresh(); }, 5000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
