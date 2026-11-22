#!/usr/bin/env python3
"""What a choice of files and folders becomes, on real files."""

import os
import tempfile
import unittest
from pathlib import Path

import _setup  # noqa: F401

from lanterna.playlist import Playlist, expand


class ExpandTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def touch(self, *names):
        for name in names:
            path = self.dir / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x")

    def names(self, playlist):
        return [os.path.basename(p) for p in playlist.paths]

    def test_a_folder_is_its_photos_and_videos_in_human_order(self):
        self.touch("img10.jpg", "img2.JPG", "film.mkv", "notes.txt", ".hidden.jpg", "sub/inner.jpg")
        result = expand([str(self.dir)])
        self.assertEqual(self.names(result), ["film.mkv", "img2.JPG", "img10.jpg"])
        self.assertEqual(result.index, 0)

    def test_a_single_photo_brings_its_neighbours_and_starts_from_itself(self):
        self.touch("a.jpg", "b.png", "c.webp", "film.mp4")
        result = expand([str(self.dir / "b.png")])
        self.assertEqual(self.names(result), ["a.jpg", "b.png", "c.webp"])
        self.assertEqual(result.index, 1)

    def test_a_single_video_stays_alone(self):
        self.touch("one.mp4", "two.mp4")
        self.assertEqual(self.names(expand([str(self.dir / "one.mp4")])), ["one.mp4"])

    def test_files_named_one_by_one_keep_their_order_even_if_not_media(self):
        self.touch("z.mp4", "a.jpg", "notes.txt")
        chosen = [str(self.dir / n) for n in ("z.mp4", "notes.txt", "a.jpg")]
        self.assertEqual(self.names(expand(chosen)), ["z.mp4", "notes.txt", "a.jpg"])

    def test_a_folder_with_nothing_to_show_is_empty(self):
        self.touch("notes.txt")
        self.assertEqual(expand([str(self.dir)]), Playlist((), 0))

    def test_a_photo_named_with_a_hidden_name_is_still_shown(self):
        self.touch(".secret.jpg", "a.jpg")
        result = expand([str(self.dir / ".secret.jpg")])
        self.assertEqual(self.names(result), [".secret.jpg"])

    def test_a_path_that_is_gone_is_left_for_the_session_to_report(self):
        self.assertEqual(self.names(expand([str(self.dir / "gone")])), ["gone"])


if __name__ == "__main__":
    unittest.main()
