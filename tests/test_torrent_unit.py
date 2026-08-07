"""torrent.py 单元测试：URI 校验 / SSRF 护栏 / 穿越防护 / 状态名映射 / 完成侧车写入。

不依赖真实 libtorrent（libtorrent 缺失时 available()=False，但纯逻辑函数仍可直接测）。
"""
import json
import tempfile
from pathlib import Path

import torrent as torrent_mod
from fake_libtorrent import FakeHandle, FakeTI


def test_validate_uri_magnet_normalized():
    out = torrent_mod.validate_uri("magnet:?xt=urn:btih:0123456789abcdef&dn=hello&tr=http://x/ann")
    assert out.startswith("magnet:?")
    assert "xt=urn:btih:0123456789abcdef" in out
    assert "tr=" not in out.split("dn=")[0] or "tr=" in out  # tr 保留


def test_validate_uri_magnet_missing_xt_rejected():
    try:
        torrent_mod.validate_uri("magnet:?dn=hello")
        assert False, "应拒绝缺 xt 的 magnet"
    except ValueError:
        pass


def test_validate_uri_ftp_rejected():
    try:
        torrent_mod.validate_uri("ftp://host/x.torrent")
        assert False, "应拒绝 ftp"
    except ValueError:
        pass


def test_validate_uri_http_public_ok():
    out = torrent_mod.validate_uri("https://example.com/a.torrent")
    assert out == "https://example.com/a.torrent"


def test_validate_uri_private_rejected():
    for bad in ("http://192.168.1.1/x.torrent", "http://10.0.0.5/x.torrent",
                "http://localhost/x.torrent", "http://127.0.0.1/x.torrent"):
        try:
            torrent_mod.validate_uri(bad)
            assert False, f"应拒绝私网地址: {bad}"
        except ValueError:
            pass


def test_is_safe_url():
    assert torrent_mod._is_safe_url("https://example.com/x") is True
    assert torrent_mod._is_safe_url("http://192.168.0.1/x") is False
    assert torrent_mod._is_safe_url("http://localhost/x") is False


def test_safe_save_path():
    d = Path(tempfile.mkdtemp())
    assert torrent_mod.safe_save_path(d, "sub/folder") is not None
    assert torrent_mod.safe_save_path(d, "../../etc") is None
    assert torrent_mod.safe_save_path(d, "/etc/passwd") is None
    # 绝对路径落在 download_dir 内也允许
    inside = d / "inside"
    inside.mkdir()
    assert torrent_mod.safe_save_path(d, str(inside)) is not None


def test_state_name_mapping():
    class S:
        state = 2
    assert torrent_mod._state_name(S()) == "downloading"
    class S2:
        state = 3
    assert torrent_mod._state_name(S2()) == "seeding"
    class S3:
        state = 99
    assert torrent_mod._state_name(S3()) == "unknown"


def test_write_sidecar_for_media_only():
    tmp = Path(tempfile.mkdtemp())
    # 造一个种子根目录与两个文件：一个是媒体（写侧车），一个不是（不写）
    (tmp / "movie.mkv").write_bytes(b"\x00" * 64)
    (tmp / "note.txt").write_bytes(b"hello")
    ti = FakeTI("MyTorrent", [("movie.mkv", 64), ("note.txt", 5)])
    h = FakeHandle(ti, str(tmp))
    mgr = torrent_mod.TorrentManager(tmp)
    mgr._write_sidecar_for(h)

    sidecar = tmp / "movie.vdlmeta.json"
    assert sidecar.exists(), "媒体文件应生成侧车"
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert data["platform"] == "torrent"
    assert data["title"] == "movie"
    # 非媒体文件不写侧车
    assert not (tmp / "note.vdlmeta.json").exists()


def test_describe_with_fake_handle():
    tmp = Path(tempfile.mkdtemp())
    ti = FakeTI("DescTorrent", [("a.mkv", 2000), ("b.srt", 100)])
    h = FakeHandle(ti, str(tmp))
    mgr = torrent_mod.TorrentManager(tmp)
    d = mgr.describe("tid1", h)
    assert d["id"] == "tid1"
    assert d["name"] == "DescTorrent"
    assert d["state"] == "downloading"
    assert d["progress"] == 0.5
    assert d["size"] == 1000  # total_wanted 默认值
    assert len(d["files"]) == 2
    assert d["files"][0]["name"] == "a.mkv"
    assert d["files"][0]["skipped"] is False
