import { db, latestRun, handler } from "../lib/db.js";

export default handler(async (req, params) => {
  const run = await latestRun(req);
  const id = params.get("id");
  const [issue] = (await db().query("SELECT * FROM issues WHERE run_id=$1 AND issue_id=$2", [run, id])).rows;
  if (!issue) throw Object.assign(new Error("issue not found"), { status: 404 });
  const members = (await db().query(
    `SELECT review_id, intent, severity, sentiment, evidence_quote, review_rating, review_likes, review_timestamp, needs_review, cache_source_id
       FROM reviews WHERE run_id=$1 AND issue_id=$2 AND cache_source_id IS NULL
      ORDER BY severity DESC, review_likes DESC NULLS LAST, review_id LIMIT 25`, [run, id])).rows;
  const sev = (await db().query(
    "SELECT severity, COUNT(*)::int n FROM reviews WHERE run_id=$1 AND issue_id=$2 GROUP BY severity ORDER BY severity", [run, id])).rows;
  return { run, issue, severity_distribution: sev, sample_members: members };
});
