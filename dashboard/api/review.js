import { db, latestRun, handler } from "../lib/db.js";

// Trace one review: source record -> enrichment -> verifier -> issue membership -> rank -> memo claims.
export default handler(async (req, params) => {
  const run = await latestRun(req);
  const id = params.get("id");
  const [r] = (await db().query("SELECT * FROM reviews WHERE run_id=$1 AND review_id=$2", [run, id])).rows;
  if (!r) throw Object.assign(new Error("review not stored (unknown, or outside the classified scope)"), { status: 404 });
  const issue = r.issue_id ? (await db().query("SELECT rank, issue_id, name, complaint_count, severity_sum, mean_severity, priority_score FROM issues WHERE run_id=$1 AND issue_id=$2", [run, r.issue_id])).rows[0] : null;
  const claims = r.issue_id ? (await db().query("SELECT claim_id, metric, value FROM claims WHERE run_id=$1 AND issue_id=$2 ORDER BY claim_id", [run, r.issue_id])).rows : [];
  const [rec] = (await db().query("SELECT evidence_pack FROM recommendations WHERE run_id=$1", [run])).rows;
  const inPack = (rec?.evidence_pack || []).some((e) => e.review_id === id);
  return { run, review: r, issue, claims, in_memo_evidence_pack: inPack };
});
