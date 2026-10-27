const stages = ["state", "diarization_state", "postprocessing_state", "summary_state"];
export function stage(job) {
  const next = stages.findIndex(name => job[name] !== "complete");
  if (next === -1) return "complete";
  const state = job[stages[next]];
  if (state === "failed") return "failed";
  if (!next) return job.asr_state === "active" ? "transcribing" :
    job.download_state === "active" ? "downloading" : job.state ? "queued" : "indexed";
  if (state === "active") return job[[null,"diarization_stage","postprocessing_stage","summary_stage"][next]] === "download"
    ? "downloading" : [null,"diarizing","postprocessing","summarizing"][next];
  return [null,"awaiting_diarization","awaiting_postprocessing","awaiting_summary"][next];
}

function due(job, options, now) {
  const count = options.postprocessing === "off" ? 2 : options.no_summary ? 3 : 4;
  const next = job.scheduling.slice(0, count).findIndex(row => row[0] !== "complete");
  if (next < 0) return false;
  const [state, step, retry, downloads, attempts, saves] = job.scheduling[next];
  const budget = options.max_attempts ?? 3;
  return [null, "pending", "failed"].includes(state) &&
    (downloads ?? 0) < budget && (saves ?? 0) < budget &&
    (step === "save" && next >= 2 || (attempts ?? 0) < budget) &&
    (options.wait || (retry ?? 0) <= now);
}

export function buildFeed({feed, channels, queue}, now = Date.now() / 1000) {
  const scheduled = new Set();
  for (const {options} of queue) {
    if (!options || typeof options !== "object") continue;
    let candidates = feed.filter(job => job.job_id != null &&
      job.profile === (options.profile ?? "turbo-v1") &&
      (!options.video || options.video.includes(job.video_id)) &&
      (!options.channel || job.channel_id === options.channel) && due(job, options, now));
    candidates.sort((a,b) => String(b.published_at ?? "").localeCompare(String(a.published_at ?? "")) || Number(a.job_id)-Number(b.job_id));
    if (options.limit != null) candidates = candidates.slice(0, Math.max(0, Number(options.limit)));
    for (const job of candidates) scheduled.add(job.key);
  }
  const names = new Map(channels.map(c => [c.id, c.title]));
  const jobs = feed.map(source => {
    const {scheduling, transcript_ready, processed_ready, summary_ready, cover_revision, ...job} = source;
    const base = job.profile ? `/videos/${encodeURIComponent(job.video_id)}/transcripts/${encodeURIComponent(job.profile)}/` : null;
    job.job_id = job.job_id == null ? null : Number(job.job_id);
    job.stage = stage(job);
    job.scheduled = scheduled.has(job.key);
    job.channel_title = names.get(job.channel_id) ?? job.channel_id;
    if (base && transcript_ready) {job.transcript_url = base; job.srt_url = base + "subtitles/";}
    if (base && job.postprocessing_state === "complete" && processed_ready) {
      job.processed_url = base;
      if (job.figures) job.cover_url = base + "cover.png?v=" + encodeURIComponent(cover_revision ?? "");
    }
    if (base && job.summary_state === "complete" && summary_ready) job.summary_url = base + "artifacts/summary/";
    return job;
  });
  return {
    full: true, revision: "realtime", generated_at: new Date(now * 1000).toISOString(), removed: [], jobs,
    channels: channels.map(c => ({...c, videos: new Set(jobs.filter(j => j.channel_id === c.id).map(j => j.video_id)).size})),
    summary: {channels: channels.length, videos: new Set(jobs.map(j => j.video_id)).size},
  };
}
