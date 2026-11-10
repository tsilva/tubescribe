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
    processingRecords: null, processingRevision: 0, pendingProcessing: new Set(),
    selectedForProcessing: new Set(),
    archive: {channels: [], jobs: [video]}, selectedVideo: null, readerLevel: "video",
    setInterval: () => 1, clearInterval: () => {},
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
    button: (item = video) => descendants(context.videoRow([item])).find(node => node.dataset.focus === `process-${item.video_id}`),
  };
}

test("failed and interrupted videos offer Retry and submit a retry for that video", async () => {
  for (const state of ["failed", "interrupted"]) {
    const f = fixture();
    await f.context.refreshProcessing();
    f.context.processingRecords = new Map([[video.video_id, {state}]]);
    assert.equal(f.button().textContent, "Retry");
    assert.equal(f.button().disabled, false);
    const pending = f.button().listeners.click();
    assert.equal(f.button().disabled, true);
    assert.deepEqual(JSON.parse(f.requests.at(-1).options.body), {action: "retry", video_ids: [video.video_id]});
    f.finish();
    await pending;
    assert.equal(f.context.canRetry([video]), false, "a queued retry cannot be admitted again");
  }
});

test("retry remains available with saved transcript progress and respects library ownership", async () => {
  const f = fixture();
  await f.context.refreshProcessing();
  f.context.processingRecords = new Map([[video.video_id, {state: "failed"}]]);
  const saved = {...video, stage: "failed", transcript_url: "/saved-transcript"};
  assert.equal(f.context.canQueue([saved]), false);
  assert.ok(descendants(f.context.videoRow([saved])).some(node => node.dataset.focus === `process-${video.video_id}`));
  f.context.ownsLibrary = false;
  assert.equal(f.context.canRetry([saved]), false);
  f.context.ownsLibrary = true;
  f.context.processingSettings = null;
  assert.equal(f.context.canRetry([saved]), false);
});

test("Process queues exactly the clicked library video and keeps a spinner while queued", async () => {
  const f = fixture();
  assert.equal(f.button(), undefined, "wait for owner queue status before admitting work");
  await f.context.refreshProcessing();
  assert.equal(f.requests[0].path, "/api/processing");
  assert.match(f.button().title, /Estimated transcription \$0\.002/);
  const pending = f.button().listeners.click();
  assert.equal(f.button().disabled, true);
  await f.context.changeProcessing("queue", [video.video_id]);
  assert.equal(f.requests.filter(request => request.options).length, 1);
  assert.deepEqual(JSON.parse(f.requests[1].options.body), {action: "queue", video_ids: [video.video_id]});
  f.finish();
  await pending;
  assert.equal(f.button().disabled, true);
  assert.equal(f.button().attributes["aria-busy"], "true");
  assert.equal(f.button().title, "Queued");
  assert.ok(descendants(f.context.videoRow([video])).some(node => node.className === "badge queued" && node.text === "Queued"));
  assert.equal(f.controls.get("processing-message").textContent, "1 video queued.");
  f.context.processingRecords = new Map([[video.video_id, {state: "queued", retry_at: 100}]]);
  assert.equal(f.button().disabled, true);
  assert.equal(f.button().title, "Retry scheduled");
});

test("library rows support bulk selection and removal of waiting videos", async () => {
  const f = fixture();
  const other = {...video, video_id: "other-video"};
  f.context.archive.jobs.push(other);
  await f.context.refreshProcessing();
  for (const item of [video, other]) {
    const checkbox = descendants(f.context.videoRow([item])).find(node => node.type === "checkbox");
    checkbox.checked = true;
    checkbox.listeners.change();
  }
  assert.equal(f.controls.get("queue-selected").disabled, false);
  assert.match(f.controls.get("processing-estimate").textContent, /^2 selected/);
  const queue = f.context.changeProcessing("queue", [...f.context.selectedForProcessing]);
  assert.deepEqual(JSON.parse(f.requests.at(-1).options.body), {action: "queue", video_ids: [video.video_id, other.video_id]});
  f.finish();
  await queue;
  assert.equal(f.controls.get("queue-selected").disabled, true);

  const remove = () => descendants(f.context.videoRow([video])).find(node => node.dataset.focus === `remove-${video.video_id}`);
  const cancellation = remove().listeners.click();
  assert.equal(remove().disabled, true);
  assert.deepEqual(JSON.parse(f.requests.at(-1).options.body), {action: "cancel", video_ids: [video.video_id]});
  f.finish();
  await cancellation;
  f.context.processingRecords.set(video.video_id, {state: "active"});
  assert.equal(remove(), undefined);
});

test("long-video estimate includes bounded voice reference usage", () => {
  const f = fixture();
  assert.equal(f.context.transcriptionEstimate([120]),
    "Estimated transcription $0.003–$0.005 (includes speaker matching)");
  assert.equal(f.context.transcriptionEstimate([30, 30]), "Estimated transcription $0.002");
});

test("queue updates keep library row and reader status pills in sync", async () => {
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

test("Process admission stays unavailable for other owners and previously admitted videos", async () => {
  const f = fixture();
  await f.context.refreshProcessing();
  for (const state of ["queued", "active", "complete"]) {
    f.context.processingRecords = new Map([[video.video_id, {state}]]);
    assert.equal(f.context.canQueue([video]), false, state);
    assert.equal(Boolean(f.button()), ["queued", "active"].includes(state), state);
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

test("independent videos can be queued concurrently and only their own request is locked", async () => {
  const f = fixture();
  await f.context.refreshProcessing();
  const other = {...video, video_id: "other-video", title: "Another upload"};
  const first = f.button().listeners.click();
  const finishFirst = f.finish;
  // Keep the second request pending while the first completes.
  let releaseSecond;
  const fetch = f.context.libraryFetch;
  f.context.libraryFetch = async (path, options) => {
    if (!options) return fetch(path, options);
    f.requests.push({path, options});
    await new Promise(resolve => {releaseSecond = resolve;});
    return {ok: true, json: async () => ({changed: 1})};
  };
  assert.equal(f.button(other).disabled, false);
  const second = f.button(other).listeners.click();
  assert.equal(f.button(other).disabled, true);
  await f.context.changeProcessing("queue", [video.video_id]);
  assert.equal(f.requests.filter(request => request.options).length, 2);
  finishFirst();
  await first;
  assert.equal(f.context.pendingProcessing.has(video.video_id), false);
  assert.equal(f.context.pendingProcessing.has(other.video_id), true);
  releaseSecond();
  await second;
  assert.equal(f.context.pendingProcessing.size, 0);
});

test("library buttons show queued and active spinners alongside status pills", () => {
  const {context} = fixture();
  for (const stage of ["queued", "downloading", "transcribing", "awaiting_diarization", "diarizing",
    "awaiting_postprocessing", "postprocessing", "awaiting_summary", "summarizing"]) {
    const row = context.videoRow([{...video, stage, scheduled: true}]);
    const nodes = descendants(row);
    assert.ok(nodes.some(node => node.className === `badge ${stage}`), stage);
    assert.equal(nodes.find(node => node.className === "button process-button").disabled, true, stage);
    assert.ok(nodes.some(node => node.className === "process-spinner"), stage);
  }
  const completed = descendants(context.videoRow([{...video, stage: "complete"}]));
  assert.equal(completed.some(node => node.className === "process-spinner"), false);
  assert.ok(completed.some(node => node.className === "completion-badge"));
});

test("account changes prevent an old request from unlocking a new owner's video", async () => {
  const f = fixture();
  await f.context.refreshProcessing();
  const pending = f.button().listeners.click();
  f.context.identityGeneration += 1;
  f.context.pendingProcessing.clear();
  f.context.pendingProcessing.add(video.video_id);
  f.finish();
  await pending;
  assert.equal(f.context.pendingProcessing.has(video.video_id), true);
});
