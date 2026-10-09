"use strict";

const $ = (id) => document.getElementById(id);
let archive = null;
let rendered = "";
let refreshing = false;
let previewRequest = 0;
let previewFiles = [];

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
    : dateOnly ? parsed.toLocaleDateString() : parsed.toLocaleString();
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
    const retry = [`Attempts: download ${job.download_attempts || 0} · ASR ${job.asr_attempts || 0} · speakers ${job.diarization_attempts || 0}`];
    if (job.next_retry) retry.push(`Retry ${date(job.next_retry)}`);
    details.append(node("div", retry.join(" · "), "muted"));
  }
  return details;
}
function videoRow(jobs) {
  const video = jobs[0];
  const row = node("li", undefined, "video-row");
  const description = node("div", undefined, "video-description");
  description.append(externalLink(video.title || video.video_id, video.url, "video-title"));
  const meta = [date(video.published_at, true), duration(video.duration), human(video.kind), video.video_id].filter(Boolean);
  description.append(node("div", meta.join(" · "), "video-meta"));
  const runs = node("div", undefined, "video-runs");
  for (const job of jobs) {
    const run = node("div", undefined, "video-run");
    const status = node("div", undefined, "video-status");
    status.append(node("span", human(job.stage), `badge ${job.stage}`));
    if (jobs.length > 1) status.append(node("div", job.profile || "No profile", "muted"));
    if (!archive.worker.running && ["downloading", "transcribing", "diarizing"].includes(job.stage)) {
      status.append(node("div", "Worker stopped", "muted"));
    }
    const action = node("div", undefined, "transcript-action");
    const files = transcriptFiles(job);
    if (files.length) {
      const button = node("button", "Transcript", "transcript-button");
      button.dataset.job = String(job.job_id);
      button.setAttribute("aria-label", `Read transcript: ${video.title}${jobs.length > 1 ? ` (${job.profile})` : ""}`);
      button.addEventListener("click", () => openTranscript(job, files));
      action.append(button);
    } else action.append(node("span", "No transcript yet", "muted"));
    run.append(status, processingDetails(job), action);
    runs.append(run);
  }
  row.append(description, runs);
  return row;
}
function renderChannels() {
  if (!archive) return;
  const search = $("video-search").value.trim().toLowerCase();
  const signature = JSON.stringify([search, archive.channels, archive.jobs, archive.worker.running]);
  if (signature === rendered) return;
  rendered = signature;
  const focusedJob = document.activeElement?.dataset.job;
  const byChannel = new Map(archive.channels.map((channel) => [channel.id, new Map()]));
  for (const job of archive.jobs) {
    const videos = byChannel.get(job.channel_id);
    if (!videos) continue;
    if (!search || `${job.channel_title} ${job.channel_id} ${job.title} ${job.video_id}`.toLowerCase().includes(search)) {
      if (!videos.has(job.video_id)) videos.set(job.video_id, []);
      videos.get(job.video_id).push(job);
    }
  }
  let count = 0;
  const sections = [];
  for (const channel of archive.channels) {
    const videos = byChannel.get(channel.id);
    if (search && !videos.size && !`${channel.title} ${channel.id}`.toLowerCase().includes(search)) continue;
    count += videos.size;
    const section = node("section", undefined, "channel");
    const heading = node("div", undefined, "channel-heading");
    const title = node("h2");
    title.append(externalLink(channel.title, channel.url));
    heading.append(title, node("span", `${videos.size} video${videos.size === 1 ? "" : "s"}`, "muted"));
    section.append(heading);
    for (const discovery of channel.discovery || []) {
      if (discovery.error) section.append(node("p", `${human(discovery.kind)} discovery: ${discovery.error}`, "discovery-error"));
    }
    if (videos.size) {
      const labels = node("div", undefined, "column-labels");
      labels.setAttribute("aria-hidden", "true");
      labels.append(node("span", "Video"), node("span", "Status"), node("span", "Processing details"), node("span", "Transcript"));
      const list = node("ul", undefined, "video-list");
      list.append(...Array.from(videos.values(), videoRow));
      section.append(labels, list);
    } else section.append(node("p", "No videos indexed yet.", "empty"));
    sections.push(section);
  }
  $("channels").replaceChildren(...(sections.length ? sections : [node("p", search ? "No channels or videos match your search." : "No channels registered yet. Add one with the CLI.", "empty")]));
  $("result-count").textContent = `${count} video${count === 1 ? "" : "s"}${search ? " found" : ""}`;
  if (focusedJob) {
    const button = Array.from(document.querySelectorAll(".transcript-button")).find((element) => element.dataset.job === focusedJob);
    button?.focus({preventScroll: true});
  }
}
function openTranscript(job, files) {
  previewFiles = files;
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
    $("archive-summary").textContent = `${data.summary.channels} channel${data.summary.channels === 1 ? "" : "s"} · ${data.summary.videos} video${data.summary.videos === 1 ? "" : "s"} · Worker ${data.worker.running ? "running" : "stopped"}`;
    $("root-label").textContent = data.archive_root;
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
