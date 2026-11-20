from __future__ import annotations

import asyncio
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from importlib.metadata import distribution
from urllib.parse import parse_qs, urlsplit

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError


class MusicError(Exception):
    """A message suitable for the Discord user."""


VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
HOSTS = {"youtube.com", "www.youtube.com", "music.youtube.com", "m.youtube.com"}


@dataclass(frozen=True)
class Track:
    id: str
    title: str
    artist: str = ""
    duration: int = 0

    @property
    def url(self):
        return f"https://www.youtube.com/watch?v={self.id}"

    @property
    def label(self):
        details = self.title
        if self.artist and self.artist.casefold() not in details.casefold():
            details += f" - {self.artist}"
        suffix = ""
        if self.duration:
            minutes, seconds = divmod(self.duration, 60)
            suffix = f" [{minutes:02}:{seconds:02}]"
        return details[:100 - len(suffix)].rstrip() + suffix


def parse_query(text):
    text = text.strip()
    if not text:
        raise MusicError("Enter a song name or a YouTube video link.")
    if re.match(r"^(?:(?:www|music|m)\.)?(?:youtube\.com|youtu\.be)/", text, re.I):
        text = "https://" + text
    if "://" not in text and not text.startswith("//"):
        return text
    try:
        parts = urlsplit(text)
        host = parts.hostname
    except ValueError:
        raise MusicError("That link could not be read.") from None
    video_id = ""
    if parts.scheme in {"http", "https"} and not parts.username:
        if host in HOSTS and parts.path == "/watch":
            video_id = parse_qs(parts.query).get("v", [""])[0]
        elif host in HOSTS and parts.path.startswith(("/shorts/", "/live/", "/embed/")):
            video_id = parts.path.rstrip("/").split("/")[-1]
        elif host == "youtu.be":
            video_id = parts.path.strip("/")
    if not VIDEO_ID.fullmatch(video_id):
        raise MusicError("Use a song name or a link to a single YouTube video.")
    return Track(video_id, f"YouTube video {video_id}")


def bundled_node():
    package = distribution("nodejs-wheel-binaries")
    return str(next(package.locate_file(file).resolve() for file in package.files
                    if file.name == "node.exe"))


class QuietLogger:
    def debug(self, message): pass
    def warning(self, message): pass
    def error(self, message): pass


class YouTube:
    def __init__(self):
        self.node = bundled_node()
        self.cache = OrderedDict()
        self.pending = {}
        self.limit = asyncio.Semaphore(4)

    def extract(self, query, **settings):
        options = {
            "quiet": True, "logger": QuietLogger(), "cachedir": False,
            "socket_timeout": 8, "retries": 1, "extractor_retries": 1,
            "js_runtimes": {"node": {"path": self.node}}, **settings,
        }
        with YoutubeDL(options) as ydl:
            return ydl.extract_info(query, download=False)

    async def _search(self, text):
        async with self.limit:
            result = await asyncio.to_thread(self.extract, "ytsearch10:" + text, extract_flat=True)
        tracks = []
        for item in (result or {}).get("entries", []):
            if item and VIDEO_ID.fullmatch(item.get("id") or ""):
                tracks.append(Track(item["id"], item.get("title") or item["id"],
                                    item.get("artist") or item.get("channel") or item.get("uploader") or "",
                                    int(item.get("duration") or 0)))
        return tracks

    async def search(self, text):
        key = text.strip().casefold()
        cached = self.cache.get(key)
        if cached and time.monotonic() - cached[0] < 300:
            return cached[1]
        if key not in self.pending:
            if len(self.pending) >= 12:
                raise MusicError("YouTube search is busy. Try again in a moment.")
            async def lookup():
                try:
                    tracks = await self._search(text)
                    self.cache[key] = (time.monotonic(), tracks)
                    self.cache.move_to_end(key)
                    if len(self.cache) > 128:
                        self.cache.popitem(last=False)
                    return tracks
                except DownloadError:
                    raise MusicError("YouTube search is unavailable. Try a video link.") from None
                finally:
                    self.pending.pop(key, None)
            task = asyncio.create_task(lookup())
            # Autocomplete can expire while the lookup finishes for the next request.
            task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
            self.pending[key] = task
        return await asyncio.shield(self.pending[key])

    def known_track(self, track):
        for _, tracks in reversed(self.cache.values()):
            for found in tracks:
                if found.id == track.id:
                    return found
        return track

    async def stream(self, track):
        try:
            info = await asyncio.to_thread(
                self.extract, track.url, noplaylist=True,
                format=("bestaudio[protocol^=m3u8]/best[protocol^=m3u8]/"
                        "bestaudio[protocol=https]/best[protocol=https]"),
            )
        except DownloadError:
            raise MusicError("YouTube did not make this video playable from this PC. Try another recording.") from None
        url = (info or {}).get("url") or ""
        host = (urlsplit(url).hostname or "").lower()
        if not url.startswith("https://") or not host.endswith(".googlevideo.com"):
            raise MusicError("YouTube did not return a playable audio stream for this video.")
        return info.get("title") or track.title, url, info.get("http_headers") or {}
