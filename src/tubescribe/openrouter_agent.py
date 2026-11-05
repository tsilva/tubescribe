"""Checkpointed OpenRouter text/vision runs using TubeScribe's bounded tools."""

import hashlib
import json
import os
import subprocess
import time
from contextlib import nullcontext

import httpx

from .billing import charge, record
from .execution import assert_owned
from .inference import EndpointPolicy, PostprocessingUnavailable
from .openrouter import MAX_REQUEST_BYTES, REJECTIONS, TEXT_MODEL, launcher
from .storage import atomic_write

MODEL = TEXT_MODEL


def transport(request):
    payload = json.dumps({"operation": "chat", "request": request}).encode()
    if len(payload) > MAX_REQUEST_BYTES:
        raise ValueError("OpenRouter text/vision request exceeds the upload bound")
    environment = {
        name: os.environ[name]
        for name in ("PATH", "HOME", "TMPDIR", "USER", "LOGNAME")
        if name in os.environ
    }
    result = subprocess.run(
        [str(launcher())], input=payload, capture_output=True, timeout=700, env=environment
    )
    if result.returncode:
        raise PostprocessingUnavailable("OpenRouter proxy did not return a verified outcome")
    return json.loads(result.stdout)


class OpenRouterAgentRunner:
    model = MODEL

    def __init__(self, store, run_key, *, send=None, max_new_calls=None):
        self.store, self.run_key = store, run_key
        self.send = send or transport
        self.max_new_calls = max_new_calls
        self.new_calls = 0

    def completion(self, index, request):
        raw = json.dumps(request, sort_keys=True).encode()
        if len(raw) > MAX_REQUEST_BYTES - 100:
            raise ValueError("OpenRouter text/vision request exceeds the upload bound")
        fingerprint = hashlib.sha256(raw).hexdigest()
        key = self.run_key + "/openrouter/call-" + str(index).zfill(4)
        saved, pending = key + ".json", key + ".pending"
        unknown = "OpenRouter request outcome unknown; saved intent retained for billing review"
        with self.store.db:
            assert_owned(self.store.db, self.store.root)
            if self.store.exists(saved):
                value = json.loads(self.store.read_key(saved))
                if value["request_sha256"] != fingerprint:
                    raise PostprocessingUnavailable(
                        "OpenRouter request changed; saved checkpoint retained for review"
                    )
                record(charge(key, value["response"]))
                return value["response"]
            if self.store.exists(pending):
                record(charge(key))
                raise PostprocessingUnavailable(unknown)
            if self.max_new_calls is not None and self.new_calls >= self.max_new_calls:
                from .agent_gateway import DurableRunPending

                raise DurableRunPending("Next model call is ready for a new workflow step", retry_after=0)
            self.store.blobs.put(pending, fingerprint.encode())
        record(charge(key))
        self.new_calls += 1
        try:
            result = self.send(request)
        except (OSError, ValueError, subprocess.SubprocessError, PostprocessingUnavailable):
            raise PostprocessingUnavailable(unknown) from None
        if result.get("status") in REJECTIONS:
            self.store.blobs.delete(pending)
            record(charge(key, rejected=True))
            if result["status"] == 429:
                raise PostprocessingUnavailable("OpenRouter rate limit; safe to retry")
            raise ValueError(
                "OpenRouter rejected the text/vision request (" + str(result["status"]) + ")"
            )
        if result.get("status") != 200 or not isinstance(result.get("response"), dict):
            raise PostprocessingUnavailable(unknown)
        response = result["response"]
        self.store.blobs.put(
            saved, json.dumps({"request_sha256": fingerprint, "response": response}).encode()
        )
        self.store.blobs.delete(pending)
        record(charge(key, response))
        return response

    def run(self, request, tools):
        started = time.monotonic()
        journal_key = self.run_key + "/agent-run.json"
        journal = {"provider": "openrouter", "model": self.model}
        if (
            self.store.exists(journal_key)
            and json.loads(self.store.read_key(journal_key)).get("provider") != "openrouter"
        ):
            raise PostprocessingUnavailable(
                "An existing legacy agent run must finish before switching providers"
            )
        self.store.blobs.put(journal_key, json.dumps(journal).encode())
        atomic_write(self.store.root / journal_key, json.dumps(journal).encode())
        definitions = request.get("tools", [])
        names = {item["function"]["name"] for item in definitions}
        maximum = request.get("max_tool_calls", 0)
        if type(maximum) is not int or not 0 <= maximum <= 256:
            raise ValueError("Invalid tool-call budget")
        messages = [
            {"role": "system", "content": request["instructions"]},
            {"role": "user", "content": request["input"]},
        ]
        calls, charges = [], []
        totals = {
            "inputTokens": 0,
            "cachedInputTokens": 0,
            "outputTokens": 0,
            "reasoningOutputTokens": 0,
            "totalTokens": 0,
        }
        direct = self.max_new_calls is not None
        with (nullcontext(None) if direct else tools.open()) as executor, httpx.Client(timeout=120, trust_env=False) as client:
            if not direct:
                EndpointPolicy().check(executor["url"])
            for index in range(maximum + 1):
                if time.monotonic() - started >= request["timeout_seconds"]:
                    raise PostprocessingUnavailable(
                        "OpenRouter run deadline reached; saved calls retained"
                    )
                body = {
                    "model": self.model,
                    "messages": messages,
                    "stream": False,
                    "reasoning": {"effort": request.get("reasoning_effort", "high")},
                    "max_completion_tokens": 32768,
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {
                            "name": "tubescribe_result",
                            "strict": True,
                            "schema": request["output_schema"],
                        },
                    },
                    "provider": {"require_parameters": True},
                }
                if definitions:
                    body.update(tools=definitions, tool_choice="auto")
                response = self.completion(index, body)
                charges.append(
                    charge(self.run_key + "/openrouter/call-" + str(index).zfill(4), response)
                )
                usage = response.get("usage") or {}
                for target, field in [
                    ("inputTokens", "prompt_tokens"),
                    ("outputTokens", "completion_tokens"),
                    ("totalTokens", "total_tokens"),
                ]:
                    totals[target] += usage.get(field, 0)
                totals["cachedInputTokens"] += (usage.get("prompt_tokens_details") or {}).get(
                    "cached_tokens", 0
                )
                totals["reasoningOutputTokens"] += (
                    usage.get("completion_tokens_details") or {}
                ).get("reasoning_tokens", 0)
                choices = response.get("choices") or []
                if not choices:
                    raise ValueError("OpenRouter returned no completion")
                message = choices[0]["message"]
                if choices[0].get("finish_reason") in {"length", "content_filter"}:
                    raise ValueError("OpenRouter did not finish a usable result")
                selected = message.get("tool_calls") or []
                if not selected:
                    output = json.loads(message["content"])
                    known = [c["cost_usd"] for c in charges if c["cost_usd"] is not None]
                    complete = len(known) == len(charges)
                    cost = sum(known) if known else None
                    return {
                        "id": self.run_key,
                        "model": self.model,
                        "reasoning_effort": request.get("reasoning_effort", "high"),
                        "output": output,
                        "tool_calls": calls,
                        "usage": {"total": {**totals, "cost": cost}},
                        "charges": charges,
                        "cost": cost,
                        "cost_complete": complete,
                        "elapsed_seconds": round(time.monotonic() - started, 2),
                    }
                if len(calls) + len(selected) > maximum:
                    raise ValueError("OpenRouter exceeded the tool-call budget")
                messages.append(
                    {
                        k: message[k]
                        for k in ("role", "content", "tool_calls", "reasoning_details")
                        if k in message
                    }
                )
                images = []
                for call in selected:
                    name = call["function"]["name"]
                    arguments = json.loads(call["function"]["arguments"])
                    if name not in names or not isinstance(arguments, dict):
                        raise ValueError(
                            "OpenRouter requested an undeclared tool or invalid arguments"
                        )
                    # Replaying saved model calls also replays local read-only tools,
                    # rebuilding the domain's asset and evidence state deterministically.
                    if direct:
                        value = tools.direct(name, arguments)
                    else:
                        result = client.post(
                            executor["url"],
                            headers={"Authorization": "Bearer " + executor["token"]},
                            json={"run_id": self.run_key, "call_id": call["id"],
                                  "tool": name, "arguments": arguments},
                        )
                        result.raise_for_status()
                        value = result.json()
                    text = []
                    count = 0
                    for item in value["contentItems"]:
                        if item["type"] == "inputText":
                            text.append(item["text"])
                        elif item["type"] == "inputImage" and item["imageUrl"].startswith(
                            ("data:image/png;base64,", "data:image/jpeg;base64,")
                        ):
                            images.append(
                                {
                                    "type": "text",
                                    "text": "Image returned by "
                                    + name
                                    + " (call "
                                    + call["id"]
                                    + ").",
                                }
                            )
                            images.append(
                                {"type": "image_url", "image_url": {"url": item["imageUrl"]}}
                            )
                            count += 1
                        else:
                            raise ValueError("Tool returned an unsupported content item")
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": json.dumps(
                                {
                                    "success": value["success"],
                                    "text": text,
                                    "images_in_following_message": count,
                                }
                            ),
                        }
                    )
                    calls.append(
                        {
                            "call_id": call["id"],
                            "tool": name,
                            "arguments": arguments,
                            "success": value["success"],
                            "image_count": count,
                        }
                    )
                if images:
                    messages.append({"role": "user", "content": images})
        raise ValueError("OpenRouter run exhausted its tool budget without a final result")
