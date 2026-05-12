# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Pricing Settings page."""

from datetime import datetime, timezone

from nicegui import ui
from webui import data


@ui.page("/pricing")
def pricing_page():
    ui.dark_mode(False)

    # ── Header ──
    with ui.header().classes("bg-white text-gray-800 shadow-sm items-center px-6"):
        ui.button(icon="arrow_back", on_click=lambda: ui.navigate.to("/")).props("flat round")
        ui.label("Pricing Settings").classes("text-xl font-bold ml-2")
        ui.space()
        sync_info = data.get_pricing_sync_info()
        if sync_info:
            synced_at = sync_info.get("synced_at", "")
            updated = int(sync_info.get("models_updated", 0))
            skipped = int(sync_info.get("models_skipped", 0))
            ui.label(f"Last sync: {synced_at}  |  {updated} updated, {skipped} unchanged").classes("text-sm text-gray-500")

    models = data.get_all_pricing()

    # ── Edit/Add dialog ──
    with ui.dialog() as edit_dialog, ui.card().classes("min-w-[400px]"):
        dlg_title = ui.label("").classes("text-lg font-semibold")
        dlg_model = ui.input(label="Model ID").classes("w-full")
        dlg_input = ui.number(label="Input $/1K tokens", format="%.6f").classes("w-full")
        dlg_output = ui.number(label="Output $/1K tokens", format="%.6f").classes("w-full mt-2")
        dlg_date = ui.input(label="Effective Date (ISO 8601)").classes("w-full mt-2")
        with ui.row().classes("w-full justify-end mt-4 gap-2"):
            ui.button("Cancel", on_click=edit_dialog.close).props("flat")
            ui.button("Save", on_click=lambda: save_edit()).props("color=primary")

    edit_ctx = {}

    def open_edit(model_id="", input_per_1k=0, output_per_1k=0, effective_date="", is_new=True):
        edit_ctx.update(is_new=is_new, original_date=effective_date, model_id=model_id)
        dlg_title.text = "Add Pricing" if is_new else "Edit Pricing"
        dlg_model.value = model_id
        dlg_model.props("readonly" if (model_id and not is_new) else "")
        if not model_id or is_new:
            dlg_model.props(remove="readonly")
        dlg_input.value = input_per_1k
        dlg_output.value = output_per_1k
        dlg_date.value = effective_date or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        edit_dialog.open()

    def save_edit():
        model_id = dlg_model.value
        if not model_id:
            ui.notify("Model ID is required", type="warning")
            return
        data.save_pricing(model_id, dlg_input.value or 0, dlg_output.value or 0, dlg_date.value)
        if not edit_ctx["is_new"] and edit_ctx["original_date"] != dlg_date.value:
            data.delete_pricing(model_id, edit_ctx["original_date"])
        ui.notify(f"Saved pricing for {model_id}", type="positive")
        edit_dialog.close()
        # Refresh history dialog if open
        if history_dialog.value and edit_ctx["model_id"]:
            show_history(edit_ctx["model_id"])

    # ── Delete confirmation ──
    with ui.dialog() as del_dialog, ui.card():
        ui.label("Delete this pricing record?").classes("text-lg font-semibold")
        del_info = ui.label("").classes("text-sm text-gray-500")
        with ui.row().classes("w-full justify-end mt-4 gap-2"):
            ui.button("Cancel", on_click=del_dialog.close).props("flat")
            ui.button("Delete", on_click=lambda: confirm_delete()).props("color=negative")

    del_ctx = {}

    def open_delete(model_id, effective_date):
        del_ctx.update(model_id=model_id, effective_date=effective_date)
        del_info.text = f"{model_id}  |  {effective_date}"
        del_dialog.open()

    def confirm_delete():
        data.delete_pricing(del_ctx["model_id"], del_ctx["effective_date"])
        ui.notify("Deleted", type="info")
        del_dialog.close()
        # Refresh history dialog
        show_history(del_ctx["model_id"])

    # ── History dialog (lazy load on click) ──
    with ui.dialog() as history_dialog, ui.card().classes("min-w-[760px]"):
        history_header = ui.row().classes("w-full items-center")
        history_container = ui.column().classes("w-full")

    def show_history(model_id):
        history_header.clear()
        history_container.clear()
        records = data.get_pricing_history(model_id)
        with history_header:
            ui.label(f"Price History — {model_id}").classes("text-lg font-semibold")
            ui.space()
            ui.button("Add", icon="add", on_click=lambda: open_edit(model_id=model_id)).props("flat dense color=primary")
            ui.button(icon="close", on_click=history_dialog.close).props("flat round dense")
        with history_container:
            for r in records:
                with ui.row().classes("w-full items-center py-1 px-2 hover:bg-gray-50 rounded gap-2 flex-nowrap"):
                    ui.label(r["effective_date"]).classes("text-sm min-w-[180px]")
                    ui.label(f'In: {r["input_per_1k"]:.6f}').classes("text-sm min-w-[120px]")
                    ui.label(f'Out: {r["output_per_1k"]:.6f}').classes("text-sm min-w-[120px]")
                    ui.badge(r["source"], color="blue-3" if r["source"] == "litellm" else "orange-3")
                    ui.space()
                    with ui.row().classes("gap-0 flex-nowrap"):
                        ui.button(icon="edit", on_click=lambda _r=r: open_edit(
                            model_id=_r["model_id"], input_per_1k=_r["input_per_1k"],
                            output_per_1k=_r["output_per_1k"], effective_date=_r["effective_date"], is_new=False,
                        )).props("flat dense round size=sm color=primary")
                        ui.button(icon="delete", on_click=lambda _r=r: open_delete(
                            _r["model_id"], _r["effective_date"],
                        )).props("flat dense round size=sm color=negative")
            if not records:
                ui.label("No pricing records").classes("text-gray-400")
        history_dialog.open()

    # ── Main table ──
    with ui.column().classes("max-w-6xl mx-auto p-6 w-full"):
        with ui.row().classes("w-full items-center justify-between mb-2"):
            with ui.row().classes("items-center gap-2"):
                ui.label(f"{len(models)} models").classes("text-sm text-gray-500")
                ui.button("Add Model", icon="add", on_click=lambda: open_edit()).props("flat dense color=primary")
            search = ui.input(placeholder="Filter models...").props("dense outlined clearable").classes("w-64")

        columns = [
            {"name": "model_id", "label": "Model ID", "field": "model_id", "align": "left", "sortable": True},
            {"name": "input_per_1k", "label": "Input $/1K tokens", "field": "input_per_1k", "sortable": True},
            {"name": "output_per_1k", "label": "Output $/1K tokens", "field": "output_per_1k", "sortable": True},
            {"name": "effective_date", "label": "Effective Date", "field": "effective_date", "sortable": True},
            {"name": "source", "label": "Source", "field": "source", "sortable": True},
        ]
        rows = [{
            "model_id": m["model_id"],
            "input_per_1k": f'{m["input_per_1k"]:.6f}',
            "output_per_1k": f'{m["output_per_1k"]:.6f}',
            "effective_date": m["effective_date"],
            "source": m["source"],
        } for m in models]

        table = ui.table(columns=columns, rows=rows, row_key="model_id", pagination=25).classes(
            "w-full"
        ).props('dense flat')
        table.on("rowClick", lambda e: show_history(e.args[1]["model_id"]))
        search.bind_value_to(table, "filter")
