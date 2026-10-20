-- Postgres (Supabase) schema for the processed results served to the dashboard.
-- Loaded by `python -m pipeline.publish --run-dir runs/final` (requires DATABASE_URL). Re-runnable: each run_id is replaced.

CREATE TABLE IF NOT EXISTS runs (
  run_id text PRIMARY KEY,
  published_at timestamptz NOT NULL DEFAULT now(),
  source_sha256 text NOT NULL,
  label_config text NOT NULL,
  summary jsonb NOT NULL          -- overall metrics, coverage, cost, verifier and golden summaries
);

CREATE TABLE IF NOT EXISTS issues (
  run_id text REFERENCES runs(run_id) ON DELETE CASCADE,
  rank integer NOT NULL,
  issue_id text NOT NULL,
  name text, description text, topic text, subtopic text,
  complaint_count integer NOT NULL, severity_sum integer NOT NULL, mean_severity numeric(10,6) NOT NULL,
  priority_score integer NOT NULL, cancellation_records integer, severity_4_or_5 integer, needs_review integer,
  example_review_ids text[],
  PRIMARY KEY (run_id, issue_id)
);

CREATE TABLE IF NOT EXISTS area_aggregates (
  run_id text REFERENCES runs(run_id) ON DELETE CASCADE,
  scope text NOT NULL,             -- 'all_completed' | 'sample_only'
  area text NOT NULL, topics text, complaints integer, share_of_complaints_pct numeric(6,1),
  severity_sum integer, mean_severity numeric(10,6), cancellation_records integer, severity_4_or_5 integer,
  PRIMARY KEY (run_id, scope, area)
);

CREATE TABLE IF NOT EXISTS monthly_trends (
  run_id text REFERENCES runs(run_id) ON DELETE CASCADE,
  month text NOT NULL, completed_records integer, partial_month boolean, row jsonb NOT NULL,
  PRIMARY KEY (run_id, month)
);

CREATE TABLE IF NOT EXISTS reviews (
  run_id text REFERENCES runs(run_id) ON DELETE CASCADE,
  review_id text NOT NULL,
  status text NOT NULL, reason text,
  topic text, subtopic text, intent text, severity smallint, sentiment real, needs_review boolean,
  evidence_quote text, entities text[], cache_source_id text, issue_id text,
  review_rating smallint, review_likes integer, review_timestamp text, in_sample boolean,
  verifier_topic text, verifier_intent text, verifier_severity smallint,
  PRIMARY KEY (run_id, review_id)
);
CREATE INDEX IF NOT EXISTS reviews_issue_idx ON reviews (run_id, issue_id, severity DESC, review_likes DESC);

CREATE TABLE IF NOT EXISTS status_counts (       -- computed inside Postgres from the reviews table at publish time
  run_id text REFERENCES runs(run_id) ON DELETE CASCADE,
  status text, reason text, n integer, cache_reused integer
);

CREATE TABLE IF NOT EXISTS recommendations (
  run_id text PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
  label_config text, status text, memo jsonb NOT NULL, evidence_pack jsonb NOT NULL, validation_errors jsonb
);

CREATE TABLE IF NOT EXISTS claims (
  run_id text REFERENCES runs(run_id) ON DELETE CASCADE,
  claim_id text, issue_id text, metric text, value text,
  PRIMARY KEY (run_id, claim_id)
);

-- Read-only role for the dashboard backend (optional; run once as the project owner and use its credentials in Vercel):
-- CREATE ROLE dashboard_reader LOGIN PASSWORD '<choose one>'; GRANT USAGE ON SCHEMA public TO dashboard_reader;
-- GRANT SELECT ON ALL TABLES IN SCHEMA public TO dashboard_reader;
