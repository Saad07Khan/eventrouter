import hashlib
import hmac
import json
from typing import NamedTuple

import httpx

TIMEOUT = 10.0


class DeliveryResult(NamedTuple):
    ok: bool
    error: str | None = None
    retry_after: int | None = None  # seconds; set when a destination says "come back in N"


def _sign(secret: str, body: bytes) -> str:
    """HMAC-SHA256 of the exact body, so the receiver can verify it's really us
    and that the payload wasn't tampered with in transit."""
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _parse_retry_after(resp: httpx.Response) -> int | None:
    value = resp.headers.get("Retry-After")
    if value and value.isdigit():
        return int(value)
    return None


async def _send(url: str, body: bytes, headers: dict) -> DeliveryResult:
    """POST a body and turn the outcome into a DeliveryResult.

    Every failure mode becomes a result, never an exception: a timeout, a dead
    host, or a 5xx are all just 'not ok, retry'. A 429 additionally carries the
    Retry-After hint so backoff can honor it.
    """
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(url, content=body, headers=headers)
    except httpx.TimeoutException:
        return DeliveryResult(ok=False, error="timeout")
    except httpx.HTTPError as exc:
        return DeliveryResult(ok=False, error=f"connection error: {type(exc).__name__}")

    if 200 <= resp.status_code < 300:
        return DeliveryResult(ok=True)
    return DeliveryResult(
        ok=False,
        error=f"HTTP {resp.status_code}",
        retry_after=_parse_retry_after(resp),
    )


async def deliver_http(payload: dict, config: dict) -> DeliveryResult:
    """Generic webhook: POST JSON, optionally HMAC-signed with a per-destination secret."""
    url = config.get("url")
    if not url:
        return DeliveryResult(ok=False, error="destination config missing 'url'")
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    secret = config.get("secret")
    if secret:
        headers["X-Signature"] = _sign(secret, body)
    return await _send(url, body, headers)


async def deliver_slack(payload: dict, config: dict) -> DeliveryResult:
    """Slack incoming webhook: same POST, but no signing and its own config key.

    Slack rate-limits with 429 + Retry-After — _send already surfaces that, and
    backoff honors it. This is why destinations are separate implementations:
    they differ not just in URL but in what failure MEANS.
    """
    url = config.get("webhook_url") or config.get("url")
    if not url:
        return DeliveryResult(ok=False, error="destination config missing 'webhook_url'")
    body = json.dumps(payload).encode()
    return await _send(url, body, {"Content-Type": "application/json"})


# Which function handles each destination type. Warehouse is added in Step 6.
DELIVERERS = {
    "http": deliver_http,
    "slack": deliver_slack,
}
