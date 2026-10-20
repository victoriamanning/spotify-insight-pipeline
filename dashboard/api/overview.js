import { db, latestRun, handler } from "../lib/db.js";

export default handler(async (req) => {
  const run = await latestRun(req);
  const q = (sql) => db().query(sql, [run]).then((r) => r.rows);
  const [[runRow], areas, trends, counts] = await Promise.all([
    q("SELECT run_id, published_at, source_sha256, label_config, summary FROM runs WHERE run_id=$1"),
    q("SELECT * FROM area_aggregates WHERE run_id=$1 ORDER BY scope, severity_sum DESC"),
    q("SELECT month, completed_records, partial_month, row FROM monthly_trends WHERE run_id=$1 ORDER BY month"),
    q("SELECT status, reason, n, cache_reused FROM status_counts WHERE run_id=$1 ORDER BY n DESC"),
  ]);
  return { run: runRow, areas, trends, db_status_counts: counts };
});
