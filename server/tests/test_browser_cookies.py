"""browser_cookies 单元测试（离线，不碰真实浏览器数据）。

为什么单独测这个模块
--------------------
`browser_cookies` 是「自动读取浏览器登录态」的唯一入口，一旦它在某个环节
静默失败，上层只会看到「未检测到登录态」——用户会以为是自己没登录，
实际是解密/枚举/挑 profile 出了问题。这类故障**没有异常、没有日志**，
只能靠用例把每一环的契约钉死：

- 域名匹配（带前导点 / 子域 / CDN 域）
- 拼 Cookie 头（非 ASCII 与控制字符必须剔除，否则整份请求头发不出去）
- Windows 解密（AES-GCM v10/v11；Chromium 130+ 明文前的 32 字节
  SHA256(host_key) 必须剥掉；v20/ABE 必须如实报原因而不是当成「没登录」）
- Profile 枚举（新路径 `Network/Cookies` 与旧路径 `Cookies` 都要收）
- 来源排序（**可读优先**，其次**正在运行优先**，最后比 Cookie 数）
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from unittest import mock

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import browser_cookies as bc  # noqa: E402


# ——————————————————————————————————————————————————————————————
# 测试替身
# ——————————————————————————————————————————————————————————————

def _chromium_db(path: Path, rows, *, legacy: bool = False) -> str:
    """造一个最小可用的 Chromium Cookies 库，返回 db 路径。"""
    if legacy:
        db = path / "Cookies"
    else:
        db = path / "Network" / "Cookies"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db))
    try:
        con.execute(
            "CREATE TABLE cookies (host_key TEXT, name TEXT, encrypted_value BLOB,"
            " value TEXT, path TEXT, is_secure INTEGER)"
        )
        con.executemany(
            "INSERT INTO cookies VALUES (?,?,?,?,?,?)",
            [(h, n, enc, plain, "/", sec) for (h, n, enc, plain, sec) in rows],
        )
        con.commit()
    finally:
        con.close()
    return str(db)


def _src(key: str, root: Path, procs=("fake.exe",)) -> dict:
    return {
        "key": key, "kind": "chromium", "label": key.upper(),
        "procs": tuple(procs), "roots": (str(root),),
    }


def _encrypt(value: str, key: bytes, host_key: str = "") -> bytes:
    """按 Chromium 的格式造一条 v10 密文（可选带上 32 字节 host 前缀）。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    import hashlib

    plain = value.encode("utf-8")
    if host_key:
        plain = hashlib.sha256(host_key.encode("utf-8")).digest() + plain
    nonce = b"0" * 12
    ct = AESGCM(key).encrypt(nonce, plain, None)  # 密文尾 16 字节即 tag
    return b"v10" + nonce + ct


# ——————————————————————————————————————————————————————————————
# 域名匹配
# ——————————————————————————————————————————————————————————————

@pytest.mark.parametrize(
    "host, domain, want",
    [
        ("pan.baidu.com", "baidu.com", True),
        (".baidu.com", "baidu.com", True),
        ("BAIDU.COM", "baidu.com", True),
        ("baidu.com", "baidu.com", True),
        ("notbaidu.com", "baidu.com", False),      # 后缀但非子域，不能误命中
        ("baidu.com.evil.cn", "baidu.com", False),
        ("upos-sz-mirror.bilivideo.cn", "bilibili.com", False),
    ],
)
def test_domain_match(host, domain, want):
    assert bc._domain_match(host, [domain]) is want


def test_domain_match_multi_domains():
    assert bc._domain_match("upos.bilivideo.cn", ["bilibili.com", "bilivideo.cn"]) is True


# ——————————————————————————————————————————————————————————————
# 拼 Cookie 头
# ——————————————————————————————————————————————————————————————

def test_build_cookie_header_drops_illegal_values():
    """含控制字符/非 ASCII 的单条 Cookie 只能丢这一条，不能污染整份头。

    实测踩过：UC 解密出的个别值残留非法字节，带上后 requests 抛
    InvalidHeader，整条下载链路直接失败。
    """
    rows = [
        {"name": "BDUSS", "value": "good-value"},
        {"name": "STOKEN", "value": "bad\x01value"},
        {"name": "PAC", "value": "中文乱码"},
        {"name": "", "value": "no-name"},
    ]
    header = bc.build_cookie_header(rows)
    assert header == "BDUSS=good-value"


def test_build_cookie_header_keeps_empty_value():
    """空值是合法 Cookie（有些站用它表示「已清空」），不该整条丢掉。"""
    assert bc.build_cookie_header([{"name": "A", "value": ""}]) == "A="


def test_build_cookie_header_last_wins():
    rows = [{"name": "A", "value": "1"}, {"name": "A", "value": "2"}]
    assert bc.build_cookie_header(rows) == "A=2"


def test_build_cookie_header_none_when_nothing_valid():
    assert bc.build_cookie_header([]) is None
    assert bc.build_cookie_header([{"name": "X", "value": "\x00"}]) is None


# ——————————————————————————————————————————————————————————————
# 解密
# ——————————————————————————————————————————————————————————————

def test_decrypt_value_strips_chromium130_host_prefix():
    """Chromium 130+ 在明文前附 32 字节 SHA256(host_key)，必须剥掉。

    不剥的直接后果：拼出的 Cookie 头以乱码开头，httpx 抛 InvalidHeader
    （2026-10-24 实测：UC 解出的 BDUSS 前面就挂着这 32 字节）。
    """
    pytest.importorskip("cryptography")
    key = b"k" * 16
    enc = _encrypt("hello-world", key, host_key=".pan.baidu.com")
    val, reason = bc.decrypt_value(enc, key, ".pan.baidu.com")
    assert reason == ""
    assert val == "hello-world"


def test_decrypt_value_without_host_prefix_is_unchanged():
    pytest.importorskip("cryptography")
    key = b"k" * 16
    enc = _encrypt("hello-world", key)  # 老版本：明文前没有哈希
    val, reason = bc.decrypt_value(enc, key, ".pan.baidu.com")
    assert (val, reason) == ("hello-world", "")


def test_decrypt_value_wrong_key_reports_reason():
    pytest.importorskip("cryptography")
    enc = _encrypt("hello", b"k" * 16, host_key=".a.com")
    val, reason = bc.decrypt_value(enc, b"z" * 16, ".a.com")
    assert val is None and reason == "decrypt_failed"


def test_decrypt_value_missing_key_is_not_plain_failure():
    val, reason = bc.decrypt_value(b"v10" + b"\x00" * 40, None)
    assert val is None and reason == "key_missing"


def test_decrypt_value_v20_reports_abe():
    """v20/ABE 必须单独报：这是「浏览器不允许第三方解」，
    和「你没登录」是两件事，提示文案要能区分。"""
    val, reason = bc.decrypt_value(b"v20" + b"\x00" * 40, b"k" * 16)
    assert val is None and reason == "abe_v20"


def test_decrypt_value_plaintext_passthrough():
    assert bc.decrypt_value(b"raw-value", None) == ("raw-value", "")


# ——————————————————————————————————————————————————————————————
# Profile 枚举 / 读取
# ——————————————————————————————————————————————————————————————

def test_iter_profiles_collects_network_and_legacy(tmp_path):
    _chromium_db(tmp_path / "Default", [])
    _chromium_db(tmp_path / "Profile 1", [], legacy=True)
    got = bc.iter_profiles(_src("fake", tmp_path))
    names = {p for p, _ in got}
    assert names == {"Default", "Profile 1"}
    assert len(got) == 2


def test_iter_profiles_missing_root_is_empty(tmp_path):
    assert bc.iter_profiles(_src("fake", tmp_path / "nope")) == []


def test_read_cookies_filters_by_domain(tmp_path):
    db = _chromium_db(
        tmp_path / "Default",
        [
            ("pan.baidu.com", "BDUSS", None, "plain-bduss", 1),
            ("pcs.baidu.com", "STOKEN", None, "plain-stoken", 1),
            ("example.com", "OTHER", None, "nope", 0),
        ],
    )
    rows, reason = bc.read_cookies(db, "chromium", ["baidu.com"])
    assert reason == ""
    assert {r["name"] for r in rows} == {"BDUSS", "STOKEN"}
    assert bc.build_cookie_header(rows) is not None


def test_read_cookies_decrypts_encrypted_rows(tmp_path):
    pytest.importorskip("cryptography")
    key = b"k" * 16
    db = _chromium_db(
        tmp_path / "Default",
        [("pan.baidu.com", "BDUSS", _encrypt("s3cret", key, "pan.baidu.com"), None, 1)],
    )
    local_state = tmp_path / "Local State"
    local_state.write_text('{"os_crypt": {}}', encoding="utf-8")
    with mock.patch.object(bc, "master_key", return_value=key):
        rows, reason = bc.read_cookies(db, "chromium", ["baidu.com"])
    assert reason == ""
    assert rows and rows[0]["value"] == "s3cret"


def test_open_db_negative_cache_avoids_repeated_timeout(tmp_path):
    """打不开的库要记负缓存。

    一次探测会走 find_source + locked_browsers 两条路径，把同一个被独占锁的
    库试两遍（Edge 实测每遍 ~1.4s×2），接口就会卡住好几秒。第二次必须命中
    缓存直接返回 locked，而不是再等一次 sqlite 超时。
    """
    db = tmp_path / "Default" / "Cookies"
    db.mkdir(parents=True)  # 目录冒充库文件：必然打不开
    bc._LOCKED_CACHE.clear()
    try:
        assert bc._open_db(str(db)) == (None, None, True)
        # 换成真库也不该立刻放开——缓存命中期间保持 locked
        db.rmdir()
        _chromium_db(tmp_path / "Default", [], legacy=True)  # 真库落在同一条路径上
        assert bc._open_db(str(db)) == (None, None, True)
        bc._LOCKED_CACHE.clear()
        con, tmp, locked = bc._open_db(str(db))
        assert locked is False
        bc._close_db(con, tmp)
    finally:
        bc._LOCKED_CACHE.clear()


# ——————————————————————————————————————————————————————————————
# 来源选择
# ——————————————————————————————————————————————————————————————

def test_find_source_prefers_running_browser(tmp_path):
    """正在用的浏览器优先：用户此刻登录的会话才有意义。"""
    a, b = tmp_path / "a", tmp_path / "b"
    _chromium_db(a, [("pan.baidu.com", "A", None, "1", 1)])
    _chromium_db(
        b,
        [("pan.baidu.com", f"C{i}", None, str(i), 1) for i in range(5)],
    )
    with mock.patch.object(bc, "_BROWSER_SOURCES", (_src("idle", a), _src("busy", b))), \
         mock.patch.object(bc, "is_browser_running", lambda s: s["key"] == "busy"):
        src = bc.find_source(["baidu.com"])
    assert src and src["browser"] == "busy" and src["running"] is True


def test_find_source_readable_beats_running_but_locked(tmp_path):
    """读不到 ≠ 优先：开着但库被锁的浏览器拿不到 Cookie，不能因为「在用」就选它。"""
    good, locked = tmp_path / "good", tmp_path / "locked"
    _chromium_db(good, [("pan.baidu.com", "A", None, "1", 1)])
    _chromium_db(locked, [("pan.baidu.com", "B", None, "2", 1)])
    real_count = bc.count_cookies

    def fake_count(db, kind, domains):
        return -1 if str(locked) in db else real_count(db, kind, domains)

    with mock.patch.object(bc, "_BROWSER_SOURCES", (_src("open", locked), _src("closed", good))), \
         mock.patch.object(bc, "is_browser_running", lambda s: s["key"] == "open"), \
         mock.patch.object(bc, "count_cookies", fake_count):
        src = bc.find_source(["baidu.com"])
    assert src and src["browser"] == "closed"


def test_find_source_returns_none_without_domains(tmp_path):
    _chromium_db(tmp_path / "Default", [("pan.baidu.com", "A", None, "1", 1)])
    with mock.patch.object(bc, "_BROWSER_SOURCES", (_src("fake", tmp_path),)):
        assert bc.find_source([]) is None


def test_locked_browsers_reports_only_locked(tmp_path):
    _chromium_db(tmp_path / "Default", [])
    with mock.patch.object(bc, "_BROWSER_SOURCES", (_src("fake", tmp_path),)), \
         mock.patch.object(bc, "is_browser_running", return_value=True), \
         mock.patch.object(bc, "_open_db", return_value=(None, None, True)):
        locked = bc.locked_browsers()
    assert [x["browser"] for x in locked] == ["fake"]


def test_locked_browsers_ignores_closed_browser(tmp_path):
    _chromium_db(tmp_path / "Default", [])
    with mock.patch.object(bc, "_BROWSER_SOURCES", (_src("fake", tmp_path),)), \
         mock.patch.object(bc, "is_browser_running", return_value=False), \
         mock.patch.object(bc, "_open_db", return_value=(None, None, True)):
        assert bc.locked_browsers() == []


def test_list_installed_browsers_reports_running_flag(tmp_path):
    _chromium_db(tmp_path / "Default", [])
    with mock.patch.object(bc, "_BROWSER_SOURCES", (_src("fake", tmp_path),)), \
         mock.patch.object(bc, "is_browser_running", return_value=True):
        got = bc.list_installed_browsers()
    assert got == [{"browser": "fake", "label": "FAKE", "running": True, "profiles": 1}]
