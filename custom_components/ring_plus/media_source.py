"""Media source for ring_plus (phase 4).

Shows the clips saved by clips.py in the HA media browser as
"Ring Plus" -> camera -> day -> "11:35:54 · Klingeln", newest first,
with the JPG preview as thumbnail. Playback uses HA's own local media
URL (/media/local/ring/...), which the frontend signs for the user.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
import re
from urllib.parse import quote

from homeassistant.components.media_player import BrowseError, MediaClass
from homeassistant.components.media_source import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
    Unresolvable,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import DOMAIN

_CLIP = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})_(?P<time>\d{2}-\d{2}-\d{2})_(?P<kind>motion|ding)(?:_\d+)?\.mp4$"
)
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_TEXT = {
    "de": {
        "motion": "Bewegung",
        "ding": "Klingeln",
        "days": ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"],
        "today": "Heute",
        "yesterday": "Gestern",
        "clip": "Clip",
        "clips": "Clips",
    },
    "en": {
        "motion": "Motion",
        "ding": "Ding",
        "days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
        "today": "Today",
        "yesterday": "Yesterday",
        "clip": "clip",
        "clips": "clips",
    },
}


async def async_get_media_source(hass: HomeAssistant) -> MediaSource:
    """Set up the Ring Plus media source."""
    return RingPlusMediaSource(hass)


class RingPlusMediaSource(MediaSource):
    """Browse saved Ring clips by camera and day."""

    name = "Ring Plus"

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(DOMAIN)
        self.hass = hass

    # ---- helpers ----------------------------------------------------------

    def _media_dir(self) -> tuple[str, Path]:
        """(media dir id, path) of the folder that holds 'ring'."""
        dirs = self.hass.config.media_dirs
        if "local" in dirs:
            return "local", Path(dirs["local"])
        if dirs:
            key = next(iter(dirs))
            return key, Path(dirs[key])
        return "local", Path(self.hass.config.path("media"))

    @property
    def _root(self) -> Path:
        return self._media_dir()[1] / "ring"

    def _text(self) -> dict:
        lang = (self.hass.config.language or "en").split("-")[0]
        return _TEXT.get(lang, _TEXT["en"])

    def _url(self, camera: str, filename: str) -> str:
        dir_id = self._media_dir()[0]
        return f"/media/{dir_id}/ring/{quote(camera)}/{quote(filename)}"

    def _safe_camera(self, camera: str) -> Path:
        path = (self._root / camera).resolve()
        if path.parent != self._root.resolve() or not path.is_dir():
            raise BrowseError(f"Unknown camera: {camera}")
        return path

    def _day_title(self, day: str) -> str:
        text = self._text()
        d = date.fromisoformat(day)
        today = dt_util.now().date()
        if d == today:
            prefix = text["today"]
        elif (today - d).days == 1:
            prefix = text["yesterday"]
        else:
            prefix = text["days"][d.weekday()]
        return f"{prefix}, {d:%d.%m.%Y}"

    # ---- MediaSource API --------------------------------------------------

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        """Turn 'camera/file.mp4' into a playable URL."""
        camera, _, filename = (item.identifier or "").partition("/")
        if not camera or not _CLIP.match(filename):
            raise Unresolvable(f"Not a Ring clip: {item.identifier}")

        def _exists() -> bool:
            try:
                return (self._safe_camera(camera) / filename).is_file()
            except BrowseError:
                return False

        if not await self.hass.async_add_executor_job(_exists):
            raise Unresolvable(f"Clip not found: {item.identifier}")
        return PlayMedia(self._url(camera, filename), "video/mp4")

    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        """Root -> cameras, camera -> days, camera/day -> clips."""
        identifier = item.identifier or ""
        if not identifier:
            return await self.hass.async_add_executor_job(self._browse_root)
        camera, _, day = identifier.partition("/")
        if not day:
            return await self.hass.async_add_executor_job(self._browse_camera, camera)
        if _DAY.match(day):
            return await self.hass.async_add_executor_job(self._browse_day, camera, day)
        raise BrowseError(f"Unknown item: {identifier}")

    # ---- builders (run in executor) ---------------------------------------

    def _dir_node(
        self, identifier: str, title: str, children: list[BrowseMediaSource] | None
    ) -> BrowseMediaSource:
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=identifier,
            media_class=MediaClass.DIRECTORY,
            media_content_type="",
            title=title,
            can_play=False,
            can_expand=True,
            children=children,
            children_media_class=MediaClass.DIRECTORY,
        )

    def _clips(self, camera_dir: Path) -> list[re.Match]:
        return [
            m for p in camera_dir.iterdir() if p.is_file() and (m := _CLIP.match(p.name))
        ]

    def _browse_root(self) -> BrowseMediaSource:
        cameras = []
        if self._root.is_dir():
            for path in sorted(self._root.iterdir(), key=lambda p: p.name.lower()):
                if path.is_dir() and self._clips(path):
                    cameras.append(self._dir_node(path.name, path.name, None))
        return self._dir_node("", "Ring Plus", cameras)

    def _browse_camera(self, camera: str) -> BrowseMediaSource:
        camera_dir = self._safe_camera(camera)
        counts: dict[str, int] = {}
        for m in self._clips(camera_dir):
            counts[m["date"]] = counts.get(m["date"], 0) + 1
        text = self._text()
        days = [
            self._dir_node(
                f"{camera}/{day}", f"{self._day_title(day)} ({n} {text['clip' if n == 1 else 'clips']})", None
            )
            for day, n in sorted(counts.items(), reverse=True)
        ]
        return self._dir_node(camera, camera, days)

    def _browse_day(self, camera: str, day: str) -> BrowseMediaSource:
        camera_dir = self._safe_camera(camera)
        text = self._text()
        clips = sorted(
            (m for m in self._clips(camera_dir) if m["date"] == day),
            key=lambda m: m.string,
            reverse=True,
        )
        children = []
        for m in clips:
            filename = m.string
            jpg = filename[:-4] + ".jpg"
            thumb = (
                self._url(camera, jpg) if (camera_dir / jpg).is_file() else None
            )
            children.append(
                BrowseMediaSource(
                    domain=DOMAIN,
                    identifier=f"{camera}/{filename}",
                    media_class=MediaClass.VIDEO,
                    media_content_type="video/mp4",
                    title=f"{m['time'].replace('-', ':')} · {text[m['kind']]}",
                    can_play=True,
                    can_expand=False,
                    thumbnail=thumb,
                )
            )
        node = self._dir_node(f"{camera}/{day}", f"{camera} – {self._day_title(day)}", children)
        node.children_media_class = MediaClass.VIDEO
        return node
