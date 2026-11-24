import asyncio
from pathlib import Path

import pytest

from rascal.data.enums import PlaybackMode
from rascal.domain.playback import decide_playback, guess_mode_from_container
from rascal.streaming.http_range import ByteRange, RangeNotSatisfiable, parse_range
from rascal.streaming.media import build_ffmpeg_command, sniff_container
from rascal.streaming.pieces import FileSpan, PieceTimeout, stream_file_range

from conftest import HAS_FFMPEG, make_video


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, None),
        ("bytes=0-99", ByteRange(0, 99)),
        ("bytes=100-", ByteRange(100, 999)),
        ("bytes=-50", ByteRange(950, 999)),
        ("bytes=-5000", ByteRange(0, 999)),
        ("bytes=900-5000", ByteRange(900, 999)),
        ("bytes=0-1,5-9", ByteRange(0, 1)),
        ("items=0-1", None),
        ("bytes=abc", None),
        ("bytes=9-3", None),
    ],
)
def test_parse_range(header: str | None, expected: ByteRange | None) -> None:
    assert parse_range(header, 1000) == expected


@pytest.mark.parametrize("header", ["bytes=1000-", "bytes=-0", "bytes=5000-6000"])
def test_parse_range_unsatisfiable(header: str) -> None:
    with pytest.raises(RangeNotSatisfiable):
        parse_range(header, 1000)


def test_file_span_math() -> None:
    span = FileSpan(offset=100, size=1000, piece_length=64)  # archivo empieza a mitad de la pieza 1
    assert span.piece_at(0) == 1 and span.first_piece == 1
    assert span.last_piece == (100 + 999) // 64
    assert span.piece_end_in_file(1) == 27  # bytes 0..27 del archivo están en la pieza 1
    assert list(span.pieces_for(0, 27)) == [1]
    assert list(span.pieces_for(27, 28)) == [1, 2]
    assert span.head_pieces(64)[0] == 1 and span.tail_pieces(10)[-1] == span.last_piece


class SimulatedPieces:
    """Piezas que "llegan" progresivamente (como una descarga real)."""

    def __init__(self, total: int) -> None:
        self.available: set[int] = set()
        self.urgent_requests: list[int] = []
        self.total = total

    def have_piece(self, piece: int) -> bool:
        return piece in self.available

    def prioritize(self, pieces, *, urgent: bool) -> None:  # type: ignore[no-untyped-def]
        if urgent:
            self.urgent_requests.extend(pieces)


def test_stream_waits_for_pieces_then_reads(tmp_path: Path) -> None:
    payload = bytes(range(256)) * 40  # 10 240 bytes
    path = tmp_path / "f.bin"
    path.write_bytes(payload)
    span = FileSpan(offset=0, size=len(payload), piece_length=1024)
    source = SimulatedPieces(total=10)

    async def run() -> bytes:
        async def deliver() -> None:
            for piece in [9, 8, 2, 3, 4, 5, 6, 7]:  # llegan desordenadas, como con deadlines
                await asyncio.sleep(0.01)
                source.available.add(piece)

        task = asyncio.create_task(deliver())
        chunks = [c async for c in stream_file_range(path, span, source, 2500, 9999, chunk_size=700,
                                                     readahead_bytes=4096, timeout=5)]
        await task
        return b"".join(chunks)

    assert asyncio.run(run()) == payload[2500:10000]
    assert 2 in source.urgent_requests  # se pidió con urgencia la pieza que se estaba esperando


def test_stream_times_out_when_piece_never_arrives(tmp_path: Path) -> None:
    path = tmp_path / "f.bin"
    path.write_bytes(b"x" * 4096)
    span = FileSpan(offset=0, size=4096, piece_length=1024)

    async def run() -> None:
        async for _ in stream_file_range(path, span, SimulatedPieces(4), 0, 4095, chunk_size=1024,
                                         readahead_bytes=1024, timeout=0.2):
            pass

    with pytest.raises(PieceTimeout):
        asyncio.run(run())


@pytest.mark.parametrize(
    ("container", "video", "audio", "pix", "expected"),
    [
        ("mp4", "h264", "aac", "yuv420p", PlaybackMode.DIRECT),
        ("webm", "vp9", "opus", "yuv420p", PlaybackMode.DIRECT),
        ("mkv", "h264", "aac", "yuv420p", PlaybackMode.REMUX),
        ("mkv", "h264", "ac3", "yuv420p", PlaybackMode.REMUX_AUDIO),
        ("mp4", "h264", "aac", "yuv420p10le", PlaybackMode.UNSUPPORTED),
        ("mkv", "hevc", "aac", "yuv420p", PlaybackMode.UNSUPPORTED),
    ],
)
def test_decide_playback(container: str, video: str, audio: str, pix: str, expected: PlaybackMode) -> None:
    assert decide_playback(container, video, audio, pix).mode is expected


def test_transcode_only_when_allowed() -> None:
    assert decide_playback("mkv", "hevc", "aac", allow_transcode=True).mode is PlaybackMode.TRANSCODE
    assert guess_mode_from_container("mkv") is PlaybackMode.REMUX
    assert guess_mode_from_container(None) is PlaybackMode.UNKNOWN


def test_ffmpeg_command_shapes(settings) -> None:  # type: ignore[no-untyped-def]
    remux = build_ffmpeg_command(settings, "http://x/raw", PlaybackMode.REMUX, start_s=12.5)
    assert remux[remux.index("-ss") + 1] == "12.500" and remux.index("-ss") < remux.index("-i")
    assert "copy" in remux and "frag_keyframe+empty_moov+default_base_moof" in remux
    audio = build_ffmpeg_command(settings, "http://x/raw", PlaybackMode.REMUX_AUDIO)
    assert audio[audio.index("-c:a") + 1] == "aac"
    with pytest.raises(ValueError):
        build_ffmpeg_command(settings, "http://x/raw", PlaybackMode.DIRECT)


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg no disponible")
def test_sniff_real_containers(tmp_path: Path) -> None:
    mp4 = make_video(tmp_path / "a.mp4", seconds=1)
    mkv = make_video(tmp_path / "a.mkv", seconds=1)
    webm = tmp_path / "a.webm"
    import subprocess

    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10",
                    "-t", "1", "-c:v", "libvpx-vp9", str(webm)], check=True)
    assert sniff_container(mp4.read_bytes()[:512]) == "mp4"
    assert sniff_container(mkv.read_bytes()[:512]) == "mkv"
    assert sniff_container(webm.read_bytes()[:512]) == "webm"
    assert sniff_container(b"garbage" * 100) is None
