import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import test from "node:test";
import {runInNewContext} from "node:vm";

const source = await readFile(new URL("../src/tubescribe/dashboard_assets/app.js", import.meta.url), "utf8");
function element(tag, text, className) {
  return {tag, textContent: text, className, dataset: {}, attributes: {}, children: [], listeners: {},
    append(...children) {this.children.push(...children);},
    prepend(...children) {this.children.unshift(...children);},
    replaceChildren(...children) {this.children = children;},
    setAttribute(name, value) {this.attributes[name] = value;},
    addEventListener(name, listener) {this.listeners[name] = listener;}, focus() {},
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
    icon: () => element("img"), backToLibrary() {}, date: value => value ? "Today" : "", duration: value => `${value}s`,
    human: value => value[0].toUpperCase() + value.slice(1), readingCache: new Map(),
    resourceLink: () => element("a"),
    $: id => {if (!controls.has(id)) controls.set(id, element("div")); return controls.get(id);},
    libraryFetch: async (url, options) => {requests.push({url, options}); if (failure) throw new Error("Offline");
      return {ok: true, json: async () => result};},
  };
  runInNewContext(source.slice(source.indexOf("async function renderReader("), source.indexOf("function applyFeed(")), context);
  return {context, requests, nodes: () => flatten(controls.get("reader")),
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
  assert.ok(f.nodes().some(n => n.textContent === "Recorded cost · partial"));
  assert.ok(f.nodes().some(n => n.textContent === "Included in hosted transcription"));
  assert.ok(f.nodes().some(n => n.textContent === "Waiting"));
});

test("unknown costs stay explicit and zero values remain visible", () => {
  const f = fixture(), content = element("div");
  f.context.renderPipeline(content, {...data, cost_usd: null}, {label: "Queued"});
  const nodes = flatten(content);
  assert.ok(nodes.some(n => n.textContent === "Not recorded"));
  assert.equal(nodes.filter(n => n.tag === "dd" && n.textContent === "0").length, 2);
  assert.ok(nodes.some(n => n.textContent === "Cost not recorded"));
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
  f.context.libraryFetch = async () => ({ok: true, json: () => new Promise(resolve => {finish = resolve;})});
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
