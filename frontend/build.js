// Vercel build: copy the static pages to dist/, write config.js with the API base,
// and add a Content-Security-Policy that lets the pages talk to that API only.
//   VITE_API_BASE=https://<cloud-run-url> node build.js
const fs = require("fs");
const path = require("path");

const apiBase = (process.env.VITE_API_BASE || "http://localhost:8787").trim().replace(/\/+$/, "");
let apiOrigin;
try {
  apiOrigin = new URL(apiBase).origin;
} catch {
  console.error(`VITE_API_BASE is not a valid URL: ${apiBase}`);
  process.exit(1);
}

const csp = [
  "default-src 'self'",
  "script-src 'self'",
  "style-src 'self' https://fonts.googleapis.com",
  "font-src https://fonts.gstatic.com",
  "img-src 'self' data:",
  `connect-src 'self' ${apiOrigin}`,
  "base-uri 'none'",
  "form-action 'self'",
  "object-src 'none'",
].join("; ");

const src = __dirname;
const out = path.join(src, "dist");
fs.rmSync(out, { recursive: true, force: true });
fs.mkdirSync(out);

for (const name of fs.readdirSync(src)) {
  if (!/\.(html|css|js)$/.test(name) || name === "build.js" || name === "config.js") continue;
  let text = fs.readFileSync(path.join(src, name), "utf8");
  if (name.endsWith(".html")) {
    text = text.replace("<!--CSP-->", `<meta http-equiv="Content-Security-Policy" content="${csp}">`);
  }
  fs.writeFileSync(path.join(out, name), text);
}

const config = `window.VITE_API_BASE = ${JSON.stringify(apiBase)};\n`;
fs.writeFileSync(path.join(out, "config.js"), config);
fs.writeFileSync(path.join(src, "config.js"), config); // for opening the source pages directly
console.log(`Built dist/ with API base ${apiBase}`);
