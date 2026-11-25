"""Event history for ring_plus (phase 5).

Keeps a list of all Ring events (motion, ding) of the last 30 days (option), like
the history in the Ring app. Three sources fill it:

1. Realtime events (push) -> appear within seconds.
2. The history the devices coordinator already reads every minute
   (last 10 entries per camera) -> fills gaps, e.g. after a lost push.
3. Once at startup: the last BACKFILL_LIMIT entries per camera
   -> events from while Home Assistant was off.

Every event remembers its saved clip (from clips.py) if there is one.
The list is saved to .storage, so it survives restarts.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from pathlib import Path
from typing import Any

from ring_doorbell import RingError, RingEvent

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CLIP_KINDS,
    CLIP_RETENTION,
    DOMAIN,
    HISTORY_BACKFILL_LIMIT,
    HISTORY_KEEP,
    HISTORY_MAX_EVENTS,
)

_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 1
SAVE_DELAY = 30  # seconds; collects several changes into one disk write
DEFAULT_DURATION = 30  # seconds, when Ring does not tell us
MATCH_WINDOW = 90  # seconds; push event and history entry are the same event


@dataclass
class RingHistoryEvent:
    """One event in the history."""

    id: str
    device_id: int
    camera: str
    kind: str  # "motion" | "ding"
    start: datetime
    duration: float = DEFAULT_DURATION
    clip: str | None = None  # path of the saved MP4
    confirmed: bool = False  # True once seen in Ring's own history

    @property
    def end(self) -> datetime:
        return self.start + timedelta(seconds=max(self.duration, 10))

    def clip_media_id(self, retention: timedelta = CLIP_RETENTION) -> str | None:
        """media-source:// id for the media browser / players, if the clip is still local."""
        if not self.clip or dt_util.utcnow() - self.start > retention:
            return None
        path = Path(self.clip)
        return f"media-source://{DOMAIN}/{path.parent.name}/{path.name}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "device_id": self.device_id,
            "camera": self.camera,
            "kind": self.kind,
            "start": self.start.isoformat(),
            "duration": self.duration,
            "clip": self.clip,
            "confirmed": self.confirmed,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RingHistoryEvent | None:
        try:
            start = dt_util.parse_datetime(data["start"])
            if start is None:
                return None
            return cls(
                id=str(data["id"]),
                device_id=int(data["device_id"]),
                camera=str(data["camera"]),
                kind=str(data["kind"]),
                start=start,
                duration=float(data.get("duration") or DEFAULT_DURATION),
                clip=data.get("clip"),
                confirmed=bool(data.get("confirmed")),
            )
        except (KeyError, TypeError, ValueError):
            return None


def _parse_created(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt_util.UTC)
    if isinstance(value, str):
        return dt_util.parse_datetime(value)
    return None


def _duration(entry: dict[str, Any]) -> float:
    try:
        value = float(entry.get("duration") or 0)
    except (TypeError, ValueError):
        value = 0
    return value if 0 < value < 3600 else DEFAULT_DURATION


class EventHistory:
    """All Ring events of the last HISTORY_KEEP days, newest first."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        devices_coordinator: Any,
        listen_coordinator: Any,
        clips: Any,
        keep: timedelta = HISTORY_KEEP,
        clip_retention: timedelta = CLIP_RETENTION,
    ) -> None:
        self.hass = hass
        self.keep = keep
        self.clip_retention = clip_retention
        self._devices = devices_coordinator
        self._listen = listen_coordinator
        self._clips = clips
        self._store: Store[dict[str, Any]] = Store(
            hass, STORE_VERSION, f"{DOMAIN}.{entry_id}.history"
        )
        self._events: dict[str, RingHistoryEvent] = {}
        self._listeners: list[CALLBACK_TYPE] = []
        self._unsubs: list[Callable[[], None]] = []
        self._backfill_task: asyncio.Task | None = None

    # ---- lifecycle --------------------------------------------------------

    async def async_setup(self) -> None:
        data = await self._store.async_load() or {}
        for raw in data.get("events", []):
            if (event := RingHistoryEvent.from_dict(raw)) is not None:
                self._events[event.id] = event
        self._prune()
        if self._clips is not None:
            for event in self._events.values():
                if event.clip is None:
                    event.clip = self._clips.clip_path(event.id)
        self._unsubs.append(self._devices.async_add_listener(self._on_devices_update))
        self._unsubs.append(self._listen.async_add_event_callback(self._on_ring_event))
        if self._clips is not None:
            self._unsubs.append(self._clips.async_add_saved_listener(self._on_clip_saved))
        self._on_devices_update()
        self._backfill_task = self.hass.async_create_background_task(
            self._async_backfill(), "ring_plus history backfill"
        )

    async def async_unload(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._backfill_task and not self._backfill_task.done():
            self._backfill_task.cancel()
        await self._store.async_save(self._data_to_save())

    # ---- public API (calendar + sensor) -------------------------------------

    @property
    def events(self) -> list[RingHistoryEvent]:
        """Newest first."""
        return sorted(self._events.values(), key=lambda e: e.start, reverse=True)

    def media_id(self, event: RingHistoryEvent) -> str | None:
        """media-source id of the event's local clip (None when gone)."""
        return event.clip_media_id(self.clip_retention)

    def events_between(self, start: datetime, end: datetime) -> list[RingHistoryEvent]:
        return sorted(
            (e for e in self._events.values() if e.end > start and e.start < end),
            key=lambda e: e.start,
        )

    @callback
    def async_add_listener(self, update_callback: CALLBACK_TYPE) -> CALLBACK_TYPE:
        self._listeners.append(update_callback)

        @callback
        def remove() -> None:
            if update_callback in self._listeners:
                self._listeners.remove(update_callback)

        return remove

    # ---- sources ----------------------------------------------------------

    @callback
    def _on_ring_event(self, event: RingEvent) -> None:
        """Realtime push event."""
        if event.kind not in CLIP_KINDS:
            return
        ding_id = str(event.id)
        if ding_id in self._events:
            return
        start = dt_util.utc_from_timestamp(event.now) if event.now else dt_util.utcnow()
        camera = event.device_name or self._camera_name(event.doorbot_id)
        self._add(
            RingHistoryEvent(
                id=ding_id,
                device_id=int(event.doorbot_id),
                camera=camera,
                kind=event.kind,
                start=start,
            )
        )
        self._changed()

    @callback
    def _on_devices_update(self) -> None:
        """The devices coordinator read the last 10 entries per camera."""
        data = self._devices.data
        if data is None:
            return
        changed = False
        for device in data.video_devices:
            for entry in getattr(device, "last_history", None) or []:
                changed |= self._merge_history_entry(device, entry)
        if changed:
            self._changed()

    @callback
    def _on_clip_saved(self, ding_id: str, path: str) -> None:
        if (event := self._events.get(ding_id)) is not None and event.clip != path:
            event.clip = path
            self._changed()

    async def _async_backfill(self) -> None:
        """Once per start: read older entries so nothing is missing."""
        data = self._devices.data
        if data is None:
            return
        total = 0
        for device in list(data.video_devices):
            try:
                # Separate call on purpose: async_history() also overwrites
                # device.last_history, which the minute updates use anyway.
                entries = await device.async_history(limit=HISTORY_BACKFILL_LIMIT)
            except (RingError, TimeoutError, OSError) as err:
                _LOGGER.info("History backfill for %s failed: %s", device.name, err)
                continue
            for entry in entries or []:
                if self._merge_history_entry(device, entry):
                    total += 1
            if self._clips is not None and entries:
                # also fetch clips the minute updates (last 10 only) missed
                self._clips.add_history(device, entries)
            await asyncio.sleep(1)  # be gentle with the Ring API
        _LOGGER.debug("History backfill added/updated %s events", total)
        if total:
            self._changed()

    # ---- internals --------------------------------------------------------

    def _camera_name(self, device_id: int) -> str:
        data = self._devices.data
        if data is not None:
            for device in data.video_devices:
                if device.id == device_id:
                    return str(device.name)
        return str(device_id)

    def _merge_history_entry(self, device: Any, entry: dict[str, Any]) -> bool:
        """Add or confirm one entry of Ring's history. True if something changed."""
        try:
            ding_id = str(entry["id"])
            kind = entry.get("kind")
        except (KeyError, TypeError, AttributeError):
            return False
        if kind not in CLIP_KINDS:
            return False
        start = _parse_created(entry.get("created_at"))
        if start is None or dt_util.utcnow() - start > self.keep:
            return False
        duration = _duration(entry)

        if (known := self._events.get(ding_id)) is not None:
            if known.confirmed:
                return False
            known.start, known.duration, known.confirmed = start, duration, True
            return True

        # Same event already known from push, but with another id?
        for other in self._events.values():
            if (
                not other.confirmed
                and other.device_id == device.id
                and other.kind == kind
                and abs((other.start - start).total_seconds()) <= MATCH_WINDOW
            ):
                del self._events[other.id]
                other.id, other.start, other.duration = ding_id, start, duration
                other.confirmed = True
                self._events[ding_id] = other
                if other.clip is None and self._clips is not None:
                    other.clip = self._clips.clip_path(ding_id)
                return True

        self._add(
            RingHistoryEvent(
                id=ding_id,
                device_id=int(device.id),
                camera=str(device.name),
                kind=kind,
                start=start,
                duration=duration,
                confirmed=True,
            )
        )
        return True

    def _add(self, event: RingHistoryEvent) -> None:
        if event.clip is None and self._clips is not None:
            event.clip = self._clips.clip_path(event.id)
        self._events[event.id] = event

    def _prune(self) -> None:
        limit = dt_util.utcnow() - self.keep
        events = sorted(
            (e for e in self._events.values() if e.start >= limit),
            key=lambda e: e.start,
            reverse=True,
        )[:HISTORY_MAX_EVENTS]
        self._events = {e.id: e for e in events}

    def _data_to_save(self) -> dict[str, Any]:
        return {"events": [e.as_dict() for e in self.events]}

    @callback
    def _changed(self) -> None:
        self._prune()
        self._store.async_delay_save(self._data_to_save, SAVE_DELAY)
        for update_callback in list(self._listeners):
            update_callback()
