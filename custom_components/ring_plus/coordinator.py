"""Data coordinators for the ring integration."""

from asyncio import TaskGroup, timeout as asyncio_timeout
from contextlib import suppress
from datetime import datetime
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
import logging
import time
from typing import Any, override

from ring_doorbell import (
    AuthenticationError,
    Ring,
    RingDevices,
    RingError,
    RingEvent,
    RingTimeout,
)
from ring_doorbell.listen import RingEventListener

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import (
    BaseDataUpdateCoordinatorProtocol,
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util

from .compat import has_capability
from .const import (
    CONN_CONNECTED,
    CONN_CONNECTING,
    CONN_DISCONNECTED,
    CONN_RECONNECTING,
    DOMAIN,
    LISTENER_RESTART_BACKOFF,
    LISTENER_STALE_SECONDS,
    LISTENER_STARTING_GRACE_CHECKS,
    SCAN_INTERVAL,
    WATCHDOG_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class RingData:
    """Class to support type hinting of ring data collection."""

    api: Ring
    devices: RingDevices
    devices_coordinator: RingDataCoordinator
    listen_coordinator: RingListenCoordinator
    clips: Any = None  # ClipManager (ring_plus)
    history: Any = None  # EventHistory (ring_plus)


type RingConfigEntry = ConfigEntry[RingData]


class RingDataCoordinator(DataUpdateCoordinator[RingDevices]):
    """Base class for device coordinators."""

    config_entry: RingConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: RingConfigEntry,
        ring_api: Ring,
    ) -> None:
        """Initialize my coordinator."""
        super().__init__(
            hass,
            name="devices",
            logger=_LOGGER,
            update_interval=SCAN_INTERVAL,
            config_entry=config_entry,
        )
        self.ring_api: Ring = ring_api
        self.first_call: bool = True

    async def _call_api[*_Ts, _R](
        self,
        target: Callable[[*_Ts], Coroutine[Any, Any, _R]],
        *args: *_Ts,
    ) -> _R:
        try:
            return await target(*args)
        except AuthenticationError as err:
            # Raising ConfigEntryAuthFailed will cancel future updates
            # and start a config flow with SOURCE_REAUTH (async_step_reauth)
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN,
                translation_key="api_authentication",
            ) from err
        except RingTimeout as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="api_timeout",
            ) from err
        except RingError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="api_error",
            ) from err

    @override
    async def _async_update_data(self) -> RingDevices:
        """Fetch data from API endpoint."""
        update_method: str = (
            "async_update_data" if self.first_call else "async_update_devices"
        )
        await self._call_api(getattr(self.ring_api, update_method))
        self.first_call = False
        devices: RingDevices = self.ring_api.devices()
        subscribed_device_ids = set(self.async_contexts())
        for device in devices.all_devices:
            # Don't update all devices in the ring api, only those that set
            # their device id as context when they subscribed.
            if device.id in subscribed_device_ids:
                try:
                    async with TaskGroup() as tg:
                        if has_capability(device, "history"):
                            tg.create_task(
                                self._call_api(
                                    lambda device: device.async_history(limit=10),
                                    device,
                                )
                            )
                        tg.create_task(
                            self._call_api(
                                device.async_update_health_data,
                            )
                        )
                except ExceptionGroup as eg:
                    raise eg.exceptions[0]  # noqa: B904

        return devices


class RingListenCoordinator(BaseDataUpdateCoordinatorProtocol):
    """Global notifications coordinator, with a watchdog (ring_plus).

    Ring sends realtime events (ding, motion) as push messages. The push
    client can die or hang silently; then no events arrive any more. The
    watchdog checks every minute and builds a fresh listener when needed.
    """

    config_entry: RingConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: RingConfigEntry,
        ring_api: Ring,
        listen_credentials: dict[str, Any] | None,
        listen_credentials_updater: Callable[[dict[str, Any]], None],
    ) -> None:
        """Initialize my coordinator."""
        self.hass = hass
        self.logger = _LOGGER
        self.ring_api: Ring = ring_api
        self._listen_credentials = listen_credentials
        self._listen_credentials_updater = listen_credentials_updater
        self.event_listener = self._new_event_listener()
        self._listeners: dict[CALLBACK_TYPE, tuple[CALLBACK_TYPE, object | None]] = {}
        self._listen_callback_id: int | None = None

        # Health / watchdog state (shown by the diagnostic sensors)
        self.connection_state: str = CONN_CONNECTING
        self.last_event_time: datetime | None = None
        self.last_event_kind: str | None = None
        self.last_event_device: str | None = None
        self.reconnect_count = 0
        self.last_reconnect: datetime | None = None
        self.last_reconnect_reason: str | None = None
        self._health_listeners: list[CALLBACK_TYPE] = []
        self._event_callbacks: list[Callable[[RingEvent], None]] = []
        self._not_started_checks = 0
        self._restart_attempt = 0
        self._next_restart_allowed = 0.0
        self._restarting = False
        self._unsub_watchdog: CALLBACK_TYPE | None = None

        self.config_entry = config_entry
        self.start_timeout = 10
        self.config_entry.async_on_unload(self.async_shutdown)
        self.index_alerts()

    def _store_listen_credentials(self, creds: dict[str, Any]) -> None:
        self._listen_credentials = creds
        self._listen_credentials_updater(creds)

    def _new_event_listener(self) -> RingEventListener:
        return RingEventListener(
            self.ring_api, self._listen_credentials, self._store_listen_credentials
        )

    def index_alerts(self) -> None:
        "Index the active alerts."
        self.alerts = {
            (alert.doorbot_id, alert.kind): alert
            for alert in self.ring_api.active_alerts()
        }

    async def async_shutdown(self) -> None:
        """Cancel any scheduled call, and ignore new runs."""
        if self._unsub_watchdog:
            self._unsub_watchdog()
            self._unsub_watchdog = None
        if self.event_listener.started:
            await self._async_stop_listen()

    async def _async_stop_listen(self) -> None:
        self.logger.debug("Stopping ring listener")
        try:
            async with asyncio_timeout(10):
                await self.event_listener.stop()
        except Exception as err:  # noqa: BLE001
            self.logger.debug("Error while stopping ring listener: %s", err)
        self.logger.debug("Stopped ring listener")

    async def _async_start_listen(self) -> None:
        """Start listening for realtime events."""
        self.logger.debug("Starting ring listener.")
        try:
            await self.event_listener.start(
                timeout=self.start_timeout,
            )
        except (TimeoutError, RingError, OSError) as err:
            self.logger.warning("Ring event listener could not start: %s", err)
        if self.event_listener.started is True:
            self.logger.debug("Started ring listener")
        else:
            self.logger.warning(
                "Ring event listener failed to start after %s seconds",
                self.start_timeout,
            )
        self._listen_callback_id = self.event_listener.add_notification_callback(
            self._on_event
        )
        self.index_alerts()
        if self._unsub_watchdog is None:
            self._unsub_watchdog = async_track_time_interval(
                self.hass,
                self._async_watchdog,
                WATCHDOG_INTERVAL,
                name="ring_plus listener watchdog",
            )
        self._evaluate_health()
        # Update the listeners so they switch from Unavailable to Unknown
        self._async_update_listeners()

    def _on_event(self, event: RingEvent) -> None:
        self.logger.debug("Ring event received: %s", event)
        self.last_event_time = dt_util.utcnow()
        self.last_event_kind = event.kind
        self.last_event_device = event.device_name or str(event.doorbot_id)
        self.index_alerts()
        self._async_update_listeners(event.doorbot_id)
        self._notify_health()
        for event_callback in list(self._event_callbacks):
            try:
                event_callback(event)
            except Exception:  # one broken consumer must not stop the others
                self.logger.exception("Error in Ring event consumer")

    @callback
    def async_add_event_callback(
        self, event_callback: Callable[[RingEvent], None]
    ) -> CALLBACK_TYPE:
        """Get every realtime event (used by the event history)."""
        self._event_callbacks.append(event_callback)

        @callback
        def remove() -> None:
            with suppress(ValueError):
                self._event_callbacks.remove(event_callback)

        return remove

    # ---- watchdog -------------------------------------------------------

    def _receiver_status(self) -> tuple[str | None, float | None]:
        """Run state name and last message time of the push client."""
        receiver = getattr(self.event_listener, "_receiver", None)
        if receiver is None:
            return None, None
        run_state = getattr(receiver, "run_state", None)
        return (
            getattr(run_state, "name", None),
            getattr(receiver, "last_message_time", None),
        )

    def _evaluate_health(self) -> str | None:
        """Update connection_state; return a restart reason if one is needed."""
        run_state, last_message = self._receiver_status()
        reason: str | None = None
        if run_state == "STARTED" and self.event_listener.started:
            self._not_started_checks = 0
            if last_message is not None and (
                time.time() - last_message > LISTENER_STALE_SECONDS
            ):
                reason = f"no message for {int(time.time() - last_message)} s"
                self._set_state(CONN_DISCONNECTED)
            else:
                self._restart_attempt = 0
                self._set_state(CONN_CONNECTED)
        elif run_state in (None, "STOPPING", "STOPPED") or not (
            self.event_listener.started
        ):
            reason = f"listener stopped ({run_state or 'not started'})"
            self._set_state(CONN_DISCONNECTED)
        else:  # CREATED, STARTING_*, RESETTING: give it some time
            self._not_started_checks += 1
            self._set_state(CONN_CONNECTING)
            if self._not_started_checks > LISTENER_STARTING_GRACE_CHECKS:
                reason = f"stuck in {run_state}"
        return reason

    async def _async_watchdog(self, now: datetime | None = None) -> None:
        if not self._listeners or self._restarting:
            return
        reason = self._evaluate_health()
        if reason is None or time.monotonic() < self._next_restart_allowed:
            return
        await self._async_restart_listener(reason)

    async def _async_restart_listener(self, reason: str) -> None:
        self._restarting = True
        self.logger.warning("Ring event listener problem (%s), reconnecting", reason)
        self._set_state(CONN_RECONNECTING)
        try:
            old = self.event_listener
            if self._listen_callback_id is not None:
                with suppress(Exception):
                    old.remove_notification_callback(self._listen_callback_id)
                self._listen_callback_id = None
            await self._async_stop_listen()
            self.event_listener = self._new_event_listener()
            await self._async_start_listen()
        except Exception:
            self.logger.exception("Reconnecting the Ring event listener failed")
        finally:
            self._restarting = False
            self.reconnect_count += 1
            self.last_reconnect = dt_util.utcnow()
            self.last_reconnect_reason = reason
            delay = LISTENER_RESTART_BACKOFF[
                min(self._restart_attempt, len(LISTENER_RESTART_BACKOFF) - 1)
            ]
            self._restart_attempt += 1
            self._next_restart_allowed = time.monotonic() + delay
            self._evaluate_health()
            self._notify_health()
            if self.connection_state == CONN_CONNECTED:
                self.logger.info("Ring event listener reconnected")

    def _set_state(self, state: str) -> None:
        if state != self.connection_state:
            self.connection_state = state
            self._notify_health()

    @property
    def last_heartbeat(self) -> datetime | None:
        """Time of the last message (incl. heartbeats) from the push service."""
        _, last_message = self._receiver_status()
        if last_message is None:
            return None
        return dt_util.utc_from_timestamp(last_message)

    @callback
    def async_add_health_listener(self, update_callback: CALLBACK_TYPE) -> CALLBACK_TYPE:
        """Register a callback for health changes (diagnostic sensors)."""
        self._health_listeners.append(update_callback)

        @callback
        def remove() -> None:
            with suppress(ValueError):
                self._health_listeners.remove(update_callback)

        return remove

    @callback
    def _notify_health(self) -> None:
        for update_callback in list(self._health_listeners):
            update_callback()

    # ---- entity listeners -------------------------------------------------

    @callback
    def _async_update_listeners(self, doorbot_id: int | None = None) -> None:
        """Update all registered listeners."""
        for update_callback, device_api_id in list(self._listeners.values()):
            if not doorbot_id or device_api_id == doorbot_id:
                update_callback()

    @callback
    @override
    def async_add_listener(
        self, update_callback: CALLBACK_TYPE, context: Any = None
    ) -> Callable[[], None]:
        """Listen for data updates."""
        start_listen = not self._listeners

        @callback
        def remove_listener() -> None:
            """Remove update listener."""
            self._listeners.pop(remove_listener)
            if not self._listeners:
                self.config_entry.async_create_task(
                    self.hass,
                    self._async_stop_listen(),
                    "Ring event listener stop",
                    eager_start=True,
                )

        self._listeners[remove_listener] = (update_callback, context)

        # This is the first listener, start the event listener.
        if start_listen:
            self.config_entry.async_create_task(
                self.hass,
                self._async_start_listen(),
                "Ring event listener start",
                eager_start=True,
            )
        return remove_listener
