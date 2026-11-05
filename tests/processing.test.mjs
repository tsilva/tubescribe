import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import test from "node:test";
import {runInNewContext} from "node:vm";

const source = await readFile(new URL("../src/tubescribe/dashboard_assets/app.js", import.meta.url), "utf8");
const functions = source.slice(source.indexOf("function processingRecord("), source.indexOf("function renderFeed("));
const statusFunctions = source.slice(source.indexOf("function human("), source.indexOf("function duration("))
  + source.slice(source.indexOf("function displayStage("), source.indexOf("function syncSelectedRows("));
const video = {video_id: "testvideo01", title: "A discovered upload", channel_id: "channel", duration: 60, stage: "not_processed"};
function element(tag, text, className) {
  return {tag, text, className,
    get textContent() {return this.text;}, set textContent(value) {this.text = value;},
    dataset: {}, attributes: {}, children: [], listeners: {},
    classList: {toggle() {}},
    setAttribute(name, value) {this.attributes[name] = value;},
    append(...children) {this.children.push(...children);},
    prepend(...children) {this.children.unshift(...children);},
    replaceChildren(...children) {this.children = children;},
    addEventListener(name, listener) {this.listeners[name] = listener;},
  };
}
function descendants(row) {return [row, ...row.children.flatMap(descendants)];}
function fixture() {
  const controls = new Map();
  const requests = [];
  let records = [], failure = false, finish;
  const context = {
    ownsLibrary: true, signedInUser: "owner", identityGeneration: 1,
    processingSettings: {profile: "mai-openrouter-v1", price_per_second: 0.1 / 3600,
      chunk_seconds: 60, reference_seconds: 50},
    processingRecords: null, processingRevision: 0, processingBusy: false,
    selectedForProcessing: new Set(), discoveredView: false,
    archive: {channels: [], jobs: [video]}, selectedVideo: null, readerLevel: "video",
    node: element, bestJob: jobs => jobs[0], duration: () => "1m", date: () => "today",
    selectVideo: () => {throw new Error("Process must not open the reader");},
    renderFeed() {},
    $: id => {
      if (!controls.has(id)) controls.set(id, element("div"));
      return controls.get(id);
    },
    libraryFetch: async (path, options) => {
      if (path.includes("/pipeline")) return {ok: true, json: async () => ({steps: []})};
      requests.push({path, options});
      if (!options) return {ok: true, json: async () => ({videos: records})};
      await new Promise(resolve => {finish = resolve;});
      if (failure) throw new Error("Queue temporarily unavailable");
      records = [{video_id: video.video_id, state: "queued"}];
      return {ok: true, json: async () => ({changed: 1})};
    },
  };
  runInNewContext(statusFunctions + functions, context);
  return {context, controls, requests, finish: () => finish(), fail: () => {failure = true;},
    setRecords: value => {records = value;},
    button: () => descendants(context.videoRow([video])).find(node => node.attributes["aria-label"] === `Process ${video.title}`),
  };
}

test("Process queues exactly the clicked library video and shows its queued state", async () => {
  const f = fixture();
  assert.equal(f.button(), undefined, "wait for owner queue status before admitting work");
  await f.context.refreshProcessing();
  assert.equal(f.requests[0].path, "/api/processing", "queue status loads outside Discovered");
  assert.match(f.button().title, /Estimated transcription \$0\.002/);
  const pending = f.button().listeners.click();
  assert.equal(f.button().disabled, true);
  await f.context.changeProcessing("queue", ["another-id"]);
  assert.equal(f.requests.filter(request => request.options).length, 1);
  assert.deepEqual(JSON.parse(f.requests[1].options.body), {action: "queue", video_ids: [video.video_id]});
  f.finish();
  await pending;
  assert.equal(f.button(), undefined);
  assert.ok(descendants(f.context.videoRow([video])).some(node => node.text === "Queued"));
  assert.equal(f.controls.get("processing-message").textContent, "1 video queued.");
  f.context.processingRecords = new Map([[video.video_id, {state: "queued", retry_at: 100}]]);
  assert.equal(f.button(), undefined);
  assert.ok(descendants(f.context.videoRow([video])).some(node => node.text === "Retry scheduled"));
});

test("long-video estimate includes bounded voice reference usage", () => {
  const f = fixture();
  assert.equal(f.context.transcriptionEstimate([120]),
    "Estimated transcription $0.003–$0.005 (includes speaker matching)");
  assert.equal(f.context.transcriptionEstimate([30, 30]), "Estimated transcription $0.002");
});

test("queue-only updates keep the selected reader and row badges in sync", async () => {
  const f = fixture();
  Object.assign(f.context, {
    readerTab: "summary", readerIdentity: "", readerSignature: "", readerController: null, feedRevision: "",
    AbortController, backToLibrary() {}, icon: () => element("img"),
    externalLink: () => element("a"),
  });
  runInNewContext(source.slice(source.indexOf("async function renderReader("), source.indexOf("function applyFeed(")), f.context);
  f.context.renderFeed = () => f.context.renderReader([video], video);
  await f.context.renderReader([video], video);
  const badge = () => descendants(f.controls.get("reader")).find(node => node.className?.startsWith("badge "));
  const assertMatches = label => {
    assert.equal(badge().text, label);
    const rowBadge = descendants(f.context.videoRow([video])).find(node => node.className?.startsWith("badge "));
    assert.equal(rowBadge.text, label);
    assert.equal(rowBadge.className, badge().className);
  };
  assertMatches("Not Processed");
  for (const [record, expected] of [
    [{state: "queued"}, "Queued"],
    [{state: "active"}, "Processing"],
    [{state: "queued", retry_at: 100}, "Retry scheduled"],
    [{state: "failed"}, "Failed"],
    [{state: "interrupted"}, "Interrupted"],
    [{state: "cancelled"}, "Not Processed"],
  ]) {
    f.setRecords([{video_id: video.video_id, ...record}]);
    await f.context.refreshProcessing();
    assertMatches(expected);
  }
});

test("processing status preserves real pipeline progress and confirmed completion", () => {
  const {context} = fixture();
  context.processingRecords = new Map([[video.video_id, {state: "active"}]]);
  assert.equal(context.processingStatus([{...video, stage: "transcribing"}]).label, "Transcribing");
  assert.equal(context.processingStatus([{...video, stage: "failed"}]).label, "Processing");
  for (const state of ["queued", "active", "failed", "interrupted"]) {
    context.processingRecords = new Map([[video.video_id, {state}]]);
    assert.equal(context.processingStatus([{...video, stage: "complete"}]).label, "Complete");
  }
  context.processingRecords = new Map([[video.video_id, {state: "complete"}]]);
  assert.equal(context.processingStatus([video]).label, "Not Processed", "completion requires the catalogue to confirm published content");
});

test("Process is hidden for unavailable queues, other owners and videos already admitted or transcribed", async () => {
  const f = fixture();
  await f.context.refreshProcessing();
  for (const state of ["queued", "active", "complete", "failed", "interrupted"]) {
    f.context.processingRecords = new Map([[video.video_id, {state}]]);
    assert.equal(f.button(), undefined, state);
  }
  f.context.processingRecords = new Map();
  assert.equal(f.context.canQueue([{...video, transcript_url: "/transcript"}]), false);
  assert.equal(f.context.canQueue([{...video, scheduled: true}]), false);
  f.context.ownsLibrary = false;
  assert.equal(f.button(), undefined);
  await f.context.changeProcessing("queue", [video.video_id]);
  await f.context.refreshProcessing();
  assert.equal(f.requests.length, 1);
  f.context.ownsLibrary = true;
  f.context.processingSettings = null;
  assert.equal(f.button(), undefined);
});

test("queue failures leave Process usable and surface feedback outside bulk controls", async () => {
  const f = fixture();
  await f.context.refreshProcessing();
  f.fail();
  const pending = f.button().listeners.click();
  f.finish();
  await pending;
  assert.equal(f.button().disabled, false);
  assert.equal(f.controls.get("processing-message").textContent, "Queue temporarily unavailable");
  const html = await readFile(new URL("../src/tubescribe/dashboard_assets/index.html", import.meta.url), "utf8");
  assert.match(html, /<\/div>\s*<p id="processing-message" role="status">/);
});
