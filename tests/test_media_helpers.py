"""Subtítulos (SRT/VTT → WebVTT, nombres seguros, listado) y fotos de perfil (firma binaria, rutas)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from rascal.media.avatars import (
    AvatarError,
    avatar_url,
    delete_avatar,
    detect_image_ext,
    save_avatar,
)
from rascal.media.subtitles import (
    MAX_SUBTITLE_BYTES,
    SubtitleError,
    decode_text,
    list_subtitles,
    safe_label,
    save_subtitle,
    srclang_for,
    to_webvtt,
)

SRT = (
    "1\r\n00:00:01,500 --> 00:00:03,000\r\nHola, ¿cómo estás?\r\n\r\n"
    "2\r\n00:00:04,000 --> 00:00:06,250\r\nBien, gracias.\r\n"
).encode("utf-8")

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 64


class SubtitleTests(unittest.TestCase):
    def test_srt_becomes_webvtt_with_dot_decimal(self) -> None:
        vtt = to_webvtt(SRT.decode("utf-8"))
        self.assertTrue(vtt.startswith("WEBVTT\n"))
        self.assertIn("00:00:01.500 --> 00:00:03.000", vtt)
        self.assertNotIn("\r", vtt)

    def test_existing_webvtt_is_kept(self) -> None:
        text = "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHola\n"
        self.assertEqual(to_webvtt(text), text)

    def test_rejects_files_without_timestamps(self) -> None:
        with self.assertRaises(SubtitleError):
            to_webvtt("esto no es un subtítulo")

    def test_decodes_windows_1252_when_utf8_fails(self) -> None:
        raw = "Canción: ¿dónde?".encode("cp1252")
        self.assertEqual(decode_text(raw), "Canción: ¿dónde?")

    def test_labels_are_safe_for_filenames(self) -> None:
        self.assertEqual(safe_label("Español Latino"), "Español_Latino")
        self.assertEqual(safe_label("../../etc/passwd"), "etc_passwd")
        self.assertEqual(safe_label("   "), "subtitulo")
        self.assertLessEqual(len(safe_label("x" * 500)), 60)

    def test_srclang_hints(self) -> None:
        self.assertEqual(srclang_for("Español Latino"), "es")
        self.assertEqual(srclang_for("Japonés (VO)"), "ja")
        self.assertEqual(srclang_for("Klingon"), "und")

    def test_save_and_list_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            name = save_subtitle(base, 7, SRT, "Español")
            self.assertEqual(name, "Español.vtt")
            save_subtitle(base, 7, SRT, "Inglés")
            tracks = list_subtitles(base, 7)
            self.assertEqual([t["label"] for t in tracks], ["Español", "Inglés"])
            self.assertTrue(tracks[0]["default"])
            self.assertFalse(tracks[1]["default"])
            self.assertEqual(tracks[0]["srclang"], "es")
            self.assertEqual(list_subtitles(base, 999), [])

    def test_size_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(SubtitleError):
            save_subtitle(Path(tmp), 1, b"-->" + b"x" * (MAX_SUBTITLE_BYTES + 1), "Español")


class AvatarTests(unittest.TestCase):
    def test_detects_real_image_types_by_signature(self) -> None:
        self.assertEqual(detect_image_ext(PNG), "png")
        self.assertEqual(detect_image_ext(JPG), "jpg")
        self.assertEqual(detect_image_ext(WEBP), "webp")

    def test_renamed_executable_is_rejected(self) -> None:
        with self.assertRaises(AvatarError):
            detect_image_ext(b"MZ\x90\x00" + b"\x00" * 60)  # cabecera de .exe con extensión .png

    def test_save_replace_and_delete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = save_avatar(root, "Ana", PNG)
            self.assertTrue(first.startswith("avatars/ana-") and first.endswith(".png"))
            self.assertTrue((root / first).is_file())
            second = save_avatar(root, "Ana", JPG)
            delete_avatar(root, first)
            self.assertFalse((root / first).exists())
            self.assertTrue((root / second).is_file())

    def test_delete_refuses_paths_outside_avatars(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "secreto.txt"
            outside.write_text("no me borres")
            delete_avatar(root, "../secreto.txt")
            self.assertTrue(outside.exists())

    def test_size_limit_and_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(AvatarError):
                save_avatar(Path(tmp), "ana", PNG + b"x" * (2 * 1024 * 1024))
            with self.assertRaises(AvatarError):
                save_avatar(Path(tmp), "ana", b"")

    def test_public_urls(self) -> None:
        self.assertEqual(avatar_url("avatars/ana-1.png"), "/avatars/ana-1.png")
        self.assertEqual(avatar_url("https://cdn.example/x.png"), "https://cdn.example/x.png")
        self.assertIsNone(avatar_url("../etc/passwd"))
        self.assertIsNone(avatar_url(None))


if __name__ == "__main__":
    unittest.main()
