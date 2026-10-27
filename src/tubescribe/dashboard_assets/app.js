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
let liveFeed = null;
let ownsLibrary = true;
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
  if (displayStage(job) !== "complete") {
    bottom.append(statusBadge(job));
    description.append(bottom);
  }
  main.append(description);
  row.append(main);
  return row;
}
function renderFeed() {
  if (!archive) return;
  const search = $("video-search").value.trim().toLowerCase();
  const signature = JSON.stringify([libraryScope, search, archive.channels, archive.jobs]);
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
  const matches = videos.filter(({jobs}) => (!libraryScope || jobs[0].channel_id === libraryScope) && (!search || jobs.some((job) =>
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
    const signature = JSON.stringify([jobs]);
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
  $("feed-empty").textContent = search ? "No videos match this search. Try a different title or channel." : "No videos yet";
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
  const signature = JSON.stringify([identity, readerTab, tabs, job.title, job.channel_title, job.published_at, job.duration, job.url, job.stage, job.scheduled, job.updated_at, job.summary_updated_at, job.summary_dir, job.postprocessing_dir]);
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
  const title = node("h2", job.title || job.video_id);
  title.id = "reader-title";
  const statusRow = node("div", undefined, "reader-status-row");
  const youtube = externalLink("", job.url, "document-button");
  youtube.title = "View on YouTube";
  youtube.setAttribute("aria-label", youtube.title);
  const play = node("span", "▶", "youtube-icon");
  play.setAttribute("aria-hidden", "true");
  youtube.append(play);
  statusRow.append(statusBadge(job));
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
  actions.append(youtube);
  statusRow.append(actions);
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
  if (activeTab) parts.push(shareStatus);
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
    content.append(node("h3", "The transcript is not ready yet."), node("p", job.stage === "failed" ? "The transcript is currently unavailable." : "This video is in your library. Refresh after processing finishes to read its transcript."));
    return;
  }
  content.append(node("p", `Loading ${activeTab.label.toLowerCase()}…`, "muted"));
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
}
async function refresh(event) {
  if (refreshing || !signedInUser) return;
  if (realtimeSettings.enabled && ownsLibrary) {
    if (liveFeed && liveFeed.status !== "error") {
      await liveFeed.wake();
      if (event?.type === "click") refreshDigests();
      return;
    }
    liveFeed?.close();
    const generation = identityGeneration;
    let initial = true;
    liveFeed = TubeScribeRealtime.connect({
      getCapabilities: async () => {
        const response = await libraryFetch(realtimeSettings.endpoint);
        if (!response.ok) throw new Error("Live updates are unavailable. Refresh to retry.");
        return response.json();
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
        $("worker-status").textContent = state === "live" ? "Library live" :
          state === "error" ? "Live connection unavailable" : "Reconnecting…";
        if (state === "error") {
          $("error").textContent = "Live updates stopped. Refresh to reconnect. Your last snapshot is preserved.";
          $("error").classList.remove("hidden");
        }
      },
    });
    await liveFeed.start();
    return;
  }
  const generation = identityGeneration;
  refreshing = true;
  $("refresh").disabled = true;
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
}
function chooseScope(channel = "", overview = false) {
  libraryScope = channel;
  $("channel-scope").value = channel;
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

function clearPrivateLibrary() {
  identityGeneration += 1;
  liveFeed?.close();
  liveFeed = null;
  ownsLibrary = true;
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
  delete $("channel-scope").dataset.signature;
  $("archive-summary").textContent = "Your private library";
  $("updated").textContent = "";
  $("result-count").textContent = "";
  $("error").classList.add("hidden");
  $("feed").replaceChildren(node("p", "No videos yet", "empty"));
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
    signedInUser = null;
    clearPrivateLibrary();
    document.querySelector(".workspace").hidden = true;
    $("auth-panel").hidden = false;
    $("auth-message").textContent = "Your session has ended. Please sign in again.";
    throw new Error("Sign in required");
  }
  return response;
}
async function syncIdentity({user, session}) {
  const next = user && session ? user.id : null;
  if (next === signedInUser) return;
  signedInUser = next;
  clearPrivateLibrary();
  const generation = identityGeneration;
  document.querySelector(".workspace").hidden = !next;
  $("auth-panel").hidden = Boolean(next);
  if (!next) {
    window.Clerk.unmountUserButton($("user-button"));
    $("worker-status").textContent = "Sign in to continue";
    $("auth-message").textContent = "Sign in or create an account to read your videos.";
    window.Clerk.mountSignIn($("sign-in"), {routing: "hash", forceRedirectUrl: "/" + window.location.search});
    return;
  }
  window.Clerk.unmountSignIn($("sign-in"));
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
    $("error").textContent = error.message;
    $("error").classList.remove("hidden");
  }
}
async function initializeAuthentication() {
  clearPrivateLibrary();
  try {
    const response = await fetch("/auth/config", {cache: "no-store"});
    if (!response.ok) throw new Error("Sign-in configuration is unavailable. Refresh to retry.");
    const settings = await response.json();
    authEnabled = settings.enabled;
    realtimeSettings = settings.realtime || {enabled: false};
    $("feed-help").textContent = realtimeSettings.enabled ? "New videos appear automatically" : "Refresh to check for new videos";
    if (!authEnabled) {
      signedInUser = "local";
      $("auth-panel").hidden = true;
      document.querySelector(".workspace").hidden = false;
      await refresh();
      return;
    }
    const frontend = new URL(settings.frontend_api);
    if (frontend.protocol !== "https:" || frontend.pathname !== "/") throw new Error("Invalid sign-in configuration");
    const scripts = [
      "/npm/@clerk/ui@1.40.0/dist/ui.browser.js",
      "/npm/@clerk/clerk-js@6.39.0/dist/clerk.browser.js",
    ];
    for (const path of scripts) {
      await new Promise((resolve, reject) => {
        const script = document.createElement("script");
        script.src = frontend.origin + path;
        script.crossOrigin = "anonymous";
        script.dataset.clerkPublishableKey = settings.publishable_key;
        script.onload = resolve;
        script.onerror = () => reject(new Error("Sign-in could not load. Refresh to retry."));
        document.head.append(script);
      });
    }
    await window.Clerk.load({ui: {ClerkUI: window.__internal_ClerkUICtor}});
    // Force initial signed-out rendering, then clear every cache on account changes.
    signedInUser = undefined;
    window.Clerk.addListener((state) => { void syncIdentity(state); });
  } catch (error) {
    $("auth-message").textContent = error.message;
    $("worker-status").textContent = "Sign-in unavailable";
  }
}
void initializeAuthentication();
