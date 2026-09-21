import { cp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { resolve } from "node:path";

const root = fileURLToPath(new URL(".", import.meta.url));
const source = resolve(root, "src");
const output = resolve(root, "dist");
const apiBaseUrl = (process.env.VITE_API_BASE_URL || "").trim().replace(/\/$/, "");

await rm(output, { recursive: true, force: true });
await mkdir(output, { recursive: true });
await cp(source, output, { recursive: true });

const indexPath = resolve(output, "index.html");
const index = await readFile(indexPath, "utf8");
await writeFile(
  indexPath,
  index.replace("__PAGES_BASE__", process.env.PAGES_BASE_PATH || "./"),
  "utf8",
);
await writeFile(
  resolve(output, "config.js"),
  `window.MONITOR_CONFIG = ${JSON.stringify({ apiBaseUrl, mode: apiBaseUrl ? "configured" : "demo" })};\n`,
  "utf8",
);
