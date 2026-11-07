import {createRequire} from "node:module";
import {copyFile, mkdir, readFile, readdir} from "node:fs/promises";
import {dirname, join} from "node:path";

const require = createRequire(import.meta.url);
const root = dirname(require.resolve("@clerk/ui/package.json"));
const {version} = require("@clerk/ui/package.json");
const dist = join(root, "dist");
const entry = "ui.browser.js";
const source = await readFile(join(dist, entry), "utf8");
const chunkHash = source.match(/\.h=\(\)=>"([0-9a-f]{6})[0-9a-f]+"/)?.[1];
if (!chunkHash) throw new Error("Could not identify the pinned Clerk UI browser chunks");
const chunks = (await readdir(dist)).filter(file => file.endsWith(`_ui_${chunkHash}_${version}.js`));
if (!chunks.length) throw new Error("The pinned Clerk UI browser chunks are missing");

const out = join("clerk-ui-build", version);
await mkdir(out, {recursive: true});
await Promise.all([
  copyFile(join(root, "LICENSE"), join(out, "LICENSE")),
  ...[entry, ...chunks].map(file => copyFile(join(dist, file), join(out, file))),
]);
console.log(`Staged Clerk UI ${version} with ${chunks.length} browser chunks`);
