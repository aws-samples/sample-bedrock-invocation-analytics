# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Time-versioned model prices, shared by the SQL recompute (VALUES table) and the Python reference (bisect)."""

import bisect
from dataclasses import dataclass
from datetime import datetime, timezone

from boto3.dynamodb.conditions import Key

PRICE_COLUMNS = ("p_in", "p_out", "p_cr", "p_cw", "p_cw1h", "p_req")  # micro-USD per 1k tokens, p_req per request
FAR_FUTURE = datetime(9999, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class PriceVersion:
    eff_from: datetime
    eff_to: datetime
    prices: tuple[int, ...]  # aligned with PRICE_COLUMNS


def micro(value) -> int:
    return int(round(float(value or 0) * 1_000_000))


def parse_effective(sk: str) -> datetime:
    return datetime.fromisoformat(sk.replace("Z", "+00:00")).astimezone(timezone.utc)


def versions_from_items(items: list[dict]) -> list[PriceVersion]:
    """Pricing table items of one model → contiguous versions ordered by effective time."""
    ordered = sorted(items, key=lambda i: parse_effective(i["SK"]))
    out = []
    for n, item in enumerate(ordered):
        cache_write = micro(item.get("cache_write_per_1k"))
        prices = (micro(item.get("input_per_1k")), micro(item.get("output_per_1k")), micro(item.get("cache_read_per_1k")),
                  cache_write, micro(item.get("cache_write_1h_per_1k")) or cache_write, micro(item.get("per_request")))
        eff_to = parse_effective(ordered[n + 1]["SK"]) if n + 1 < len(ordered) else FAR_FUTURE
        out.append(PriceVersion(parse_effective(item["SK"]), eff_to, prices))
    return out


def load_versions(pricing_table, models: set[str]) -> dict[str, list[PriceVersion]]:
    out = {}
    for model in sorted(models):
        kwargs = {"KeyConditionExpression": Key("PK").eq(f"MODEL#{model}"), "ConsistentRead": True}  # a price saved just before a reprice applies
        items = []
        while True:
            resp = pricing_table.query(**kwargs)
            items.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        if items:
            out[model] = versions_from_items(items)
    return out


def price_at(versions: list[PriceVersion] | None, ts: datetime) -> tuple[int, ...] | None:
    """Prices in effect at ts (eff_from <= ts < eff_to), or None when the model is unpriced at that time."""
    if not versions:
        return None
    i = bisect.bisect_right([v.eff_from for v in versions], ts) - 1
    return versions[i].prices if i >= 0 and ts < versions[i].eff_to else None


def values_table(versions: dict[str, list[PriceVersion]]) -> str:
    """SQL `VALUES` rows (model_id, eff_from, eff_to, prices...). Empty input yields one row that matches nothing."""
    def ts(dt):
        return f"from_iso8601_timestamp('{dt.strftime('%Y-%m-%dT%H:%M:%S.%fZ')}')"
    rows = [f"('{model.replace(chr(39), chr(39) * 2)}', {ts(v.eff_from)}, {ts(v.eff_to)}, "
            + ", ".join(f"CAST({p} AS BIGINT)" for p in v.prices) + ")"
            for model, vs in sorted(versions.items()) for v in vs]
    if not rows:
        rows = [f"(CAST(NULL AS VARCHAR), {ts(FAR_FUTURE)}, {ts(FAR_FUTURE)}, " + ", ".join("CAST(0 AS BIGINT)" for _ in PRICE_COLUMNS) + ")"]
    return ",\n".join(rows)
