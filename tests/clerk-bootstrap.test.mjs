import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import test from "node:test";
import {runInNewContext} from "node:vm";

const source = await readFile(new URL("../src/tubescribe/dashboard_assets/clerk-bootstrap.js", import.meta.url), "utf8");
const settings = {
  frontend_api: "https://clerk.example.test",
  browser_scripts: ["/ui.js", "/clerk.js"],
  publishable_key: "pk_test_fixture",
};

test("Clerk starts after parsing and independently of the deferred reader", async () => {
  const scripts = [];
  const listeners = {};
  const ui = {};
  let loaded;
  const context = {
    URL,
    window: {
      __internal_ClerkUICtor: ui,
      Clerk: {async load(options) {loaded = options;}},
    },
    document: {
      readyState: "loading",
      querySelector: () => ({content: JSON.stringify(settings)}),
      addEventListener: (event, callback) => {listeners[event] = callback;},
      createElement: () => ({dataset: {}}),
      head: {append(script) {scripts.push(script); queueMicrotask(script.onload);}},
    },
  };
  runInNewContext(source, context);
  const ready = context.window.tubeScribeClerkReady;
  runInNewContext(source, context);
  assert.equal(context.window.tubeScribeClerkReady, ready);
  assert.deepEqual(scripts, []);
  context.document.readyState = "interactive";
  listeners.readystatechange();
  await ready;
  assert.deepEqual(scripts.map(script => script.src), settings.browser_scripts.map(path => settings.frontend_api + path));
  assert.ok(scripts.every(script => script.crossOrigin === "anonymous" && script.dataset.clerkPublishableKey === settings.publishable_key));
  assert.equal(loaded.ui.ClerkUI, ui);
  assert.equal(loaded.localization.signIn.start.title, "Sign in to TubeScribe");
});

test("Clerk bootstrap supports an older document and reports a failed bundle", async () => {
  const requests = [];
  const context = {
    URL,
    window: {},
    fetch: async path => {
      requests.push(path);
      return {ok: true, json: async () => settings};
    },
    document: {
      readyState: "interactive",
      querySelector: () => null,
      createElement: () => ({dataset: {}}),
      head: {append(script) {requests.push(script.src); queueMicrotask(script.onerror);}},
    },
  };
  runInNewContext(source, context);
  await assert.rejects(context.window.tubeScribeClerkReady, /Sign-in could not load/);
  assert.deepEqual(requests, ["/auth/config", settings.frontend_api + settings.browser_scripts[0]]);
});
