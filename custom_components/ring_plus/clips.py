"""Clip download for ring_plus (phase 3).

Every minute the devices coordinator already reads the last 10 history
entries of each camera from Ring. We look at that data (no extra polling):
for every motion/ding entry whose recording is "ready" and that we have not
saved yet, we download the MP4 and extract a JPG preview with ffmpeg.

Files: <media>/ring/<camera>/<YYYY-MM-DD_HH-MM-SS>_<motion|ding>.mp4 (+ .jpg)

Not ready yet? Nothing to do: the next coordinator update (1 min later)
looks again. Entries older than MAX_AGE are skipped.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timedelta
import logging
from pathlib import Path
import re
import shutil
import time
from typing import Any

import aiohttp
from ring_doorbell import RingDoorBell, RingError

from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CLIP_KINDS,
    CLIP_MAX_AGE,
    CLIP_MAX_FAILURES,
    CLIP_MIN_FREE_BYTES,
    CLIP_RETENTION,
    CLIP_STORE_KEEP,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 1
DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=300, sock_read=60)
CHUNK = 256 * 1024
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def safe_name(name: str) -> str:
    """Camera name usable as a folder name (umlauts are fine)."""
    cleaned = _UNSAFE.sub("_", name).strip(" .")
    return cleaned or "Kamera"


def clip_basename(created_at: datetime, kind: str) -> str:
    """'2026-11-25_11-50-37_motion' in local time."""
    return f"{dt_util.as_local(created_at):%Y-%m-%d_%H-%M-%S}_{kind}"


def _parse_created(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt_util.UTC)
    if isinstance(value, str):
        return dt_util.parse_datetime(value)
    return None


class ClipManager:
    """Downloads finished Ring recordings into the media folder."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        devices_coordinator: Any,
        retention: timedelta = CLIP_RETENTION,
    ) -> None:
        self.hass = hass
        self.retention = retention
        self._coordinator = devices_coordinator
        self._store: Store[dict[str, Any]] = Store(
            hass, STORE_VERSION, f"{DOMAIN}.{entry_id}.clips"
        )
        # ding id -> {"path": str, "ts": float}  or failures: id -> count
        self._done: dict[str, dict[str, Any]] = {}
        self._failures: dict[str, int] = {}
        self._task: asyncio.Task | None = None
        self._again = False
        self._unsubs: list[Any] = []
        self._saved_callbacks: list[Any] = []
        # ClipArchive (phase 6): when set, a clip is only deleted locally
        # after Internxt confirmed its upload.
        self.archive: Any = None
        # Older history entries (from the startup backfill of the event
        # history): device id -> (device, entries). The minute update only
        # sees the last 10 entries per camera, so busy cameras could miss clips.
        self._extra: dict[Any, tuple[Any, list[dict[str, Any]]]] = {}
        media = hass.config.media_dirs.get("local") or hass.config.path("media")
        self.root = Path(media) / "ring"
        # Shown later by sensors (phase 7)
        self.last_clip_path: str | None = None
        self.last_clip_time: datetime | None = None
        self.last_error: str | None = None

    async def async_setup(self) -> None:
        """Load state and start listening to coordinator updates."""
        data = await self._store.async_load() or {}
        self._done = data.get("done", {})
        self._failures = data.get("failures", {})
        self._unsubs.append(self._coordinator.async_add_listener(self._on_update))
        self._unsubs.append(
            async_track_time_interval(
                self.hass, self._async_cleanup, timedelta(hours=6), name="ring_plus clip cleanup"
            )
        )
        # one cleanup shortly after start, then every 6 h
        self.hass.async_create_background_task(
            self._async_cleanup(), "ring_plus clip cleanup (start)"
        )
        self._on_update()

    async def async_unload(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._task and not self._task.done():
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        await self._async_save()

    # ---- scheduling -----------------------------------------------------

    @callback
    def _on_update(self) -> None:
        if self._task and not self._task.done():
            self._again = True
            return
        self._task = self.hass.async_create_background_task(
            self._async_process(), "ring_plus clip download"
        )

    async def _async_process(self) -> None:
        while True:
            self._again = False
            try:
                await self._async_process_once()
            except Exception:  # never let the loop die silently
                _LOGGER.exception("Unexpected error in clip download")
            if not self._again:
                return

    def _pending(self) -> list[tuple[RingDoorBell, dict[str, Any], datetime]]:
        now = dt_util.utcnow()
        pending = []
        data = self._coordinator.data
        if data is None:
            return []
        sources: list[tuple[Any, list[dict[str, Any]]]] = [
            (device, list(getattr(device, "last_history", None) or []))
            for device in data.video_devices
        ]
        sources.extend(self._extra.values())
        seen: set[str] = set()
        for device, entries in sources:
            for entry in entries:
                try:
                    ding_id = str(entry["id"])
                    kind = entry.get("kind")
                    status = (entry.get("recording") or {}).get("status")
                except (KeyError, TypeError, AttributeError):
                    continue
                created = _parse_created(entry.get("created_at"))
                if ding_id in seen:
                    continue
                seen.add(ding_id)
                if (
                    kind not in CLIP_KINDS
                    or created is None
                    or ding_id in self._done
                    or self._failures.get(ding_id, 0) >= CLIP_MAX_FAILURES
                    or now - created > CLIP_MAX_AGE
                ):
                    continue
                if status != "ready":
                    _LOGGER.debug("Recording %s (%s) not ready yet: %s", ding_id, device.name, status)
                    continue
                pending.append((device, entry, created))
        pending.sort(key=lambda item: item[2])  # oldest first
        return pending

    async def _async_process_once(self) -> None:
        pending = self._pending()
        if not pending:
            return
        for device, entry, created in pending:
            if not await self._async_enough_space():
                self.last_error = "not enough free disk space"
                _LOGGER.warning(
                    "Ring clip download paused: less than %s GB free under %s",
                    CLIP_MIN_FREE_BYTES // 1024**3,
                    self.root,
                )
                return
            await self._async_download(device, entry, created)
        await self._async_save()

    # ---- download -------------------------------------------------------

    async def _async_enough_space(self) -> bool:
        def _free() -> int:
            self.root.mkdir(parents=True, exist_ok=True)
            return shutil.disk_usage(self.root).free

        try:
            return await self.hass.async_add_executor_job(_free) >= CLIP_MIN_FREE_BYTES
        except OSError as err:
            _LOGGER.warning("Cannot check free space for %s: %s", self.root, err)
            return False

    async def _async_download(
        self, device: RingDoorBell, entry: dict[str, Any], created: datetime
    ) -> None:
        ding_id = str(entry["id"])
        kind = entry["kind"]
        folder = self.root / safe_name(device.name)
        base = clip_basename(created, kind)
        mp4 = folder / f"{base}.mp4"
        if await self.hass.async_add_executor_job(mp4.exists):
            mp4 = folder / f"{base}_{ding_id}.mp4"
        part = mp4.with_suffix(".mp4.part")
        try:
            url = await device.async_recording_url(int(ding_id))
            if not url:
                raise RingError("no recording url (subscription?)")
            await self.hass.async_add_executor_job(
                lambda: folder.mkdir(parents=True, exist_ok=True)
            )
            size = await self._async_fetch(url, part)
            if size < 1024:
                raise RingError(f"recording too small ({size} bytes)")
            await self.hass.async_add_executor_job(part.replace, mp4)
        except (RingError, aiohttp.ClientError, TimeoutError, OSError) as err:
            count = self._failures.get(ding_id, 0) + 1
            self._failures[ding_id] = count
            self.last_error = f"{device.name}: {err}"
            log = _LOGGER.warning if count >= CLIP_MAX_FAILURES else _LOGGER.info
            log(
                "Download of Ring clip %s (%s) failed (%s/%s): %s",
                ding_id, device.name, count, CLIP_MAX_FAILURES, err,
            )
            with suppress(OSError):
                await self.hass.async_add_executor_job(part.unlink, True)
            return

        await self._async_snapshot(mp4)
        self._done[ding_id] = {"path": str(mp4), "ts": time.time()}
        self._failures.pop(ding_id, None)
        self.last_clip_path = str(mp4)
        self.last_clip_time = created
        self.last_error = None
        _LOGGER.info("Saved Ring clip %s", mp4)
        for saved_callback in list(self._saved_callbacks):
            try:
                saved_callback(ding_id, str(mp4))
            except Exception:
                _LOGGER.exception("Error in clip-saved consumer")

    # ---- for the event history (phase 5) --------------------------------

    @callback
    def add_history(self, device: Any, entries: list[dict[str, Any]]) -> None:
        """Look at more (older) history entries of a camera for missing clips."""
        if entries:
            self._extra[device.id] = (device, list(entries))
            self._on_update()

    @callback
    def async_add_saved_listener(self, saved_callback: Any) -> Any:
        """Call saved_callback(ding_id, path) after each saved clip."""
        self._saved_callbacks.append(saved_callback)

        @callback
        def remove() -> None:
            with suppress(ValueError):
                self._saved_callbacks.remove(saved_callback)

        return remove

    def clip_path(self, ding_id: str) -> str | None:
        """Path of the saved clip for this Ring event id, if any."""
        return (self._done.get(ding_id) or {}).get("path")

    async def _async_fetch(self, url: str, target: Path) -> int:
        """Stream the MP4 to disk without loading it into memory."""
        session = async_get_clientsession(self.hass)
        written = 0
        handle = await self.hass.async_add_executor_job(target.open, "wb")
        try:
            async with session.get(url, timeout=DOWNLOAD_TIMEOUT) as resp:
                resp.raise_for_status()
                async for chunk in resp.content.iter_chunked(CHUNK):
                    await self.hass.async_add_executor_job(handle.write, chunk)
                    written += len(chunk)
        finally:
            await self.hass.async_add_executor_job(handle.close)
        return written

    async def _async_snapshot(self, mp4: Path) -> None:
        """Extract a JPG preview (frame at 1 s) next to the clip."""
        jpg = mp4.with_suffix(".jpg")
        binary = get_ffmpeg_manager(self.hass).binary
        try:
            proc = await asyncio.create_subprocess_exec(
                binary, "-hide_banner", "-loglevel", "error", "-y",
                "-ss", "1", "-i", str(mp4), "-frames:v", "1", "-q:v", "3", str(jpg),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            async with asyncio.timeout(60):
                _, err = await proc.communicate()
            if proc.returncode != 0:
                _LOGGER.info("No preview for %s: %s", mp4.name, err.decode(errors="ignore")[:200])
        except (OSError, TimeoutError) as err:
            _LOGGER.info("No preview for %s: %s", mp4.name, err)

    # ---- housekeeping ---------------------------------------------------

    async def _async_cleanup(self, now: datetime | None = None) -> None:
        """Delete local clips older than the retention and forget old ids.

        With the cloud archive active, only clips whose upload Internxt
        confirmed are deleted; the others stay until they are uploaded.
        """
        limit = time.time() - self.retention.total_seconds()
        archive = self.archive
        uploaded = archive.uploaded_paths() if archive is not None else None

        def _clean() -> tuple[int, list[str], int]:
            removed = 0
            removed_mp4: list[str] = []
            kept = 0
            if not self.root.is_dir():
                return 0, [], 0
            for path in list(self.root.rglob("*")):
                if path.is_file() and path.suffix in (".mp4", ".part"):
                    with suppress(OSError):
                        if path.stat().st_mtime < limit:
                            if (
                                path.suffix == ".mp4"
                                and uploaded is not None
                                and str(path) not in uploaded
                            ):
                                kept += 1  # not in the cloud yet
                                continue
                            path.unlink()
                            removed += 1
                            if path.suffix == ".mp4":
                                removed_mp4.append(str(path))
                            # the preview belongs to the clip
                            path.with_suffix(".jpg").unlink(missing_ok=True)
                elif path.is_file() and path.suffix == ".jpg":
                    with suppress(OSError):
                        if (
                            not path.with_suffix(".mp4").exists()
                            and path.stat().st_mtime < limit
                        ):
                            path.unlink()
            for folder in sorted(self.root.rglob("*"), reverse=True):
                if folder.is_dir():
                    with suppress(OSError):
                        folder.rmdir()  # only works when empty
            return removed, removed_mp4, kept

        removed, removed_mp4, kept = await self.hass.async_add_executor_job(_clean)
        if removed:
            _LOGGER.info("Removed %s old Ring clip files (older than %s)", removed, self.retention)
        if kept:
            _LOGGER.info("Keeping %s old Ring clips until their upload to Internxt is confirmed", kept)
        if archive is not None and removed_mp4:
            archive.forget(removed_mp4)
        oldest = dt_util.utcnow() - CLIP_MAX_AGE
        for key, (device, entries) in list(self._extra.items()):
            fresh = [
                e for e in entries
                if (created := _parse_created(e.get("created_at"))) and created > oldest
            ]
            if fresh:
                self._extra[key] = (device, fresh)
            else:
                del self._extra[key]
        keep_after = time.time() - CLIP_STORE_KEEP.total_seconds()
        self._done = {k: v for k, v in self._done.items() if v.get("ts", 0) > keep_after}
        if len(self._failures) > 500:
            self._failures = dict(list(self._failures.items())[-200:])
        await self._async_save()

    async def _async_save(self) -> None:
        await self._store.async_save({"done": self._done, "failures": self._failures})
