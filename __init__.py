"""Serve the webhook platform's routes under the API server.

Hermes's webhook platform listens on its own port (8644). Where only the API
server's port is reachable from outside -- a proxy that forwards ``/v1/*`` and
nothing else -- the webhook routes cannot be called at all.

This plugin answers ``POST /v1/webhooks/{route}`` on the API server and passes
the request, body and signature headers unchanged, to the webhook platform on
127.0.0.1. The webhook platform still checks the HMAC signature, the rate limit
and duplicates; on top of that the caller must send the API server's key
(``Authorization: Bearer``) like every other ``/v1`` call.

It adds a middleware by replacing ``cors_middleware`` in
``gateway.platforms.api_server`` with one that handles the bridge path and
otherwise calls the original. The gateway discovers plugins before it connects
the platforms, so the API server is built with it. If a Hermes release renames
what this relies on, the plugin logs one warning and changes nothing.

Target: ``WEBHOOK_BRIDGE_TARGET`` (default ``http://127.0.0.1:$WEBHOOK_PORT``,
port 8644).
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

PREFIX = "/v1/webhooks/"
_MARK = "_webhook_v1_bridge"
# What the webhook platform reads: signatures, event type, delivery id.
_PASS_HEADERS = (
    "Content-Type",
    "X-Webhook-Signature",
    "X-Hub-Signature-256",
    "X-Gitlab-Token",
    "X-GitHub-Event",
    "X-GitLab-Event",
    "X-GitHub-Delivery",
    "X-Request-ID",
)


def _target() -> str:
    explicit = os.environ.get("WEBHOOK_BRIDGE_TARGET", "").strip()
    if explicit:
        return explicit.rstrip("/")
    return f"http://127.0.0.1:{os.environ.get('WEBHOOK_PORT', '8644').strip() or '8644'}"


def _route_name(path: str) -> str | None:
    """The route after the prefix: one plain path segment, else None."""
    name = path[len(PREFIX):]
    if not name or "/" in name or name in (".", ".."):
        return None
    return name


async def _forward(request, web, aiohttp):
    adapter = request.app.get("api_server_adapter")
    if adapter is not None:
        denied = adapter._check_auth(request)
        if denied is not None:
            return denied
    name = _route_name(request.path)
    if name is None:
        return web.json_response({"error": "Unknown webhook route"}, status=404)
    body = await request.read()
    headers = {h: request.headers[h] for h in _PASS_HEADERS if h in request.headers}
    url = f"{_target()}/webhooks/{name}"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.post(url, data=body, headers=headers) as upstream:
                payload = await upstream.read()
                return web.Response(
                    body=payload,
                    status=upstream.status,
                    content_type=upstream.content_type or "application/json",
                )
    except aiohttp.ClientError as exc:
        logger.warning("webhook-v1-bridge: %s unreachable (%s)", url, exc)
        return web.json_response(
            {"error": "Webhook platform not reachable; is platforms.webhook enabled?"},
            status=502,
        )


def apply() -> bool:
    """Patch the API server. True when the bridge is in place."""
    try:
        import aiohttp
        from aiohttp import web
        from gateway.platforms import api_server
    except Exception as exc:  # noqa: BLE001
        logger.warning("webhook-v1-bridge: cannot import the API server (%s); not applied", exc)
        return False
    original = getattr(api_server, "cors_middleware", None)
    if original is None or not hasattr(api_server.APIServerAdapter, "_check_auth"):
        logger.warning("webhook-v1-bridge: this Hermes has no api_server.cors_middleware "
                       "or APIServerAdapter._check_auth; not applied")
        return False
    if getattr(original, _MARK, False):
        return True

    @web.middleware
    async def bridge_middleware(request, handler):
        if request.method == "POST" and request.path.startswith(PREFIX):
            return await _forward(request, web, aiohttp)
        return await original(request, handler)

    setattr(bridge_middleware, _MARK, True)
    api_server.cors_middleware = bridge_middleware
    logger.info("webhook-v1-bridge: POST %s{route} -> %s/webhooks/{route}", PREFIX, _target())
    return True


def register(ctx) -> None:
    apply()
