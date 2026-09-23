# HTTP surface and CORS - internal notes

Internal companion to `DEPLOY_EC2.md`. That file says how to run it; this one says what
is exposed, what the CORS setting actually does, and what we accepted when we chose it.

## What the container serves

The app defines no routes of its own - everything below is Streamlit's, reached on 8501.

| Route | Method | Purpose | Auth |
|---|---|---|---|
| `/` | GET | the app | none |
| `/_stcore/stream` | GET (WebSocket) | the session: every widget change and every run | none |
| `/_stcore/health` | GET | liveness, used by the container healthcheck and by an ALB | none |
| `/_stcore/upload_file/...` | POST | Streamlit's uploader - unused by this app, `maxUploadSize = 5` | XSRF token |
| `/_stcore/allowed-message-origins` | GET | origins the frontend will accept postMessage from | none |
| `/static/...`, `/media/...` | GET | assets | none |

**There is no authentication anywhere.** Whoever reaches port 8501 can run a comparison,
read both schemas, and read every report. Access control is the security group's job, or
a proxy in front.

## What `enableCORS = false` means here

Streamlit's own semantics, from `web/server/server_util.py` in 1.64:

- `allow_all_cross_origin_requests()` returns true, so responses carry
  `Access-Control-Allow-Origin: *`.
- `is_url_from_allowed_origins()` short-circuits to true, so the **WebSocket is accepted
  from any origin**.
- `corsAllowedOrigins` is ignored entirely - it only applies when CORS is enabled.
- Streamlit logs a warning at startup saying exactly this. It is expected, not a
  misconfiguration.

### The consequence, stated plainly

A page on any origin can open `ws://<host>:8501/_stcore/stream` from a visitor's browser
and drive the app as that visitor: trigger runs, and read back the schema, the row-level
differences and the report. `enableXsrfProtection` does not prevent it - the token covers
form posts and the upload route, not the WebSocket handshake.

This is only acceptable because the port is expected to be closed to the internet. If
8501 is ever opened to `0.0.0.0/0`, the app is effectively public and so is the schema of
both databases.

### Why we did it anyway

Asked for: "CORS all origin". It is the right setting for an internal tool reached from
several hosts, IPs and port-forwards during development, where maintaining an origin
allowlist is friction with no benefit *while the port is restricted*. The two ways out
are both one line, no rebuild:

```env
STREAMLIT_ENABLE_CORS=true
CORS_ORIGINS=https://dbcompare.internal.example.com
```

## Data that leaves the instance

Worth knowing when the network policy is written:

| To | What | When |
|---|---|---|
| `api.openai.com` | table and column names, types, row counts, and sampled rows in observations | every run |
| SQL Server (1433) | read-only queries, always rolled back | every run |

The schema summary is in every prompt, so **table and column names are sent to OpenAI on
every run**. If that is unacceptable, point `OPENAI_BASE_URL` at a self-hosted model; the
provider is swappable, and `LLM_PROVIDER=mock` runs the whole agent loop offline.

## Hardening, if this stops being an internal tool

1. Put an authenticating proxy in front (ALB + OIDC, or nginx with basic auth), bind the
   container to `127.0.0.1` via `BIND_ADDRESS`.
2. Turn CORS back on with a real origin list.
3. Terminate TLS at the proxy; nothing here speaks HTTPS.
4. Give the SQL login `db_datareader` only - the app never writes, and `run_select`
   rejects anything that is not a single `SELECT`/`WITH`, but defence in depth is free.
5. Move the key out of `.env` into Secrets Manager.

## Container facts worth remembering

- Runs as uid 10001 (`dbcompare`), non-root; `/app/logs` is the only writable path.
- `.env` is mounted read-only and excluded from the image by `.dockerignore`, so an image
  that leaks carries no credentials.
- Memory is capped at 2 GB: `compare_table_data` holds both sides of a table in memory,
  and an unbounded container would take a small instance down with it.
- The healthcheck polls `/_stcore/health`; `docker compose ps` shows unhealthy before the
  app can serve.
