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

for (const signedIn of [false, true]) {
  test(`Clerk starts independently of the reader for a signed-${signedIn ? "in" : "out"} visitor`, async () => {
    const scripts = [];
    const listeners = {};
    const ui = {};
    const container = {};
    const message = {textContent: "Connecting securely…"};
    const mounts = [];
    const unmounts = [];
    let loaded;
    const config = signedIn ? {...settings, ui_script: "/clerk-ui/1.40.0/ui.browser.js"} : settings;
    const context = {
      URL,
      window: {
        location: {search: "?return_to=%2Fread%2Fexample"},
        __internal_ClerkUICtor: ui,
        Clerk: {
          user: signedIn ? {id: "user_fixture"} : null,
          session: signedIn ? {} : null,
          async load(options) {loaded = options;},
          mountSignIn(node, options) {mounts.push({node, ...options});},
          unmountSignIn(node) {unmounts.push(node);},
        },
      },
      document: {
        readyState: "loading",
        querySelector: () => ({content: JSON.stringify(config)}),
        getElementById: id => ({"sign-in": container, "auth-message": message})[id],
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
    const setSignInVisible = await ready;
    assert.deepEqual(scripts.map(script => script.src), [
      config.ui_script ?? settings.frontend_api + settings.browser_scripts[0],
      settings.frontend_api + settings.browser_scripts[1],
    ]);
    assert.ok(scripts.every(script => script.crossOrigin === "anonymous" && script.dataset.clerkPublishableKey === settings.publishable_key));
    assert.equal(loaded.ui.ClerkUI, ui);
    assert.equal(loaded.localization.signIn.start.title, "Sign in to TubeScribe");
    const mount = {node: container, routing: "hash", forceRedirectUrl: "/?return_to=%2Fread%2Fexample"};
    assert.deepEqual(mounts, signedIn ? [] : [mount]);
    assert.equal(message.textContent, signedIn ? "Connecting securely…" : "");

    setSignInVisible(!signedIn);
    assert.deepEqual(mounts, signedIn ? [] : [mount]);
    assert.deepEqual(unmounts, []);
    setSignInVisible(true);
    setSignInVisible(false);
    setSignInVisible(false);
    assert.deepEqual(mounts, [mount]);
    assert.deepEqual(unmounts, [container]);
    setSignInVisible(true);
    assert.deepEqual(mounts, [mount, mount]);
  });
}

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
