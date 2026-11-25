"""Diagnostic sensors for the realtime event connection (ring_plus).

They sit on their own "Ring Plus" service device:
- Event connection: connected / connecting / reconnecting / disconnected
- Last event: time of the last realtime event (any camera), kept across restarts
"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo

from .const import ATTRIBUTION, CONN_STATES, DOMAIN
from .coordinator import RingConfigEntry, RingListenCoordinator

_VERSION = json.loads(
    (Path(__file__).parent / "manifest.json").read_text(encoding="utf-8")
)["version"]


def hub_device_info(entry: RingConfigEntry) -> DeviceInfo:
    """The 'Ring Plus' service device that holds integration-wide entities."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="Ring Plus",
        manufacturer="Ring Plus",
        model="Ring Plus",
        sw_version=_VERSION,
        entry_type=DeviceEntryType.SERVICE,
    )


class _HealthSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_attribution = ATTRIBUTION
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, entry: RingConfigEntry, coordinator: RingListenCoordinator, key: str
    ) -> None:
        self.coordinator = coordinator
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.entry_id}-{key}"
        self._attr_device_info = hub_device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_health_listener(self._on_health_update)
        )

    @callback
    def _on_health_update(self) -> None:
        self.async_write_ha_state()


class RingConnectionSensor(_HealthSensor):
    """State of the realtime event connection."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = CONN_STATES

    def __init__(
        self, entry: RingConfigEntry, coordinator: RingListenCoordinator
    ) -> None:
        super().__init__(entry, coordinator, "event_connection")

    @property
    def native_value(self) -> str:
        return self.coordinator.connection_state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        c = self.coordinator
        return {
            "reconnect_count": c.reconnect_count,
            "last_reconnect": c.last_reconnect.isoformat() if c.last_reconnect else None,
            "last_reconnect_reason": c.last_reconnect_reason,
        }


class RingLastEventSensor(_HealthSensor, RestoreSensor):
    """Time of the last realtime event."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(
        self, entry: RingConfigEntry, coordinator: RingListenCoordinator
    ) -> None:
        super().__init__(entry, coordinator, "last_event")
        self._restored: datetime | None = None
        self._restored_attrs: dict[str, Any] = {}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_sensor_data()) and isinstance(
            last.native_value, datetime
        ):
            self._restored = last.native_value
        if (state := await self.async_get_last_state()) is not None:
            self._restored_attrs = {
                k: state.attributes.get(k) for k in ("device", "kind")
            }

    @property
    def native_value(self) -> datetime | None:
        return self.coordinator.last_event_time or self._restored

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self.coordinator.last_event_time is None:
            return dict(self._restored_attrs)
        return {
            "device": self.coordinator.last_event_device,
            "kind": self.coordinator.last_event_kind,
        }


def health_sensors(
    entry: RingConfigEntry, coordinator: RingListenCoordinator
) -> list[SensorEntity]:
    """All diagnostic health sensors."""
    return [
        RingConnectionSensor(entry, coordinator),
        RingLastEventSensor(entry, coordinator),
    ]
