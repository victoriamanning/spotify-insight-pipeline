// Server-side only. DATABASE_URL is a Vercel environment variable; it never reaches the browser.
import pg from "pg";

let pool;
export function db() {
  if (!pool) {
    if (!process.env.DATABASE_URL) throw new Error("DATABASE_URL is not configured");
    // Supabase transaction pooler (port 6543) + one short-lived connection per serverless instance.
    pool = new pg.Pool({ connectionString: process.env.DATABASE_URL, max: 1, idleTimeoutMillis: 5000, connectionTimeoutMillis: 8000,
                         ssl: { rejectUnauthorized: false } });
  }
  return pool;
}

export async function latestRun(req) {
  const asked = new URL(req.url, "http://x").searchParams.get("run");
  const { rows } = asked
    ? await db().query("SELECT run_id FROM runs WHERE run_id=$1", [asked])
    : await db().query("SELECT run_id FROM runs ORDER BY published_at DESC LIMIT 1");
  if (!rows.length) throw Object.assign(new Error("run not found"), { status: 404 });
  return rows[0].run_id;
}

export function handler(fn) {
  return async (req, res) => {
    try {
      const data = await fn(req, new URL(req.url, "http://x").searchParams);
      res.setHeader("Cache-Control", "s-maxage=300, stale-while-revalidate=600");
      res.status(200).json(data);
    } catch (e) {
      res.status(e.status || 500).json({ error: e.message });
    }
  };
}
