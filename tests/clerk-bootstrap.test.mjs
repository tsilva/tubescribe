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

for (const {signedIn, existingReader} of [
  {signedIn: false, existingReader: false},
  {signedIn: true, existingReader: false},
  {signedIn: true, existingReader: true},
]) {
  test(`Clerk starts for a signed-${signedIn ? "in" : "out"} visitor${existingReader ? " with an older document" : ""}`, async () => {
    const scripts = existingReader ? [{src: "/app.js"}] : [];
    const listeners = {};
    const ui = {};
    const container = {};
    const message = {textContent: "Connecting securely…"};
    const mounts = [];
    const unmounts = [];
    let loaded;
    let identityListener;
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
          addListener(listener) {
            identityListener = listener;
            return () => { identityListener = undefined; };
          },
        },
      },
      document: {
        readyState: "loading",
        querySelector: selector => selector.startsWith("meta")
          ? {content: JSON.stringify(settings)}
          : scripts.find(script => script.src === "/app.js"),
        getElementById: id => ({"sign-in": container, "auth-message": message})[id],
        addEventListener: (event, callback) => {listeners[event] = callback;},
        createElement: () => ({dataset: {}}),
        head: {append(script) {scripts.push(script); if (script.onload) queueMicrotask(script.onload);}},
      },
    };
    runInNewContext(source, context);
    const ready = context.window.tubeScribeClerkReady;
    runInNewContext(source, context);
    assert.equal(context.window.tubeScribeClerkReady, ready);
    assert.equal(scripts.length, Number(existingReader));
    context.document.readyState = "interactive";
    listeners.readystatechange();
    const setSignInVisible = await ready;
    const clerkScripts = scripts.filter(script => script.src !== "/app.js");
    assert.deepEqual(clerkScripts.map(script => script.src), settings.browser_scripts.map(path => settings.frontend_api + path));
    assert.ok(clerkScripts.every(script => script.crossOrigin === "anonymous" && script.dataset.clerkPublishableKey === settings.publishable_key));
    assert.equal(scripts.some(script => script.src === "/app.js"), signedIn);
    if (!signedIn) {
      identityListener({user: {id: "user_fixture"}, session: null});
      assert.equal(scripts.some(script => script.src === "/app.js"), false);
      identityListener({user: {id: "user_fixture"}, session: {}});
      identityListener({user: {id: "user_fixture"}, session: {}});
      await Promise.resolve();
      assert.equal(identityListener, undefined);
    }
    assert.equal(scripts.filter(script => script.src === "/app.js").length, 1);
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
  const message = {textContent: "Connecting securely…"};
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
      getElementById: id => id === "auth-message" ? message : null,
      createElement: () => ({dataset: {}}),
      head: {append(script) {requests.push(script.src); queueMicrotask(script.onerror);}},
    },
  };
  runInNewContext(source, context);
  await assert.rejects(context.window.tubeScribeClerkReady, /Sign-in could not load/);
  assert.equal(message.textContent, "Sign-in could not load. Refresh to retry.");
  assert.deepEqual(requests, ["/auth/config", settings.frontend_api + settings.browser_scripts[0]]);
});
