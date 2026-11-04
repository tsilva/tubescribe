import {createRealtimeClient} from "@neon/realtime/client";

export function connect({getCapabilities, getFeed, onFeed, onState, onError = () => {},
  createClient = createRealtimeClient, setTimer = setTimeout, clearTimer = clearTimeout, now = Date.now}) {
  let closed = false, client, subscription, timer, config, renewal;
  let revision = null, loadedRevision, loading = false, generation = 0;
  let phase = "connecting";
  function validate(value) {
    if (!value || typeof value.url !== "string" || !value.queries ||
        Object.keys(value.queries).join() !== "revision" ||
        !Number.isFinite(value.queries.revision?.expiresAt) || value.queries.revision.expiresAt <= now()) {
      throw Object.assign(new Error("Invalid live query response"), {code: "invalid_live_query_response"});
    }
    return value;
  }
  const notify = status => {phase = status; onState(status);};
  function close() {
    closed = true;
    generation++;
    clearTimer(timer);
    subscription?.unsubscribe();
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
  const expires = () => config.queries.revision.expiresAt;
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
      await subscription.renew(next.queries.revision);
      if (!closed) {config = next; schedule();}
    })().catch(error => fail(error, "renewal")).finally(() => {renewal = undefined;});
    return renewal;
  }
  async function load() {
    if (loading || closed || revision === undefined) return;
    loading = true;
    try {
      while (!closed && revision !== undefined && revision !== loadedRevision) {
        const expected = revision, current = generation;
        const feed = await getFeed();
        if (closed) return;
        if (current !== generation) continue;
        onFeed(feed);
        loadedRevision = expected;
      }
    } catch (error) {fail(error, "snapshot");}
    finally {loading = false;}
  }
  function receive(snapshot) {
    if (closed) return;
    if (snapshot.status === "error") return fail(snapshot.error, "revision");
    notify(snapshot.status);
    if (!Array.isArray(snapshot.data)) return;
    const next = snapshot.data[0]?.revision;
    if (next === revision) return;
    revision = next;
    generation++;
    if (next === undefined) {
      loadedRevision = undefined;
      onFeed({full: true, revision: "", generated_at: new Date(now()).toISOString(),
        channels: [], jobs: [], removed: [], summary: {channels: 0, videos: 0}});
    } else {
      void load();
    }
  }
  async function start() {
    let phase = "authorization";
    try {
      notify("connecting");
      const received = await getCapabilities();
      if (closed) return;
      config = validate(received);
      phase = "client";
      client = createClient({url: config.url, logLevel: "silent"});
      subscription = client.subscribe(config.queries.revision);
      subscription.onChange(receive);
      schedule();
    } catch (error) {fail(error, phase);}
  }
  return {start, close, get status() {return phase;}, wake() {
    if (!closed && config && now() >= expires() - 10_000) return renew();
  }};
}
