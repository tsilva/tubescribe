// Only fixed, parameterized reader queries can be sealed. No client SQL is accepted.
const owner = `EXISTS (SELECT 1 FROM reader_library l
  WHERE l.id='archive' AND l.clerk_user_id=$1 AND l.issuer=$2)`;

export const queries = Object.freeze({
  channels: `SELECT c.id,c.title,c.url FROM channels c WHERE ${owner}`,
  feed: `SELECT COALESCE(j.id::text,'video:'||v.id) AS key,
    v.id AS video_id,v.channel_id,v.title,v.url,v.duration,v.published_at,
    j.id AS job_id,j.profile,j.state,j.asr_state,j.download_state,j.updated_at,j.completed_at,
    t.language,t.json_path IS NOT NULL AS transcript_ready,
    d.state AS diarization_state,d.stage AS diarization_stage,d.speaker_count,
    p.state AS postprocessing_state,p.stage AS postprocessing_stage,p.figures,
    p.output_dir IS NOT NULL AS processed_ready,p.manifest_sha256 AS cover_revision,
    a.state AS summary_state,a.stage AS summary_stage,a.updated_at AS summary_updated_at,
    a.output_dir IS NOT NULL AS summary_ready,
    jsonb_build_array(
      jsonb_build_array(j.state,NULL,j.next_retry,j.download_attempts,j.asr_attempts,0),
      jsonb_build_array(d.state,d.stage,d.next_retry,d.download_attempts,d.attempts,d.save_attempts),
      jsonb_build_array(p.state,p.stage,p.next_retry,p.download_attempts,p.attempts,p.save_attempts),
      jsonb_build_array(a.state,a.stage,a.next_retry,0,a.attempts,a.save_attempts)
    ) AS scheduling
    FROM videos v LEFT JOIN jobs j ON j.video_id=v.id
    LEFT JOIN transcripts t ON t.job_id=j.id
    LEFT JOIN diarizations d ON d.job_id=j.id
    LEFT JOIN postprocessings p ON p.job_id=j.id
    LEFT JOIN artifacts a ON a.job_id=j.id AND a.workflow='summary'
    WHERE ${owner}`,
  queue: `SELECT s.id,
    jsonb_build_object('profile',r->'profile','postprocessing',r->'postprocessing',
      'no_summary',r->'no_summary','max_attempts',r->'max_attempts','wait',r->'wait',
      'limit',r->'limit','video',r->'video','channel',r->'channel') AS options
    FROM submissions s CROSS JOIN LATERAL
      (SELECT CASE WHEN s.request IS JSON OBJECT THEN s.request::jsonb ELSE '{}'::jsonb END AS request) q
    CROSS JOIN LATERAL (SELECT q.request->'options' AS r) options
    WHERE s.state='queued' AND s.pid IS NULL AND q.request->>'command'='work'
      AND jsonb_typeof(q.request->'options')='object' AND ${owner}`,
});
