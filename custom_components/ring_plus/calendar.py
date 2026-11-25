"""Calendar 'Ring Plus Verlauf' (phase 5).

Every Ring event (motion, ding) is a short calendar entry, e.g.
"Hof: Bewegung" at 11:35:54. The HA calendar view then shows the history
day by day, like the Ring app. Automations can use calendar triggers.
"""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import ATTRIBUTION
from .coordinator import RingConfigEntry
from .health import hub_device_info
from .history import EventHistory, RingHistoryEvent

PARALLEL_UPDATES = 0

_KIND_TEXT = {
    "de": {"motion": "Bewegung", "ding": "Klingeln", "clip": "Clip in Medien › Ring Plus"},
    "en": {"motion": "Motion", "ding": "Ding", "clip": "Clip in Media › Ring Plus"},
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RingConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the history calendar."""
    history = entry.runtime_data.history
    if history is not None:
        async_add_entities([RingHistoryCalendar(entry, history)])


class RingHistoryCalendar(CalendarEntity):
    """All Ring events as calendar entries."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_attribution = ATTRIBUTION
    _attr_translation_key = "history"

    def __init__(self, entry: RingConfigEntry, history: EventHistory) -> None:
        self._history = history
        self._attr_unique_id = f"{entry.entry_id}-history"
        self._attr_device_info = hub_device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._history.async_add_listener(self._on_update))

    @callback
    def _on_update(self) -> None:
        self.async_write_ha_state()

    def _text(self) -> dict[str, str]:
        lang = (self.hass.config.language or "en").split("-")[0]
        return _KIND_TEXT.get(lang, _KIND_TEXT["en"])

    def _to_calendar(self, event: RingHistoryEvent) -> CalendarEvent:
        text = self._text()
        return CalendarEvent(
            start=event.start,
            end=event.end,
            summary=f"{event.camera}: {text.get(event.kind, event.kind)}",
            description=text["clip"] if self._history.media_id(event) else None,
            location=event.camera,
            uid=event.id,
        )

    @property
    def event(self) -> CalendarEvent | None:
        """The newest event (state is 'on' while it lasts)."""
        events = self._history.events
        return self._to_calendar(events[0]) if events else None

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        return [
            self._to_calendar(e)
            for e in self._history.events_between(start_date, end_date)
        ]
