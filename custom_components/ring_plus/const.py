"""The Ring constants."""

from datetime import timedelta
from typing import Final

from homeassistant.const import Platform

ATTRIBUTION = "Data provided by Ring.com"

NOTIFICATION_ID = "ring_notification"
NOTIFICATION_TITLE = "Ring Setup"

DOMAIN = "ring_plus"
DEFAULT_ENTITY_NAMESPACE = "ring_plus"

PLATFORMS = [
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.EVENT,
    Platform.LIGHT,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.SIREN,
    Platform.SWITCH,
]


SCAN_INTERVAL = timedelta(minutes=1)

CONF_2FA = "2fa"
CONF_LISTEN_CREDENTIALS = "listen_token"

CONF_CONFIG_ENTRY_MINOR_VERSION: Final = 3

# Watchdog for the realtime event listener (ring_plus)
WATCHDOG_INTERVAL = timedelta(seconds=60)
# FCM sends heartbeats every 10-20 s; this long without any message = hung.
LISTENER_STALE_SECONDS: Final = 180
# How many watchdog runs a listener may stay "connecting" before a restart.
LISTENER_STARTING_GRACE_CHECKS: Final = 3
# Wait between restart attempts (seconds), last value repeats.
LISTENER_RESTART_BACKOFF: Final = (0, 60, 120, 300, 600, 900)

CONN_CONNECTED: Final = "connected"
CONN_CONNECTING: Final = "connecting"
CONN_RECONNECTING: Final = "reconnecting"
CONN_DISCONNECTED: Final = "disconnected"
CONN_STATES: Final = [CONN_CONNECTED, CONN_CONNECTING, CONN_RECONNECTING, CONN_DISCONNECTED]

# Clip download (ring_plus phase 3)
CLIP_KINDS: Final = ("motion", "ding")
CLIP_MAX_AGE = timedelta(hours=24)  # older history entries are not downloaded
CLIP_MAX_FAILURES: Final = 5
CLIP_MIN_FREE_BYTES: Final = 2 * 1024**3  # pause downloads below 2 GB free
CLIP_RETENTION = timedelta(days=7)  # local files older than this are deleted
CLIP_STORE_KEEP = timedelta(days=14)  # remember downloaded ids this long
