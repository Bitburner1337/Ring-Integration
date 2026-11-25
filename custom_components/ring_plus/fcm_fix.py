"""Workaround for a push-notification bug in firebase-messaging.

Ring's realtime events (ding, motion) arrive as encrypted push messages.
Their key ("crypto-key") and salt ("encryption") are base64 strings that
are now sent WITHOUT trailing "=" padding. firebase-messaging 0.4.x decodes
them strictly, fails with "Incorrect padding" and then shuts the whole push
client down, so no more events reach Home Assistant until a restart.

We wrap the decrypt function once: add the missing padding (and drop any
extra ";param=..." parts), then call the original. Valid input is passed on
unchanged, so this is safe for every user of the library.
"""

from __future__ import annotations

import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)
_PATCH_FLAG = "_ring_plus_padding_fix"


def _clean_b64(value: str, prefix: str) -> str:
    """Return the bare base64 value with correct '=' padding."""
    value = value.split(";", 1)[0].strip()
    if value.startswith(prefix):
        value = value[len(prefix) :]
    value = value.rstrip("=")
    return value + "=" * (-len(value) % 4)


def apply() -> None:
    """Patch FcmPushClient._decrypt_raw_data once (idempotent)."""
    try:
        from firebase_messaging import fcmpushclient  # noqa: PLC0415
    except ImportError:  # library layout changed: do nothing
        _LOGGER.debug("firebase_messaging not found, padding fix not applied")
        return

    client_cls = fcmpushclient.FcmPushClient
    original = client_cls.__dict__.get("_decrypt_raw_data")
    if original is None or getattr(original, _PATCH_FLAG, False):
        return
    original_func = original.__func__ if isinstance(original, staticmethod) else original

    def _decrypt_raw_data(
        credentials: Any, crypto_key_str: str, salt_str: str, raw_data: bytes
    ) -> bytes:
        return original_func(
            credentials,
            _clean_b64(crypto_key_str, "dh="),
            _clean_b64(salt_str, "salt="),
            raw_data,
        )

    setattr(_decrypt_raw_data, _PATCH_FLAG, True)
    client_cls._decrypt_raw_data = staticmethod(_decrypt_raw_data)  # noqa: SLF001
    _LOGGER.debug("Applied firebase-messaging base64 padding fix")
