"""Sensor 'Letzte Events' (phase 5).

State: number of Ring events today (motion + ding, all cameras).
Attribute 'events': the last 20 events, newest first, for dashboards:
  time, camera, kind, clip (media-source id of the saved clip or null)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.core import callback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.util import dt as dt_util

from .const import ATTRIBUTION, HISTORY_SENSOR_EVENTS
from .coordinator import RingConfigEntry
from .health import hub_device_info
from .history import EventHistory


class RingRecentEventsSensor(SensorEntity):
    """Events today + the last 20 events as attribute."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_attribution = ATTRIBUTION
    _attr_translation_key = "recent_events"
    _attr_state_class = SensorStateClass.MEASUREMENT
    # The list changes often and is long: keep it out of the recorder DB.
    _unrecorded_attributes = frozenset({"events"})

    def __init__(self, entry: RingConfigEntry, history: EventHistory) -> None:
        self._history = history
        self._attr_unique_id = f"{entry.entry_id}-recent_events"
        self._attr_device_info = hub_device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._history.async_add_listener(self._on_update))
        # new day -> counter back to 0
        self.async_on_remove(
            async_track_time_change(self.hass, self._on_midnight, hour=0, minute=0, second=5)
        )

    @callback
    def _on_update(self) -> None:
        self.async_write_ha_state()

    @callback
    def _on_midnight(self, _now: datetime) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> int:
        today = dt_util.now().date()
        return sum(
            1 for e in self._history.events if dt_util.as_local(e.start).date() == today
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "events": [
                {
                    "time": dt_util.as_local(e.start).isoformat(timespec="seconds"),
                    "camera": e.camera,
                    "kind": e.kind,
                    "clip": e.clip_media_id,
                }
                for e in self._history.events[:HISTORY_SENSOR_EVENTS]
            ]
        }


def history_sensors(entry: RingConfigEntry) -> list[SensorEntity]:
    history = entry.runtime_data.history
    return [] if history is None else [RingRecentEventsSensor(entry, history)]
