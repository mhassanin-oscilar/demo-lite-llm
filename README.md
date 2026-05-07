# LiteLLM Gateway — Per-Tenant Cost Tracking Demo

A minimal end-to-end setup that runs the **LiteLLM AI Gateway** in Docker
(against OpenAI upstream) and shows how to attribute every request to a
**tenant** and a **function name**, then read the spend back broken down
by tenant → function → model.

## What it showcases

- **A self-hosted OpenAI-compatible proxy** ([docker-compose.yml](docker-compose.yml))
  fronting OpenAI, with Postgres as the spend/key store.
- **Multi-tenant cost attribution** without creating one virtual API key per
  tenant — driven entirely by request metadata.
- **Function-level cost attribution** so you can answer "how much did the
  `summarize_email` function cost tenant_acme last week?".
- **Programmatic spend reporting** via `/spend/logs?summarize=false` aggregated
  client-side ([main.py](main.py)).

What you get on stdout after a run:

```
=== Spend by tenant ===
  tenant_acme          $0.000216  (3 calls)
  tenant_globex        $0.000037  (2 calls)

=== Spend by tenant -> function ===
  tenant_acme          summarize_email          $0.000189
  tenant_acme          classify_intent          $0.000027
  ...

=== Spend by tenant -> function -> model ===
  tenant_acme          summarize_email          openai/gpt-4o        $0.000180
  tenant_acme          summarize_email          openai/gpt-4o-mini   $0.000009
  ...
```

## Architecture

```
   main.py  ──HTTP──▶  LiteLLM proxy (:4000)  ──HTTPS──▶  OpenAI
   (OpenAI SDK)             │
                            ▼
                       Postgres  (spend logs, keys, users)
```

`main.py` runs on the host inside any Python 3.10+ environment. The gateway
and Postgres run as Docker containers.

## Prerequisites

- Docker + Docker Compose v2
- An OpenAI API key
- Python 3.10+ (use whatever environment manager you prefer — conda, venv, uv)

## Setup

```bash
# 1. Configure secrets
cp .env.example .env
$EDITOR .env                    # fill in OPENAI_API_KEY at minimum

# 2. Install client deps into your Python 3.10+ env
pip install -r requirements.txt

# 3. Bring up the gateway + Postgres
docker compose up -d

# 4. (Optional) wait until healthy
docker compose ps               # both services should report (healthy)
```

Required env vars in `.env` (template in [.env.example](.env.example)):

| Var | Purpose |
|---|---|
| `OPENAI_API_KEY` | Used by the proxy to call OpenAI |
| `LITELLM_MASTER_KEY` | Proxy admin key (must start with `sk-`); also used by `main.py` |
| `LITELLM_SALT_KEY` | Encrypts model creds in Postgres — **don't change after first boot** |
| `POSTGRES_PASSWORD` | Shared between the postgres container and the proxy |
| `LITELLM_BASE_URL` | Defaults to `http://localhost:4000` |

## Run the demo

```bash
python main.py
```

This sends 5 chat completions across 2 tenants × 3 function names × 2 models,
waits 2 seconds for the proxy to flush spend logs to Postgres, then queries
`/spend/logs?summarize=false` and prints the three breakdowns shown above.

## How attribution works

Every call from [main.py](main.py) carries this body:

```jsonc
{
  "model": "gpt-4o-mini",
  "messages": [...],
  "user": "tenant_acme",                              // OpenAI end-user field
  "metadata": {
    "tags": ["tenant:tenant_acme", "function:summarize_email", "model:gpt-4o-mini"],
    "spend_logs_metadata": {
      "tenant_id": "tenant_acme",
      "function_name": "summarize_email"
    }
  }
}
```

- `spend_logs_metadata` — survives even when caller-supplied tags are stripped
  (the master key doesn't have `allow_client_tags: true`); this is the
  primary signal `main.py` reads back.
- `tags` — secondary fallback; useful once you mint per-tenant virtual keys
  with `metadata.allow_client_tags=true`.
- `user` — populates the OpenAI `end_user` slot. Gives you another aggregation
  axis (`/global/spend/end_users`).

Spend rows then come back with `metadata.spend_logs_metadata` intact, so the
reporter in [main.py](main.py) builds three rollups:

1. by tenant
2. by (tenant, function)
3. by (tenant, function, model)

## Smoke-test the proxy directly

```bash
# Health
curl http://localhost:4000/health/liveness

# A raw chat completion (replace $LITELLM_MASTER_KEY)
curl http://localhost:4000/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4o-mini",
    "messages": [{"role":"user","content":"ping"}],
    "user": "tenant_acme",
    "metadata": {"spend_logs_metadata":{"tenant_id":"tenant_acme","function_name":"smoke"}}
  }'

# Per-request spend rows
curl -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  "http://localhost:4000/spend/logs?start_date=2026-05-06&end_date=2026-05-08&summarize=false" \
  | jq '.[0]'
```

## Files

| File | Role |
|---|---|
| [docker-compose.yml](docker-compose.yml) | Postgres + LiteLLM proxy (port 4000) |
| [config.yaml](config.yaml) | Proxy model list (gpt-4o, gpt-4o-mini) and DB wiring |
| [.env.example](.env.example) | Secrets template (`cp` to `.env`) |
| [requirements.txt](requirements.txt) | Client deps: openai, httpx, python-dotenv |
| [main.py](main.py) | Exercises the gateway and prints the spend breakdown |

## Cleanup

```bash
docker compose down            # stop containers, keep data
docker compose down -v         # also drop the postgres volume
```
