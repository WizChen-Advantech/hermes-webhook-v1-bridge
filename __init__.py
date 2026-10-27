"""Serve the webhook platform's routes under the API server.

Hermes's webhook platform listens on its own port (8644). Where only the API
server's port is reachable from outside -- a proxy that forwards ``/v1/*`` and
nothing else -- the webhook routes cannot be called at all.

This plugin answers ``POST /v1/webhooks/{route}`` on the API server and passes
the request, body and signature headers unchanged, to the webhook platform on
127.0.0.1. The webhook platform still checks the HMAC signature, the rate limit
and duplicates; on top of that the caller must send the API server's key
(``Authorization: Bearer``) like every other ``/v1`` call.

It patches aiohttp's ``UrlDispatcher.resolve``: for the API server's router
only (the one serving ``/v1/chat/completions``), a ``POST`` under the prefix
resolves to the bridge handler; every other request resolves as before. The
router is consulted on each request, so this also works when the plugin loads
after the API server has started -- which some deployments do. If a Hermes
release renames what this relies on, the plugin logs one warning and changes
nothing.

Target: ``WEBHOOK_BRIDGE_TARGET``, else ``http://127.0.0.1:<port>`` with the
port from ``WEBHOOK_PORT``, ``platforms.webhook.extra.port`` or 8644.
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


def _config_port() -> str:
    """platforms.webhook.extra.port from config.yaml, or ''."""
    try:
        from hermes_cli.config import load_config
        port = load_config()["platforms"]["webhook"]["extra"]["port"]
        return str(int(port))
    except Exception:  # noqa: BLE001
        return ""


def _target() -> str:
    explicit = os.environ.get("WEBHOOK_BRIDGE_TARGET", "").strip()
    if explicit:
        return explicit.rstrip("/")
    # The webhook platform reads WEBHOOK_PORT over config.yaml, so do we.
    port = os.environ.get("WEBHOOK_PORT", "").strip() or _config_port() or "8644"
    return f"http://127.0.0.1:{port}"


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


def _serves_api(router) -> bool:
    """Whether this router is the API server's: it has /v1/chat/completions."""
    try:
        return any(
            getattr(resource, "canonical", None) == "/v1/chat/completions"
            for resource in router.resources()
        )
    except Exception:  # noqa: BLE001
        return False


def apply() -> bool:
    """Patch the API server. True when the bridge is in place."""
    try:
        import aiohttp
        from aiohttp import web
        from aiohttp.web_urldispatcher import UrlDispatcher
        from gateway.platforms import api_server
    except Exception as exc:  # noqa: BLE001
        logger.warning("webhook-v1-bridge: cannot import the API server (%s); not applied", exc)
        return False
    if not hasattr(api_server.APIServerAdapter, "_check_auth") or not hasattr(UrlDispatcher, "resolve"):
        logger.warning("webhook-v1-bridge: this Hermes has no APIServerAdapter._check_auth "
                       "or aiohttp UrlDispatcher.resolve; not applied")
        return False
    original = UrlDispatcher.resolve
    if getattr(original, _MARK, False):
        return True

    async def handle(request):
        return await _forward(request, web, aiohttp)

    # A router of its own, never attached to an app, only to build match info
    # for the bridge path the same way any route gets it.
    bridge = UrlDispatcher()
    bridge.add_route("POST", PREFIX + "{route:.*}", handle)
    api_routers: dict[int, bool] = {}

    async def resolve(self, request):
        if request.method == "POST" and request.path.startswith(PREFIX):
            serves_api = api_routers.get(id(self))
            if serves_api is None:
                serves_api = api_routers[id(self)] = _serves_api(self)
            if serves_api:
                return await original(bridge, request)
        return await original(self, request)

    setattr(resolve, _MARK, True)
    UrlDispatcher.resolve = resolve
    logger.info("webhook-v1-bridge: POST %s{route} -> %s/webhooks/{route}", PREFIX, _target())
    return True


def register(ctx) -> None:
    apply()
