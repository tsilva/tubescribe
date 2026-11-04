import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import test from "node:test";
import {runInNewContext} from "node:vm";

const source = await readFile(new URL("../src/tubescribe/dashboard_assets/app.js", import.meta.url), "utf8");
const functions = source.slice(source.indexOf("function processingRecord("), source.indexOf("function renderFeed("));
const video = {video_id: "testvideo01", title: "A discovered upload", channel_id: "channel", duration: 60, stage: "not_processed"};
function element(tag, text, className) {
  return {tag, text, className, dataset: {}, attributes: {}, children: [], listeners: {},
    classList: {toggle() {}},
    setAttribute(name, value) {this.attributes[name] = value;},
    append(...children) {this.children.push(...children);},
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
    processingSettings: {profile: "nemotron-openrouter-v1", price_per_second: 0.000033},
    processingRecords: null, processingRevision: 0, processingBusy: false,
    selectedForProcessing: new Set(), discoveredView: false,
    archive: {channels: [], jobs: [video]}, selectedVideo: null, readerLevel: "video",
    node: element, bestJob: jobs => jobs[0], duration: () => "1m", date: () => "today",
    displayStage: job => job.stage, statusBadge: job => element("span", job.stage),
    human: value => value, selectVideo: () => {throw new Error("Process must not open the reader");},
    renderFeed() {},
    $: id => {
      if (!controls.has(id)) controls.set(id, {});
      return controls.get(id);
    },
    libraryFetch: async (path, options) => {
      requests.push({path, options});
      if (!options) return {ok: true, json: async () => ({videos: records})};
      await new Promise(resolve => {finish = resolve;});
      if (failure) throw new Error("Queue temporarily unavailable");
      records = [{video_id: video.video_id, state: "queued"}];
      return {ok: true, json: async () => ({changed: 1})};
    },
  };
  runInNewContext(functions, context);
  return {context, controls, requests, finish: () => finish(), fail: () => {failure = true;},
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
