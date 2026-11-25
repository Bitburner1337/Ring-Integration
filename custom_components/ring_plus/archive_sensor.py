"""Sensor 'Cloud-Archiv' (phase 6).

State: number of clips still waiting for the upload to Internxt.
Attributes: status, last upload (time + path), last error, next try.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.const import EntityCategory
from homeassistant.core import callback

from .archive import ClipArchive
from .const import ATTRIBUTION
from .coordinator import RingConfigEntry
from .health import hub_device_info


class RingArchiveSensor(SensorEntity):
    """Clips waiting for Internxt."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_attribution = ATTRIBUTION
    _attr_translation_key = "archive_queue"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, entry: RingConfigEntry, archive: ClipArchive) -> None:
        self._archive = archive
        self._attr_unique_id = f"{entry.entry_id}-archive_queue"
        self._attr_device_info = hub_device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._archive.async_add_listener(self._on_update))

    @callback
    def _on_update(self) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> int:
        return self._archive.pending

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        a = self._archive
        return {
            "status": a.status,
            "uploaded_local_clips": a.uploaded_count,
            "last_upload": a.last_upload.isoformat() if a.last_upload else None,
            "last_upload_file": a.last_upload_file,
            "last_error": a.last_error,
            "next_try": a.next_try.isoformat() if a.next_try else None,
        }


def archive_sensors(entry: RingConfigEntry) -> list[SensorEntity]:
    archive = entry.runtime_data.archive
    return [] if archive is None else [RingArchiveSensor(entry, archive)]
