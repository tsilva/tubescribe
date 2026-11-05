"""Isolated, placeholder-only client for the reviewed Apify media actor.

Only fixed operations leave this process. Raw dataset items, logs, signed
YouTube stream URLs and API credentials never enter the response envelope.
"""

import json
import os
import re
import ssl
import sys
from urllib.parse import urlsplit

import httpx

ACTOR = "UUhJDfKJT2SsXdclR"
BUILD = "0.0.119"
PLACEHOLDER = "tubescribe_apify_proxy_placeholder"
BASE = "https://api.apify.com/v2"
MAX_COST = .50
RESOURCE = re.compile(r"[a-zA-Z0-9]{17}")
VIDEO = re.compile(r"[a-zA-Z0-9_-]{11}")


def resource(value):
    if not isinstance(value, str) or not RESOURCE.fullmatch(value):
        raise ValueError("Invalid media resource")
    return value


def stored_file(value, store_id=None):
    url = urlsplit(value if isinstance(value, str) else "")
    match = re.fullmatch(r"/v2/key-value-stores/([a-zA-Z0-9]{17})/records/([^/]+)", url.path)
    if (url.scheme != "https" or url.netloc != "api.apify.com" or url.query or url.fragment
            or not match or (store_id and match[1] != store_id)):
        raise ValueError("Media must be an Apify-hosted stored file")
    return value


def receipt(run):
    if run.get("actId") != ACTOR:
        raise ValueError("Unexpected media actor")
    return {key: run.get(key) for key in (
        "id", "actId", "status", "startedAt", "finishedAt", "defaultDatasetId",
        "defaultKeyValueStoreId", "usageTotalUsd", "stats", "options",
    )}


def perform(request, client):
    def api(method, path, **kwargs):
        response = client.request(method, BASE + path, **kwargs)
        response.raise_for_status()
        return response.json()

    operation = request["operation"]
    if operation == "start":
        video = request["video_id"]
        nonce = request["nonce"]
        if not VIDEO.fullmatch(video) or not re.fullmatch(r"[a-f0-9]{32}", nonce):
            raise ValueError("Invalid selected media request")
        return {"run": receipt(api("POST", f"/actors/{ACTOR}/runs", params={
            "build": BUILD, "memory": 4096, "timeout": 180,
            "maxTotalChargeUsd": MAX_COST, "restartOnError": "false",
            "forcePermissionLevel": "LIMITED_PERMISSIONS",
        }, json={
            "videos": [{"url": "https://www.youtube.com/watch?v=" + video}],
            "storeInKVStore": True, "preferredFormat": "mp4", "preferredQuality": "720p",
            "filenameTemplateParts": ["title"], "transcriptionAndSubtitle": "NONE",
            "tubescribeRequestId": nonce,
        })["data"])}
    if operation == "recover":
        # No documented idempotent start endpoint. Reconcile the saved input
        # nonce instead of guessing whether an acknowledged request was charged.
        runs = api("GET", f"/actors/{ACTOR}/runs", params={"desc": "true", "limit": 20})["data"]["items"]
        candidates = [r for r in runs if r.get("startedAt", "") >= request["started_at"]]
        if len(candidates) > 6:
            return {"ambiguous": True}
        matches = []
        for run in candidates:
            value = api("GET", f"/key-value-stores/{resource(run['defaultKeyValueStoreId'])}/records/INPUT")
            if value.get("tubescribeRequestId") == request["nonce"]:
                matches.append(receipt(run))
        return {"run": matches[0] if len(matches) == 1 else None, "ambiguous": len(matches) > 1}
    run_id = resource(request["run_id"])
    run = receipt(api("GET", f"/actor-runs/{run_id}")["data"])
    if operation == "status":
        return {"run": run}
    if operation == "resurrect":
        # The caller supplies the observed count/finish marker; a lost resume
        # acknowledgement cannot accidentally resurrect the next terminal run.
        if (run["status"] != "TIMED-OUT" or run.get("finishedAt") != request["finished_at"]
                or (run.get("stats") or {}).get("resurrectCount", 0) != request["count"]):
            return {"run": run}
        return {"run": receipt(api("POST", f"/actor-runs/{run_id}/resurrect", params={
            "timeout": 360, "memory": 4096, "maxTotalChargeUsd": MAX_COST,
        })["data"])}
    if operation != "output":
        raise ValueError("Unsupported media operation")
    items = api("GET", f"/datasets/{resource(run['defaultDatasetId'])}/items",
                params={"clean": "true", "limit": 2})
    video = request["video_id"]
    if len(items) != 1 or items[0].get("id") != video:
        return {"run": run, "file": None}
    item = items[0]
    source = urlsplit(item.get("input", ""))
    if source.netloc not in {"www.youtube.com", "youtube.com"} or source.query != "v=" + video:
        raise ValueError("Media output does not match selected input")
    # Finished downloads use a separate KVS from the run's INPUT/state store.
    # Accept only its selected-video dataset item and the exact stored-file host.
    return {"run": run, "file": stored_file(item.get("downloadedFileUrl"))}


def main():
    try:
        proxy = os.environ.get("HTTPS_PROXY", "")
        if (os.environ.get("APIFY_TOKEN") != PLACEHOLDER or urlsplit(proxy).hostname
                not in {"127.0.0.1", "localhost", "::1"} or not os.environ.get("SSL_CERT_FILE")):
            raise ValueError("Reviewed placeholder proxy required")
        raw = sys.stdin.buffer.read(16385)
        if not raw or len(raw) > 16384:
            raise ValueError("Invalid media envelope")
        with httpx.Client(proxy=proxy, verify=ssl.create_default_context(cafile=os.environ["SSL_CERT_FILE"]),
                          trust_env=False, timeout=20, follow_redirects=False,
                          headers={"Authorization": "Bearer " + PLACEHOLDER}) as client:
            result = perform(json.loads(raw), client)
        print(json.dumps({"status": 200, **result}))
    except httpx.HTTPStatusError as error:
        print(json.dumps({"status": error.response.status_code}))
    except Exception:
        # Includes transport ambiguity. No provider body, URL or token escapes.
        print('{"status":0}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
