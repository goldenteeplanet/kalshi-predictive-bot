import http from "node:http";
import { readFile } from "node:fs/promises";
import { extname, resolve, sep } from "node:path";

const root = resolve(process.argv[2]);
const port = Number(process.argv[3] || 8767);
const contentTypes = {
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
};
const server = http.createServer(async (request, response) => {
  const pathname = decodeURIComponent(new URL(request.url, `http://127.0.0.1:${port}`).pathname);
  const relative = pathname === "/" ? "ui_obs5c_normal.html" : pathname.replace(/^\/+/, "");
  const target = resolve(root, relative);
  if (!(target === root || target.startsWith(root + sep))) {
    response.writeHead(403).end("Forbidden");
    return;
  }
  try {
    const body = await readFile(target);
    response.writeHead(200, { "Content-Type": contentTypes[extname(target)] || "application/octet-stream", "Cache-Control": "no-store" });
    response.end(body);
  } catch {
    response.writeHead(404).end("Not found");
  }
});
server.listen(port, "127.0.0.1");
