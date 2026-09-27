// Copies the web-ifc wasm binaries into public/wasm/ so the IFC loader runs
// without a CDN. Run automatically by `pnpm dev` and `pnpm build`.
// The wasm must match the web-ifc JS that the app bundles (pinned in
// package.json), so it is copied from the same installed package every time.
import { copyFileSync, mkdirSync, readdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const webIfcDir = join(root, "node_modules", "web-ifc");
const { version } = JSON.parse(readFileSync(join(webIfcDir, "package.json"), "utf8"));
const outDir = join(root, "public", "wasm");
mkdirSync(outDir, { recursive: true });
for (const f of readdirSync(webIfcDir)) {
  if (f.endsWith(".wasm") && !f.includes("node")) {
    copyFileSync(join(webIfcDir, f), join(outDir, f));
  }
}
console.log(`web-ifc ${version} wasm copied to ${outDir}`);
