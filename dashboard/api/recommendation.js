import { db, latestRun, handler } from "../lib/db.js";

// Returns the AI-generated memo plus a live re-check of every claim against the issues table in the database.
export default handler(async (req) => {
  const run = await latestRun(req);
  const [rec] = (await db().query("SELECT * FROM recommendations WHERE run_id=$1", [run])).rows;
  const claims = (await db().query(
    `SELECT c.claim_id, c.issue_id, c.metric, c.value, i.name, i.rank,
            CASE c.metric WHEN 'complaint_count' THEN i.complaint_count::text WHEN 'severity_sum' THEN i.severity_sum::text
                          WHEN 'mean_severity' THEN to_char(i.mean_severity, 'FM999990.000000') WHEN 'priority_score' THEN i.priority_score::text END AS db_value
       FROM claims c LEFT JOIN issues i ON i.run_id=c.run_id AND i.issue_id=c.issue_id WHERE c.run_id=$1 ORDER BY c.claim_id`, [run])).rows;
  for (const c of claims) c.matches_database = c.db_value === c.value;
  return { run, ...rec, claims };
});
