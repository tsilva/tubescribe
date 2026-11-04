// Only fixed, parameterized reader queries can be sealed. No client SQL is accepted.
const owner = `JOIN reader_library l
  ON l.id='archive' AND l.clerk_user_id=$1 AND l.issuer=$2`;

export const queries = Object.freeze({
  channels: `SELECT c.id,c.title,c.url FROM channels c ${owner}`,
  feed: `SELECT v.id AS video_id,v.channel_id,v.title,v.url,v.duration,v.published_at,
    j.id AS job_id,j.profile,j.state,j.asr_state,j.download_state,j.updated_at,j.completed_at,
    t.language,t.json_path IS NOT NULL AS transcript_ready,
    d.state AS diarization_state,d.stage AS diarization_stage,d.speaker_count,
    p.state AS postprocessing_state,p.stage AS postprocessing_stage,p.figures,
    p.output_dir IS NOT NULL AS processed_ready,p.manifest_sha256 AS cover_revision,
    a.state AS summary_state,a.stage AS summary_stage,a.updated_at AS summary_updated_at,
    a.output_dir IS NOT NULL AS summary_ready,
    j.next_retry AS asr_retry,j.download_attempts AS asr_downloads,j.asr_attempts AS asr_attempts,
    d.next_retry AS diarization_retry,d.download_attempts AS diarization_downloads,
    d.attempts AS diarization_attempts,d.save_attempts AS diarization_saves,
    p.next_retry AS postprocessing_retry,p.download_attempts AS postprocessing_downloads,
    p.attempts AS postprocessing_attempts,p.save_attempts AS postprocessing_saves,
    a.next_retry AS summary_retry,a.attempts AS summary_attempts,a.save_attempts AS summary_saves
    FROM videos v ${owner}
    LEFT JOIN jobs j ON j.video_id=v.id
    LEFT JOIN transcripts t ON t.job_id=j.id
    LEFT JOIN diarizations d ON d.job_id=j.id
    LEFT JOIN postprocessings p ON p.job_id=j.id
    LEFT JOIN artifacts a ON a.job_id=j.id AND a.workflow='summary'`,
  queue: `SELECT s.id,
    r->'profile' AS profile,r->'postprocessing' AS postprocessing,
    r->'no_summary' AS no_summary,r->'max_attempts' AS max_attempts,r->'wait' AS wait,
    r->'limit' AS "limit",r->'video' AS video,r->'channel' AS channel
    FROM submissions s ${owner} CROSS JOIN LATERAL
      (SELECT CASE WHEN s.request IS JSON OBJECT THEN s.request::jsonb ELSE '{}'::jsonb END AS request) q
    CROSS JOIN LATERAL (SELECT q.request->'options' AS r) options
    WHERE s.state='queued' AND s.pid IS NULL AND q.request->>'command'='work'
      AND jsonb_typeof(q.request->'options')='object'`,
});
