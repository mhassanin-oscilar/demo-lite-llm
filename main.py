"""Exercise the LiteLLM gateway with tenant + function-name cost tracking.

Usage:
    cp .env.example .env   # then fill in OPENAI_API_KEY etc.
    docker compose up -d
    python main.py

The proxy is hit through the OpenAI SDK (base_url -> http://localhost:4000).
Each call carries:
  - user           -> tenant_id        (LiteLLM aggregates spend per `user`)
  - metadata.tags  -> function + model (used to break down spend per function)

After the calls we pull /spend/logs and aggregate locally so we can show
total cost per tenant, per (tenant, function), and per (tenant, function, model).
"""

from __future__ import annotations

import os
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import httpx
from dotenv import load_dotenv
from openai import OpenAI 

load_dotenv()

LITELLM_BASE_URL = os.environ.get("LITELLM_BASE_URL", "http://localhost:4000")
LITELLM_MASTER_KEY = os.environ["LITELLM_MASTER_KEY"]


def call_llm(tenant_id: str, function_name: str, model: str, prompt: str) -> str:
    client = OpenAI(base_url=LITELLM_BASE_URL, api_key=LITELLM_MASTER_KEY)
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        user=tenant_id,
        extra_body={
            "metadata": {
                "tags": [
                    f"tenant:{tenant_id}",
                    f"function:{function_name}",
                    f"model:{model}",
                ],
                "spend_logs_metadata": {
                    "tenant_id": tenant_id,
                    "function_name": function_name,
                },
            },
        },
    )
    return resp.choices[0].message.content or ""


def fetch_spend_logs(start_date: str, end_date: str) -> list[dict]:
    # summarize=false returns per-request rows. Default is daily aggregates,
    # which drop the per-request `spend_logs_metadata` we need for tenant/function.
    with httpx.Client(timeout=30.0) as h:
        r = h.get(
            f"{LITELLM_BASE_URL}/spend/logs",
            params={
                "start_date": start_date,
                "end_date": end_date,
                "summarize": "false",
            },
            headers={"Authorization": f"Bearer {LITELLM_MASTER_KEY}"},
        )
        r.raise_for_status()
        data = r.json()
    return data if isinstance(data, list) else data.get("logs", [])


def _extract_tenant_and_function(entry: dict) -> tuple[str, str]:
    meta = entry.get("metadata") or {}
    slm = meta.get("spend_logs_metadata") or {}

    tenant = slm.get("tenant_id") or entry.get("user") or "unknown"
    function = slm.get("function_name")

    if not function:
        tags = entry.get("request_tags") or meta.get("tags") or []
        for tag in tags:
            if isinstance(tag, str) and tag.startswith("function:"):
                function = tag.split(":", 1)[1]
                break
    return tenant, function or "unknown"


def report(logs: list[dict]) -> None:
    by_tenant: dict[str, float] = defaultdict(float)
    by_tenant_fn: dict[tuple[str, str], float] = defaultdict(float)
    by_tenant_fn_model: dict[tuple[str, str, str], float] = defaultdict(float)
    calls: dict[str, int] = defaultdict(int)

    for entry in logs:
        spend = float(entry.get("spend") or 0.0)
        model = entry.get("model") or "unknown"
        tenant, fn = _extract_tenant_and_function(entry)

        by_tenant[tenant] += spend
        by_tenant_fn[(tenant, fn)] += spend
        by_tenant_fn_model[(tenant, fn, model)] += spend
        calls[tenant] += 1

    print("\n=== Spend by tenant ===")
    for t, v in sorted(by_tenant.items(), key=lambda x: -x[1]):
        print(f"  {t:<20s} ${v:.6f}  ({calls[t]} calls)")

    print("\n=== Spend by tenant -> function ===")
    for (t, fn), v in sorted(by_tenant_fn.items(), key=lambda x: -x[1]):
        print(f"  {t:<20s} {fn:<24s} ${v:.6f}")

    print("\n=== Spend by tenant -> function -> model ===")
    for (t, fn, m), v in sorted(by_tenant_fn_model.items(), key=lambda x: -x[1]):
        print(f"  {t:<20s} {fn:<24s} {m:<20s} ${v:.6f}")


def main() -> None:
    cases = [
        ("tenant_acme",   "summarize_email",  "gpt-4o-mini", "Summarize: 'Lunch is moved to 1pm.'"),
        ("tenant_acme",   "classify_intent",  "gpt-4o-mini", "Classify intent: 'I want a refund.'"),
        ("tenant_acme",   "summarize_email",  "gpt-4o",      "Summarize this email in one sentence: 'The Q2 roadmap review is rescheduled to Friday.'"),
        ("tenant_globex", "classify_intent",  "gpt-4o-mini", "Classify intent: 'Where is my package?'"),
        ("tenant_globex", "extract_fields",   "gpt-4o-mini", "Extract name and date as JSON: 'John, 2026-05-07'"),
    ]

    for tenant, fn, model, prompt in cases:
        out = call_llm(tenant, fn, model, prompt)
        print(f"[{tenant}/{fn}/{model}] -> {out[:80]!r}")

    # spend rows are written async by the proxy
    time.sleep(2)

    today = datetime.now(timezone.utc).date()
    logs = fetch_spend_logs(
        start_date=str(today - timedelta(days=1)),
        end_date=str(today + timedelta(days=1)),
    )
    print(f"\nFetched {len(logs)} spend log rows")
    report(logs)


if __name__ == "__main__":
    main()
