# hermes-webhook-v1-bridge

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that serves the
webhook platform's routes on the API server, as `POST /v1/webhooks/{route}`.

## Why

The webhook platform listens on its own port (8644). Behind a proxy that forwards only
the API server's `/v1/*` paths, those routes cannot be reached from outside at all.

This plugin lets the API server answer `/v1/webhooks/{route}` and pass the request --
body and signature headers unchanged -- to the webhook platform on `127.0.0.1`. The
webhook platform still checks the HMAC signature, the rate limit and duplicate
deliveries; on top of that the caller must send the API server key
(`Authorization: Bearer`), like every other `/v1` call.

## Measured (Hermes 0.13.0)

| Request to `/v1/webhooks/<route>` | Result |
|---|---|
| no API key | 401 `Invalid API key` |
| API key, wrong signature | 401 `Invalid signature` |
| API key, right signature | 202 `accepted`, the agent runs the route's prompt |
| same `X-Request-ID` again | 200 `duplicate` |
| `/v1/webhooks/a/b` | 404 |

Other API server paths (`/v1/models`, `/v1/chat/completions`, ...) are unchanged, and
so are other aiohttp servers in the process. The bridge works whether the plugin loads
before or after the API server starts (`tests/test_bridge.py`, `late`).

## Install

1. Enable the webhook platform with at least one route in `config.yaml`
   (`platforms.webhook.enabled: true`, `platforms.webhook.extra.routes`). Give each
   route a `secret`, or set `WEBHOOK_ENABLED=true` and `WEBHOOK_SECRET` in `.env` (the
   secret from the environment is only read when `WEBHOOK_ENABLED` is set).
   Plugins load only when listed in `plugins.enabled` in `config.yaml`.
2. Dashboard: **Plugins → Install** with `WizChen-Advantech/hermes-webhook-v1-bridge`,
   enable it, then restart the gateway. Or on the host:

   ```bash
   hermes plugins install WizChen-Advantech/hermes-webhook-v1-bridge
   hermes plugins enable webhook-v1-bridge
   ```

The gateway log then shows
`webhook-v1-bridge: POST /v1/webhooks/{route} -> http://127.0.0.1:8644/webhooks/{route}`.
The port comes from `WEBHOOK_PORT`, else `platforms.webhook.extra.port`, else 8644;
`WEBHOOK_BRIDGE_TARGET` overrides the whole address.

## Call

```bash
BODY='{"event_type":"error_case","case_id":"T001","reason":"..."}'
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$WEBHOOK_SECRET" | awk '{print $2}')
curl -X POST "https://<host>/v1/webhooks/<route>" \
  -H "Authorization: Bearer $API_SERVER_KEY" \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Signature: $SIG" \
  -H "X-Request-ID: T001" \
  -d "$BODY"
```

## Safety

- The bridge only forwards `POST` to one route name; nested paths get 404.
- Only signature, event and delivery headers are passed on; `Authorization` is not.
- Only the API server's router (the one serving `/v1/chat/completions`) is bridged.
- If a Hermes release no longer has `APIServerAdapter._check_auth`, or aiohttp no longer
  has `UrlDispatcher.resolve`, the plugin logs one warning and changes nothing.

## Tests

```bash
PYTHONPATH=<hermes source> python tests/test_bridge.py        # applied before start
PYTHONPATH=<hermes source> python tests/test_bridge.py late   # applied after start
```

To undo, disable the plugin and restart the gateway.

## License

MIT
