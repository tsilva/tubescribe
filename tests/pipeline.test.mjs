import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import test from "node:test";
import {runInNewContext} from "node:vm";

const source = await readFile(new URL("../src/tubescribe/dashboard_assets/app.js", import.meta.url), "utf8");
function element(tag, text, className) {
  let value = text ?? "";
  return {tag, className, dataset: {}, attributes: {}, children: [], listeners: {},
    get textContent() {return value + this.children.map(child => child.textContent).join("");},
    set textContent(text) {this.replaceChildren(); value = text ?? "";},
    append(...children) {for (const child of children) {child.remove(); child.parent = this; this.children.push(child);}},
    prepend(...children) {for (const child of children.reverse()) {child.remove(); child.parent = this; this.children.unshift(child);}},
    replaceChildren(...children) {for (const child of [...this.children]) child.remove(); value = ""; this.append(...children);},
    insertBefore(child, before) {child.remove(); child.parent = this; this.children.splice(this.children.indexOf(before), 0, child);},
    remove() {if (this.parent) {this.parent.children.splice(this.parent.children.indexOf(this), 1); this.parent = null;}},
    setAttribute(name, value) {this.attributes[name] = value;},
    addEventListener(name, listener) {this.listeners[name] = listener;}, focus() {this.focused = true;},
  };
}
const flatten = node => [node, ...node.children.flatMap(flatten)];
const job = {video_id: "test", profile: "hosted", stage: "queued", title: "Test video"};
const data = {complete: true, cost_usd: 0.002, cost_complete: false, cost_note: "Recorded charges only.",
  steps: [{key: "transcription", label: "Transcription", state: "complete", cost_usd: 0.002, cost_complete: true,
    elapsed_seconds: 1.25, attempts: 2, total_tokens: 100},
  {key: "diarization", label: "Speaker attribution", state: "included", note: "Included in hosted transcription"},
  {key: "summary", label: "Summary", state: "pending"}],
  duration_seconds: 120, total_tokens: 100, speakers: 0, figures: 0, model: "Example", profile: "hosted"};
function fixture() {
  const controls = new Map(), requests = [];
  let result = data, failure = false;
  const context = {readerIdentity: "", readerTab: "summary", readerSignature: "", readerController: null,
    feedRevision: "1", node: element, AbortController, URL, location: {href: "http://localhost/"},
    processingRecord: () => null, processingStatus: () => ({stage: "queued", label: "Queued"}),
    statusBadge: () => element("span", "Queued"), externalLink: () => element("a"),
    icon: () => element("img"), backToLibrary() {}, setReaderOpen() {}, date: value => value ? "Today" : "", duration: value => `${value}s`,
    human: value => value[0].toUpperCase() + value.slice(1), readingCache: new Map(),
    resourceLink: () => element("a"),
    $: id => {if (!controls.has(id)) controls.set(id, element("div")); return controls.get(id);},
    libraryFetch: async (url, options) => {requests.push({url, options}); if (failure) throw new Error("Offline");
      return {ok: true, json: async () => result};},
  };
  runInNewContext(source.slice(source.indexOf("function resetReader("), source.indexOf("function applyFeed(")), context);
  return {context, requests, nodes: () => flatten(controls.get("reader")), pane: () => controls.get("reader"),
    result: value => {result = value;}, fail: () => {failure = true;}};
}

test("unprocessed videos have an accessible Pipeline tab and no document links", async () => {
  const f = fixture();
  await f.context.renderReader([job], job);
  assert.equal(f.requests[0].url, "/api/videos/test/pipeline?profile=hosted");
  const tab = f.nodes().find(n => n.attributes.role === "tab");
  assert.equal(tab.textContent, "Pipeline");
  assert.equal(tab.attributes["aria-selected"], "true");
  assert.equal(f.nodes().find(n => n.attributes.role === "tabpanel").attributes["aria-labelledby"], "reader-tab-pipeline");
  assert.equal(f.nodes().some(n => n.className === "reader-document-actions"), false);
  assert.ok(f.nodes().some(n => n.textContent === "$0.0020"));
  assert.ok(f.nodes().some(n => n.textContent === "Processing cost · partial"));
  assert.ok(f.nodes().some(n => n.textContent === "Identifies speakers as part of transcription."));
  assert.ok(f.nodes().some(n => n.textContent === "Waiting"));
  assert.equal(f.nodes().some(n => n.className === "pipeline-cost-note"), false);
});

test("unknown costs stay explicit and zero values remain visible", () => {
  const f = fixture(), content = element("div");
  f.context.renderPipeline(content, {...data, cost_usd: null}, {label: "Queued"});
  const nodes = flatten(content);
  assert.ok(nodes.some(n => n.textContent === "Not recorded"));
  assert.equal(nodes.filter(n => n.tag === "dd" && n.textContent === "0").length, 2);
  assert.equal(nodes.some(n => n.textContent === "Cost not recorded"), false);
  assert.ok(nodes.some(n => n.textContent === "Turns speech into text with word-level timestamps."));
});

test("pipeline reloads on catalogue revision changes and offers retry on failures", async () => {
  const f = fixture();
  await f.context.renderReader([job], job);
  await f.context.renderReader([job], job);
  assert.equal(f.requests.length, 1);
  f.context.feedRevision = "2";
  await f.context.renderReader([job], job);
  assert.equal(f.requests.length, 2);
  f.fail(); f.context.feedRevision = "3";
  await f.context.renderReader([job], job);
  assert.ok(f.nodes().some(n => n.textContent === "Offline"));
  assert.ok(f.nodes().some(n => n.tag === "button" && n.textContent === "Try again"));
});

test("saved document tabs remain the default and Pipeline follows them", async () => {
  const f = fixture();
  const saved = {...job, summary_url: "/summary/", transcript_url: "/transcript/", srt_url: "/srt/"};
  f.context.libraryFetch = async () => {throw new Error("Document unavailable");};
  await f.context.renderReader([saved], saved);
  const tabs = f.nodes().filter(n => n.attributes.role === "tab");
  assert.deepEqual(tabs.map(n => n.textContent), ["Summary", "Transcript", "SRT timestamps", "Pipeline"]);
  assert.equal(tabs[0].attributes["aria-selected"], "true");
  await tabs[3].listeners.click();
  assert.equal(f.context.readerTab, "pipeline");
});

test("late pipeline responses cannot replace another selected video", async () => {
  const f = fixture();
  let finish;
  const delayed = new Promise(resolve => {finish = resolve;});
  f.context.libraryFetch = async () => ({ok: true, json: () => delayed});
  const pending = f.context.renderReader([job], job);
  await Promise.resolve();
  f.context.libraryFetch = async () => ({ok: true, json: async () => ({...data, model: "Current model"})});
  const other = {...job, video_id: "other"};
  await f.context.renderReader([other], other);
  finish({...data, model: "Stale model"});
  await pending;
  assert.ok(f.nodes().some(n => n.textContent === "Current model"));
  assert.equal(f.nodes().some(n => n.textContent === "Stale model"), false);
});


test("hosted speaker attribution does not count as completed work before transcription", () => {
  const f = fixture(), content = element("div");
  f.context.renderPipeline(content, {...data, complete: false,
    steps: data.steps.map(step => step.state === "included" ? step : {...step, state: "pending"})}, {label: "Queued"});
  const bar = flatten(content).find(n => n.tag === "progress");
  assert.equal(bar.max, 2);
  assert.equal(bar.value, 0);
  assert.equal(bar.attributes["aria-label"], "Video processing progress");
  assert.equal(bar.attributes["aria-valuetext"], "Queued. 0 of 2 steps complete");
});

test("live progress patches retain panel, metric, step, tab and scroll state", async () => {
  const f = fixture();
  await f.context.renderReader([job], job);
  const pane = f.pane(), panel = pane.readerView.content, metric = panel.pipelineView.stats[0].value;
  const step = panel.pipelineView.steps[0].badge, bar = panel.pipelineView.progress;
  const tab = pane.readerView.tabButtons.get("pipeline");
  pane.scrollTop = 400; tab.focus();
  f.result({...data, cost_usd: 0.004, steps: data.steps.map(s => s.key === "summary" ? {...s, state: "complete"} : s)});
  f.context.feedRevision = "2";
  await f.context.renderReader([job], job);
  assert.equal(pane.readerView.content, panel);
  assert.equal(panel.pipelineView.stats[0].value, metric);
  assert.equal(panel.pipelineView.steps[0].badge, step);
  assert.equal(panel.pipelineView.progress, bar);
  assert.equal(metric.textContent, "$0.0040");
  assert.equal(bar.value, 2);
  assert.equal(pane.readerView.tabButtons.get("pipeline"), tab);
  assert.equal(tab.focused, true);
  assert.equal(pane.scrollTop, 400);
});

test("publishing document tabs keeps Pipeline mounted and its keyboard navigation current", async () => {
  const f = fixture();
  await f.context.renderReader([job], job);
  const pane = f.pane(), view = pane.readerView, tab = view.tabButtons.get("pipeline");
  const saved = {...job, summary_url: "/summary/", transcript_url: "/transcript/", srt_url: "/srt/"};
  f.context.feedRevision = "2";
  await f.context.renderReader([saved], saved);
  assert.equal(pane.readerView, view);
  assert.equal(view.tabButtons.get("pipeline"), tab);
  assert.deepEqual(view.tabList.children.map(n => n.textContent), ["Summary", "Transcript", "SRT timestamps", "Pipeline"]);
  assert.equal(f.context.readerTab, "pipeline");
  await tab.listeners.keydown({key: "ArrowRight", preventDefault() {}});
  assert.equal(f.context.readerTab, "summary");
});

test("failed background updates preserve values and a retry repairs the same panel", async () => {
  const f = fixture();
  await f.context.renderReader([job], job);
  const panel = f.pane().readerView.content, metric = panel.pipelineView.stats[0].value;
  const goodFetch = f.context.libraryFetch;
  f.context.libraryFetch = async () => {throw new Error("Offline");};
  f.context.feedRevision = "2";
  await f.context.renderReader([job], job);
  assert.equal(metric.textContent, "$0.0020");
  assert.ok(f.nodes().some(n => n.textContent === "Offline"));
  f.context.libraryFetch = goodFetch;
  f.result({...data, cost_usd: 0.006});
  f.context.readerSignature = "";
  await f.context.renderReader([job], job);
  assert.equal(f.pane().readerView.content, panel);
  assert.equal(metric.textContent, "$0.0060");
  assert.equal(f.nodes().some(n => n.textContent === "Offline"), false);
});

test("queue updates do not duplicate an in-flight pipeline read", async () => {
  const f = fixture();
  let finish;
  const delayed = new Promise(resolve => {finish = resolve;});
  f.context.libraryFetch = async (...args) => {
    f.requests.push(args);
    return {ok: true, json: async () => delayed};
  };
  const pending = f.context.renderReader([job], job);
  await Promise.resolve();
  f.context.processingStatus = () => ({stage: "active", label: "Processing"});
  await f.context.renderReader([job], job);
  finish(data);
  await pending;
  assert.equal(f.requests.length, 1);
  assert.equal(f.pane().readerView.status.label, "Processing");
});

test("intervening revisions coalesce and stale responses cannot overwrite current progress", async () => {
  const f = fixture();
  await f.context.renderReader([job], job);
  const panel = f.pane().readerView.content;
  let finish;
  const delayed = new Promise(resolve => {finish = resolve;});
  let reads = 0;
  f.context.libraryFetch = async () => {
    reads++;
    return {ok: true, json: async () => reads === 1 ? delayed : {...data, cost_usd: 0.009}};
  };
  f.context.feedRevision = "2";
  const pending = f.context.renderReader([job], job);
  await Promise.resolve();
  f.context.feedRevision = "3";
  const newer = f.context.renderReader([job], job);
  f.context.feedRevision = "4";
  const newest = f.context.renderReader([job], job);
  assert.equal(panel.pipelineView.stats[0].value.textContent, "$0.0020");
  finish({...data, cost_usd: 0.001});
  await Promise.all([pending, newer, newest]);
  assert.equal(reads, 2);
  assert.equal(panel.pipelineView.stats[0].value.textContent, "$0.0090");
});

test("queue and library revisions do not reload a saved document or reset its scroll", async () => {
  const f = fixture();
  let reads = 0;
  f.context.libraryFetch = async () => {
    reads++;
    return {ok: true, text: async () => "Saved summary", headers: {get: () => "text/plain"}};
  };
  const saved = {...job, summary_url: "/summary/", summary_updated_at: "1"};
  await f.context.renderReader([saved], saved);
  const pane = f.pane(), panel = pane.readerView.content, paragraph = panel.children[0];
  pane.scrollTop = 200;
  f.context.feedRevision = "2";
  f.context.processingStatus = () => ({stage: "active", label: "Processing"});
  const updated = {...saved, stage: "active", updated_at: "2"};
  await f.context.renderReader([updated], updated);
  assert.equal(reads, 1);
  assert.equal(pane.readerView.content, panel);
  assert.equal(panel.children[0], paragraph);
  assert.equal(pane.scrollTop, 200);
});

test("clearing the reader drops saved view data and cancels pending reads", async () => {
  const f = fixture();
  await f.context.renderReader([job], job);
  const view = f.pane().readerView;
  f.context.resetReader();
  assert.equal(f.pane().readerView, undefined);
  assert.equal(view.controller.signal.aborted, true);
  assert.equal(f.context.readerIdentity, "");
  assert.equal(f.nodes().some(n => n.className === "pipeline-stats"), false);
});

test("a discovered video's first profile preserves the panel and rejects older profile-less responses", async () => {
  const f = fixture(), discovered = {...job, profile: null};
  await f.context.renderReader([discovered], discovered);
  const pane = f.pane(), view = pane.readerView, panel = view.content;
  pane.scrollTop = 250;
  let finish, reads = 0;
  const delayed = new Promise(resolve => {finish = resolve;});
  f.context.libraryFetch = async () => {
    const ordinal = ++reads;
    return {ok: true, json: async () => ordinal === 1 ? delayed : {...data, model: "New profile"}};
  };
  f.context.feedRevision = "2";
  const pending = f.context.renderReader([discovered], discovered);
  await Promise.resolve();
  const previous = view.controller;
  f.context.feedRevision = "3";
  await f.context.renderReader([job], job);
  finish({...data, model: "Stale profile-less result"});
  await pending;
  assert.equal(pane.readerView, view);
  assert.equal(view.content, panel);
  assert.equal(pane.scrollTop, 250);
  assert.equal(previous.signal.aborted, true);
  assert.equal(view.job.profile, "hosted");
  assert.ok(f.nodes().some(n => n.textContent === "New profile"));
  assert.equal(f.nodes().some(n => n.textContent === "Stale profile-less result"), false);
});
