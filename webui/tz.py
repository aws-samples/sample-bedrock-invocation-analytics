# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Display-layer timezone for the WebUI.

The dashboard speaks one timezone end-to-end (preset resolution, custom date input,
chart labels, "Data up to", range hint). Storage is always UTC; this module is purely
for translating UTC ↔ user-perceived wall clock.

The WEBUI_TIMEZONE env var (set by start-webui.sh) names the auto-detected "Local"
option. Users can switch between Local and UTC at runtime via the sidebar — the
selection lives in app.storage.user, set/read via set_active() / get_active().
"""

import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = ZoneInfo("UTC")


def _safe_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return UTC


# IANA name auto-detected by start-webui.sh. Falls back to UTC if the env var is missing
# or names a zone the host's tzdata doesn't know.
DETECTED_NAME: str = os.environ.get("WEBUI_TIMEZONE") or "UTC"
DETECTED_ZONE: ZoneInfo = _safe_zone(DETECTED_NAME)


def options() -> dict[str, str]:
    """Sidebar dropdown choices: {value: label}. Local is hidden if it's already UTC."""
    if DETECTED_NAME == "UTC":
        return {"UTC": "UTC"}
    return {"local": f"{DETECTED_NAME} (Local)", "UTC": "UTC"}


def resolve(value: str) -> ZoneInfo:
    """Map a dropdown value to a ZoneInfo."""
    return DETECTED_ZONE if value == "local" else UTC


def default_value() -> str:
    """Initial dropdown value: prefer Local when distinct from UTC."""
    return "local" if DETECTED_NAME != "UTC" else "UTC"
