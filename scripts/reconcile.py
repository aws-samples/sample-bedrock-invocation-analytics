#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Read-only check that DynamoDB matches the Iceberg event log. Exit 1 on any difference.

Usage: AWS_PROFILE=... python3 scripts/reconcile.py [START END]   (UTC dates; default: last 90 settled days)

For every settled (pk, day): DAILY rows must equal an independent Athena aggregation (dedup by request_id) for
invocations, tokens, latency sums and min/max per dimension, and costs/TPOT recomputed per event in Python. Invariants per DAILY day: TOTAL = sum of MODEL rows,
TOTAL >= sum of CALLER rows; per completed month: MONTHLY TOTAL = sum of DAILY TOTAL.
"""

import csv
import io
import os
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import boto3
from boto3.dynamodb.conditions import Key

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "deploy" / "lambda"))
import pricing  # noqa: E402  (price data is the source of truth; the aggregation below is independent of L2)

STACK = os.environ.get("STACK_NAME", "BedrockInvocationAnalytics")
FIELDS = ("invocations", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "latency_sum_ms",
          "max_latency_ms", "min_latency_ms")
COST_FIELDS = ("cost_micro_usd", "cost_input_micro", "cost_output_micro", "cost_cache_read_micro", "cost_cache_write_micro",
               "cost_request_micro", "tpot_sum", "tpot_count", "tpot_max", "tpot_min")


def stack_outputs():
    cfn = boto3.client("cloudformation")
    outs = {o["OutputKey"]: o["OutputValue"] for o in cfn.describe_stacks(StackName=STACK)["Stacks"][0]["Outputs"]}
    account = boto3.client("sts").get_caller_identity()["Account"]
    return outs["UsageStatsTableName"], outs["ModelPricingTableName"], outs["AthenaWorkgroupName"], f'"s3tablescatalog/bedrock-analytics-{account}".bedrock_analytics.usage_events'


def athena(workgroup, sql):
    a = boto3.client("athena")
    qid = a.start_query_execution(QueryString=sql, WorkGroup=workgroup,
                                  ResultReuseConfiguration={"ResultReuseByAgeConfiguration": {"Enabled": False}})["QueryExecutionId"]
    while (q := a.get_query_execution(QueryExecutionId=qid)["QueryExecution"])["Status"]["State"] in ("QUEUED", "RUNNING"):
        time.sleep(1)
    if q["Status"]["State"] != "SUCCEEDED":
        sys.exit(f"Athena query {qid} {q['Status']['State']}")
    bucket, key = q["ResultConfiguration"]["OutputLocation"].removeprefix("s3://").split("/", 1)
    body = boto3.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read().decode()
    return [{k: (None if v == "" else v) for k, v in r.items()} for r in csv.DictReader(io.StringIO(body))]


def event_costs(workgroup, table_ref, pricing_table, start, end):
    """(pk, day) -> dim -> cost and TPOT fields, priced per event in Python (independent of the L2 SQL)."""
    events = athena(workgroup, f"""
        SELECT account_id, region, ts, model_id, caller, input_tokens, output_tokens, cache_read_tokens,
               cache_write_5m_tokens, cache_write_1h_tokens, cache_write_total_tokens, latency_ms
        FROM (SELECT *, row_number() OVER (PARTITION BY request_id ORDER BY parsed_at) rn FROM {table_ref}
          WHERE ts >= from_iso8601_date('{start}') AND ts < from_iso8601_date('{end + timedelta(days=1)}'))
        WHERE rn = 1 AND error_code IS NULL""")
    versions = pricing.load_versions(pricing_table, {e["model_id"] or "unknown" for e in events})
    out = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    num = lambda v: int(v or 0)  # noqa: E731
    for e in events:
        ts = datetime.fromisoformat(e["ts"][:19].replace(" ", "T")).replace(tzinfo=timezone.utc)
        model = e["model_id"] or "unknown"
        p = pricing.price_at(versions.get(model), ts) or (0,) * len(pricing.PRICE_COLUMNS)
        if e["cache_write_5m_tokens"] is None and e["cache_write_1h_tokens"] is None:
            cw5, cw1 = num(e["cache_write_total_tokens"]), 0
        else:
            cw5, cw1 = num(e["cache_write_5m_tokens"]), num(e["cache_write_1h_tokens"])
        c = lambda tokens, price: (tokens * price + 500) // 1000  # noqa: E731
        ci, co, ccr = c(num(e["input_tokens"]), p[0]), c(num(e["output_tokens"]), p[1]), c(num(e["cache_read_tokens"]), p[2])
        ccw, creq = c(cw5, p[3]) + c(cw1, p[4]), p[5]
        out_tokens, lat = num(e["output_tokens"]), num(e["latency_ms"])
        tpot = (lat * 2000 + out_tokens) // (2 * out_tokens) if out_tokens > 0 and lat > 0 else None
        pk = f"{e['account_id']}#{e['region']}"
        dims = [f"MODEL#{model}", "TOTAL"] + ([f"CALLER#{e['caller']}"] if e["caller"] else [])
        for dim in dims:
            r = out[(pk, ts.date().isoformat())][dim]
            for f, v in (("cost_input_micro", ci), ("cost_output_micro", co), ("cost_cache_read_micro", ccr),
                         ("cost_cache_write_micro", ccw), ("cost_request_micro", creq),
                         ("cost_micro_usd", ci + co + ccr + ccw + creq)):
                r[f] += v
            if tpot is not None:
                r["tpot_sum"] += tpot
                r["tpot_count"] += 1
                r["tpot_max"] = max(r["tpot_max"], tpot)
                r["tpot_min"] = tpot if not r["tpot_min"] else min(r["tpot_min"], tpot)
    return out


def query_all(table, condition):
    kwargs, items = {"KeyConditionExpression": condition}, []
    while True:
        resp = table.query(**kwargs)
        items.extend(resp["Items"])
        if "LastEvaluatedKey" not in resp:
            return items
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def main():
    table_name, pricing_name, workgroup, table_ref = stack_outputs()
    table = boto3.resource("dynamodb").Table(table_name)
    today = datetime.now(timezone.utc).date()
    start, end = (date.fromisoformat(sys.argv[1]), date.fromisoformat(sys.argv[2])) if len(sys.argv) == 3 \
        else (today - timedelta(days=90), today - timedelta(days=1))

    expected = defaultdict(dict)  # (pk, day) -> {dim: {field: value}}
    for r in athena(workgroup, f"""
        WITH e AS (
          SELECT request_id, arbitrary(account_id || '#' || region) pk, arbitrary(date_format(ts, '%Y-%m-%d')) dy,
                 arbitrary(coalesce(model_id, 'unknown')) model_id, arbitrary(coalesce(caller, '')) caller,
                 arbitrary(coalesce(input_tokens, 0)) i, arbitrary(coalesce(output_tokens, 0)) o,
                 arbitrary(coalesce(cache_read_tokens, 0)) cr, arbitrary(coalesce(cache_write_total_tokens, 0)) cw,
                 arbitrary(CAST(coalesce(latency_ms, 0) AS BIGINT)) lat
          FROM {table_ref} WHERE ts >= from_iso8601_date('{start}') AND ts < from_iso8601_date('{end + timedelta(days=1)}')
            AND error_code IS NULL GROUP BY request_id)
        SELECT pk, dy, CASE WHEN grouping(model_id) = 0 THEN 'MODEL#' || model_id
                            WHEN grouping(caller) = 0 THEN 'CALLER#' || caller ELSE 'TOTAL' END dim,
               count(*) invocations, sum(i) input_tokens, sum(o) output_tokens, sum(cr) cache_read_tokens,
               sum(cw) cache_write_tokens, sum(lat) latency_sum_ms,
               coalesce(max(nullif(lat, 0)), 0) max_latency_ms, coalesce(min(nullif(lat, 0)), 0) min_latency_ms
        FROM e GROUP BY GROUPING SETS ((pk, dy, model_id), (pk, dy, caller), (pk, dy))"""):
        if r["dim"] == "CALLER#":
            continue
        expected[(r["pk"], r["dy"])][r["dim"]] = {f: int(r[f]) for f in FIELDS}

    costs = event_costs(workgroup, table_ref, boto3.resource("dynamodb").Table(pricing_name), start, end)
    for key, dims in costs.items():
        for dim, values in dims.items():
            if dim in expected[key]:
                expected[key][dim].update(values)
    problems = 0
    pks = {pk for pk, _ in expected}
    for pk in sorted(pks):
        settled = table.get_item(Key={"PK": "META", "SK": f"SETTLED#{pk}"}).get("Item", {}).get("day")
        if not settled:
            problems += 1
            print(f"{pk[-14:]} has no SETTLED watermark (settle has not run for it)")
            continue
        last = min(end, date.fromisoformat(settled))
        d = start
        while d <= last:
            got = {i["SK"].split("#", 2)[2]: i for i in query_all(table, Key("PK").eq(pk) & Key("SK").begins_with(f"DAILY#{d.isoformat()}#"))}
            want = expected.get((pk, d.isoformat()), {})
            for dim in set(got) | set(want):
                g, w = got.get(dim), want.get(dim)
                if g is None or w is None:
                    problems += 1
                    print(f"{pk[-14:]} {d} {dim}: {'missing in DynamoDB' if g is None else 'not in Iceberg'}")
                    continue
                for f in FIELDS + COST_FIELDS:
                    if int(g.get(f, 0)) != int(w.get(f, 0)):
                        problems += 1
                        print(f"{pk[-14:]} {d} {dim} {f}: ddb={int(g.get(f, 0))} iceberg={int(w.get(f, 0))}")
            if "TOTAL" in got:
                total = int(got["TOTAL"]["invocations"])
                models = sum(int(v["invocations"]) for k, v in got.items() if k.startswith("MODEL#"))
                callers = sum(int(v["invocations"]) for k, v in got.items() if k.startswith("CALLER#"))
                if models != total or callers > total:
                    problems += 1
                    print(f"{pk[-14:]} {d} invariant: TOTAL={total} sum(MODEL)={models} sum(CALLER)={callers}")
            d += timedelta(days=1)
        month = date(start.year, start.month, 1)
        while month <= last:
            nxt = date(month.year + (month.month == 12), month.month % 12 + 1, 1)
            if nxt - timedelta(days=1) <= last:
                mrow = table.get_item(Key={"PK": pk, "SK": f"MONTHLY#{month:%Y-%m}#TOTAL"}).get("Item")
                days = query_all(table, Key("PK").eq(pk) & Key("SK").between(f"DAILY#{month:%Y-%m}-01", f"DAILY#{month:%Y-%m}-31~"))
                dsum = sum(int(i["invocations"]) for i in days if i["SK"].endswith("#TOTAL"))
                if mrow is None and dsum:
                    problems += 1
                    print(f"{pk[-14:]} {month:%Y-%m} MONTHLY TOTAL missing")
                elif mrow is not None and int(mrow["invocations"]) != dsum:
                    problems += 1
                    print(f"{pk[-14:]} {month:%Y-%m} invariant: MONTHLY={int(mrow['invocations'])} sum(DAILY)={dsum}")
            month = nxt
    print(f"checked {start}..{end}: {problems} difference(s)")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
