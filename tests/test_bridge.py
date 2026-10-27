"""The bridge on a running server, applied before and after it starts.

Run with the Hermes source on PYTHONPATH:
    PYTHONPATH=<hermes source>:<dir holding this plugin> python tests/test_bridge.py
"""

import asyncio
import importlib.util
import os
import sys
from pathlib import Path

import aiohttp
from aiohttp import web

KEY = "test-key"
_HERE = Path(__file__).resolve().parent.parent


def load_plugin():
    spec = importlib.util.spec_from_file_location("bridge_plugin", _HERE / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeAdapter:
    def _check_auth(self, request):
        if request.headers.get("Authorization") == f"Bearer {KEY}":
            return None
        return web.json_response({"error": "Invalid API key"}, status=401)


async def start(app, port):
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return runner


def api_app():
    app = web.Application()
    app["api_server_adapter"] = FakeAdapter()
    app.router.add_post("/v1/chat/completions", lambda r: web.json_response({"chat": True}))
    app.router.add_get("/v1/models", lambda r: web.json_response({"models": True}))
    return app


def webhook_app(seen):
    async def receive(request):
        seen.append((request.match_info["name"], request.headers.get("X-Webhook-Signature"),
                     request.headers.get("Authorization"), await request.read()))
        return web.json_response({"status": "accepted"}, status=202)
    app = web.Application()
    app.router.add_post("/webhooks/{name}", receive)
    return app


async def check(late: bool) -> None:
    os.environ["WEBHOOK_BRIDGE_TARGET"] = "http://127.0.0.1:18744"
    plugin = load_plugin()
    seen = []
    other = web.Application()  # a server that is not the API server
    other.router.add_post("/v1/webhooks/{x}", lambda r: web.json_response({"other": True}))
    if not late:
        assert plugin.apply()
    runners = [await start(api_app(), 18742), await start(webhook_app(seen), 18744),
               await start(other, 18746)]
    if late:
        assert plugin.apply()
    assert plugin.apply()  # twice is harmless
    url = "http://127.0.0.1:18742/v1/webhooks/learn"
    auth = {"Authorization": f"Bearer {KEY}"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, data=b"{}") as r:
                assert r.status == 401, r.status
            async with s.post(url, data=b'{"a":1}', headers={**auth, "X-Webhook-Signature": "sig"}) as r:
                assert r.status == 202, (r.status, await r.text())
            assert seen == [("learn", "sig", None, b'{"a":1}')], seen
            async with s.post(url + "/nested", headers=auth) as r:
                assert r.status == 404 and "Unknown webhook route" in await r.text()
            async with s.get("http://127.0.0.1:18742/v1/models", headers=auth) as r:
                assert r.status == 200
            async with s.post("http://127.0.0.1:18742/v1/chat/completions", headers=auth) as r:
                assert (await r.json()) == {"chat": True}
            async with s.post("http://127.0.0.1:18746/v1/webhooks/z") as r:
                assert (await r.json()) == {"other": True}, "only the API server is bridged"
    finally:
        for runner in runners:
            await runner.cleanup()
    print(f"ok ({'applied after start' if late else 'applied before start'})")


if __name__ == "__main__":
    asyncio.run(check(late=sys.argv[1:] == ["late"]))
