"""Read-only cloud configuration check. Never provisions or changes a plan."""

import json
import os
import re

REQUIRED = (
    "TUBESCRIBE_DATABASE_URL", "TUBESCRIBE_S3_BUCKET", "TUBESCRIBE_S3_ENDPOINT",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "TUBESCRIBE_MEDIA_IMAGE",
    "INFISICAL_AGENT_VAULT_PROXY_TOKEN",
    "TUBESCRIBE_VAULT_CA_FINGERPRINT", "CRON_SECRET",
)


def check(env=None):
    env = os.environ if env is None else env
    missing = [name for name in REQUIRED if not env.get(name)]
    invalid = []
    mode = env.get("TUBESCRIBE_VAULT_AUTH", "oidc")
    credential = "INFISICAL_AGENT_VAULT_SESSION_TOKEN" if mode == "session" else "TUBESCRIBE_INFISICAL_IDENTITY_ID"
    if not env.get(credential):
        missing.append(credential)
    if mode not in {"oidc", "session"}:
        invalid.append("TUBESCRIBE_VAULT_AUTH")
    for key, expected in (("TUBESCRIBE_MANUAL_QUEUE", "1"), ("TUBESCRIBE_DISPATCH", "queue"),
                          ("TUBESCRIBE_MIGRATIONS", "check")):
        if env.get(key) != expected:
            invalid.append(key)
    if env.get("TUBESCRIBE_MEDIA_IMAGE") and not re.fullmatch(
        r"[a-zA-Z0-9./_-]+@sha256:[a-f0-9]{64}", env["TUBESCRIBE_MEDIA_IMAGE"]
    ):
        invalid.append("TUBESCRIBE_MEDIA_IMAGE")
    if env.get("CRON_SECRET") and len(env["CRON_SECRET"]) < 32:
        invalid.append("CRON_SECRET")
    return {"ready": not missing and not invalid, "missing": missing, "invalid": invalid,
            "backend_enabled": env.get("TUBESCRIBE_PROCESSING_BACKEND") == "workflow",
            "manual_only": env.get("TUBESCRIBE_MANUAL_QUEUE") == "1"}


def main():
    result = check()
    print(json.dumps(result, indent=2))
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
