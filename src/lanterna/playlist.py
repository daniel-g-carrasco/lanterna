"""From what the user chose to the list a session shows.

Files named one by one are taken as they are, in that order: if one is not
media the session says so. A folder stands for the photos and videos right
inside it, in the order a person would number them (2 before 10). A single
photo brings its neighbours along, as an image viewer does, so that next
and previous go through the folder; a single video stays alone, because the
next one in the folder is rarely the next thing to watch.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

PHOTOS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif", ".heic", ".heif", ".tif", ".tiff"}
VIDEOS = {".mp4", ".m4v", ".mkv", ".webm", ".mov", ".avi", ".mpg", ".mpeg", ".ts", ".m2ts", ".wmv", ".ogv", ".flv"}
_NUMBER = re.compile(r"(\d+)")


@dataclass(frozen=True)
class Playlist:
    paths: tuple[str, ...] = ()
    index: int = 0


def is_photo(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in PHOTOS


def is_media(path: str) -> bool:
    return is_photo(path) or os.path.splitext(path)[1].lower() in VIDEOS


def _human_order(name: str) -> list:
    return [(0, int(part), "") if part.isdigit() else (1, 0, part) for part in _NUMBER.split(name.casefold()) if part]


def _inside(folder: str, wanted) -> list[str]:
    """The files in `folder` that `wanted` accepts, hidden ones left out."""
    with os.scandir(folder) as entries:
        names = [e.name for e in entries if not e.name.startswith(".") and e.is_file() and wanted(e.name)]
    return [os.path.join(folder, name) for name in sorted(names, key=_human_order)]


def expand(paths: list[str]) -> Playlist:
    """OSError when a folder cannot be read; an empty Playlist when nothing in it can be shown."""
    if len(paths) == 1 and os.path.isfile(paths[0]) and is_photo(paths[0]):
        folder = os.path.dirname(os.path.abspath(paths[0]))
        chosen = os.path.join(folder, os.path.basename(paths[0]))
        try:
            photos = _inside(folder, is_photo)
        except OSError:             # the neighbours cannot be read: the photo itself still can
            photos = []
        if chosen in photos:
            return Playlist(tuple(photos), photos.index(chosen))
        return Playlist((chosen,), 0)
    out: list[str] = []
    for path in paths:
        if os.path.isdir(path):
            out.extend(_inside(path, is_media))
        else:
            out.append(path)
    return Playlist(tuple(out), 0)
