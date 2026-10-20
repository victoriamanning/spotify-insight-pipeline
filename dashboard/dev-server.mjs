// Local preview only: serves index.html and the same api/*.js handlers Vercel runs. Reads DATABASE_URL from ../.env.
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
for (const line of fs.readFileSync(path.join(here, "..", ".env"), "utf8").split("\n")) {
  const m = line.match(/^([A-Z_]+)=(.*)$/);
  if (m && !process.env[m[1]]) process.env[m[1]] = m[2].trim();
}
const port = Number(process.env.PORT || 3007);
http.createServer(async (req, res) => {
  const url = new URL(req.url, "http://localhost");
  if (url.pathname.startsWith("/api/")) {
    const file = path.join(here, "api", url.pathname.slice(5).replace(/[^a-z]/g, "") + ".js");
    if (!fs.existsSync(file)) { res.writeHead(404); return res.end("{}"); }
    const mod = await import(pathToFileURL(file).href);
    res.status = (c) => { res.statusCode = c; return res; };
    res.json = (o) => { res.setHeader("Content-Type", "application/json"); res.end(JSON.stringify(o)); };
    return mod.default(req, res);
  }
  res.setHeader("Content-Type", "text/html; charset=utf-8");
  res.end(fs.readFileSync(path.join(here, "index.html")));
}).listen(port, () => console.log(`dashboard preview on http://localhost:${port}`));
