import {build} from "esbuild";
import {readFile} from "node:fs/promises";

const check = process.argv.includes("--check");
const outfile = "src/tubescribe/dashboard_assets/realtime.js";
const result = await build({
  entryPoints: ["src/reader/realtime.js"],
  outfile,
  write: !check,
  bundle: true,
  format: "iife",
  globalName: "TubeScribeRealtime",
  target: "es2022",
  minify: true,
  legalComments: "eof",
});
if (check && !Buffer.from(result.outputFiles[0].contents).equals(await readFile(outfile))) {
  throw new Error("Reader bundle is stale; run pnpm run build:reader");
}
