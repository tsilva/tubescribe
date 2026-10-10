"use strict";

const $ = (id) => document.getElementById(id);
let archive = null;
let rendered = "";
let refreshing = false;
let previewRequest = 0;
let previewFiles = [];
let selectedVideo = null;
let selectedProfile = null;
let readerSignature = "";
let readerController = null;
const readingCache = new Map();
const collapsedChannels = new Set();
const expandedVideos = new Set();
const narrowReader = window.matchMedia("(max-width: 900px)");

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}
function human(value) {
  if (value === "asr") return "ASR";
  if (value === "indexed") return "Indexed only";
  return value ? (value[0].toUpperCase() + value.slice(1)).replaceAll("_", " ") : "—";
}
function duration(seconds) {
  if (seconds == null) return "";
  const minutes = Math.floor(seconds / 60);
  return minutes >= 60 ? `${Math.floor(minutes / 60)}h ${minutes % 60}m` : `${minutes}m ${Math.floor(seconds % 60)}s`;
}
function date(value, dateOnly = false) {
  if (!value) return "";
  const timestamp = /^\d{8}$/.test(String(value))
    ? `${String(value).slice(0, 4)}-${String(value).slice(4, 6)}-${String(value).slice(6, 8)}T00:00:00`
    : /^\d+(\.\d+)?$/.test(String(value)) ? Number(value) * 1000 : value;
  const parsed = new Date(timestamp);
  return Number.isNaN(parsed.getTime()) ? String(value)
    : dateOnly ? parsed.toLocaleDateString(undefined, {day: "numeric", month: "short", year: "numeric"}) : parsed.toLocaleString();
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
function transcriptFiles(job) {
  // Only the published transcript, never a diarization prediction or ASR checkpoint.
  const paths = new Set([job.text_path, job.json_path].filter(Boolean));
  if (job.json_path) {
    const base = job.json_path.replace(/\.json$/, "");
    for (const format of ["md", "txt", "srt"]) paths.add(`${base}.${format}`);
  }
  return ["md", "txt", "srt", "json"].flatMap((format) =>
    job.files.filter((file) => paths.has(file.path) && file.previewable && file.format === format));
}
function processingDetails(job) {
  const details = node("div", undefined, "processing-details");
  if (job.job_id == null) {
    details.append(node("div", "No transcription queued", "muted"));
    return details;
  }
  const stages = [
    `Download: ${human(job.download_state)}`,
    `ASR: ${human(job.asr_state)}`,
    `Speakers: ${human(job.diarization_state || "pending")}${job.speaker_count != null ? ` (${job.speaker_count})` : ""}`,
    `Postprocessing: ${human(job.postprocessing_state || "pending")}${job.figures != null ? ` (${job.figures} figures)` : ""}`,
    `Summary: ${human(job.summary_state || "pending")}`,
  ];
  details.append(node("div", stages.join(" · "), "stage-line"));
  const provenance = [job.profile, job.model, job.language].filter(Boolean);
  if (job.processing_seconds != null) provenance.push(`ASR ${job.processing_seconds.toFixed(1)}s`);
  if (job.diarization_seconds != null) provenance.push(`Speakers ${job.diarization_seconds.toFixed(1)}s`);
  if (provenance.length) details.append(node("div", provenance.join(" · "), "muted"));
  if (job.updated_at) details.append(node("div", `Updated ${date(job.updated_at)}`, "muted"));
  if (job.completed_at) details.append(node("div", `ASR saved ${date(job.completed_at)}`, "muted"));
  if (job.error) {
    details.append(node("div", `${human(job.failed_stage || "job")}: ${job.error}`, "job-error"));
    const retry = [`Attempts: download ${job.download_attempts || 0} · ASR ${job.asr_attempts || 0} · speakers ${job.diarization_attempts || 0} · summary ${job.summary_attempts || 0}`];
    if (job.next_retry) retry.push(`Retry ${date(job.next_retry)}`);
    details.append(node("div", retry.join(" · "), "muted"));
  }
  return details;
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
  return [...jobs].sort((a, b) => quality(b) - quality(a))[0];
}
function quality(job) {
  return job.summary_url ? 4 : job.processed_url ? 3 : job.transcript_url ? 2 : job.job_id != null ? 1 : 0;
}
function statusBadge(job) {
  return node("span", human(job.stage), `badge ${job.stage}`);
}
function selectVideo(jobs, job = bestJob(jobs), open = true) {
  selectedVideo = jobs[0].video_id;
  selectedProfile = job.profile;
  for (const row of document.querySelectorAll(".video-row")) {
    const selected = row.dataset.video === selectedVideo;
    row.classList.toggle("selected", selected);
    for (const button of row.querySelectorAll("[aria-pressed]")) button.setAttribute("aria-pressed", String(selected));
  }
  renderReader(jobs, job);
  if (open) setReaderOpen(true);
}
function rowActions(jobs, job) {
  const actions = node("div", undefined, "row-actions");
  if (job.summary_url) {
    const summary = node("button", "Summary", "summary-action");
    summary.dataset.focus = `summary-${job.job_id}`;
    summary.setAttribute("aria-label", `Read summary: ${job.title}`);
    summary.addEventListener("click", () => selectVideo(jobs, job));
    actions.append(summary);
  }
  const transcriptUrl = job.processed_url || job.transcript_url;
  if (transcriptUrl) actions.append(resourceLink("Transcript", transcriptUrl));
  else if (transcriptFiles(job).length) {
    const transcript = node("button", "Transcript");
    transcript.addEventListener("click", () => openTranscript(job, transcriptFiles(job)));
    actions.append(transcript);
  }
  const youtube = externalLink("YouTube", job.url);
  youtube.append(icon("arrow-top-right-on-square"));
  actions.append(youtube);
  return actions;
}
function videoRow(jobs) {
  const job = bestJob(jobs);
  const video = jobs[0];
  const row = node("li", undefined, "video-row");
  row.dataset.video = video.video_id;
  row.classList.toggle("selected", video.video_id === selectedVideo);
  const main = node("div", undefined, "video-main");
  if (job.cover_url) {
    const art = node("button", undefined, "video-art");
    art.setAttribute("aria-label", `Preview ${video.title}`);
    art.dataset.focus = `art-${video.video_id}`;
    const image = node("img", undefined, "video-thumbnail");
    image.src = job.cover_url;
    image.alt = "";
    image.loading = "lazy";
    image.addEventListener("error", () => art.remove(), {once: true});
    art.append(image);
    if (video.duration != null) art.append(node("span", duration(video.duration), "duration"));
    art.addEventListener("click", () => selectVideo(jobs));
    main.append(art);
  }
  const description = node("div", undefined, "video-description");
  const title = node("button", video.title || video.video_id, "video-title");
  title.dataset.focus = `title-${video.video_id}`;
  title.setAttribute("aria-pressed", String(video.video_id === selectedVideo));
  title.addEventListener("click", () => selectVideo(jobs));
  const meta = [date(video.published_at, true), !job.cover_url && duration(video.duration)].filter(Boolean);
  const bottom = node("div", undefined, "video-bottom");
  bottom.append(statusBadge(job), rowActions(jobs, job));
  description.append(title, node("p", meta.join(" · "), "video-meta"), bottom);
  if (!archive.worker.running && ["downloading", "transcribing", "diarizing", "postprocessing", "summarizing"].includes(job.stage)) {
    description.append(node("p", "Processing paused · worker stopped", "muted"));
  }
  if (job.error) description.append(node("p", `${human(job.failed_stage || "Processing")}: ${job.error}`, "row-error"));
  if (job.job_id != null) {
    const details = node("details", undefined, "run-details");
    details.dataset.video = video.video_id;
    details.open = expandedVideos.has(video.video_id);
    details.append(node("summary", jobs.length > 1 ? `Processing details · ${jobs.length} profiles` : "Processing details"));
    details.addEventListener("toggle", () => {
      if (details.open) expandedVideos.add(video.video_id);
      else expandedVideos.delete(video.video_id);
    });
    for (const run of jobs) {
      const block = node("div", undefined, "profile-run");
      if (jobs.length > 1) block.append(node("strong", `${run.profile} · ${human(run.stage)}`));
      block.append(processingDetails(run));
      if (jobs.length > 1) block.append(rowActions(jobs, run));
      details.append(block);
    }
    description.append(details);
  }
  main.append(description);
  row.append(main);
  return row;
}
function renderChannels() {
  if (!archive) return;
  const search = $("video-search").value.trim().toLowerCase();
  const signature = JSON.stringify([search, archive.channels, archive.jobs, archive.worker.running]);
  if (signature === rendered) return;
  rendered = signature;
  const focused = document.activeElement?.dataset.focus;
  const byChannel = new Map(archive.channels.map((channel) => [channel.id, new Map()]));
  const allVideos = new Map();
  for (const job of archive.jobs) {
    if (!allVideos.has(job.video_id)) allVideos.set(job.video_id, []);
    allVideos.get(job.video_id).push(job);
    const videos = byChannel.get(job.channel_id);
    if (!videos) continue;
    if (!search || `${job.channel_title} ${job.channel_id} ${job.title} ${job.video_id}`.toLowerCase().includes(search)) {
      if (!videos.has(job.video_id)) videos.set(job.video_id, []);
      videos.get(job.video_id).push(job);
    }
  }
  if (!allVideos.has(selectedVideo)) {
    const initial = Array.from(allVideos.values()).find((jobs) => bestJob(jobs).summary_url) || allVideos.values().next().value;
    selectedVideo = initial?.[0].video_id;
    selectedProfile = initial && bestJob(initial).profile;
  }
  let count = 0;
  const sections = [];
  for (const channel of archive.channels) {
    const videos = byChannel.get(channel.id);
    if (search && !videos.size && !`${channel.title} ${channel.id}`.toLowerCase().includes(search)) continue;
    count += videos.size;
    const section = node("section", undefined, "channel");
    const heading = node("div", undefined, "channel-heading");
    const toggle = node("button", undefined, "channel-toggle");
    const title = node("div");
    title.append(node("h2", channel.title), node("span", `${videos.size} video${videos.size === 1 ? "" : "s"}`, "muted"));
    const expanded = !collapsedChannels.has(channel.id) || Boolean(search);
    toggle.setAttribute("aria-expanded", String(expanded));
    const content = node("div");
    content.id = `channel-${channel.id}`;
    toggle.setAttribute("aria-controls", content.id);
    toggle.dataset.focus = `channel-${channel.id}`;
    toggle.append(title, icon("chevron-up"));
    toggle.addEventListener("click", () => {
      const next = toggle.getAttribute("aria-expanded") !== "true";
      toggle.setAttribute("aria-expanded", String(next));
      content.hidden = !next;
      if (next) collapsedChannels.delete(channel.id);
      else collapsedChannels.add(channel.id);
    });
    content.hidden = !expanded;
    heading.append(toggle);
    section.append(heading, content);
    for (const discovery of channel.discovery || []) {
      if (discovery.error) content.append(node("p", `${human(discovery.kind)} discovery: ${discovery.error}`, "discovery-error"));
    }
    if (videos.size) {
      const list = node("ul", undefined, "video-list");
      list.append(...Array.from(videos.values(), videoRow));
      content.append(list);
    } else content.append(node("p", "No videos indexed yet.", "empty"));
    sections.push(section);
  }
  $("channels").replaceChildren(...(sections.length ? sections : [node("p", search ? "No videos match this search. Try a different title or channel." : "Your library is empty. Add a channel to start collecting transcripts.", "empty")]));
  $("result-count").textContent = `${count} video${count === 1 ? "" : "s"}${search ? " found" : ""}`;
  if (focused) Array.from(document.querySelectorAll("[data-focus]")).find((element) => element.dataset.focus === focused)?.focus({preventScroll:true});
  const jobs = allVideos.get(selectedVideo);
  if (jobs) renderReader(jobs, jobs.find((job) => job.profile === selectedProfile) || bestJob(jobs));
  else resetReader();
}
// Rebuild allowed text elements rather than injecting server HTML. Source content cannot
// bring scripts, inline styles, event handlers, or external media into the dashboard.
function copyReadingContent(source, baseUrl) {
  if (source.nodeType === Node.TEXT_NODE) return document.createTextNode(source.textContent);
  if (source.nodeType !== Node.ELEMENT_NODE) return document.createDocumentFragment();
  const tag = source.tagName.toLowerCase();
  if (["script", "style", "iframe", "object", "form", "input", "button", "h1"].includes(tag)) return document.createDocumentFragment();
  const allowed = ["h2", "h3", "h4", "p", "ul", "ol", "li", "strong", "em", "code", "pre", "blockquote", "br", "a", "figure", "figcaption", "img"];
  const target = allowed.includes(tag) ? node(tag) : document.createDocumentFragment();
  if (tag === "a" || tag === "img") {
    try {
      const url = new URL(source.getAttribute(tag === "a" ? "href" : "src"), baseUrl);
      if (url.origin !== location.origin || !["http:", "https:"].includes(url.protocol)) {
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
  readerSignature = "";
  const empty = node("div", undefined, "reader-empty");
  const title = node("h2", "Find your next idea.");
  title.id = "reader-title";
  empty.append(node("p", "YOUR READING SPACE", "eyebrow"), title, node("p", "Select a video to read its summary or preview its transcript."));
  $("reader").replaceChildren(empty);
  setReaderOpen(false);
}
async function renderReader(jobs, job) {
  const sourceUrl = job.summary_url || job.processed_url || job.transcript_url;
  const signature = JSON.stringify([job.video_id, job.profile, sourceUrl, job.stage, job.updated_at, job.summary_updated_at, job.summary_dir, job.postprocessing_dir]);
  if (signature === readerSignature) return;
  readerSignature = signature;
  readerController?.abort();
  const controller = new AbortController();
  readerController = controller;
  const pane = $("reader");
  const closeBar = node("div", undefined, "reader-close");
  const close = node("button", undefined, "icon-button");
  close.setAttribute("aria-label", "Back to library");
  close.append(icon("x-mark"));
  close.addEventListener("click", backToLibrary);
  closeBar.append(node("span", "Your library"), close);
  const parts = [closeBar];
  if (job.cover_url) {
    const cover = node("img", undefined, "reader-cover");
    cover.src = job.cover_url;
    cover.alt = "Screenshot saved with this transcript";
    cover.addEventListener("error", () => cover.remove(), {once:true});
    parts.push(cover);
  }
  const title = node("h2", job.title || job.video_id);
  title.id = "reader-title";
  parts.push(title, node("p", [job.channel_title, date(job.published_at, true), duration(job.duration)].filter(Boolean).join(" · "), "reader-meta"), statusBadge(job));
  if (jobs.length > 1) {
    const label = node("label", "Transcript version", "profile-picker");
    const select = node("select");
    select.setAttribute("aria-label", "Transcript version");
    select.replaceChildren(...jobs.map((run) => new Option(run.profile, run.profile)));
    select.value = job.profile;
    select.addEventListener("change", () => selectVideo(jobs, jobs.find((run) => run.profile === select.value), false));
    label.append(select);
    parts.push(label);
  }
  const actions = node("div", undefined, "reader-actions");
  if (job.summary_url) actions.append(resourceLink("Full summary", job.summary_url, "clipboard-document", "primary"));
  const transcriptUrl = job.processed_url || job.transcript_url;
  if (transcriptUrl) actions.append(resourceLink("Transcript", transcriptUrl, "document-text"));
  actions.append(externalLink("View on YouTube", job.url));
  actions.lastChild.prepend(icon("arrow-top-right-on-square"));
  if (job.srt_url) actions.append(resourceLink("SRT timestamps", job.srt_url));
  const body = node("div", undefined, "reader-body");
  body.setAttribute("aria-live", "polite");
  const label = node("p", job.summary_url ? "SUMMARY" : sourceUrl ? "TRANSCRIPT PREVIEW" : "IN YOUR LIBRARY", "eyebrow");
  const content = node("div", undefined, "reading-content");
  body.append(label, content);
  parts.push(actions, body);
  pane.replaceChildren(...parts);
  pane.scrollTop = 0;
  if (!sourceUrl) {
    content.append(node("h3", "The transcript is not ready yet."), node("p", job.stage === "failed" ? "Processing needs attention. The details in the video row show what happened." : "This video is in your library. Its transcript will appear here once processing finishes."));
    return;
  }
  content.append(node("p", job.summary_url ? "Loading summary…" : "Loading transcript…", "muted"));
  try {
    const cacheKey = sourceUrl + "|" + signature;
    let html = readingCache.get(cacheKey);
    if (!html) {
      const response = await fetch(sourceUrl, {signal: controller.signal});
      if (!response.ok) throw new Error("This document is temporarily unavailable.");
      html = await response.text();
      if (controller.signal.aborted) return;
    }
    if (controller.signal.aborted || readerSignature !== signature) return;
    const documentSource = new DOMParser().parseFromString(html, "text/html");
    const article = documentSource.querySelector("article");
    if (!article) throw new Error("Open the full document to read this transcript.");
    if (!readingCache.has(cacheKey)) {
      readingCache.set(cacheKey, html);
      if (readingCache.size > 8) readingCache.delete(readingCache.keys().next().value);
    }
    const elements = Array.from(article.children).filter((element) => element.tagName !== "H1");
    const shown = job.summary_url ? elements : elements.slice(0, 6);
    content.replaceChildren(...shown.map((element) => copyReadingContent(element, new URL(sourceUrl, location.href).href)));
    if (!content.textContent.trim()) content.append(node("p", "Open the full document to read this transcript."));
    body.append(node("p", job.summary_url ? "Generated from this video’s transcript. The summary reflects the speaker’s views; figures link to saved source screenshots." : "An excerpt of the saved transcript. Open the full transcript for all speaker turns, figures, and downloads.", "reader-note"));
  } catch (error) {
    if (controller.signal.aborted || readerSignature !== signature) return;
    content.replaceChildren(node("p", error.message, "muted"));
    const retry = node("button", "Try again");
    retry.addEventListener("click", () => {readerSignature = ""; renderReader(jobs, job);});
    content.append(retry);
  }
}
function openTranscript(job, files) {
  previewFiles = files;
  $("preview-srt").classList.toggle("hidden", !job.srt_url);
  if (job.srt_url) $("preview-srt").href = job.srt_url;
  $("preview-title").textContent = job.title;
  $("preview-format").replaceChildren(...files.map((file) => new Option(file.format.toUpperCase(), file.path)));
  if (!$("transcript-preview").open) $("transcript-preview").showModal();
  preview(files[0]);
}
async function preview(file) {
  const request = ++previewRequest;
  $("preview-download").href = `${file.url}&download=1`;
  $("preview-download").download = file.name;
  $("preview-content").replaceChildren(node("p", "Loading transcript…", "empty"));
  try {
    const response = await fetch(`/api/preview?path=${encodeURIComponent(file.path)}`);
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Preview unavailable");
    if (request !== previewRequest) return;
    if (file.format === "md") {
      $("preview-content").replaceChildren(...result.content.split(/\n\s*\n/).map((paragraph) => {
        const element = node("p", undefined, "markdown-turn");
        const turn = paragraph.match(/^\*\*(.+?):\*\*\s(.*)$/s);
        if (turn) element.append(node("strong", `${turn[1].replace(/\\([\\*_\[\]])/g, "$1")}: `), document.createTextNode(turn[2]));
        else element.textContent = paragraph;
        return element;
      }));
    } else $("preview-content").replaceChildren(node("pre", result.content, "preview-body"));
    if (result.truncated) $("preview-content").prepend(node("p", "Preview limited to 2 MiB. Download for the complete transcript.", "preview-warning"));
  } catch (error) {
    if (request === previewRequest) $("preview-content").replaceChildren(node("p", error.message, "empty"));
  }
}
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  $("refresh").disabled = true;
  try {
    const response = await fetch("/api/snapshot", {cache: "no-store"});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Archive unavailable");
    archive = data;
    $("archive-summary").textContent = `${data.summary.channels} channel${data.summary.channels === 1 ? "" : "s"} · ${data.summary.videos} video${data.summary.videos === 1 ? "" : "s"}`;
    $("worker-status").textContent = `Worker ${data.worker.running ? "running" : "stopped"}`;
    $("updated").textContent = `Updated ${new Date(data.generated_at).toLocaleTimeString()}`;
    $("error").classList.add("hidden");
    renderChannels();
  } catch (error) {
    $("error").textContent = `${error.message}. ${archive ? "Showing the last available snapshot." : "Could not load the archive."} Refresh will retry.`;
    $("error").classList.remove("hidden");
    $("updated").textContent = "Connection unavailable";
    if (!archive) $("channels").replaceChildren(node("p", "Channels are unavailable until the archive reconnects.", "empty"));
  } finally {
    refreshing = false;
    $("refresh").disabled = false;
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
$("today").textContent = new Date().toLocaleDateString(undefined, {weekday:"short", day:"numeric", month:"long", year:"numeric"});
$("video-search").addEventListener("input", renderChannels);
$("refresh").addEventListener("click", refresh);
$("close-preview").addEventListener("click", () => $("transcript-preview").close());
$("transcript-preview").addEventListener("close", () => previewRequest++);
$("preview-format").addEventListener("change", () => {
  const file = previewFiles.find((file) => file.path === $("preview-format").value);
  if (file) preview(file);
});
refresh();
setInterval(() => { if (!document.hidden) refresh(); }, 5000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
