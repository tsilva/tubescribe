import {createRealtime, rawSql} from "@neon/realtime/server";
import {queries} from "../src/reader/queries.js";

export function realtimeURL(value) {
  const url = new URL(value);
  if (url.protocol !== "wss:" || url.username || url.password || url.search || url.hash ||
      !["neon.tech", "neon.build", "neon.com"].some(domain => url.hostname.endsWith(`.${domain}`))) {
    throw new Error("Invalid Neon Realtime URL");
  }
  return url.href;
}

export function makeHandler({env = process.env, request = fetch, issuer = createRealtime} = {}) {
  return async function handler(req, res) {
    res.setHeader("Cache-Control", "private, no-store");
    res.setHeader("CDN-Cache-Control", "no-store");
    res.setHeader("Vercel-CDN-Cache-Control", "no-store");
    res.setHeader("Content-Type", "application/json");
    res.setHeader("X-Content-Type-Options", "nosniff");
    const send = (status, data) => {res.statusCode = status; res.end(JSON.stringify(data));};
    if (req.method !== "GET") { res.setHeader("Allow", "GET"); return send(405, {error: "Read only"}); }
    if (new URL(req.url, "https://reader.invalid").search) return send(400, {error: "Query parameters are not accepted"});
    const authorization = req.headers.authorization;
    if (typeof authorization !== "string" || !/^Bearer [A-Za-z0-9_.-]{1,16000}$/.test(authorization)) {
      return send(401, {error: "Sign in required"});
    }
    try {
      if (!env.NEON_REALTIME_SECRET) return send(503, {error: "Realtime is not configured"});
      const url = realtimeURL(env.NEON_REALTIME_URL);
      const origin = new URL(env.TUBESCRIBE_PUBLIC_ORIGIN);
      if (origin.protocol !== "https:" || origin.username || origin.password || origin.search ||
          origin.hash || origin.pathname !== "/") throw new Error("Invalid application origin");
      // Reuse Python's Clerk verifier. This endpoint verifies the session without
      // querying Postgres. Ownership stays live inside every SQL query instead.
      const response = await request(new URL("/api/realtime-authorization", origin), {
        headers: {Authorization: authorization}, redirect: "error", signal: AbortSignal.timeout(5000),
      });
      if (response.status === 401) return send(401, {error: "Sign in required"});
      if (!response.ok) return send(503, {error: "Authentication unavailable"});
      const identity = await response.json();
      if (!/^user_[A-Za-z0-9]+$/.test(identity.user_id) ||
          !/^https:\/\/[a-zA-Z0-9.-]+$/.test(identity.issuer)) throw new Error("Invalid session response");
      const realtime = issuer({secret: env.NEON_REALTIME_SECRET, db: "neondb"});
      const sealed = Object.fromEntries(await Promise.all(Object.entries(queries).map(async ([kind, sql]) =>
        [kind, await realtime.seal({query: rawSql(sql, [identity.user_id, identity.issuer])})])));
      return send(200, {url, queries: sealed});
    } catch {
      // SDK/provider errors may include credentials or schema; never return/log them.
      return send(503, {error: "Realtime is temporarily unavailable"});
    }
  };
}

export default makeHandler();
