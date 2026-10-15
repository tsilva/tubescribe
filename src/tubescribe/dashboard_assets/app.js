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
let readerIdentity = "";
let readerTab = "summary";
let libraryScope = "";
let readerLevel = "digest";
let digests = null;
let digestRefreshing = false;
const readingCache = new Map();
const rowCache = new Map();
const feedRecords = new Map();
const feedCacheKey = "tubescribe.feed.v1";
let feedRevision = "";
let feedLimit = 50;
let feedSearch = "";
let feedObserver = null;
const dateFormatter = new Intl.DateTimeFormat(undefined, {day: "numeric", month: "short", year: "numeric"});
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
function displayStage(job) {
  return ["queued", "indexed"].includes(job.stage) && !job.scheduled ? "not_processed" : job.stage;
}
function statusBadge(job) {
  const stage = displayStage(job);
  return node("span", human(stage), `badge ${stage}`);
}
function selectVideo(jobs, job = bestJob(jobs), open = true) {
  readerLevel = "video";
  $("rolling-summary").setAttribute("aria-pressed", "false");
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
  const channel = video.channel_title || archive.channels.find((source) => source.id === video.channel_id)?.title || video.channel_id;
  const meta = [channel, date(video.published_at, true), !job.cover_url && duration(video.duration)].filter(Boolean);
  const bottom = node("div", undefined, "video-bottom");
  bottom.append(statusBadge(job));
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
    const populate = () => {
      if (details.childElementCount > 1) return;
      for (const run of jobs) {
        const block = node("div", undefined, "profile-run");
        if (jobs.length > 1) block.append(node("strong", `${run.profile} · ${human(displayStage(run))}`));
        block.append(processingDetails(run));
        details.append(block);
      }
    };
    if (details.open) populate();
    details.addEventListener("toggle", () => { if (details.open) populate(); });
    description.append(details);
  }
  main.append(description);
  row.append(main);
  return row;
}
function renderFeed() {
  if (!archive) return;
  const search = $("video-search").value.trim().toLowerCase();
  const signature = JSON.stringify([libraryScope, search, archive.channels, archive.jobs, archive.worker.running]);
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
  if (!allVideos.has(selectedVideo)) {
    const initial = videos.find(({jobs}) => bestJob(jobs).summary_url)?.jobs || videos[0]?.jobs;
    selectedVideo = initial?.[0].video_id;
    selectedProfile = initial && bestJob(initial).profile;
  }
  const matches = videos.filter(({jobs}) => (!libraryScope || jobs[0].channel_id === libraryScope) && (!search || jobs.some((job) =>
    `${job.channel_title} ${job.channel_id} ${job.title} ${job.video_id}`.toLowerCase().includes(search))));
  const sources = new Set(matches.map(({jobs}) => jobs[0].channel_id));
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
  const errors = [];
  for (const channel of archive.channels) {
    if (search && !sources.has(channel.id) && !`${channel.title} ${channel.id}`.toLowerCase().includes(search)) continue;
    for (const discovery of channel.discovery || []) {
      if (discovery.error) errors.push(node("p", `${channel.title} · ${human(discovery.kind)} discovery: ${discovery.error}`, "discovery-error"));
    }
  }
  $("feed-errors").replaceChildren(...errors);
  const list = $("feed-list");
  const visible = matches.slice(0, feedLimit);
  const rows = visible.map(({jobs}) => {
    const id = jobs[0].video_id;
    const signature = JSON.stringify([jobs, archive.worker.running]);
    let cached = rowCache.get(id);
    if (!cached || cached.signature !== signature) {
      cached = {signature, row: videoRow(jobs)};
      rowCache.set(id, cached);
    }
    const selected = id === selectedVideo;
    cached.row.classList.toggle("selected", selected);
    cached.row.querySelector(".video-title").setAttribute("aria-pressed", String(selected));
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
  $("feed-empty").textContent = search ? "No videos match this search. Try a different title or channel." : "Your library is empty. Add a channel to start collecting transcripts.";
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
  readerIdentity = "";
  const empty = node("div", undefined, "reader-empty");
  const title = node("h2", "Find your next idea.");
  title.id = "reader-title";
  empty.append(node("p", "YOUR READING SPACE", "eyebrow"), title, node("p", "Select a video to read its summary or transcript."));
  $("reader").replaceChildren(empty);
  setReaderOpen(false);
}
async function renderReader(jobs, job) {
  const tabs = [
    {key: "summary", label: "Summary", url: job.summary_url},
    {key: "transcript", label: "Transcript", url: job.processed_url || job.transcript_url},
    {key: "srt", label: "SRT timestamps", url: job.srt_url},
  ].filter((tab) => tab.url);
  const identity = `${job.video_id}|${job.profile}`;
  if (identity !== readerIdentity || !tabs.some((tab) => tab.key === readerTab)) readerTab = tabs[0]?.key || "";
  readerIdentity = identity;
  const activeTab = tabs.find((tab) => tab.key === readerTab);
  const sourceUrl = activeTab?.url;
  const signature = JSON.stringify([identity, readerTab, tabs, job.stage, job.scheduled, job.updated_at, job.summary_updated_at, job.summary_dir, job.postprocessing_dir]);
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
  const parts = [closeBar, digestBreadcrumbs(job.channel_id)];
  if (job.cover_url) {
    const cover = node("img", undefined, "reader-cover");
    cover.src = job.cover_url;
    cover.alt = "Screenshot saved with this transcript";
    cover.addEventListener("error", () => cover.remove(), {once:true});
    parts.push(cover);
  }
  const title = node("h2", job.title || job.video_id);
  title.id = "reader-title";
  const statusRow = node("div", undefined, "reader-status-row");
  const youtube = externalLink("View on YouTube", job.url, "reader-youtube");
  youtube.prepend(icon("arrow-top-right-on-square"));
  statusRow.append(statusBadge(job), youtube);
  parts.push(title, node("p", [job.channel_title, date(job.published_at, true), duration(job.duration)].filter(Boolean).join(" · "), "reader-meta"), statusRow);
  if (jobs.length > 1) {
    const label = node("label", "Transcript version", "profile-picker");
    const select = node("select");
    select.setAttribute("aria-label", "Transcript version");
    select.replaceChildren(...jobs.map((run) => new Option(run.profile, run.profile)));
    select.value = job.profile;
    select.addEventListener("change", () => {
      selectVideo(jobs, jobs.find((run) => run.profile === select.value), false);
      pane.querySelector(".profile-picker select")?.focus({preventScroll: true});
    });
    label.append(select);
    parts.push(label);
  }
  const chooseTab = (index) => {
    readerTab = tabs[index].key;
    renderReader(jobs, job);
    $(`reader-tab-${readerTab}`)?.focus({preventScroll: true});
  };
  const actions = node("div", undefined, "reader-actions");
  const shareStatus = node("div", undefined, "reader-share-status");
  shareStatus.setAttribute("role", "status");
  if (activeTab) {
    const tools = node("div", undefined, "reader-document-actions");
    const fullPage = resourceLink("", sourceUrl, "arrow-top-right-on-square", "document-button");
    fullPage.title = `Open ${activeTab.label} full page`;
    fullPage.setAttribute("aria-label", fullPage.title);
    const share = node("button", undefined, "document-button");
    share.title = `Copy ${activeTab.label} link`;
    share.setAttribute("aria-label", share.title);
    share.append(icon("clipboard-document"));
    share.addEventListener("click", async () => {
      const url = new URL(sourceUrl, location.href).href;
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
    tools.append(fullPage, share);
    actions.append(tools);
  }
  const tabList = node("div", undefined, "reader-tabs");
  tabList.setAttribute("role", "tablist");
  tabList.setAttribute("aria-label", "Video content");
  tabs.forEach((tab, index) => {
    const button = node("button", tab.label, "reader-tab");
    button.id = `reader-tab-${tab.key}`;
    button.setAttribute("role", "tab");
    button.setAttribute("aria-selected", String(tab.key === readerTab));
    button.setAttribute("aria-controls", "reader-panel");
    button.tabIndex = tab.key === readerTab ? 0 : -1;
    button.addEventListener("click", () => chooseTab(index));
    button.addEventListener("keydown", (event) => {
      let next;
      if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
      else if (event.key === "ArrowLeft") next = (index + tabs.length - 1) % tabs.length;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = tabs.length - 1;
      if (next !== undefined) {
        event.preventDefault();
        chooseTab(next);
      }
    });
    tabList.append(button);
  });
  const body = node("div", undefined, "reader-body");
  body.id = "reader-panel";
  body.setAttribute("aria-live", "polite");
  if (activeTab) {
    body.setAttribute("role", "tabpanel");
    body.setAttribute("aria-labelledby", `reader-tab-${readerTab}`);
    body.tabIndex = 0;
  }
  const content = node("div", undefined, "reading-content");
  if (activeTab) body.append(actions, shareStatus);
  body.append(content);
  content.addEventListener("click", (event) => {
    const link = event.target.closest("a[href]");
    if (!link || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    const url = new URL(link.href);
    const index = tabs.findIndex((tab) => new URL(tab.url, location.href).href === url.href);
    if (index !== -1) {
      event.preventDefault();
      chooseTab(index);
    }
  });
  if (tabs.length) parts.push(tabList);
  parts.push(body);
  pane.replaceChildren(...parts);
  pane.scrollTop = 0;
  if (!sourceUrl) {
    content.append(node("h3", "The transcript is not ready yet."), node("p", job.stage === "failed" ? "Processing needs attention. The details in the video row show what happened." : "This video is in your library. Its transcript will appear here once processing finishes."));
    return;
  }
  content.append(node("p", `Loading ${activeTab.label.toLowerCase()}…`, "muted"));
  try {
    const cacheKey = sourceUrl + "|" + signature;
    let documentData = readingCache.get(cacheKey);
    if (!documentData) {
      const response = await fetch(sourceUrl, {signal: controller.signal});
      if (!response.ok) throw new Error("This document is temporarily unavailable.");
      documentData = {text: await response.text(), type: response.headers.get("Content-Type") || ""};
    }
    if (controller.signal.aborted || readerSignature !== signature) return;
    if (documentData.type.includes("text/html")) {
      const documentSource = new DOMParser().parseFromString(documentData.text, "text/html");
      const source = documentSource.querySelector(readerTab === "srt" ? ".srt-cues" : "article");
      if (!source) throw new Error("This document could not be displayed. Use the full-page button to open it.");
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
    if (!content.textContent.trim()) content.append(node("p", "This document has no content."));
    if (readerTab === "summary") body.append(node("p", "Generated from this video’s transcript. The summary reflects the speaker’s views; figures link to saved source screenshots.", "reader-note"));
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
function applyFeed(data) {
  if (data.full) feedRecords.clear();
  else if (!archive) throw new Error("The library needs a fresh snapshot.");
  for (const key of data.removed || []) feedRecords.delete(key);
  for (const job of data.jobs) feedRecords.set(job.key, job);
  archive = {
    channels: data.channels,
    summary: data.summary,
    worker: data.worker,
    generated_at: data.generated_at,
    jobs: Array.from(feedRecords.values()),
  };
  feedRevision = data.revision;
}
function showArchive(cached = false) {
  $("archive-summary").textContent = `${archive.summary.channels} channel${archive.summary.channels === 1 ? "" : "s"} · ${archive.summary.videos} video${archive.summary.videos === 1 ? "" : "s"}`;
  $("worker-status").textContent = cached ? "Checking for updates…" : `Worker ${archive.worker.running ? "running" : "stopped"}`;
  $("updated").textContent = cached ? "Cached library" : `Updated ${new Date(archive.generated_at).toLocaleTimeString()}`;
  syncScopes();
  renderFeed();
}
function restoreFeed() {
  try {
    const saved = JSON.parse(sessionStorage.getItem(feedCacheKey));
    if (!saved || !Number.isFinite(saved.savedAt) || Date.now() - saved.savedAt > 86400000 || !Array.isArray(saved.jobs)
        || !saved.jobs.every((job) => typeof job.key === "string")
        || !Array.isArray(saved.channels) || !saved.summary || !saved.worker || !saved.revision) return;
    applyFeed({...saved, full: true});
    showArchive(true);
  } catch { /* Storage can be disabled or contain an obsolete cache. */ }
}
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  $("refresh").disabled = true;
  try {
    const response = await fetch(`/api/feed${feedRevision ? "?revision=" + encodeURIComponent(feedRevision) : ""}`, {cache: "no-store"});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Archive unavailable");
    const previous = feedRevision;
    applyFeed(data);
    $("error").classList.add("hidden");
    showArchive();
    refreshDigests();
    if (previous !== feedRevision || data.full) {
      try {
        sessionStorage.setItem(feedCacheKey, JSON.stringify({...archive, revision: feedRevision, savedAt: Date.now()}));
      } catch { /* Live updates still work without session storage. */ }
    }
  } catch (error) {
    $("error").textContent = `${error.message}. ${archive ? "Showing the last available snapshot." : "Could not load the archive."} Refresh will retry.`;
    $("error").classList.remove("hidden");
    $("updated").textContent = "Connection unavailable";
    $("worker-status").textContent = "Worker status unavailable";
    if (!archive) $("feed").replaceChildren(node("p", "Videos are unavailable until the archive reconnects.", "empty"));
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
$("video-search").addEventListener("input", renderFeed);
$("refresh").addEventListener("click", refresh);
$("close-preview").addEventListener("click", () => $("transcript-preview").close());
$("transcript-preview").addEventListener("close", () => previewRequest++);
$("preview-format").addEventListener("change", () => {
  const file = previewFiles.find((file) => file.path === $("preview-format").value);
  if (file) preview(file);
});
restoreFeed();
refresh();
setInterval(() => { if (!document.hidden) refresh(); }, 5000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });

function syncScopes() {
  const picker = $("channel-scope");
  const signature = JSON.stringify(archive.channels.map((c) => [c.id, c.title]));
  if (picker.dataset.signature === signature) return;
  picker.dataset.signature = signature;
  picker.replaceChildren(new Option("All channels", ""), ...archive.channels.map((c) => new Option(c.title, c.id)));
  if (!archive.channels.some((c) => c.id === libraryScope)) libraryScope = "";
  picker.value = libraryScope;
}
function chooseScope(channel = "") {
  libraryScope = channel;
  $("channel-scope").value = channel;
  feedLimit = 50;
  readerLevel = "digest";
  readerSignature = "";
  $("rolling-summary").setAttribute("aria-pressed", "true");
  renderFeed();
  renderDigest();
}
function digestBreadcrumbs(channel) {
  const nav = node("nav", undefined, "digest-breadcrumbs");
  nav.setAttribute("aria-label", "Summary levels");
  const all = node("button", "All channels");
  all.addEventListener("click", () => chooseScope());
  nav.append(all);
  if (channel) {
    const name = archive?.channels.find((c) => c.id === channel)?.title || channel;
    const button = node("button", name);
    button.addEventListener("click", () => chooseScope(channel));
    nav.append(node("span", "/"), button);
  }
  return nav;
}
async function refreshDigests() {
  if (digestRefreshing) return;
  digestRefreshing = true;
  try {
    const response = await fetch("/api/summaries", {cache: "no-store"});
    if (!response.ok) throw new Error("Seven-day summaries are temporarily unavailable.");
    digests = await response.json();
    if (readerLevel === "digest") renderDigest();
  } catch (error) {
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
  const browse = node("div", undefined, "digest-browse");
  if (!libraryScope) {
    browse.append(node("h3", "Explore channels"));
    for (const channel of archive?.channels || []) {
      const button = node("button", channel.title, "button");
      button.addEventListener("click", () => chooseScope(channel.id));
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
    body.append(node("p", digests.waiting_for_videos ? "Waiting for video processing to finish before refreshing this summary…" : entry.state === "failed" ? "Summary generation needs attention. Automatic retries are limited to three attempts." : "Updating the seven-day summary…", "muted"));
    if (entry.error) body.append(node("p", entry.error, "muted"));
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
      const response = await fetch(entry.url, {signal: controller.signal});
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
      if (channel) { event.preventDefault(); chooseScope(decodeURIComponent(channel[1])); }
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
$("channel-scope").addEventListener("change", (event) => { chooseScope(event.target.value); setReaderOpen(true); });
$("rolling-summary").addEventListener("click", () => { chooseScope(libraryScope); setReaderOpen(true); });
