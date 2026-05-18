# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Bedrock Invocation Analytics WebUI — Dashboard."""

import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from nicegui import app, context, ui
from webui import data, tz as tzmod

VERSION = ""  # Set by main.py


# All preset boundaries are computed in the user's selected display tz, then converted
# to UTC at query time. Week starts Monday (ISO).
PRESET_OPTIONS = {
    "last_24h": "Last 24h",
    "last_7d": "Last 7 days",
    "last_30d": "Last 30 days",
    "last_90d": "Last 90 days",
    "today": "Today",
    "this_week": "This Week",
    "this_month": "This Month",
    "this_quarter": "This Quarter",
    "this_year": "This Year",
    "custom": "Custom...",
}


def _resolve_preset(preset: str, tz: ZoneInfo) -> tuple[datetime, datetime]:
    now = datetime.now(tz)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if preset == "last_24h":
        return now - timedelta(hours=24), now
    if preset == "last_7d":
        return now - timedelta(days=7), now
    if preset == "last_30d":
        return now - timedelta(days=30), now
    if preset == "last_90d":
        return now - timedelta(days=90), now
    if preset == "today":
        return today, now
    if preset == "this_week":
        return today - timedelta(days=today.weekday()), now
    if preset == "this_month":
        return today.replace(day=1), now
    if preset == "this_quarter":
        q_start_month = ((today.month - 1) // 3) * 3 + 1
        return today.replace(month=q_start_month, day=1), now
    if preset == "this_year":
        return today.replace(month=1, day=1), now
    raise ValueError(f"Unknown preset: {preset}")


def _period_to_local(period: str, tz: ZoneInfo) -> str:
    """Convert a UTC SK period string to a label in the display tz.
    Inputs: 'YYYY-MM-DDTHH' (HOURLY), 'YYYY-MM-DD' (DAILY), 'YYYY-MM' (MONTHLY).
    Output is the same format but representing the wall-clock instant in `tz`.
    DAILY/MONTHLY are date-only buckets and are returned unchanged — converting them
    would map a UTC day to a partial local day, which misleads more than it helps.
    """
    if not period:
        return period
    if "T" in period:  # HOURLY
        try:
            dt = datetime.strptime(period, "%Y-%m-%dT%H").replace(tzinfo=timezone.utc)
            return dt.astimezone(tz).strftime("%Y-%m-%dT%H")
        except ValueError:
            return period
    return period


def _localize_periods(rows: list[dict], tz: ZoneInfo) -> list[dict]:
    """Return rows with each row's 'period' rewritten in tz. Non-mutating."""
    return [{**r, "period": _period_to_local(r.get("period", ""), tz)} for r in rows]


def format_number(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def format_ms(ms: int | float) -> str:
    if ms >= 1000:
        return f"{ms / 1000:.2f}s"
    return f"{ms:.2f}ms"


def format_cost(usd: float) -> str:
    return f"${usd:.2f}"


def _short_model(model: str) -> str:
    return model.replace("global.", "").replace("anthropic.", "").replace("meta.", "")[:25]


def _fetch_dashboard_data(account_region: str, start_dt: datetime, end_dt: datetime, tz: ZoneInfo) -> dict:
    """All boto3/DDB queries in one place. Runs on a worker thread.
    Period strings on trend rows are localized to `tz` before returning, so chart x-axis
    labels match the user's selected display timezone.
    """
    return {
        "summary": data.get_summary(account_region, start_dt, end_dt),
        "models": data.get_by_model(account_region, start_dt, end_dt),
        "callers": data.get_by_caller(account_region, start_dt, end_dt),
        "trend_total": _localize_periods(data.get_trend(account_region, start_dt, end_dt, "TOTAL"), tz),
    }


def _fetch_trend(account_region: str, start_dt: datetime, end_dt: datetime, dim: str, tz: ZoneInfo) -> list[dict]:
    return _localize_periods(data.get_trend(account_region, start_dt, end_dt, dim), tz)


def _fetch_ttft(model_id: str, start_dt: datetime, end_dt: datetime, tz: ZoneInfo) -> list[dict]:
    return _localize_periods(data.get_ttft_trend(model_id, start_dt, end_dt), tz)


def _current_dim(model_sel, caller_sel) -> str:
    """Resolve the active dimension for a paired model+caller selector.

    Exactly one of the two should be non-TOTAL (enforced by mutual-exclusion
    handlers). If both are TOTAL → TOTAL. Model takes precedence if somehow both set.
    """
    m = (model_sel.value if model_sel else "TOTAL") or "TOTAL"
    c = (caller_sel.value if caller_sel else "TOTAL") or "TOTAL"
    if m != "TOTAL":
        return m
    if c != "TOTAL":
        return c
    return "TOTAL"


@ui.page("/")
def dashboard_page():
    ui.dark_mode(False)

    accounts = data.get_accounts()
    _tz_initial = tzmod.default_value()
    _zone_initial = tzmod.resolve(_tz_initial)
    _start, _end = _resolve_preset("last_7d", _zone_initial)
    state = {
        "account": accounts[0]["key"] if accounts else "",
        "preset": "last_7d",
        "start_dt": _start,
        "end_dt": _end,
        "tz_value": _tz_initial,    # dropdown value: "local" or "UTC"
        "tz": _zone_initial,         # resolved ZoneInfo
    }

    # Refs to UI elements that need incremental updates
    refs: dict = {
        "summary_labels": {},   # title → ui.label
        "charts": {},           # name → ui.echart (or list for pies)
        "model_selects": {},    # name → ui.select (single-select trend dim)
        "dim_selectors": {},    # name → (model_select, caller_select) paired selectors
    }
    refreshing = {"flag": False}
    timer_ref: dict = {"timer": None}
    last_updated_label: dict = {"label": None}

    # ── Header bar ──
    with ui.header().classes("bg-white text-gray-800 shadow-sm items-center px-6"):
        ui.button(icon="menu", on_click=lambda: drawer.toggle()).props("flat round")
        ui.label("Bedrock Invocation Analytics").classes("text-xl font-bold ml-2")
        ui.space()
        last_updated_label["label"] = ui.label("").classes("text-xs text-gray-400 mr-3").tooltip(
            "Latest window processed by L2 compute_cost. Data freshness lags real time "
            "by ~5-7 min (Firehose buffer + L2 5min schedule)."
        )
        auto_refresh = ui.select(
            {0: "Off", 10: "10s", 30: "30s", 60: "1min", 300: "5min"},
            value=0, label="Auto",
        ).props("dense outlined").classes("w-24").tooltip("Auto refresh interval")
        refresh_btn = ui.button(icon="refresh", on_click=lambda: refresh_data()).props("flat round").tooltip("Refresh")
        ui.button(icon="settings", on_click=lambda: ui.navigate.to("/pricing")).props("flat round").tooltip("Pricing Settings")
        ui.button(icon="logout", on_click=lambda: (app.storage.user.clear(), ui.navigate.to("/login"))).props("flat round").tooltip("Logout")

    def on_interval_change(e):
        val = e.value or 0
        if timer_ref["timer"]:
            timer_ref["timer"].deactivate()
            timer_ref["timer"] = None
        if val > 0:
            timer_ref["timer"] = ui.timer(val, refresh_data)

    auto_refresh.on_value_change(on_interval_change)

    def on_disconnect():
        if timer_ref["timer"]:
            timer_ref["timer"].deactivate()
            timer_ref["timer"] = None

    context.client.on_disconnect(on_disconnect)

    # ── Left drawer (sidebar) ──
    with ui.left_drawer(value=True).classes("bg-gray-50 p-4") as drawer:
        ui.label("Configuration").classes("text-lg font-semibold mb-4")

        if not accounts:
            ui.label("⚠️ No data found").classes("text-red-500")
            ui.label(f"Table: {data.USAGE_TABLE}").classes("text-xs text-gray-400")
            ui.label(f"Region: {data.AWS_REGION}").classes("text-xs text-gray-400")
            return

        account_ids = sorted(set(a["account_id"] for a in accounts))
        regions = sorted(set(a["region"] for a in accounts))

        # account_id → friendly name from config.yaml (falls back to account_id if no match)
        account_name_by_id = {a["account_id"]: (a.get("name") or "") for a in accounts}

        def _format(acct_id: str) -> str:
            name = account_name_by_id.get(acct_id, "")
            return f"{acct_id} ({name})" if name else acct_id

        account_select = ui.select(
            {a: _format(a) for a in account_ids},
            value=account_ids[0],
            label="Account ID",
        ).classes("w-full")

        region_select = ui.select(
            {r: r for r in regions},
            value=regions[0],
            label="Region",
        ).classes("w-full mt-2")

        range_select = ui.select(
            PRESET_OPTIONS,
            value=state["preset"],
            label="Time Range",
        ).classes("w-full mt-2")
        # Shows the resolved [start, end] for the active range — useful for "This Month" etc.
        range_label = ui.label("").classes("text-xs text-gray-400 mt-1")

        tz_select = ui.select(
            tzmod.options(),
            value=state["tz_value"],
            label="Timezone",
        ).classes("w-full mt-2").tooltip("Timezone for time-range presets, chart labels, and 'Data up to'.")

        # ── Custom range dialog ──
        with ui.dialog() as custom_dialog, ui.card().classes("min-w-[420px]"):
            ui.label("Custom Time Range").classes("text-lg font-semibold")
            now_in_tz = datetime.now(state["tz"])
            today_str = now_in_tz.strftime("%Y-%m-%d")
            start_str = (now_in_tz - timedelta(days=7)).strftime("%Y-%m-%d")
            with ui.row().classes("w-full gap-2"):
                custom_start = ui.input(label="Start date", value=start_str).props("type=date dense outlined").classes("flex-1")
                custom_end = ui.input(label="End date", value=today_str).props("type=date dense outlined").classes("flex-1")
            with ui.row().classes("w-full justify-end mt-4 gap-2"):
                ui.button("Cancel", on_click=lambda: (custom_dialog.close(), range_select.set_value(state["preset"]))).props("flat")
                ui.button("Apply", on_click=lambda: apply_custom_range()).props("color=primary")

        def update_range_label():
            s = state["start_dt"].astimezone(state["tz"]).strftime("%Y-%m-%d %H:%M")
            e = state["end_dt"].astimezone(state["tz"]).strftime("%Y-%m-%d %H:%M")
            range_label.text = f"{s} → {e}"

        def apply_custom_range():
            try:
                tz = state["tz"]
                s = datetime.strptime(custom_start.value, "%Y-%m-%d").replace(tzinfo=tz)
                e = datetime.strptime(custom_end.value, "%Y-%m-%d").replace(hour=23, minute=59, second=59, tzinfo=tz)
            except (ValueError, TypeError):
                ui.notify("Invalid date format", color="negative")
                return
            if s >= e:
                ui.notify("Start must be before end", color="negative")
                return
            state["preset"] = "custom"
            state["start_dt"] = s
            state["end_dt"] = e
            update_range_label()
            custom_dialog.close()
            asyncio.create_task(rebuild())

        # Serving layer — what this dashboard reads
        ui.input(value=data.USAGE_TABLE, label="Usage Table").props("readonly dense outlined").classes("w-full text-xs")
        ui.input(value=data.PRICING_TABLE, label="Pricing Table").props("readonly dense outlined").classes("w-full mt-2 text-xs")

        # Source-of-truth layer — for ad-hoc queries
        ui.separator().classes("my-3")
        ui.input(value=data.ATHENA_WORKGROUP, label="Athena Workgroup").props("readonly dense outlined").classes("w-full text-xs")
        iceberg_path = (
            f"{data.ICEBERG_CATALOG}.{data.ICEBERG_DATABASE}.{data.ICEBERG_TABLE}"
            if data.ICEBERG_CATALOG else ""
        )
        ui.input(value=iceberg_path, label="Iceberg Table").props("readonly dense outlined").classes("w-full mt-2 text-xs")

        ui.separator().classes("my-4")
        ui.label(f"Deployed Region: {data.AWS_REGION}").classes("text-xs text-gray-400")
        ui.label(f"v{VERSION}").classes("text-xs text-gray-400")

    content = ui.column().classes("w-full max-w-[1600px] mx-auto p-4 gap-6")

    def mark_updated():
        """Show 'Data up to' from the L2 checkpoint, not the UI refresh time.
        L2 runs on a schedule (default 5min); data freshness lags UI refresh."""
        lbl = last_updated_label["label"]
        if lbl is None:
            return
        cp = data.get_l2_checkpoint()
        if not cp:
            lbl.text = "Data up to: —"
            return
        last_window_end = cp.get("last_window_end", "")
        # last_window_end is ISO UTC, e.g. "2026-05-09T02:55:00+00:00"
        try:
            dt_utc = datetime.fromisoformat(last_window_end.replace("Z", "+00:00"))
            shown = dt_utc.astimezone(state["tz"])
            lbl.text = f"Data up to: {shown.strftime('%H:%M')}"
        except Exception:
            lbl.text = f"Data up to: {last_window_end}"

    async def rebuild():
        """Full DOM rebuild — used when account/region/range change."""
        if refreshing["flag"]:
            return
        refreshing["flag"] = True
        refresh_btn.props("loading")
        try:
            state["account"] = f"{account_select.value}#{region_select.value}"
            # 1. Fetch data off the event loop
            dashboard_data = await asyncio.to_thread(_fetch_dashboard_data, state["account"], state["start_dt"], state["end_dt"], state["tz"])
            # 2. Render UI on the main loop (required for NiceGUI context)
            refs["summary_labels"].clear()
            refs["charts"].clear()
            refs["model_selects"].clear()
            refs["dim_selectors"].clear()
            content.clear()
            with content:
                render_dashboard(state["account"], state["start_dt"], state["end_dt"], state["tz"], dashboard_data, refs)
            mark_updated()
        except Exception as e:
            ui.notify(f"Load failed: {e}", color="negative")
        finally:
            refreshing["flag"] = False
            refresh_btn.props(remove="loading")

    async def refresh_data():
        """Incremental data refresh — updates labels and chart options in place."""
        if refreshing["flag"]:
            return
        refreshing["flag"] = True
        refresh_btn.props("loading")
        try:
            # Refreshing a rolling preset (e.g. "Last 24h") should advance the window.
            # Calendar-aligned presets (today/this_week/...) also advance their end_dt to "now".
            if state["preset"] != "custom":
                state["start_dt"], state["end_dt"] = _resolve_preset(state["preset"], state["tz"])
                update_range_label()
            s, e = state["start_dt"], state["end_dt"]

            dashboard_data = await asyncio.to_thread(_fetch_dashboard_data, state["account"], s, e, state["tz"])

            trend_data = {}
            pair = refs["dim_selectors"].get("usage_trend")
            if pair:
                dim = _current_dim(*pair)
                trend_data["usage_trend"] = await asyncio.to_thread(_fetch_trend, state["account"], s, e, dim, state["tz"])
            sel = refs["model_selects"].get("latency_trend")
            lat_dim = (sel.value if sel else "TOTAL") or "TOTAL"
            trend_data["latency_trend"] = await asyncio.to_thread(_fetch_trend, state["account"], s, e, lat_dim, state["tz"])

            ttft_sel = refs["model_selects"].get("ttft_trend")
            ttft_model = ttft_sel.value if ttft_sel else ""
            ttft_data = await asyncio.to_thread(_fetch_ttft, ttft_model, s, e, state["tz"]) if ttft_model else []

            _apply_updates(dashboard_data, trend_data, ttft_data, refs)
            mark_updated()
        except Exception as e:
            ui.notify(f"Refresh failed: {e}", color="negative")
        finally:
            refreshing["flag"] = False
            refresh_btn.props(remove="loading")

    def on_range_change(e):
        preset = e.value
        if preset == "custom":
            custom_dialog.open()
            return
        state["preset"] = preset
        state["start_dt"], state["end_dt"] = _resolve_preset(preset, state["tz"])
        update_range_label()
        asyncio.create_task(rebuild())

    def on_tz_change(e):
        state["tz_value"] = e.value
        state["tz"] = tzmod.resolve(e.value)
        # Re-resolve the preset under the new tz so "This Month" snaps to the new wall clock
        if state["preset"] != "custom":
            state["start_dt"], state["end_dt"] = _resolve_preset(state["preset"], state["tz"])
        update_range_label()
        asyncio.create_task(rebuild())

    account_select.on_value_change(lambda _: rebuild())
    region_select.on_value_change(lambda _: rebuild())
    range_select.on_value_change(on_range_change)
    tz_select.on_value_change(on_tz_change)

    # Initial render — data fetched synchronously on page load is fine
    state["account"] = f"{account_select.value}#{region_select.value}"
    update_range_label()
    initial_data = _fetch_dashboard_data(state["account"], state["start_dt"], state["end_dt"], state["tz"])
    with content:
        render_dashboard(state["account"], state["start_dt"], state["end_dt"], state["tz"], initial_data, refs)
    mark_updated()


def _set_series_data(chart, series_name, new_data):
    for s in chart.options.get("series", []):
        if s.get("name") == series_name:
            s["data"] = new_data
            return


def _apply_updates(dashboard_data: dict, trend_data: dict, ttft_data: list, refs: dict):
    """Apply fetched data to existing UI elements (no DOM rebuild)."""
    summary = dashboard_data["summary"]
    models = dashboard_data["models"]
    callers = dashboard_data["callers"]

    # Summary labels
    labels = refs["summary_labels"]
    updates = {
        "Total Invocations": format_number(summary["invocations"]),
        "Input Tokens": format_number(summary["input_tokens"]),
        "Output Tokens": format_number(summary["output_tokens"]),
        "Cache Tokens": format_number(summary["cache_read_tokens"] + summary["cache_write_tokens"]),
        "Estimated Cost": format_cost(summary["cost_usd"]),
        "Avg Latency": format_ms(summary["avg_latency_ms"]),
        "Avg TPOT": format_ms(summary["avg_tpot"]),
    }
    for k, v in updates.items():
        if k in labels:
            labels[k].text = v

    charts = refs["charts"]
    top_models = models[:15]
    model_names = [_short_model(m["model"]) for m in top_models]

    if "model_bar" in charts:
        ch = charts["model_bar"]
        ch.options["xAxis"]["data"] = model_names
        for s, key in [("Input Tokens", "input_tokens"), ("Output Tokens", "output_tokens")]:
            _set_series_data(ch, s, [m[key] for m in top_models])
        for s, key in [("Cache Read $", "cost_cache_read"), ("Cache Write $", "cost_cache_write"),
                       ("Input $", "cost_input"), ("Output $", "cost_output")]:
            _set_series_data(ch, s, [round(m[key], 4) for m in top_models])
        ch.update()

    if "model_pie" in charts:
        _update_pies(charts["model_pie"], models[:10], key_field="model")

    top_callers = callers[:15]
    caller_names = [c["caller"][:25] for c in top_callers]
    if "caller_bar" in charts:
        ch = charts["caller_bar"]
        ch.options["xAxis"]["data"] = caller_names
        for s, key in [("Input Tokens", "input_tokens"), ("Output Tokens", "output_tokens")]:
            _set_series_data(ch, s, [c[key] for c in top_callers])
        for s, key in [("Cache Read $", "cost_cache_read"), ("Cache Write $", "cost_cache_write"),
                       ("Input $", "cost_input"), ("Output $", "cost_output")]:
            _set_series_data(ch, s, [round(c[key], 4) for c in top_callers])
        ch.update()

    if "caller_pie" in charts:
        _update_pies(charts["caller_pie"], callers[:10], key_field="caller")

    if "latency_by_model" in charts:
        ch = charts["latency_by_model"]
        ch.options["xAxis"]["data"] = model_names
        _set_series_data(ch, "Min", [m.get("min_latency_ms", 0) for m in top_models])
        _set_series_data(ch, "Avg", [m.get("avg_latency_ms", 0) for m in top_models])
        _set_series_data(ch, "Max", [m.get("max_latency_ms", 0) for m in top_models])
        ch.update()

    if "tpot_by_model" in charts:
        ch = charts["tpot_by_model"]
        ch.options["xAxis"]["data"] = model_names
        _set_series_data(ch, "Min", [m.get("tpot_min", 0) for m in top_models])
        _set_series_data(ch, "Avg", [m.get("tpot_avg", 0) for m in top_models])
        _set_series_data(ch, "Max", [m.get("tpot_max", 0) for m in top_models])
        ch.update()

    # Trend charts
    if "usage_trend" in charts:
        _apply_usage_trend(charts["usage_trend"], trend_data.get("usage_trend", []))
    if "latency_trend" in charts:
        _apply_latency_trend(charts["latency_trend"], trend_data.get("latency_trend", []))
    if "ttft_trend" in charts:
        _apply_ttft_trend(charts["ttft_trend"], ttft_data)


def _update_pies(pie_charts, rows, key_field):
    """pie_charts is a list of (chart, value_key, is_cost) tuples."""
    short_names = [_short_model(r[key_field]) if key_field == "model" else r[key_field][:20] for r in rows]
    for chart, value_key, is_cost in pie_charts:
        pie_data = [
            {"name": short_names[i], "value": round(rows[i][value_key], 4) if is_cost else rows[i][value_key]}
            for i in range(len(rows))
        ]
        chart.options["series"][0]["data"] = pie_data
        chart.update()


def _apply_usage_trend(chart, t: list[dict]):
    chart.options["xAxis"]["data"] = [x["period"] for x in t]
    _set_series_data(chart, "Invocations", [x["invocations"] for x in t])
    _set_series_data(chart, "Cost ($)", [round(x["cost_usd"], 6) for x in t])
    chart.update()


def _apply_latency_trend(chart, t: list[dict]):
    chart.options["xAxis"] = {"type": "category", "data": [x["period"] for x in t]}
    _set_series_data(chart, "Min", [x["min_latency_ms"] for x in t])
    _set_series_data(chart, "Avg", [x["avg_latency_ms"] for x in t])
    _set_series_data(chart, "Max", [x["max_latency_ms"] for x in t])
    chart.update()


def _apply_ttft_trend(chart, t: list[dict]):
    chart.options["xAxis"]["data"] = [x["period"] for x in t]
    _set_series_data(chart, "Avg TTFT", [x["ttft_avg"] for x in t])
    _set_series_data(chart, "P99 TTFT", [x["ttft_p99"] for x in t])
    chart.update()


def render_dashboard(account_region: str, start_dt: datetime, end_dt: datetime, tz: ZoneInfo, dashboard_data: dict, refs: dict):
    """Build DOM. All data must be pre-fetched in dashboard_data. `tz` is used for trend
    chart period labels and any sub-fetch triggered by inline selectors."""
    summary = dashboard_data["summary"]
    models = dashboard_data["models"]
    callers = dashboard_data["callers"]
    trend_total = dashboard_data["trend_total"]

    # ── Summary cards ──
    with ui.row().classes("w-full gap-3 flex-wrap"):
        summary_card(refs, "Total Invocations", format_number(summary["invocations"]), "call_made", "blue")
        summary_card(refs, "Input Tokens", format_number(summary["input_tokens"]), "input", "green")
        summary_card(refs, "Output Tokens", format_number(summary["output_tokens"]), "output", "orange")
        cache_total = summary["cache_read_tokens"] + summary["cache_write_tokens"]
        if cache_total:
            summary_card(refs, "Cache Tokens", format_number(cache_total), "cached", "teal")
        summary_card(refs, "Estimated Cost", format_cost(summary['cost_usd']), "attach_money", "red")
        summary_card(refs, "Avg Latency", format_ms(summary['avg_latency_ms']), "speed", "purple")
        summary_card(refs, "Avg TPOT", format_ms(summary['avg_tpot']), "timer", "indigo")

    if models:
        with ui.card().classes("w-full"):
            with ui.row().classes("w-full items-center px-4 pt-2"):
                ui.label("Token Usage & Cost by Model").classes("text-lg font-semibold")
                ui.space()
                with ui.tabs().props("dense").classes("text-xs") as model_tabs:
                    ui.tab("chart", label="Chart", icon="bar_chart")
                    ui.tab("pie", label="Pie", icon="pie_chart")
                    ui.tab("table", label="Table", icon="table_rows")
            ui.separator()
            with ui.tab_panels(model_tabs, value="chart").classes("w-full max-h-[420px] overflow-auto p-0"):
                with ui.tab_panel("pie").classes("p-2"):
                    with ui.row().classes("w-full gap-0"):
                        short_names = {m["model"]: _short_model(m["model"]) for m in models}
                        pie_refs = []
                        for title, key, fmt, is_cost in [
                            ("Input Tokens", "input_tokens", "{b}\n{c}", False),
                            ("Output Tokens", "output_tokens", "{b}\n{c}", False),
                            ("Cost", "cost_usd", "{b}\n${c}", True),
                        ]:
                            pie_data = [{"name": short_names[m["model"]], "value": round(m[key], 4) if is_cost else m[key]} for m in models[:10]]
                            chart = ui.echart({
                                "tooltip": {"trigger": "item", "formatter": "{b}: {d}%"},
                                "title": {"text": title, "left": "center", "textStyle": {"fontSize": 13, "color": "#6B7280"}},
                                "series": [{"name": title, "type": "pie", "radius": ["30%", "60%"],
                                    "label": {"formatter": fmt, "fontSize": 10},
                                    "data": pie_data,
                                }],
                            }).classes("flex-1 h-80")
                            pie_refs.append((chart, key, is_cost))
                        refs["charts"]["model_pie"] = pie_refs
                with ui.tab_panel("chart").classes("p-2"):
                    model_names = [_short_model(m["model"]) for m in models[:15]]
                    refs["charts"]["model_bar"] = ui.echart({
                        "tooltip": {"trigger": "axis"},
                        "legend": {"top": 0},
                        "grid": {"top": 40, "bottom": 70, "left": 60, "right": 60},
                        "xAxis": {"type": "category", "data": model_names, "axisLabel": {"rotate": 40, "interval": 0}},
                        "yAxis": [
                            {"type": "value", "name": "Tokens"},
                            {"type": "value", "name": "Cost ($)"},
                        ],
                        "series": [
                            {"name": "Input Tokens", "type": "bar", "data": [m["input_tokens"] for m in models[:15]]},
                            {"name": "Output Tokens", "type": "bar", "data": [m["output_tokens"] for m in models[:15]]},
                            {"name": "Cache Read $", "type": "bar", "stack": "cost", "yAxisIndex": 1, "itemStyle": {"color": "#06B6D4"}, "data": [round(m["cost_cache_read"], 4) for m in models[:15]]},
                            {"name": "Cache Write $", "type": "bar", "stack": "cost", "yAxisIndex": 1, "itemStyle": {"color": "#8B5CF6"}, "data": [round(m["cost_cache_write"], 4) for m in models[:15]]},
                            {"name": "Input $", "type": "bar", "stack": "cost", "yAxisIndex": 1, "itemStyle": {"color": "#3B82F6"}, "data": [round(m["cost_input"], 4) for m in models[:15]]},
                            {"name": "Output $", "type": "bar", "stack": "cost", "yAxisIndex": 1, "itemStyle": {"color": "#F97316"}, "data": [round(m["cost_output"], 4) for m in models[:15]]},
                        ],
                    }).classes("w-full h-96")
                with ui.tab_panel("table"):
                    ui.table(
                        columns=[
                            {"name": "model", "label": "Model", "field": "model", "align": "left", "sortable": True},
                            {"name": "invocations", "label": "Calls", "field": "invocations", "sortable": True},
                            {"name": "input_tokens", "label": "Input Tokens", "field": "input_tokens", "sortable": True},
                            {"name": "output_tokens", "label": "Output Tokens", "field": "output_tokens", "sortable": True},
                            {"name": "cost_cache_read", "label": "Cache Read ($)", "field": "cost_cache_read", "sortable": True},
                            {"name": "cost_cache_write", "label": "Cache Write ($)", "field": "cost_cache_write", "sortable": True},
                            {"name": "cost_input", "label": "Input ($)", "field": "cost_input", "sortable": True},
                            {"name": "cost_output", "label": "Output ($)", "field": "cost_output", "sortable": True},
                            {"name": "cost", "label": "Total ($)", "field": "cost", "sortable": True},
                        ],
                        rows=[{
                            **m,
                            "cost_cache_read": round(m["cost_cache_read"], 4),
                            "cost_cache_write": round(m["cost_cache_write"], 4),
                            "cost_input": round(m["cost_input"], 4),
                            "cost_output": round(m["cost_output"], 4),
                            "cost": round(m["cost_usd"], 4),
                        } for m in models],
                    ).classes("w-full")

    if callers:
        with ui.card().classes("w-full"):
            with ui.row().classes("w-full items-center px-4 pt-2"):
                ui.label("Token Usage & Cost by Caller").classes("text-lg font-semibold")
                ui.space()
                with ui.tabs().props("dense").classes("text-xs") as caller_tabs:
                    ui.tab("chart", label="Chart", icon="bar_chart")
                    ui.tab("pie", label="Pie", icon="pie_chart")
                    ui.tab("table", label="Table", icon="table_rows")
            ui.separator()
            with ui.tab_panels(caller_tabs, value="chart").classes("w-full max-h-[420px] overflow-auto p-0"):
                with ui.tab_panel("pie").classes("p-2"):
                    with ui.row().classes("w-full gap-0"):
                        pie_refs = []
                        for title, key, fmt, is_cost in [
                            ("Input Tokens", "input_tokens", "{b}\n{c}", False),
                            ("Output Tokens", "output_tokens", "{b}\n{c}", False),
                            ("Cost", "cost_usd", "{b}\n${c}", True),
                        ]:
                            pie_data = [{"name": c["caller"][:20], "value": round(c[key], 4) if is_cost else c[key]} for c in callers[:10]]
                            chart = ui.echart({
                                "tooltip": {"trigger": "item", "formatter": "{b}: {d}%"},
                                "title": {"text": title, "left": "center", "textStyle": {"fontSize": 13, "color": "#6B7280"}},
                                "series": [{"name": title, "type": "pie", "radius": ["30%", "60%"],
                                    "label": {"formatter": fmt, "fontSize": 10},
                                    "data": pie_data,
                                }],
                            }).classes("flex-1 h-80")
                            pie_refs.append((chart, key, is_cost))
                        refs["charts"]["caller_pie"] = pie_refs
                with ui.tab_panel("chart").classes("p-2"):
                    caller_names = [c["caller"][:25] for c in callers[:15]]
                    refs["charts"]["caller_bar"] = ui.echart({
                        "tooltip": {"trigger": "axis"},
                        "legend": {"top": 0},
                        "grid": {"top": 40, "bottom": 70, "left": 60, "right": 60},
                        "xAxis": {"type": "category", "data": caller_names, "axisLabel": {"rotate": 40, "interval": 0}},
                        "yAxis": [
                            {"type": "value", "name": "Tokens"},
                            {"type": "value", "name": "Cost ($)"},
                        ],
                        "series": [
                            {"name": "Input Tokens", "type": "bar", "data": [c["input_tokens"] for c in callers[:15]]},
                            {"name": "Output Tokens", "type": "bar", "data": [c["output_tokens"] for c in callers[:15]]},
                            {"name": "Cache Read $", "type": "bar", "stack": "cost", "yAxisIndex": 1, "itemStyle": {"color": "#06B6D4"}, "data": [round(c["cost_cache_read"], 4) for c in callers[:15]]},
                            {"name": "Cache Write $", "type": "bar", "stack": "cost", "yAxisIndex": 1, "itemStyle": {"color": "#8B5CF6"}, "data": [round(c["cost_cache_write"], 4) for c in callers[:15]]},
                            {"name": "Input $", "type": "bar", "stack": "cost", "yAxisIndex": 1, "itemStyle": {"color": "#3B82F6"}, "data": [round(c["cost_input"], 4) for c in callers[:15]]},
                            {"name": "Output $", "type": "bar", "stack": "cost", "yAxisIndex": 1, "itemStyle": {"color": "#F97316"}, "data": [round(c["cost_output"], 4) for c in callers[:15]]},
                        ],
                    }).classes("w-full h-96")
                with ui.tab_panel("table"):
                    ui.table(
                        columns=[
                            {"name": "caller", "label": "Caller", "field": "caller", "align": "left", "sortable": True},
                            {"name": "invocations", "label": "Calls", "field": "invocations", "sortable": True},
                            {"name": "input_tokens", "label": "Input Tokens", "field": "input_tokens", "sortable": True},
                            {"name": "output_tokens", "label": "Output Tokens", "field": "output_tokens", "sortable": True},
                            {"name": "cost_cache_read", "label": "Cache Read ($)", "field": "cost_cache_read", "sortable": True},
                            {"name": "cost_cache_write", "label": "Cache Write ($)", "field": "cost_cache_write", "sortable": True},
                            {"name": "cost_input", "label": "Input ($)", "field": "cost_input", "sortable": True},
                            {"name": "cost_output", "label": "Output ($)", "field": "cost_output", "sortable": True},
                            {"name": "cost", "label": "Total ($)", "field": "cost", "sortable": True},
                        ],
                        rows=[{
                            **c,
                            "cost_cache_read": round(c["cost_cache_read"], 4),
                            "cost_cache_write": round(c["cost_cache_write"], 4),
                            "cost_input": round(c["cost_input"], 4),
                            "cost_output": round(c["cost_output"], 4),
                            "cost": round(c["cost_usd"], 4),
                        } for c in callers],
                    ).classes("w-full")

    # ── Usage Trend ──
    if trend_total:
        model_options = {"TOTAL": "All Models"} | {f"MODEL#{m['model']}": m["model"] for m in models} if models else {"TOTAL": "All Models"}
        caller_options = {"TOTAL": "All Callers"} | {f"CALLER#{c['caller']}": c["caller"] for c in callers} if callers else {"TOTAL": "All Callers"}

        with ui.card().classes("w-full p-2"):
            with ui.row().classes("w-full items-center px-2 pt-2"):
                ui.label("Usage Trend").classes("text-lg font-semibold")
                ui.space()
                usage_model_select = ui.select(model_options, value="TOTAL").props("dense outlined").classes("w-48")
                usage_caller_select = ui.select(caller_options, value="TOTAL").props("dense outlined").classes("w-48")

            usage_chart = ui.echart({
                "tooltip": {"trigger": "axis"},
                "legend": {"top": 0},
                "grid": {"top": 40, "bottom": 30, "left": 60, "right": 60},
                "xAxis": {"type": "category", "data": [x["period"] for x in trend_total]},
                "yAxis": [
                    {"type": "value", "name": "Invocations"},
                    {"type": "value", "name": "Cost ($)"},
                ],
                "series": [
                    {"name": "Invocations", "type": "bar", "data": [x["invocations"] for x in trend_total]},
                    {"name": "Cost ($)", "type": "line", "itemStyle": {"color": "#F97316"}, "yAxisIndex": 1, "smooth": True, "data": [round(x["cost_usd"], 6) for x in trend_total]},
                ],
            }).classes("w-full h-80")
            refs["charts"]["usage_trend"] = usage_chart
            # Pair the two selects; _current_dim() resolves which one is active
            refs["dim_selectors"]["usage_trend"] = (usage_model_select, usage_caller_select)

            usage_syncing = {"flag": False}

            async def on_usage_model_change(_):
                if usage_syncing["flag"]:
                    return
                # Switching model → reset caller to TOTAL (DDB has no MODEL × CALLER cross aggregation)
                if (usage_model_select.value or "TOTAL") != "TOTAL" and usage_caller_select.value != "TOTAL":
                    usage_syncing["flag"] = True
                    usage_caller_select.value = "TOTAL"
                    usage_syncing["flag"] = False
                dim = _current_dim(usage_model_select, usage_caller_select)
                t = await asyncio.to_thread(_fetch_trend, account_region, start_dt, end_dt, dim, tz)
                _apply_usage_trend(usage_chart, t)

            async def on_usage_caller_change(_):
                if usage_syncing["flag"]:
                    return
                if (usage_caller_select.value or "TOTAL") != "TOTAL" and usage_model_select.value != "TOTAL":
                    usage_syncing["flag"] = True
                    usage_model_select.value = "TOTAL"
                    usage_syncing["flag"] = False
                dim = _current_dim(usage_model_select, usage_caller_select)
                t = await asyncio.to_thread(_fetch_trend, account_region, start_dt, end_dt, dim, tz)
                _apply_usage_trend(usage_chart, t)

            usage_model_select.on_value_change(on_usage_model_change)
            usage_caller_select.on_value_change(on_usage_caller_change)

    # ── Performance ──
    if models:
        model_names = [_short_model(m["model"]) for m in models[:15]]
        with ui.row().classes("w-full gap-4"):
            with ui.card().classes("flex-1 p-2"):
                ui.label("Latency by Model").classes("text-lg font-semibold px-2 pt-2")
                refs["charts"]["latency_by_model"] = ui.echart({
                    "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
                    "legend": {"top": 0},
                    "grid": {"top": 40, "bottom": 70, "left": 60, "right": 20},
                    "xAxis": {"type": "category", "data": model_names, "axisLabel": {"rotate": 40, "interval": 0}},
                    "yAxis": {"type": "value", "name": "ms"},
                    "series": [
                        {"name": "Min", "type": "bar", "data": [m.get("min_latency_ms", 0) for m in models[:15]], "itemStyle": {"color": "#10B981"}},
                        {"name": "Avg", "type": "bar", "data": [m.get("avg_latency_ms", 0) for m in models[:15]], "itemStyle": {"color": "#E879F9", "opacity": 0.6}},
                        {"name": "Max", "type": "bar", "data": [m.get("max_latency_ms", 0) for m in models[:15]], "itemStyle": {"color": "#8B5CF6"}},
                    ],
                }).classes("w-full h-80")

            with ui.card().classes("flex-1 p-2"):
                ui.label("TPOT by Model (approx)").classes("text-lg font-semibold px-2 pt-2")
                refs["charts"]["tpot_by_model"] = ui.echart({
                    "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
                    "legend": {"top": 0},
                    "grid": {"top": 40, "bottom": 70, "left": 60, "right": 20},
                    "xAxis": {"type": "category", "data": model_names, "axisLabel": {"rotate": 40, "interval": 0}},
                    "yAxis": {"type": "value", "name": "ms/token"},
                    "series": [
                        {"name": "Min", "type": "bar", "data": [m.get("tpot_min", 0) for m in models[:15]], "itemStyle": {"color": "#10B981"}},
                        {"name": "Avg", "type": "bar", "data": [m.get("tpot_avg", 0) for m in models[:15]], "itemStyle": {"color": "#6366F1"}},
                        {"name": "Max", "type": "bar", "data": [m.get("tpot_max", 0) for m in models[:15]], "itemStyle": {"color": "#A78BFA", "opacity": 0.6}},
                    ],
                }).classes("w-full h-80")

        # Row 2: Latency Trend + TTFT Trend
        if trend_total:
            model_options = {"TOTAL": "All Models"} | {f"MODEL#{m['model']}": m["model"] for m in models}
            with ui.row().classes("w-full gap-4"):
                with ui.card().classes("flex-1 p-2"):
                    with ui.row().classes("w-full items-center px-2 pt-2"):
                        ui.label("Latency Trend").classes("text-lg font-semibold")
                        ui.space()
                        lat_model_select = ui.select(model_options, value="TOTAL").props("dense outlined").classes("w-48")

                    lat_chart = ui.echart({
                        "tooltip": {"trigger": "axis"},
                        "legend": {"top": 0},
                        "grid": {"top": 40, "bottom": 30, "left": 60, "right": 20},
                        "xAxis": {"type": "category", "data": [x["period"] for x in trend_total]},
                        "yAxis": {"type": "value", "name": "ms"},
                        "series": [
                            {"name": "Min", "type": "line", "data": [x["min_latency_ms"] for x in trend_total], "itemStyle": {"color": "#10B981"}},
                            {"name": "Avg", "type": "line", "data": [x["avg_latency_ms"] for x in trend_total], "itemStyle": {"color": "#E879F9"}, "lineStyle": {"type": "dashed"}},
                            {"name": "Max", "type": "line", "data": [x["max_latency_ms"] for x in trend_total], "itemStyle": {"color": "#8B5CF6"}},
                        ],
                    }).classes("w-full h-72")
                    refs["charts"]["latency_trend"] = lat_chart
                    refs["model_selects"]["latency_trend"] = lat_model_select

                    async def on_lat_select_change(_):
                        t = await asyncio.to_thread(_fetch_trend, account_region, start_dt, end_dt, lat_model_select.value or "TOTAL", tz)
                        _apply_latency_trend(lat_chart, t)

                    lat_model_select.on_value_change(on_lat_select_change)

                with ui.card().classes("flex-1 p-2"):
                    ttft_model_options = {m["model"]: _short_model(m["model"]) for m in models}
                    first_model = models[0]["model"] if models else ""

                    with ui.row().classes("w-full items-center px-2 pt-2"):
                        ui.label("TTFT Trend (CloudWatch)").classes("text-lg font-semibold")
                        ui.space()
                        ttft_model_select = ui.select(ttft_model_options, value=first_model).props("dense outlined").classes("w-48")

                    ttft_chart = ui.echart({
                        "tooltip": {"trigger": "axis"},
                        "legend": {"top": 0},
                        "grid": {"top": 40, "bottom": 30, "left": 60, "right": 20},
                        "xAxis": {"type": "category", "data": []},
                        "yAxis": {"type": "value", "name": "ms"},
                        "series": [
                            {"name": "Avg TTFT", "type": "line", "data": [], "itemStyle": {"color": "#6366F1"}, "smooth": True},
                            {"name": "P99 TTFT", "type": "line", "data": [], "itemStyle": {"color": "#A78BFA"}, "lineStyle": {"type": "dashed"}},
                        ],
                    }).classes("w-full h-72")
                    refs["charts"]["ttft_trend"] = ttft_chart
                    refs["model_selects"]["ttft_trend"] = ttft_model_select

                    async def on_ttft_select_change(_):
                        mid = ttft_model_select.value or ""
                        if not mid:
                            return
                        t = await asyncio.to_thread(_fetch_ttft, mid, start_dt, end_dt, tz)
                        _apply_ttft_trend(ttft_chart, t)

                    ttft_model_select.on_value_change(on_ttft_select_change)

                    if first_model:
                        # Initial TTFT load is async so the page render doesn't block on CloudWatch
                        async def _initial_ttft():
                            t = await asyncio.to_thread(_fetch_ttft, first_model, start_dt, end_dt, tz)
                            _apply_ttft_trend(ttft_chart, t)
                        ui.timer(0.01, _initial_ttft, once=True)


def summary_card(refs: dict, title: str, value: str, icon: str, color: str):
    with ui.card().classes("min-w-[138px] flex-1 p-4 h-[120px]"):
        with ui.row().classes("items-center gap-2"):
            ui.icon(icon).classes(f"text-xl text-{color}-500")
            ui.label(title).classes("text-sm text-gray-500")
        refs["summary_labels"][title] = ui.label(value).classes("text-2xl font-bold mt-2")
