"""Timezone-aware timestamps for report cards.

DLMM_TZ (IANA name, e.g. "Asia/Jakarta", set in the profile .env) selects the
operator's display zone for every card this skill emits; unset falls back to
the system local zone. %Z in the format renders the zone label (WIB, UTC, ...),
so cards self-describe whatever zone the operator picked.
"""
import os
from datetime import datetime, timezone


def local_time_str(fmt="%H:%M %Z"):
    tz_name = os.environ.get("DLMM_TZ", "").strip()
    try:
        if tz_name:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo(tz_name)).strftime(fmt)
        return datetime.now().astimezone().strftime(fmt)
    except Exception:
        # Unknown zone name or missing tzdata — never break a report card.
        return datetime.now(timezone.utc).strftime(fmt.replace("%Z", "UTC"))
