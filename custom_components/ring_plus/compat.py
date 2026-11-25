"""Device compatibility fixes for ring_plus.

The ring-doorbell library does not know every Ring device type ("kind").
Unknown kinds get no capabilities, so Home Assistant creates no motion
event for them. Here we add the missing kinds without changing the library
itself, so the official Ring integration (same Python process) is untouched.
"""

from __future__ import annotations

from typing import Any

from ring_doorbell import RingCapability, RingError


# kind -> (model name, extra capabilities)
EXTRA_KINDS: dict[str, tuple[str, frozenset[RingCapability]]] = {
    # Seen on newer Stick Up Cams (e.g. "Hof" and "Garten").
    "cocoa_camera_v2": (
        "Stick Up Cam (cocoa_camera_v2)",
        frozenset(
            {
                RingCapability.HISTORY,
                RingCapability.MOTION_DETECTION,
                RingCapability.VIDEO,
            }
        ),
    ),
}


def _as_capability(capability: RingCapability | str) -> RingCapability | None:
    if isinstance(capability, RingCapability):
        return capability
    try:
        return RingCapability.from_name(capability)
    except RingError:
        return None


def has_capability(device: Any, capability: RingCapability | str) -> bool:
    """Like device.has_capability(), but also knows the extra kinds."""
    extra = EXTRA_KINDS.get(getattr(device, "kind", None) or "")
    if extra is not None:
        cap = _as_capability(capability)
        if cap is not None and cap in extra[1]:
            return True
        if cap is RingCapability.BATTERY:
            # Only claim a battery if Ring actually reports a level.
            return getattr(device, "battery_life", None) is not None
    return bool(device.has_capability(capability))


def device_model(device: Any) -> str:
    """Model name, without the library's 'Unknown kind' error for extra kinds."""
    extra = EXTRA_KINDS.get(getattr(device, "kind", None) or "")
    if extra is not None:
        return extra[0]
    return str(device.model)
