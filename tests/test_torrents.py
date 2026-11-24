import base64
import hashlib
from pathlib import Path

import pytest

from rascal.torrents.bencode import BencodeError, bdecode, bencode
from rascal.torrents.ingest import InvalidTorrent, inspect_torrent
from rascal.torrents.magnet import InvalidMagnet, parse_magnet

from conftest import make_torrent

HEX = "dd8255ecdc7ca55fb0bbf81323d87062db1f6d1c"


def test_bencode_roundtrip_and_info_span() -> None:
    data = bencode({"a": 1, "info": {"name": "x", "list": [1, b"y"]}, "z": -5})
    decoded = bdecode(data)
    assert decoded.value[b"a"] == 1 and decoded.value[b"z"] == -5
    start, end = decoded.info_span  # type: ignore[misc]
    assert data[start:end] == bencode({"name": "x", "list": [1, b"y"]})


@pytest.mark.parametrize("bad", [b"i03e", b"i-0e", b"ie", b"5:abc", b"d1:ai1e", b"i1ei2e", b"x", b"d i1e e"])
def test_bencode_rejects_malformed(bad: bytes) -> None:
    with pytest.raises(BencodeError):
        bdecode(bad)


def test_bencode_rejects_deep_nesting() -> None:
    with pytest.raises(BencodeError):
        bdecode(b"l" * 200 + b"e" * 200)


def test_inspect_multifile_picks_largest_video_not_sample() -> None:
    files = {
        "Movie/sample.mkv": b"s" * 50_000,
        "Movie/movie.mkv": b"m" * 40_000,
        "Movie/subs.srt": b"1\n00:00\nhola\n",
    }
    data = make_torrent(files, name="Pack")
    meta = inspect_torrent(data)
    info = bdecode(data).value[b"info"]  # type: ignore[index]
    assert meta.info_hash_v1 == hashlib.sha1(bencode(info)).hexdigest()  # noqa: S324
    primary = meta.primary_video()
    assert primary is not None and primary.path == "Pack/Movie/movie.mkv" and primary.index == 1
    assert primary.container == "mkv"
    assert meta.trackers == ("udp://tracker.example:1337/announce",)


def test_inspect_single_file() -> None:
    meta = inspect_torrent(make_torrent({"film.mp4": b"v" * 30_000}))
    assert meta.files[0].path == "film.mp4" and meta.primary_video().container == "mp4"  # type: ignore[union-attr]


def test_inspect_rejects_path_traversal() -> None:
    data = make_torrent({"ok/../../etc/passwd.mp4": b"x" * 100})
    with pytest.raises(InvalidTorrent, match="peligrosa"):
        inspect_torrent(data)


def test_inspect_rejects_piece_count_mismatch() -> None:
    info = {"name": "a.mp4", "piece length": 16384, "length": 100_000, "pieces": b"\x00" * 20}
    with pytest.raises(InvalidTorrent, match="piezas"):
        inspect_torrent(bencode({"info": info}))


def test_inspect_rejects_garbage_and_size() -> None:
    with pytest.raises(InvalidTorrent):
        inspect_torrent(b"not a torrent")
    with pytest.raises(InvalidTorrent, match="máximo"):
        inspect_torrent(b"d" + b"0" * 100, max_bytes=10)


def test_no_video_files() -> None:
    assert inspect_torrent(make_torrent({"docs/readme.txt": b"hola"}, name="X")).primary_video() is None


def test_interop_with_libtorrent_hybrid(tmp_path: Path) -> None:
    lt = pytest.importorskip("libtorrent")
    content = tmp_path / "Pack"
    content.mkdir()
    (content / "a.mkv").write_bytes(b"a" * 70_000)
    (content / "b.srt").write_bytes(b"b" * 1_000)
    if hasattr(lt, "list_files"):  # API 2.1
        creator = lt.create_torrent(lt.list_files(str(content)), 16384)
    else:
        fs = lt.file_storage()
        lt.add_files(fs, str(content))
        creator = lt.create_torrent(fs, 16384)
    # por defecto libtorrent 2.x crea torrents híbridos v1+v2
    lt.set_piece_hashes(creator, str(tmp_path))
    data = lt.bencode(creator.generate())

    meta = inspect_torrent(data)
    ti = lt.torrent_info(lt.bdecode(data))
    hashes = ti.info_hashes()
    assert meta.info_hash_v1 == str(hashes.v1)
    assert meta.info_hash_v2 == str(hashes.v2)
    lt_files = ti.layout() if hasattr(ti, "layout") else ti.files()
    for f in meta.files:  # índices y rutas idénticos a los de libtorrent (incluye pad files)
        assert lt_files.file_path(f.index) == f.path.replace("/", lt_files.file_path(0)[len("Pack")])
        assert lt_files.file_size(f.index) == f.size


def test_magnet_hex_base32_and_canonical() -> None:
    m = parse_magnet(f"magnet:?xt=urn:btih:{HEX.upper()}&dn=Big+Buck+Bunny&tr=udp%3A%2F%2Fexplodie.org%3A6969&tr=javascript:alert(1)")
    assert m.info_hash == HEX and m.display_name == "Big Buck Bunny"
    assert m.trackers == ("udp://explodie.org:6969",)  # esquema no permitido descartado
    b32 = base64.b32encode(bytes.fromhex(HEX)).decode()
    assert parse_magnet(f"magnet:?xt=urn:btih:{b32}").info_hash == HEX
    again = parse_magnet(m.canonical())
    assert again == m


def test_magnet_v2_and_hybrid() -> None:
    v2 = "a" * 64
    only_v2 = parse_magnet(f"magnet:?xt=urn:btmh:1220{v2}")
    assert only_v2.info_hash == v2 and only_v2.info_hash_v1 is None
    hybrid = parse_magnet(f"magnet:?xt=urn:btih:{HEX}&xt=urn:btmh:1220{v2}")
    assert hybrid.info_hash == HEX and hybrid.info_hash_v2 == v2


@pytest.mark.parametrize(
    "uri",
    [
        "http://example.com",
        "magnet:?dn=sin-hash",
        "magnet:?xt=urn:btih:1234",
        f"magnet:?xt=urn:btih:{HEX}&xt=urn:btih:{'0' * 40}",
        "magnet:?xt=urn:btmh:1111" + "a" * 64,
    ],
)
def test_magnet_invalid(uri: str) -> None:
    with pytest.raises(InvalidMagnet):
        parse_magnet(uri)
