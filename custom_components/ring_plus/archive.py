"""Cloud archive in Internxt for ring_plus (phase 6).

Every saved clip (MP4 + JPG preview) is uploaded to Internxt:

    Ring-Clips/<camera>/<YYYY-MM-DD>/<file>

We do not talk to Internxt ourselves. We call the action internxt.upload
of the installed Internxt integration, which uses its own login and
encrypts on this device. Its answer (the file uuid) is our proof that the
upload worked. Only then may clips.py delete the local file after the
retention time.

One clip at a time, oldest first. On errors we wait longer and longer
(1, 5, 15, 30, 60 minutes) and try again; nothing is lost, the local
file simply stays.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime
import logging
from pathlib import Path
import time
from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceNotFound
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    ARCHIVE_BACKOFF,
    ARCHIVE_IDLE_RECHECK,
    ARCHIVE_ROOT_FOLDER,
    ARCHIVE_START_GRACE,
    ARCHIVE_UPLOAD_TIMEOUT,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

INTERNXT = "internxt"
STORE_VERSION = 1

# Status values (shown by the "Cloud-Archiv" sensor)
ST_IDLE = "idle"  # everything uploaded
ST_UPLOADING = "uploading"
ST_RETRY = "retry_wait"  # last upload failed, waiting
ST_NO_ACCOUNT = "no_account"  # Internxt not set up / not loaded
ST_DISABLED = "disabled"  # switched off in the options
ARCHIVE_STATES = [ST_IDLE, ST_UPLOADING, ST_RETRY, ST_NO_ACCOUNT, ST_DISABLED]


class _Skip(Exception):
    """The local file is gone; nothing to upload."""


class ClipArchive:
    """Upload queue from /media/ring to Internxt."""

    def __init__(
        self, hass: HomeAssistant, entry_id: str, clips: Any, enabled: bool = True
    ) -> None:
        self.hass = hass
        self.enabled = enabled
        self._clips = clips
        self._store: Store[dict[str, Any]] = Store(
            hass, STORE_VERSION, f"{DOMAIN}.{entry_id}.archive"
        )
        # local mp4 path -> {"remote": str, "uuid": str, "ts": float}
        self._uploaded: dict[str, dict[str, Any]] = {}
        self._queue: list[Path] = []
        self._wakeup = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._unsubs: list[CALLBACK_TYPE] = []
        self._listeners: list[CALLBACK_TYPE] = []
        self._failures = 0
        self._started = time.monotonic()
        self._warned_no_account = False
        # shown by the sensor
        self.status = ST_IDLE
        self.last_upload: datetime | None = None
        self.last_upload_file: str | None = None
        self.last_error: str | None = None
        self.next_try: datetime | None = None

    # ---- lifecycle --------------------------------------------------------

    async def async_setup(self) -> None:
        data = await self._store.async_load() or {}
        self._uploaded = data.get("uploaded", {})
        if not self.enabled:
            # Off: no uploads, and clips.py deletes by age only (as before
            # phase 6). We keep the list of earlier uploads for later.
            self.status = ST_DISABLED
            return
        # Clips that exist locally but are not in Internxt yet (also all
        # clips saved before phase 6). Forget uploads whose file is gone.
        root = self._clips.root

        def _scan() -> tuple[list[Path], set[str]]:
            if not root.is_dir():
                return [], set()
            files = sorted(
                (p for p in root.glob("*/*.mp4") if p.is_file()),
                key=lambda p: p.name,
            )
            return files, {str(p) for p in files}

        files, existing = await self.hass.async_add_executor_job(_scan)
        self._uploaded = {k: v for k, v in self._uploaded.items() if k in existing}
        self._queue = [p for p in files if str(p) not in self._uploaded]
        self._unsubs.append(self._clips.async_add_saved_listener(self._on_clip_saved))
        self._clips.archive = self
        self._task = self.hass.async_create_background_task(
            self._async_worker(), "ring_plus internxt archive"
        )
        if self._queue:
            _LOGGER.info("%s Ring clips waiting for upload to Internxt", len(self._queue))

    async def async_unload(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        self._clips.archive = None
        if self._task and not self._task.done():
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        await self._async_save()

    # ---- used by clips.py and the sensor ------------------------------------

    def uploaded_paths(self) -> frozenset[str]:
        """Local MP4 paths whose upload Internxt confirmed."""
        return frozenset(self._uploaded)

    def forget(self, mp4_paths: list[str]) -> None:
        """Local files were deleted by the retention."""
        changed = False
        for path in mp4_paths:
            changed |= self._uploaded.pop(path, None) is not None
        if changed:
            self._store.async_delay_save(self._data_to_save, 30)

    @property
    def pending(self) -> int:
        return len(self._queue)

    @property
    def uploaded_count(self) -> int:
        return len(self._uploaded)

    @callback
    def async_add_listener(self, update_callback: CALLBACK_TYPE) -> CALLBACK_TYPE:
        self._listeners.append(update_callback)

        @callback
        def remove() -> None:
            with suppress(ValueError):
                self._listeners.remove(update_callback)

        return remove

    # ---- queue ----------------------------------------------------------------

    @callback
    def _on_clip_saved(self, _ding_id: str, path: str) -> None:
        mp4 = Path(path)
        if path not in self._uploaded and mp4 not in self._queue:
            self._queue.append(mp4)
            self._wakeup.set()
            self._notify()

    @callback
    def _notify(self) -> None:
        for update_callback in list(self._listeners):
            update_callback()

    def _set_status(self, status: str) -> None:
        self.status = status
        self._notify()

    def _account(self) -> str | None:
        """Entry id of a loaded Internxt account, if any."""
        for entry in self.hass.config_entries.async_entries(INTERNXT):
            if entry.state is ConfigEntryState.LOADED and not entry.disabled_by:
                return entry.entry_id
        return None

    async def _async_worker(self) -> None:
        while True:
            if not self._queue:
                self.next_try = None
                self._set_status(ST_IDLE)
                self._wakeup.clear()
                await self._wakeup.wait()
                continue

            account = self._account()
            if account is None:
                # Right after a restart Internxt may simply not be loaded yet:
                # only warn when it is still missing after the grace time.
                in_grace = time.monotonic() - self._started < ARCHIVE_START_GRACE
                if not in_grace and not self._warned_no_account:
                    self._warned_no_account = True
                    _LOGGER.warning(
                        "Internxt integration not loaded; %s Ring clips wait for upload",
                        len(self._queue),
                    )
                self.last_error = "Internxt not loaded"
                self._set_status(ST_NO_ACCOUNT)
                await asyncio.sleep(
                    min(30, ARCHIVE_IDLE_RECHECK.total_seconds())
                    if in_grace
                    else ARCHIVE_IDLE_RECHECK.total_seconds()
                )
                continue

            self._warned_no_account = False
            mp4 = self._queue[0]
            self._set_status(ST_UPLOADING)
            try:
                remote, file_uuid = await self._async_upload_clip(account, mp4)
            except _Skip:
                self._queue.pop(0)
                continue
            except (HomeAssistantError, TimeoutError, OSError) as err:
                delay = ARCHIVE_BACKOFF[min(self._failures, len(ARCHIVE_BACKOFF) - 1)]
                self._failures += 1
                self.last_error = f"{mp4.name}: {err}"
                self.next_try = dt_util.utcnow() + delay
                log = _LOGGER.warning if self._failures >= 3 else _LOGGER.info
                log(
                    "Upload of %s to Internxt failed (%s), next try in %s",
                    mp4.name, err, delay,
                )
                self._set_status(ST_RETRY)
                await asyncio.sleep(delay.total_seconds())
                continue

            self._queue.pop(0)
            self._uploaded[str(mp4)] = {"remote": remote, "uuid": file_uuid, "ts": time.time()}
            self._failures = 0
            self.last_error = None
            self.next_try = None
            self.last_upload = dt_util.utcnow()
            self.last_upload_file = remote
            await self._async_save()
            _LOGGER.debug("Archived %s to Internxt (%s)", mp4.name, remote)
            self._notify()

    async def _async_upload_clip(self, account: str, mp4: Path) -> tuple[str, str]:
        """Upload MP4 (+ JPG). Returns (remote path, uuid) of the MP4."""
        jpg = mp4.with_suffix(".jpg")
        exists = await self.hass.async_add_executor_job(
            lambda: (mp4.is_file(), jpg.is_file())
        )
        if not exists[0]:
            raise _Skip
        camera = mp4.parent.name
        day = mp4.name[:10]
        folder = f"{ARCHIVE_ROOT_FOLDER}/{camera}/{day}"
        file_uuid = await self._async_upload_file(account, mp4, folder)
        if exists[1]:
            try:
                await self._async_upload_file(account, jpg, folder)
            except _Skip:
                pass
        return f"{folder}/{mp4.name}", file_uuid

    async def _async_upload_file(self, account: str, path: Path, folder: str) -> str:
        """One file via internxt.upload. Returns the Internxt uuid."""
        try:
            async with asyncio.timeout(ARCHIVE_UPLOAD_TIMEOUT.total_seconds()):
                response = await self.hass.services.async_call(
                    INTERNXT,
                    "upload",
                    {
                        "config_entry_id": account,
                        "filename": [str(path)],
                        "destination_folder": folder,
                        "overwrite": False,
                    },
                    blocking=True,
                    return_response=True,
                )
        except ServiceNotFound as err:
            raise HomeAssistantError("internxt.upload not available") from err
        except HomeAssistantError as err:
            key = getattr(err, "translation_key", None)
            if key == "file_exists":
                # Uploaded before (e.g. HA stopped right after the upload).
                return "existing"
            if key == "file_not_found":
                raise _Skip from err
            raise
        files = (response or {}).get("files") or []
        if not files or not files[0].get("uuid"):
            raise HomeAssistantError(f"no confirmation from Internxt for {path.name}")
        return str(files[0]["uuid"])

    # ---- storage --------------------------------------------------------------

    def _data_to_save(self) -> dict[str, Any]:
        return {"uploaded": self._uploaded}

    async def _async_save(self) -> None:
        await self._store.async_save(self._data_to_save())
