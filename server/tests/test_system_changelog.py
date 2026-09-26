"""内置更新日志测试（2026-09-26）。

解决的问题：用户要求「关于本应用」能看到更新内容，但原实现只读线上更新源的
latest.json —— 线上长期停在 v1.0.18，于是本机明明是 v1.0.25，面板却只能显示
1.0.18 那一条说明（用户反馈「没看懂」）。改为随包内置 server/changelog.py，
离线、且不依赖是否对外发布过。

本测试锁住它的契约：
  1. 结构：非空；每条 version 有值；items 非空、无空白项、不超长
  2. 顺序：版本严格降序（新版本往顶部加）
  3. 命中：build_payload(当前版本) 必须 has_current=True —— 漏写日志会让面板
     退回兜底文案
  4. 兜底：未知版本不崩，entries 仍完整返回（前端有 fallback 路径）
  5. 端点：GET /api/system/changelog 免登录可用（未登录也能看更新内容）

运行：cd server && python tests/test_system_changelog.py
"""
import os
import re
import sys

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ROOT_DIR = os.path.dirname(_SERVER_DIR)
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import app as server_app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from changelog import CHANGELOG, build_payload  # noqa: E402

_MAX_ITEM_CHARS = 120


def _client():
    return TestClient(server_app.app)


def _ver_key(v: str) -> tuple:
    """把版本串数值化（'1.0.19–1.0.24' → (1, 0, 19)），用于校验降序。"""
    nums = [int(x) for x in re.findall(r"\d+", str(v))]
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums[:3])


def _repo_version() -> str:
    """仓库根 VERSION。打包态的 Resources/version.txt 由它生成，两者同源。

    不读 /api/system/info 的 current：开发环境没有 version.txt，那边恒为 0.0.0。
    """
    with open(os.path.join(_ROOT_DIR, "VERSION"), encoding="utf-8") as f:
        return f.read().strip()


def test_changelog_structure():
    """每条：版本号有值、条目非空、无空白项、单条不超长。"""
    assert CHANGELOG, "内置更新日志不能为空"
    for row in CHANGELOG:
        ver = str(row.get("version") or "").strip()
        assert ver, "存在没有版本号的条目"
        items = row.get("items") or []
        assert items, f"v{ver} 没有任何更新条目"
        for it in items:
            text = str(it).strip()
            assert text, f"v{ver} 存在空白条目"
            assert len(text) <= _MAX_ITEM_CHARS, (
                f"v{ver} 有超长条目（{len(text)} 字 > {_MAX_ITEM_CHARS}）：{text[:40]}…"
            )
    print(f"✅ 内置更新日志结构合法（{len(CHANGELOG)} 个版本）")


def test_changelog_version_descending():
    keys = [_ver_key(r["version"]) for r in CHANGELOG]
    assert keys == sorted(keys, reverse=True), (
        "CHANGELOG 必须按版本降序排列（新版本加在顶部），当前顺序："
        + " > ".join(str(r["version"]) for r in CHANGELOG)
    )
    print("✅ 版本按降序排列")


def test_current_version_has_entry():
    """当前版本必须在日志里有对应条目，否则「关于」面板只能退回兜底文案。"""
    cur = _repo_version()
    assert cur, "仓库 VERSION 为空"
    payload = build_payload(cur)
    hit = [e for e in payload["entries"] if e["current"]]
    assert payload["has_current"] is True and len(hit) == 1, (
        f"当前版本 v{cur} 在内置更新日志里找不到条目——"
        "新版本请在 server/changelog.py 顶部补一条（version 与 VERSION 一致）"
    )
    assert hit[0]["version"] == cur
    assert hit[0]["items"], f"v{cur} 的条目为空"
    print(f"✅ 当前版本 v{cur} 在内置日志中命中（{len(hit[0]['items'])} 条）")


def test_unknown_version_falls_back():
    """版本对不上（例如刚 bump 还没写日志）不能崩，历史仍要完整返回。"""
    payload = build_payload("99.99.99")
    assert payload["ok"] is True
    assert payload["has_current"] is False
    assert payload["entries"], "未命中当前版本时也应返回完整历史，供前端兜底"
    assert all(e["current"] is False for e in payload["entries"])
    assert build_payload("")["has_current"] is False
    print("✅ 版本未命中时优雅降级（返回完整历史、不崩）")


def test_endpoint_available_without_login():
    """关于面板在未登录时也要能打开 → 该端点不能被登录门禁拦住。"""
    r = _client().get("/api/system/changelog")
    assert r.status_code == 200, f"GET /api/system/changelog 非 200: {r.status_code}"
    data = r.json()
    assert data.get("ok") is True
    assert isinstance(data.get("current"), str), "端点应回传 current 字段"
    entries = data.get("entries")
    assert isinstance(entries, list) and entries, "端点未返回 entries"
    first = entries[0]
    for key in ("version", "date", "items", "current"):
        assert key in first, f"条目缺少字段 {key}"
    assert isinstance(first["items"], list)
    print(f"✅ GET /api/system/changelog 免登录可用（{len(entries)} 个版本）")


if __name__ == "__main__":
    test_changelog_structure()
    test_changelog_version_descending()
    test_current_version_has_entry()
    test_unknown_version_falls_back()
    test_endpoint_available_without_login()
    print("\n🎉 内置更新日志测试全部通过 — 「关于本应用」的更新内容不再依赖线上发布。")
