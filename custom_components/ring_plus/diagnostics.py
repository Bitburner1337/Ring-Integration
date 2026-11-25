"""Diagnostics support for Ring."""

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .coordinator import RingConfigEntry

TO_REDACT = {
    "id",
    "device_id",
    "description",
    "first_name",
    "last_name",
    "email",
    "location_id",
    "ring_net_id",
    "wifi_name",
    "latitude",
    "longitude",
    "address",
    "ring_id",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: RingConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    ring_data = entry.runtime_data
    devices_data = ring_data.api.devices_data
    devices_raw = [
        devices_data[device_type][device_id]
        for device_type in devices_data
        for device_id in devices_data[device_type]
    ]
    return {
        **async_redact_data({"device_data": devices_raw}, TO_REDACT),
        "ring_plus": _ring_plus_diagnostics(entry),
    }


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _ring_plus_diagnostics(entry: RingConfigEntry) -> dict[str, Any]:
    """State of the ring_plus extras (no tokens, no personal data)."""
    data = entry.runtime_data
    listen = data.listen_coordinator
    result: dict[str, Any] = {
        "options": dict(entry.options),
        "event_connection": {
            "state": listen.connection_state,
            "reconnect_count": listen.reconnect_count,
            "last_reconnect": _iso(listen.last_reconnect),
            "last_reconnect_reason": listen.last_reconnect_reason,
            "last_event": _iso(listen.last_event_time),
            "last_event_kind": listen.last_event_kind,
        },
    }
    if (clips := data.clips) is not None:
        result["clips"] = {
            "retention_days": clips.retention.days,
            "downloaded_ids": len(clips._done),  # noqa: SLF001
            "failed_ids": len(clips._failures),  # noqa: SLF001
            "last_clip": _iso(clips.last_clip_time),
            "last_error": clips.last_error,
        }
    if (archive := data.archive) is not None:
        result["archive"] = {
            "enabled": archive.enabled,
            "status": archive.status,
            "pending": archive.pending,
            "uploaded_local_clips": archive.uploaded_count,
            "last_upload": _iso(archive.last_upload),
            "last_error": archive.last_error,
        }
    if (history := data.history) is not None:
        events = history.events
        result["history"] = {
            "keep_days": history.keep.days,
            "events": len(events),
            "oldest": _iso(events[-1].start) if events else None,
            "newest": _iso(events[0].start) if events else None,
        }
    return result
