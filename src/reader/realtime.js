import {createRealtimeClient} from "@neon/realtime/client";
import {buildFeed} from "./feed.js";

export function connect({getCapabilities, onFeed, onState, onError = () => {}, createClient = createRealtimeClient,
  setTimer = setTimeout, clearTimer = clearTimeout, now = Date.now}) {
  let closed = false, client, timer, config, renewal;
  let rowUpdate = false;
  let phase = "connecting";
  const subscriptions = new Map(), data = {}, states = {};
  function validate(value) {
    if (!value || typeof value.url !== "string" || !value.queries ||
        Object.keys(value.queries).sort().join() !== "channels,feed,queue" ||
        !Object.values(value.queries).every(query => Number.isFinite(query.expiresAt) && query.expiresAt > now())) {
      throw Object.assign(new Error("Invalid live query response"), {code: "invalid_live_query_response"});
    }
    return value;
  }
  const notify = status => {phase = status; onState(status);};
  function close() {
    closed = true;
    clearTimer(timer);
    for (const subscription of subscriptions.values()) subscription.unsubscribe();
    subscriptions.clear();
    client?.close();
  }
  function fail(error, phase) {
    if (closed) return;
    const code = /^[a-z][a-z0-9_]{0,63}$/.test(error?.code ?? "") ? error.code : "unknown";
    const sqlState = /^[A-Z0-9]{5}$/.test(error?.sqlState ?? "") ? error.sqlState : undefined;
    onError({phase, code, sqlState});
    close();
    notify("error");
  }
  const expires = () => Math.min(...Object.values(config.queries).map(query => query.expiresAt));
  function schedule() {
    clearTimer(timer);
    if (!closed) timer = setTimer(() => renew(), Math.max(0, expires() - now() - 10_000));
  }
  async function renew() {
    if (closed || renewal) return renewal;
    renewal = (async () => {
      const next = validate(await getCapabilities());
      if (closed) return;
      if (next.url !== config.url) throw new Error("Realtime endpoint changed");
      await Promise.all([...subscriptions].map(([kind, subscription]) => subscription.renew(next.queries[kind])));
      if (!closed) {config = next; schedule();}
    })().catch(error => fail(error, "renewal")).finally(() => {renewal = undefined;});
    return renewal;
  }
  function receive(kind, snapshot) {
    if (closed) return;
    states[kind] = snapshot.status;
    if (snapshot.status === "error") return fail(snapshot.error, kind);
    if (snapshot.data !== undefined && snapshot.data !== data[kind]) {
      data[kind] = snapshot.data;
      if (!rowUpdate) {
        rowUpdate = true;
        queueMicrotask(() => {
          rowUpdate = false;
          try {
            if (!closed && Object.keys(config.queries).every(key => Array.isArray(data[key]))) onFeed(buildFeed(data));
          } catch (error) {fail(error, "render");}
        });
      }
    }
    notify(Object.values(states).includes("stale") ? "stale" :
      Object.keys(config.queries).every(key => states[key] === "live") ? "live" : "connecting");
  }
  async function start() {
    let phase = "authorization";
    try {
      notify("connecting");
      const received = await getCapabilities();
      if (closed) return;
      config = validate(received);
      // One WebSocket multiplexes all queries. SDK diagnostics stay out of logs.
      phase = "client";
      client = createClient({url: config.url, logLevel: "silent"});
      for (const [kind, query] of Object.entries(config.queries)) {
        phase = "subscribe_" + kind;
        const subscription = client.subscribe(query);
        subscriptions.set(kind, subscription);
        subscription.onChange(snapshot => receive(kind, snapshot));
      }
      schedule();
    } catch (error) {fail(error, phase);}
  }
  return {start, close, get status() {return phase;}, wake() {
    if (!closed && config && now() >= expires() - 10_000) return renew();
  }};
}
