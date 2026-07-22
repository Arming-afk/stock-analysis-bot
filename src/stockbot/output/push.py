"""Web Push (VAPID) to the installed PWA.

iOS supports Web Push from 16.4, but only for a PWA that has been added to the
Home Screen — a Safari tab will not receive these. That constraint is why the
dashboard is built as an installable PWA rather than a plain page.

Generate a key pair once:
    python -m stockbot.output.push --generate-keys
"""

from __future__ import annotations

import json

from ..config import Config
from ..logging_setup import get_logger

log = get_logger("output.push")


def collect_subscriptions(cfg: Config, stored: list[dict]) -> list[dict]:
    """Merge DB-registered subscriptions with one supplied via the environment.

    A scheduled run on a throwaway runner has no server for the phone to
    register against, so the subscription JSON can be handed in as a secret
    instead. Deduplicated by endpoint, so both paths can coexist.
    """
    subscriptions = list(stored)
    raw = cfg.secrets.push_subscription_json
    if not raw:
        return subscriptions

    try:
        from_env = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.error("PUSH_SUBSCRIPTION_JSON is not valid JSON (%s) — ignoring", exc)
        return subscriptions

    for sub in from_env if isinstance(from_env, list) else [from_env]:
        if not isinstance(sub, dict) or not sub.get("endpoint"):
            log.error("PUSH_SUBSCRIPTION_JSON entry has no endpoint — ignoring")
            continue
        if any(s.get("endpoint") == sub["endpoint"] for s in subscriptions):
            continue
        subscriptions.append(sub)

    return subscriptions


def send_push(cfg: Config, subscriptions: list[dict], title: str, body: str, url: str = "/") -> tuple[int, list[str]]:
    """Returns (delivered_count, dead_endpoints)."""
    if not cfg.secrets.has_vapid:
        log.warning("VAPID keys not configured — skipping push")
        return 0, []
    if not subscriptions:
        log.info("no push subscriptions registered")
        return 0, []

    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        log.error("pywebpush not installed — skipping push")
        return 0, []

    payload = json.dumps({"title": title, "body": body, "url": url})
    delivered = 0
    dead: list[str] = []

    for sub in subscriptions:
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=cfg.secrets.vapid_private_key,
                vapid_claims={"sub": cfg.secrets.vapid_contact},
            )
            delivered += 1
        except WebPushException as exc:
            status = getattr(exc.response, "status_code", None)
            if status in (404, 410):
                # Subscription is gone for good; the caller prunes it.
                dead.append(sub.get("endpoint", ""))
                log.info("pruning dead push subscription (%s)", status)
            else:
                log.warning("push failed: %s", exc)

    log.info("push: %d delivered, %d dead", delivered, len(dead))
    return delivered, dead


def generate_vapid_keys() -> tuple[str, str]:
    """Returns (public_key, private_key), base64url-encoded for .env."""
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    private = ec.generate_private_key(ec.SECP256R1())
    public = private.public_key()

    private_bytes = private.private_numbers().private_value.to_bytes(32, "big")
    public_bytes = public.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )

    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return b64(public_bytes), b64(private_bytes)


if __name__ == "__main__":
    import sys

    if "--generate-keys" in sys.argv:
        pub, priv = generate_vapid_keys()
        print("Add these to your .env:\n")
        print(f"VAPID_PUBLIC_KEY={pub}")
        print(f"VAPID_PRIVATE_KEY={priv}")
    else:
        print(__doc__)
