import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import test from "node:test";
import {runInNewContext} from "node:vm";

const source = await readFile(new URL("../src/tubescribe/dashboard_assets/app.js", import.meta.url), "utf8");
function element(tag, text = "", className) {
  return {tag, textContent: text, className, dataset: {}, children: [], attributes: {}, listeners: {},
    classList: {add() {}},
    append(...children) {this.children.push(...children);},
    replaceChildren(...children) {this.children = children;},
    setAttribute(name, value) {this.attributes[name] = value;},
    addEventListener(name, listener) {this.listeners[name] = listener;},
  };
}
function fixture() {
  const controls = new Map();
  const $ = id => {
    if (!controls.has(id)) controls.set(id, element("div"));
    return controls.get(id);
  };
  $("channel-sidebar").querySelectorAll = () => [
    ...$("library-navigation").children, ...$("channel-navigation").children,
  ];
  const context = {
    $, node: element, icon: () => element("img"), Option: function(text, value) {return {text, value};},
    archive: {channels: [{id: "a", title: "Channel A"}, {id: "b", title: "Channel B"}]},
    libraryScope: "", feedLimit: 150, readerLevel: "video", readerSignature: "", rendered: "",
    renderFeed() {context.lastRenderedScope = context.libraryScope;},
  };
  runInNewContext(source.slice(source.indexOf("function syncScopes()"), source.indexOf("function digestBreadcrumbs(")), context);
  return {context, $, controls};
}

test("desktop channel selection and the mobile dropdown share the same filter", () => {
  const {context, $} = fixture();
  context.syncScopes();
  const channel = $("channel-navigation").children[1];
  channel.listeners.click();
  assert.equal(context.lastRenderedScope, "b");
  assert.equal($("channel-scope").value, "b");
  assert.equal(channel.attributes["aria-current"], "page");
  assert.equal(context.feedLimit, 50);
  context.chooseScope("a");
  assert.equal(channel.attributes["aria-current"], "false");
  assert.equal($("channel-navigation").children[0].attributes["aria-current"], "page");
  $("library-navigation").children[0].listeners.click();
  assert.equal(context.lastRenderedScope, "");
  assert.equal($("channel-scope").value, "");
});

test("channel changes remove stale navigation and reset a removed filter", () => {
  const {context, $} = fixture();
  context.syncScopes();
  context.chooseScope("b");
  context.archive.channels = [{id: "a", title: "Renamed channel"}];
  context.syncScopes();
  assert.equal(context.libraryScope, "");
  assert.equal($("channel-scope").value, "");
  assert.equal($("channel-navigation").children.length, 1);
  assert.equal($("channel-navigation").children[0].title, "Renamed channel");
  assert.equal($("library-navigation").children[0].attributes["aria-current"], "page");
});

test("switching accounts clears the former owner's channel names", () => {
  const {context, $} = fixture();
  context.syncScopes();
  Object.assign(context, {
    identityGeneration: 0, liveFeed: null, processingRevision: 0,
    pendingProcessing: new Set(), selectedForProcessing: new Set(), readerController: null, feedObserver: null,
    readingCache: new Map(), rowCache: new Map(), feedRecords: new Map(),
    resetReader() {}, sessionStorage: {removeItem() {}},
  });
  runInNewContext(source.slice(source.indexOf("function clearPrivateLibrary()"), source.indexOf("async function libraryFetch(")), context);
  context.clearPrivateLibrary();
  assert.equal($("library-navigation").children.length, 0);
  assert.equal($("channel-navigation").children.length, 0);
  assert.equal(context.archive, null);
  assert.equal($("channel-scope").children.length, 1);
});
