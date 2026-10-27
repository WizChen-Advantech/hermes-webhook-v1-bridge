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

Other API server paths (`/v1/models`, `/v1/chat/completions`, ...) are unchanged.

## Install

1. Enable the webhook platform with at least one route in `config.yaml`
   (`platforms.webhook.extra.routes`), and set `WEBHOOK_ENABLED=true` and
   `WEBHOOK_SECRET` in `.env` (the secret from the environment is only read when
   `WEBHOOK_ENABLED` is set).
2. Dashboard: **Plugins → Install** with `WizChen-Advantech/hermes-webhook-v1-bridge`,
   enable it, then restart the gateway. Or on the host:

   ```bash
   hermes plugins install WizChen-Advantech/hermes-webhook-v1-bridge
   hermes plugins enable webhook-v1-bridge
   ```

The gateway log then shows
`webhook-v1-bridge: POST /v1/webhooks/{route} -> http://127.0.0.1:8644/webhooks/{route}`.
If the webhook platform runs elsewhere, set `WEBHOOK_BRIDGE_TARGET`
(default `http://127.0.0.1:$WEBHOOK_PORT`, port 8644).

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
- If a Hermes release no longer has `gateway.platforms.api_server.cors_middleware` or
  `APIServerAdapter._check_auth`, the plugin logs one warning and changes nothing.

To undo, disable the plugin and restart the gateway.

## License

MIT
