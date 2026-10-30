# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Reprice jobs started from the WebUI, advanced one chunk per scheduled incremental run (under the L2 lease).

Job record PK=REPRICE, SK=latest. The WebUI writes job_id, status (queued / cancelling / cancelled), start, dry_run,
started_by and created_at; everything else is written here. The record is treated as untrusted input.
Status: queued → running → done | failed; cancelling → cancelled. Every write is conditional on job_id.
"""

import os
import re
import time
from datetime import date, datetime, timedelta

import boto3

import compute_cost as cc

JOB_KEY = {"PK": "REPRICE", "SK": "latest"}
ACTIVE = ("queued", "running", "cancelling")
CHUNK_S = 420                       # lease time a chunk may add on top of the incremental run
SETTLE_MINUTE = int(os.environ.get("SETTLE_MINUTE", "20"))
SETTLE_GUARD = (8, 5)               # no chunk starts this many minutes before / after the settle minute
MAX_STALLS = 3
DIFF_LIMIT = 300
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
START_RE = re.compile(r"^(all|\d{4}-\d{2}-\d{2})$")
TOTAL_KEYS = ("days", "puts", "deletes", "drops", "days_changed", "months_changed")


class Deadline:
    """Lambda context stand-in whose remaining time is capped at `seconds` from now."""

    def __init__(self, context, seconds: int):
        self.context, self.end = context, time.time() + seconds

    def get_remaining_time_in_millis(self) -> int:
        real = self.context.get_remaining_time_in_millis() if self.context else 300_000
        return int(min(real, (self.end - time.time()) * 1000))


class Stale(Exception):
    """The job changed under us (cancelled, replaced); stop without counting a stall."""


_table = None


def table():
    global _table
    if _table is None or _table.name != cc.USAGE_STATS_TABLE:
        _table = boto3.resource("dynamodb").Table(cc.USAGE_STATS_TABLE)
    return _table


def load_job() -> dict | None:
    return table().get_item(Key=JOB_KEY, ConsistentRead=True).get("Item")


def update_job(job_id: str, sets: dict, removes: tuple = (), statuses: tuple = (), next_is: str | None = None):
    """SET/REMOVE on the job, conditional on job_id (and optionally status ∈ statuses, next = next_is)."""
    names, values, parts = {"#j": "job_id"}, {":j": job_id}, []
    for i, (k, v) in enumerate(sets.items()):
        names[f"#s{i}"], values[f":s{i}"] = k, v
        parts.append(f"#s{i} = :s{i}")
    expr = "SET " + ", ".join(parts) if parts else ""
    if removes:
        for i, k in enumerate(removes):
            names[f"#r{i}"] = k
        expr += " REMOVE " + ", ".join(f"#r{i}" for i in range(len(removes)))
    cond = "#j = :j"
    if statuses:
        names["#st"] = "status"
        cond += " AND #st IN (" + ", ".join(f":st{i}" for i in range(len(statuses))) + ")"
        values.update({f":st{i}": v for i, v in enumerate(statuses)})
    if next_is is not None:
        names["#n"], values[":n"] = "next", next_is
        cond += " AND #n = :n"
    try:
        table().update_item(Key=JOB_KEY, UpdateExpression=expr.strip(), ConditionExpression=cond,
                            ExpressionAttributeNames=names, ExpressionAttributeValues=values)
    except table().meta.client.exceptions.ConditionalCheckFailedException:
        raise Stale() from None


def validate(job: dict):
    if not UUID_RE.match(str(job.get("job_id", ""))):
        raise ValueError("invalid job_id")
    if not START_RE.match(str(job.get("start", ""))) or (job["start"] != "all" and not _is_date(job["start"])):
        raise ValueError("invalid start")
    if not isinstance(job.get("dry_run"), bool):
        raise ValueError("invalid dry_run")
    if job["status"] in ("running", "cancelling"):
        if not _is_date(str(job.get("next", ""))) or date.fromisoformat(job["next"]).day != 1:
            raise ValueError("invalid next")
        if not isinstance(job.get("first_full"), dict) or not all(_is_date(str(v)) for v in job["first_full"].values()):
            raise ValueError("invalid first_full")


def _is_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
        return len(value) == 10
    except ValueError:
        return False


def near_settle(t: datetime) -> bool:
    offset = (t.minute - SETTLE_MINUTE) % 60
    return offset >= 60 - SETTLE_GUARD[0] or offset < SETTLE_GUARD[1]


def month_start(d: date) -> date:
    return d.replace(day=1)


def remaining_months(next_day: date, yesterday: date) -> int:
    n, m = 0, next_day
    while m <= yesterday:
        n, m = n + 1, cc.next_month(m)
    return n


def merge_progress(job: dict, result: dict) -> dict:
    totals = {k: int(job.get("totals", {}).get(k, 0)) + int(result.get(k, 0)) for k in TOTAL_KEYS}
    diffs = cc.trim_diffs(list(job.get("diffs", [])) + list(result.get("diffs", [])), DIFF_LIMIT)
    return {"totals": totals, "diffs": diffs}


# ── Chunk ──

def run_chunk(context) -> dict | None:
    """Advance the active job by at most CHUNK_S. Call with the L2 lease held."""
    job = load_job()
    if not job or job.get("status") not in ACTIVE:
        return None
    job_id = str(job.get("job_id", ""))
    now = cc.utcnow()
    try:
        validate(job)
    except ValueError as e:
        if UUID_RE.match(job_id):
            _quiet(update_job, job_id, {"status": "failed", "error": str(e), "finished_at": now.isoformat()})
        return {"job": job_id, "status": "failed", "error": str(e)}
    try:
        if job["status"] == "cancelling":
            return finish(job_id, "cancelled", now)
        if near_settle(now):
            return {"job": job_id, "skipped": "settle window"}
        deadline = Deadline(context, CHUNK_S)  # counts from here so the chunk stays clear of the next settle
        if job["status"] == "queued":
            try:
                job = start_job(job, now)
            except Stale:
                raise
            except Exception as e:
                stalls = int(job.get("stalls", 0)) + 1
                _quiet(update_job, job_id, {"error": f"{type(e).__name__}: {e}"[:500], "updated_at": cc.utcnow().isoformat(),
                                            **_stall_fields(stalls)}, statuses=("queued",))
                raise
            if job["status"] != "running":
                return {"job": job_id, "status": job["status"]}
        return advance(job, deadline)
    except Stale:
        current = load_job() or {}
        if current.get("job_id") == job_id and current.get("status") == "cancelling":
            return _quiet(finish, job_id, "cancelled", cc.utcnow())
        return {"job": job_id, "stale": True}


def start_job(job: dict, now: datetime) -> dict:
    first_full = cc.iceberg_first_complete_days()
    if not first_full:
        finish(job["job_id"], "done", now, statuses=("queued",))
        return {**job, "status": "done"}
    earliest = month_start(min(first_full.values()))
    start = earliest if job["start"] == "all" else max(month_start(date.fromisoformat(job["start"])), earliest)
    sets = {"status": "running", "next": start.isoformat(), "first_full": {pk: d.isoformat() for pk, d in first_full.items()},
            "months_done": 0, "months_total": remaining_months(start, now.date() - timedelta(days=1)), "today_done": False,
            "stalls": 0, "totals": {k: 0 for k in TOTAL_KEYS}, "diffs": [], "started_at": now.isoformat(),
            "updated_at": now.isoformat()}
    update_job(job["job_id"], sets, statuses=("queued",), removes=("error",))
    if job["start"] == "all" and not job["dry_run"]:
        cc.clear_legacy_unpriced()
    return {**job, **sets}


def advance(job: dict, deadline: Deadline) -> dict:
    job_id, dry_run = job["job_id"], job["dry_run"]
    first_full = {pk: date.fromisoformat(d) for pk, d in job["first_full"].items()}
    expected = job["next"]
    stalls = int(job.get("stalls", 0))
    if job.get("inflight_next") == expected:  # the previous chunk was killed mid-way
        stalls += 1
        if stalls >= MAX_STALLS:
            return finish(job_id, "failed", cc.utcnow(), error="a chunk was stopped before it finished 3 times")
    update_job(job_id, {"inflight_next": expected, "stalls": stalls}, statuses=("running",), next_is=expected)
    started = progressed = False
    try:
        while True:
            now = cc.utcnow()
            yesterday = now.date() - timedelta(days=1)
            month = date.fromisoformat(expected)
            if month <= yesterday:
                if cc.remaining_s(deadline) < cc.MONTH_MARGIN_S:
                    break
                if (load_job() or {}).get("status") != "running":
                    raise Stale()
                last = min(cc.next_month(month) - timedelta(days=1), yesterday)
                days = {pk: {d for d in _days(max(month, first), last)} for pk, first in first_full.items()}
                started = True
                result = cc.settle_days(days, now, deadline, dry_run=dry_run, markers={}, first_full=first_full)
                if result["next"] is not None:
                    break  # stopped inside the month; it is redone next chunk
                new_next = cc.next_month(month).isoformat()
                update_job(job_id, {"next": new_next, "months_done": int(job.get("months_done", 0)) + 1,
                                    "months_total": int(job.get("months_done", 0)) + 1
                                    + remaining_months(cc.next_month(month), yesterday),
                                    "inflight_next": new_next, "stalls": 0, "updated_at": now.isoformat(),
                                    **merge_progress(job, result)},
                           removes=("error",), statuses=("running", "cancelling"), next_is=expected)
                job = {**job, "months_done": int(job.get("months_done", 0)) + 1, **merge_progress(job, result)}
                expected, progressed, stalls = new_next, True, 0
                continue
            if not dry_run and not job.get("today_done"):
                if cc.remaining_s(deadline) < cc.MONTH_MARGIN_S:
                    break
                if (load_job() or {}).get("status") != "running":
                    raise Stale()
                started = True
                recompute_today(now, deadline, first_full)
            return finish(job_id, "done", cc.utcnow(), extra={"today_done": not dry_run, "months_total": job.get("months_done", 0)},
                          removes=("inflight_next",), statuses=("running", "cancelling"))
    except Stale:
        raise
    except Exception as e:
        _quiet(update_job, job_id, {"error": f"{type(e).__name__}: {e}"[:500], "updated_at": cc.utcnow().isoformat(),
                                    **_stall_fields(stalls + 1)}, removes=("inflight_next",))
        raise
    stalled = started and not progressed
    sets = {"updated_at": cc.utcnow().isoformat(), **(_stall_fields(stalls + 1) if stalled else {})}
    if stalled:
        sets["error"] = "a month did not finish within one chunk"
    update_job(job_id, sets, removes=("inflight_next",))
    return {"job": job_id, "next": expected, "progressed": progressed, "stalled": stalled}


def _stall_fields(stalls: int) -> dict:
    fields = {"stalls": stalls}
    if stalls >= MAX_STALLS:
        fields.update(status="failed", finished_at=cc.utcnow().isoformat())
    return fields


def _days(first: date, last: date):
    d = first
    while d <= last:
        yield d
        d += timedelta(days=1)


def recompute_today(now: datetime, context, first_full: dict):
    today = cc.day_start(now.date())
    pks = set(first_full) | set(cc.list_accounts())
    hours = {(pk, today + timedelta(hours=h)) for pk in pks for h in range(now.hour + 1)}
    cc.recompute_hours(hours, now, context, dry_run=False, clear_unpriced=True)


def finish(job_id: str, status: str, now: datetime, error: str | None = None, extra: dict | None = None,
           removes: tuple = (), statuses: tuple = ()) -> dict:
    sets = {"status": status, "finished_at": now.isoformat(), "updated_at": now.isoformat(), **(extra or {})}
    if error:
        sets["error"] = error
    update_job(job_id, sets, removes=removes, statuses=statuses or (("cancelling",) if status == "cancelled" else ()))
    return {"job": job_id, "status": status}


def _quiet(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Stale:
        return None
