import assert from "node:assert/strict";
import {createDecipheriv, randomBytes} from "node:crypto";
import {readFile} from "node:fs/promises";
import test from "node:test";
import {runInNewContext} from "node:vm";
import {makeHandler, realtimeURL} from "../api/realtime.js";
import {connect} from "../src/reader/realtime.js";
import {buildFeed, stage} from "../src/reader/feed.js";
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

function fixture() {
  let count = 0, callback, tick, timerCleared = 0, clientClosed = 0, now = 1_000_000;
  const subscriptions = new Map(), feeds = [], states = [];
  const capabilities = {url: env.NEON_REALTIME_URL, queries: Object.fromEntries(
    ["channels", "feed", "queue"].map(kind => [kind, {capability: kind, expiresAt: now + 60_000, queryFingerprint: kind}]))};
  const client = {subscribe(query) {
    const subscription = {onChange(fn) {callback = fn; this.receive = fn;},
      renew: async next => {assert.equal(next.queryFingerprint, query.queryFingerprint); subscription.renewed = true;},
      unsubscribe() {subscription.closed = true;}};
    subscriptions.set(query.capability, subscription);
    return subscription;
  }, close() {clientClosed++;}};
  const stream = connect({getCapabilities: async () => {count++; return structuredClone(capabilities);},
    onFeed: feed => feeds.push(feed), onState: state => states.push(state), createClient: () => client,
    now: () => now, setTimer: fn => {tick = fn; return 1;}, clearTimer: () => {timerCleared++;}});
  return {stream, subscriptions, feeds, states, capabilities,
    count: () => count, closed: () => clientClosed, cleared: () => timerCleared,
    tick: () => tick(), advance: ms => {now += ms;}};
}
const settle = () => new Promise(resolve => setImmediate(resolve));

test("one socket applies row changes without polling; capability renewal preserves rows", async () => {
  const fixture = fixtureStream();
  await fixture.stream.start();
  assert.equal(fixture.count(), 1);
  for (const [kind, subscription] of fixture.subscriptions) subscription.receive({status: "live", data: []});
  await settle();
  assert.equal(fixture.feeds.length, 1);
  assert.equal(fixture.stream.status, "live");
  fixture.advance(51_000);
  await fixture.stream.wake();
  assert.equal(fixture.count(), 2);
  assert.equal(fixture.feeds.length, 1);
  assert.ok([...fixture.subscriptions.values()].every(s => s.renewed));
  fixture.stream.close();
  assert.ok([...fixture.subscriptions.values()].every(s => s.closed));
  assert.equal(fixture.closed(), 1);
});
// Keep the helper separate from test-local variable names.
const fixtureStream = fixture;

test("closing during authorization ignores late private data; terminal errors never poll", async () => {
  let finish;
  const updates = [];
  const stream = connect({getCapabilities: () => new Promise(resolve => {finish = resolve;}),
    onFeed: data => updates.push(data), onState() {}, createClient() {throw new Error("closed session opened socket");}});
  const starting = stream.start();
  stream.close();
  finish({url: env.NEON_REALTIME_URL, queries: {}});
  await starting;
  assert.deepEqual(updates, []);
  const fixture = fixtureStream();
  await fixture.stream.start();
  fixture.subscriptions.get("feed").receive({status: "error"});
  await fixture.stream.wake();
  assert.equal(fixture.stream.status, "error");
  assert.equal(fixture.count(), 1);
  assert.equal(fixture.closed(), 1);
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

test("the reader loads its channel snapshot before a failed live connection and refreshes it on request", async () => {
  const source = await readFile(new URL("../src/tubescribe/dashboard_assets/app.js", import.meta.url), "utf8");
  const controls = new Map();
  const requests = [];
  const snapshot = {channels: [{id: "owner-channel"}], jobs: []};
  const context = {
    signedInUser: "user_owner", ownsLibrary: true, refreshing: false,
    identityGeneration: 1, archive: null, feedRevision: "", liveFeed: null,
    realtimeSettings: {enabled: true},
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
    TubeScribeRealtime: {connect: ({onState}) => ({
      status: "error", close() {},
      start: async () => {
        assert.equal(context.archive, snapshot);
        onState("error");
      },
    })},
  };
  runInNewContext(source.slice(source.indexOf("async function refresh(event)"),
    source.indexOf('document.addEventListener("keydown"')), context);
  await context.refresh();
  assert.deepEqual(requests, ["/api/feed"]);
  assert.equal(context.archive.channels[0].id, "owner-channel");
  assert.match(controls.get("error").textContent, /last snapshot is preserved/);
  await context.refresh({type: "click"});
  assert.deepEqual(requests, ["/api/feed", "/api/feed"]);

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

test("feed keeps source URLs, status and queued budget rules without prompt fields", () => {
  const job = {key: "1", video_id: "video", channel_id: "c", job_id: "1", profile: "turbo-v1",
    state: "complete", diarization_state: "complete", postprocessing_state: "complete", summary_state: "pending",
    scheduling: [["complete"],["complete"],["complete"],["pending","save",0,0,9,0]],
    transcript_ready: true, processed_ready: true, summary_ready: false, figures: 1, cover_revision: "hash"};
  const feed = buildFeed({feed: [job], channels: [{id: "c", title: "Channel"}], queue: [{options: {}}]}, 1);
  assert.equal(feed.jobs[0].stage, "awaiting_summary");
  assert.equal(feed.jobs[0].scheduled, true);
  assert.equal(feed.jobs[0].transcript_url, "/videos/video/transcripts/turbo-v1/");
  assert.match(feed.jobs[0].cover_url, /cover.png\?v=hash$/);
  assert.equal(feed.jobs[0].scheduling, undefined);
  assert.equal(feed.channels[0].videos, 1);
  assert.equal(stage({...job, summary_state: "failed"}), "failed");
  const exhausted = structuredClone(job); exhausted.scheduling[3][5] = 3;
  assert.equal(buildFeed({feed: [exhausted], channels: [], queue: [{options: {}}]}, 1).jobs[0].scheduled, false);
});

test("scalar live rows retain legacy scheduling and exclude internal counters", () => {
  const job = {video_id: "video", channel_id: "c", job_id: "1", profile: "turbo-v1", state: "pending"};
  const channels = [{id: "c", title: "Channel"}];
  const legacy = buildFeed({
    channels, feed: [{...job, key: "1", scheduling: [["pending", null, 0, 0, 0, 0]]}],
    queue: [{options: {}}],
  }, 1);
  const scalar = buildFeed({
    channels, feed: [{...job, asr_retry: 0, asr_downloads: 0, asr_attempts: 0}],
    queue: [{id: "submission", profile: null}],
  }, 1);
  assert.deepEqual(scalar, legacy);
  assert.equal(scalar.jobs[0].scheduled, true);
  assert.equal(scalar.jobs[0].asr_attempts, undefined);
  assert.equal(scalar.jobs[0].scheduling, undefined);
  assert.equal(buildFeed({
    channels, feed: [{video_id: "new-video", channel_id: "c", job_id: null}], queue: [],
  }, 1).jobs[0].key, "video:new-video");
});
