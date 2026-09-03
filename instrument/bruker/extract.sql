-- Bruker Compass maintenance signal extraction -> single JSON document.
-- Read-only. Assumes filtered restore of cst.*, cdr.data_set, mm.*, cst.station.

WITH runs AS (
  SELECT
    d.file_system_path AS fp,
    split_part(replace(d.file_system_path, E'\\', '/'), '/', -1) AS fname,
    t.status,
    t.status_text,
    t.start_date,
    t.end_date,
    EXTRACT(EPOCH FROM (t.end_date - t.start_date))/60.0 AS dur_min
  FROM cdr.data_set d
  JOIN cst.task t ON t.analysis_fk = d.origin_id
  WHERE t.start_date IS NOT NULL
),
parsed AS (
  SELECT r.*,
    CASE
      WHEN substring(fname from '^([0-9]{8})') ~ '^(19|20)[0-9][0-9](0[1-9]|1[0-2])(0[1-9]|[12][0-9]|3[01])$'
        THEN to_date(substring(fname from '^([0-9]{8})'),'YYYYMMDD')
      WHEN substring(fname from '^([0-9]{8})') ~ '^(0[1-9]|1[0-2])(0[1-9]|[12][0-9]|3[01])(19|20)[0-9][0-9]$'
        THEN to_date(substring(fname from '^([0-9]{8})'),'MMDDYYYY')
      ELSE NULL END AS run_date,
    substring(fname from '(S[0-9]+-[A-H][0-9]{1,2})') AS well,
    regexp_replace(lower(coalesce(substring(fname from '_([0-9]{1,4}[sS][pP][dD])'),'')),' ','') AS method_spd,
    CASE
      WHEN status IN ('DONE') THEN 'ok'
      WHEN status_text ~* 'no evotip|tip expected but not present|tip .*not present' THEN 'Evotip missing / not picked up'
      WHEN status_text ~* 'pressure' THEN 'LC pressure / clog'
      WHEN status_text ~* 'connection|paser|client connection aborted' THEN 'Connection lost'
      WHEN status_text ~* 'otof|python|proteoscape|scan mode|calibration process|hystar' THEN 'MS / acquisition software error'
      WHEN status_text ~* 'cancel|1800s|timeout' THEN 'Timeout / canceled'
      WHEN status IN ('FAILED','ABORTED') THEN 'Other failure'
      ELSE 'In progress'
    END AS category
  FROM runs r
),
latest_day AS (
  SELECT max(run_date) AS d FROM parsed WHERE run_date IS NOT NULL
)
SELECT json_build_object(
  'generated_at', to_char(now() AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS"Z"'),
  'backup_path', :'backup_path',
  'backup_date', :'backup_date',
  'instrument', (SELECT json_build_object(
        'name', name, 'status', status,
        'status_text', left(coalesce(status_text,''),200)) FROM cst.station ORDER BY name LIMIT 1),
  'summary', (SELECT json_build_object(
        'total_runs', count(*),
        'done', count(*) FILTER (WHERE status='DONE'),
        'failed', count(*) FILTER (WHERE status='FAILED'),
        'aborted', count(*) FILTER (WHERE status='ABORTED'),
        'success_rate_pct', round(100.0*count(*) FILTER (WHERE status='DONE')/nullif(count(*),0),1),
        'first_run', to_char(min(start_date),'YYYY-MM-DD'),
        'last_run', to_char(max(start_date),'YYYY-MM-DD"T"HH24:MI:SS'),
        'active_days', (SELECT count(DISTINCT run_date) FROM parsed WHERE run_date IS NOT NULL),
        'span_days', (max(start_date)::date - min(start_date)::date),
        'median_run_min', round(percentile_cont(0.5) WITHIN GROUP (
              ORDER BY dur_min) FILTER (WHERE status='DONE' AND dur_min BETWEEN 0 AND 360)::numeric,1)
      ) FROM parsed),
  'throughput_monthly', (SELECT coalesce(json_agg(x ORDER BY x.month),'[]'::json) FROM (
        SELECT to_char(date_trunc('month',start_date),'YYYY-MM') AS month,
               count(*) AS total,
               count(*) FILTER (WHERE status='DONE') AS done,
               count(*) FILTER (WHERE status IN ('FAILED','ABORTED')) AS failed
        FROM parsed GROUP BY 1) x),
  'throughput_daily', (SELECT coalesce(json_agg(x ORDER BY x.day),'[]'::json) FROM (
        SELECT to_char(run_date,'YYYY-MM-DD') AS day,
               count(*) AS total,
               count(*) FILTER (WHERE status='DONE') AS done,
               count(*) FILTER (WHERE status IN ('FAILED','ABORTED')) AS failed
        FROM parsed WHERE run_date >= (SELECT max(run_date) FROM parsed) - 120
        GROUP BY 1) x),
  'failures_by_category', (SELECT coalesce(json_agg(x ORDER BY x.count DESC),'[]'::json) FROM (
        SELECT category, count(*) AS count
        FROM parsed WHERE status IN ('FAILED','ABORTED') GROUP BY 1) x),
  'failures_recent', (SELECT coalesce(json_agg(x ORDER BY x.start_date DESC),'[]'::json) FROM (
        SELECT to_char(start_date,'YYYY-MM-DD HH24:MI') AS start_date,
               fname, well, category,
               left(regexp_replace(coalesce(status_text,''),'\s+',' ','g'),140) AS message
        FROM parsed WHERE status IN ('FAILED','ABORTED')
        ORDER BY start_date DESC LIMIT 60) x),
  'duration_trend_monthly', (SELECT coalesce(json_agg(x ORDER BY x.month),'[]'::json) FROM (
        SELECT to_char(date_trunc('month',start_date),'YYYY-MM') AS month,
               round(percentile_cont(0.5) WITHIN GROUP (ORDER BY dur_min)::numeric,1) AS median_min,
               round(percentile_cont(0.9) WITHIN GROUP (ORDER BY dur_min)::numeric,1) AS p90_min,
               count(*) AS n
        FROM parsed WHERE status='DONE' AND dur_min BETWEEN 0 AND 360
        GROUP BY 1) x),
  'methods', (SELECT coalesce(json_agg(x ORDER BY x.count DESC),'[]'::json) FROM (
        SELECT CASE WHEN method_spd='' THEN 'other/unspecified' ELSE method_spd END AS method,
               count(*) AS count,
               count(*) FILTER (WHERE status='DONE') AS done,
               count(*) FILTER (WHERE status IN ('FAILED','ABORTED')) AS failed
        FROM parsed GROUP BY 1) x),
  'utilization_monthly', (SELECT coalesce(json_agg(x ORDER BY x.month),'[]'::json) FROM (
        SELECT to_char(date_trunc('month',start_date),'YYYY-MM') AS month,
               count(DISTINCT start_date::date) AS active_days,
               count(*) AS runs,
               round((sum(dur_min) FILTER (WHERE dur_min BETWEEN 0 AND 360)/60.0
                     / (24.0*EXTRACT(DAY FROM (date_trunc('month',min(start_date))+interval '1 month'-interval '1 day')))
                     *100.0)::numeric,1) AS duty_pct
        FROM parsed GROUP BY 1) x),
  'idle', (SELECT json_build_object(
        'last_run', to_char(max(start_date),'YYYY-MM-DD"T"HH24:MI:SS'),
        'longest_gap_days', (SELECT round(max(gap)::numeric,1) FROM (
              SELECT EXTRACT(EPOCH FROM (start_date - lag(start_date) OVER (ORDER BY start_date)))/86400.0 AS gap
              FROM parsed WHERE status='DONE') g),
        'longest_gap_end', (SELECT to_char(start_date,'YYYY-MM-DD') FROM (
              SELECT start_date, lag(start_date) OVER (ORDER BY start_date) AS prev,
                     EXTRACT(EPOCH FROM (start_date - lag(start_date) OVER (ORDER BY start_date)))/86400.0 AS gap
              FROM parsed WHERE status='DONE') g ORDER BY gap DESC NULLS LAST LIMIT 1),
        'longest_gap_start', (SELECT to_char(prev,'YYYY-MM-DD') FROM (
              SELECT lag(start_date) OVER (ORDER BY start_date) AS prev,
                     EXTRACT(EPOCH FROM (start_date - lag(start_date) OVER (ORDER BY start_date)))/86400.0 AS gap
              FROM parsed WHERE status='DONE') g ORDER BY gap DESC NULLS LAST LIMIT 1)
      ) FROM parsed),
  'latest_plate', (SELECT json_build_object(
        'date', to_char((SELECT d FROM latest_day),'YYYY-MM-DD'),
        'n_total', count(*),
        'n_pass', count(*) FILTER (WHERE status='DONE'),
        'n_fail', count(*) FILTER (WHERE status IN ('FAILED','ABORTED')),
        'wells', (SELECT coalesce(json_agg(w ORDER BY w.well),'[]'::json) FROM (
              SELECT well,
                     substring(fname from '[sSpPdD]_([A-Za-z0-9:.+-]+)_S[0-9]+-') AS sample,
                     max(status) AS status,
                     max(category) AS category,
                     left(max(coalesce(status_text,'')),120) AS message
              FROM parsed WHERE run_date=(SELECT d FROM latest_day) AND well IS NOT NULL
              GROUP BY well, sample) w)
      ) FROM parsed WHERE run_date=(SELECT d FROM latest_day))
);
