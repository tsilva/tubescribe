import assert from "node:assert/strict";
import {createDecipheriv, randomBytes} from "node:crypto";
import {readFile} from "node:fs/promises";
import test from "node:test";
import {runInNewContext} from "node:vm";
import {makeHandler, realtimeURL} from "../api/realtime.js";
import {connect} from "../src/reader/realtime.js";
import {queries} from "../src/reader/queries.js";

const key = randomBytes(32);
const env = {NEON_REALTIME_SECRET: "nrt_live_1" + key.toString("base64url"),
  NEON_REALTIME_URL: "wss://test.realtime.neon.tech", TUBESCRIBE_PUBLIC_ORIGIN: "https://reader.test"};
const identity = {user_id: "user_owner", issuer: "https://clerk.test"};
async function invoke(options = {}, req = {}) {
  const headers = {};
  let result;
  const response = {setHeader: (name, value) => {headers[name] = value;}, end: text => {result = JSON.parse(text);}};
  await makeHandler({env, request: async () => ({status: 200, ok: true, json: async () => identity}), ...options})(
    {method: "GET", url: "/api/live", headers: {authorization: "Bearer signed.session.token"}, ...req}, response);
  return {status: response.statusCode, headers, result};
}
function claims(capability) {
  const [header, encryptedKey, iv, ciphertext, tag] = capability.split(".");
  assert.equal(encryptedKey, "");
  const decipher = createDecipheriv("aes-256-gcm", key, Buffer.from(iv, "base64url"));
  decipher.setAAD(Buffer.from(header));
  decipher.setAuthTag(Buffer.from(tag, "base64url"));
  return JSON.parse(Buffer.concat([decipher.update(Buffer.from(ciphertext, "base64url")), decipher.final()]));
}

test("official SDK seals only fixed queries, scoped to the verified Clerk identity", async () => {
  const seen = [];
  const response = await invoke({request: async (url, options) => {
    seen.push({url: url.href, options});
    return {status: 200, ok: true, json: async () => identity};
  }});
  assert.equal(response.status, 200);
  assert.equal(seen.length, 1);
  assert.equal(seen[0].url, "https://reader.test/api/realtime-authorization");
  assert.equal(seen[0].options.redirect, "error");
  assert.match(response.headers["Cache-Control"], /no-store/);
  assert.equal(response.result.url, env.NEON_REALTIME_URL + "/");
  for (const [kind, query] of Object.entries(response.result.queries)) {
    const decoded = claims(query.capability);
    assert.equal(decoded.sql, queries[kind]);
    assert.match(decoded.sql, /reader_library/);
    assert.match(decoded.sql, /clerk_user_id=\$1 AND l.issuer=\$2/);
    assert.equal(decoded.parameters[0].value, Buffer.from(identity.user_id).toString("base64"));
    assert.equal(decoded.parameters[1].value, Buffer.from(identity.issuer).toString("base64"));
    assert.equal(decoded.database, "neondb");
    assert.equal(decoded.error_details, "safe");
    assert.ok(decoded.exp - decoded.iat <= 60);
  }
  assert.ok(!JSON.stringify(response.result).includes(env.NEON_REALTIME_SECRET));
  assert.ok(!JSON.stringify(response.result).includes("user_owner"));
});

test("issuer fails closed for unauthenticated, arbitrary-query and unavailable requests", async () => {
  const noIssue = () => {throw new Error("should not seal");};
  assert.equal((await invoke({issuer: noIssue}, {headers: {}})).status, 401);
  assert.equal((await invoke({issuer: noIssue}, {url: "/api/live?sql=SELECT+secret"})).status, 400);
  assert.equal((await invoke({issuer: noIssue}, {method: "POST"})).status, 405);
  assert.equal((await invoke({env: {...env, NEON_REALTIME_SECRET: ""}, issuer: noIssue})).status, 503);
  assert.equal((await invoke({request: async () => ({status: 401}), issuer: noIssue})).status, 401);
  const result = await invoke({request: async () => {throw new Error(env.NEON_REALTIME_SECRET);}});
  assert.equal(result.status, 503);
  assert.ok(!JSON.stringify(result).includes(env.NEON_REALTIME_SECRET));
  for (const url of ["ws://test.neon.tech", "wss://attacker.test", "wss://test.neon.tech?secret=x", "wss://user:pass@test.neon.tech"]) {
    assert.throws(() => realtimeURL(url));
  }
});

function fixture(loadFeed = async count => ({revision: "feed-" + count, jobs: [{key: "saved"}]})) {
  let count = 0, feedReads = 0, tick, clientClosed = 0, now = 1_000_000;
  const feeds = [], states = [];
  const capabilities = {url: env.NEON_REALTIME_URL,
    queries: {revision: {capability: "revision", expiresAt: now + 60_000, queryFingerprint: "revision"}}};
  const subscription = {
    onChange(callback) {this.receive = callback;},
    renew: async query => {assert.equal(query.queryFingerprint, "revision"); subscription.renewed = true;},
    unsubscribe() {subscription.closed = true;},
  };
  const client = {subscribe: () => subscription, close() {clientClosed++;}};
  const stream = connect({
    getCapabilities: async () => {count++; return structuredClone(capabilities);},
    getFeed: async () => loadFeed(++feedReads),
    onFeed: feed => feeds.push(feed), onState: state => states.push(state), createClient: () => client,
    now: () => now, setTimer: callback => {tick = callback; return 1;}, clearTimer() {},
  });
  return {stream, subscription, feeds, states, capabilities,
    count: () => count, feedReads: () => feedReads, closed: () => clientClosed,
    tick: () => tick(), advance: milliseconds => {now += milliseconds;}};
}
const settle = () => new Promise(resolve => setImmediate(resolve));

test("revision notifications load changed data once and renew without reading the catalogue", async () => {
  const f = fixture();
  await f.stream.start();
  assert.equal(f.count(), 1);
  assert.equal(f.feedReads(), 0);
  f.subscription.receive({status: "live", data: [{revision: "first"}]});
  await settle();
  assert.equal(f.feedReads(), 1);
  f.subscription.receive({status: "live", data: [{revision: "first"}]});
  await settle();
  assert.equal(f.feedReads(), 1);
  f.subscription.receive({status: "live", data: [{revision: "second"}]});
  await settle();
  assert.equal(f.feedReads(), 2);
  assert.equal(f.feeds.length, 2);
  assert.equal(f.stream.status, "live");
  f.advance(51_000);
  await f.stream.wake();
  assert.equal(f.count(), 2);
  assert.equal(f.feedReads(), 2);
  assert.ok(f.subscription.renewed);
  f.stream.close();
  assert.ok(f.subscription.closed);
  assert.equal(f.closed(), 1);
});

test("closing during authorization ignores private data and terminal errors never poll", async () => {
  let finish;
  const updates = [];
  const stream = connect({getCapabilities: () => new Promise(resolve => {finish = resolve;}),
    getFeed() {throw new Error("closed session read data");},
    onFeed: data => updates.push(data), onState() {},
    createClient() {throw new Error("closed session opened socket");}});
  const starting = stream.start();
  stream.close();
  finish({url: env.NEON_REALTIME_URL, queries: {}});
  await starting;
  assert.deepEqual(updates, []);
  const f = fixture();
  await f.stream.start();
  f.subscription.receive({status: "error"});
  await f.stream.wake();
  assert.equal(f.stream.status, "error");
  assert.equal(f.count(), 1);
  assert.equal(f.feedReads(), 0);
  assert.equal(f.closed(), 1);
});

test("ownership revocation clears the library and rejects an in-flight snapshot", async () => {
  let finish;
  const f = fixture(() => new Promise(resolve => {finish = resolve;}));
  await f.stream.start();
  f.subscription.receive({status: "live", data: [{revision: "first"}]});
  f.subscription.receive({status: "live", data: []});
  assert.equal(f.feeds.length, 1);
  assert.deepEqual(f.feeds[0].jobs, []);
  finish({revision: "old", jobs: [{key: "private"}]});
  await settle();
  assert.equal(f.feeds.length, 1);
  f.stream.close();
  const empty = fixture();
  await empty.stream.start();
  empty.subscription.receive({status: "live", data: []});
  assert.equal(empty.feeds.length, 1);
  assert.equal(empty.feedReads(), 0);
  empty.stream.close();
});

test("changes during a snapshot are coalesced and stale snapshots are discarded", async () => {
  let finish;
  const f = fixture(count => count === 1 ? new Promise(resolve => {finish = resolve;}) :
    {revision: "latest", jobs: [{key: "current"}]});
  await f.stream.start();
  f.subscription.receive({status: "live", data: [{revision: "first"}]});
  f.subscription.receive({status: "live", data: [{revision: "second"}]});
  f.subscription.receive({status: "live", data: [{revision: "third"}]});
  finish({revision: "old", jobs: [{key: "stale"}]});
  await settle();
  assert.equal(f.feedReads(), 2);
  assert.equal(f.feeds.length, 1);
  assert.equal(f.feeds[0].revision, "latest");
  f.stream.close();
});

test("live failure diagnostics exclude provider messages and credentials", async () => {
  const failures = [];
  const stream = connect({
    getCapabilities: async () => {
      throw Object.assign(new Error(env.NEON_REALTIME_SECRET),
        {code: "authorization_http_503", sqlState: env.NEON_REALTIME_SECRET});
    },
    onFeed() {}, onState() {}, onError: failure => failures.push(failure),
  });
  await stream.start();
  assert.equal(stream.status, "error");
  assert.deepEqual(failures, [{phase: "authorization", code: "authorization_http_503", sqlState: undefined}]);
  assert.ok(!JSON.stringify(failures).includes(env.NEON_REALTIME_SECRET));
});

test("Realtime shares one script load and can retry a failed download", async () => {
  const source = await readFile(new URL("../src/tubescribe/dashboard_assets/app.js", import.meta.url), "utf8");
  const scripts = [];
  const context = {
    window: {}, realtimeLoader: null,
    document: {
      createElement: () => ({remove() {this.removed = true;}}),
      head: {append: script => scripts.push(script)},
    },
  };
  runInNewContext(source.slice(source.indexOf("function loadRealtime()"),
    source.indexOf("function showLiveUnavailable()")), context);
  const first = context.loadRealtime();
  assert.equal(context.loadRealtime(), first);
  assert.equal(scripts.length, 1);
  assert.equal(scripts[0].src, "/realtime.js");
  scripts[0].onerror();
  assert.equal(await first, null);
  assert.equal(scripts[0].removed, true);
  const retry = context.loadRealtime();
  assert.equal(scripts.length, 2);
  const sdk = {connect() {}};
  context.window.TubeScribeRealtime = sdk;
  scripts[1].onload();
  assert.equal(await retry, sdk);
  assert.equal(await context.loadRealtime(), sdk);
  assert.equal(scripts.length, 2);
});

test("the reader loads its channel snapshot before a failed live connection and refreshes it on request", async () => {
  const source = await readFile(new URL("../src/tubescribe/dashboard_assets/app.js", import.meta.url), "utf8");
  const controls = new Map();
  const requests = [];
  const snapshot = {channels: [{id: "owner-channel"}], jobs: []};
  const context = {
    signedInUser: "user_owner", ownsLibrary: true, refreshing: false,
    identityGeneration: 1, archive: null, feedRevision: "", liveFeed: null,
    realtimeSettings: {enabled: true}, readerTab: "summary",
    $: id => {
      if (!controls.has(id)) controls.set(id, {classList: {add() {}, remove() {}}});
      return controls.get(id);
    },
    libraryFetch: async path => {
      requests.push(path);
      return {ok: true, json: async () => snapshot};
    },
    applyFeed: data => {context.archive = data;},
    showArchive() {}, refreshDigests() {},
    loadRealtime: async () => ({connect: ({onState}) => ({
      status: "error", close() {},
      start: async () => {
        assert.equal(context.archive, snapshot);
        onState("error");
      },
    })}),
  };
  runInNewContext(source.slice(source.indexOf("function showLiveUnavailable()"),
    source.indexOf('document.addEventListener("keydown"')), context);
  await context.refresh();
  assert.deepEqual(requests, ["/api/feed"]);
  assert.equal(context.archive.channels[0].id, "owner-channel");
  assert.match(controls.get("error").textContent, /last snapshot is preserved/);
  await context.refresh({type: "click"});
  assert.deepEqual(requests, ["/api/feed", "/api/feed"]);

  const loadRealtime = context.loadRealtime;
  context.liveFeed = null;
  context.loadRealtime = async () => null;
  await context.refresh();
  assert.equal(context.archive, snapshot);
  assert.match(controls.get("error").textContent, /last snapshot is preserved/);

  context.loadRealtime = async () => {
    context.identityGeneration++;
    return {connect() {assert.fail("An obsolete identity must not open a subscription");}};
  };
  await context.refresh();
  assert.equal(context.liveFeed, null);

  context.loadRealtime = loadRealtime;
  context.archive = null;
  context.liveFeed = null;
  context.libraryFetch = async () => {
    context.identityGeneration++;
    context.signedInUser = null;
    return {ok: true, json: async () => snapshot};
  };
  await context.refresh();
  assert.equal(context.archive, null);
  assert.equal(context.liveFeed, null);
});
