import { db, latestRun, handler } from "../lib/db.js";

export default handler(async (req) => {
  const run = await latestRun(req);
  const { rows } = await db().query("SELECT * FROM issues WHERE run_id=$1 ORDER BY rank", [run]);
  return { run, issues: rows };
});
