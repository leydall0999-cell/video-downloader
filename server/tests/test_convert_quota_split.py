#!/usr/bin/env python
"""音乐转换 / 图片转换「独立计次」+ 个人中心使用统计补全（web-dev，2026-10-19）

背景（用户反馈）
----------------
个人中心「使用统计」只列了 8 行（下载视频、在线转码、在线拼接、图片/PDF 去水印、
字幕提取/烧录/翻译），网页版还有 音乐转换、图片转换 两个入口没进表 ——
它们此前与「在线转码」共用 cloud_convert 一个计数器（前端两条入口都走
/api/upload-convert 与 upload-chunk/finish 的同一个闸门），所以表里根本看不到。

按用户定档「补显示，次数向 app 版靠齐，app 版是什么样网页就什么样」：
拆成独立键 convert_audio / convert_image（与桌面端同名同档：免费 5 次/日、
会员 200 次/日），计数点由 routers/convert.py 的 _convert_resource() 按
目标格式分派。

本文件守卫 5 件事
-----------------
1. `_convert_resource()` 的分派矩阵（音频目标 → convert_audio；图片目标或
   is_image=True → convert_image；视频容器 / gif → cloud_convert）；
2. 表里两行的 free_limit / member_limit 与 FREE_DAILY_LIMITS / DAILY_QUOTA_LIMITS
   **完全一致**（否则「体验剩余」列与实际拦截打架）；
3. `feature_usage_status()` 真的多出这两行（用户看得到）；
4. `quota_state()` 真能拦住（不在这两张表里 ⇒ 未知资源 fail-open，等于没限）；
5. 源码级接线：三个云端转码闸门都必须走 `_convert_resource`，不得再写死
   `resource="cloud_convert"`（写死就等于又把音乐/图片并回在线转码）。
"""
import os
import sys
from pathlib import Path

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

os.environ.setdefault("VDL_DATA_DIR", "/tmp/vdl_test_convert_split")
os.environ.setdefault("VDL_CLOUD_LINK", "0")
os.environ.setdefault("VDL_OFFLINE_TESTS", "1")
for _k in ("VDL_QUOTA_RELAY_URL", "VDL_WORKER_URL"):
    os.environ.pop(_k, None)

from membership import (  # noqa: E402
    FEATURE_USAGE_DEFS, FREE_DAILY_LIMITS, DAILY_QUOTA_LIMITS,
    MembershipStore, feature_usage_status, download_benefits,
)
import app as _app_mod  # noqa: E402,F401  必须先加载 app（它自己 include_router），
# 否则直接 import routers.convert 会撞上「partially initialized module」循环导入。
from routers.convert import _convert_resource  # noqa: E402


def _row(key: str) -> dict:
    for d in FEATURE_USAGE_DEFS:
        if d.get("key") == key:
            return d
    raise AssertionError(f"FEATURE_USAGE_DEFS 缺少 {key} 行 —— 统计表又少一项")


# --------------------------------------------------------------------------- #
# 1. 分派矩阵
# --------------------------------------------------------------------------- #
def test_dispatch_audio_targets():
    for t in ("mp3", "m4a", "wav", "flac", "aac", "opus", "wma", "mp2"):
        assert _convert_resource(t) == "convert_audio", t


def test_dispatch_image_targets():
    for t in ("png", "jpg", "jpeg", "webp", "bmp", "tiff"):
        assert _convert_resource(t) == "convert_image", t
    # 显式 is_image（分片上传的图片转换走这个入参）优先于扩展名判断
    assert _convert_resource("mp4", True) == "convert_image"


def test_dispatch_video_targets_stay_cloud_convert():
    for t in ("mp4", "mov", "mkv", "webm", "avi", "flv", "ts", "hevc", "gif"):
        assert _convert_resource(t) == "cloud_convert", t


def test_dispatch_is_case_and_space_insensitive():
    assert _convert_resource("  MP3 ") == "convert_audio"
    assert _convert_resource(None) == "cloud_convert"


# --------------------------------------------------------------------------- #
# 2. 表与额度一致
# --------------------------------------------------------------------------- #
def test_defs_match_quota_tables():
    for key, resource in (("convert_audio", "convert_audio"),
                          ("convert_image", "convert_image")):
        row = _row(key)
        assert row["resource"] == resource
        assert row["free_limit"] == FREE_DAILY_LIMITS[resource], key
        assert row["member_limit"] == DAILY_QUOTA_LIMITS[resource], key


def test_limits_follow_desktop_app():
    """与桌面端同档：免费 5 次/日、会员 200 次/日。"""
    for r in ("convert_audio", "convert_image"):
        assert FREE_DAILY_LIMITS[r] == 5, r
        assert DAILY_QUOTA_LIMITS[r] == 200, r


def test_benefit_copy_exists():
    """有配额就必须有会员权益文案（守卫 test_feature_usage_gate.mjs 同款语义）。"""
    texts = " ".join(x["text"] for x in download_benefits())
    assert "音乐转换 200 次/日" in texts
    assert "图片转换 200 次/日" in texts


# --------------------------------------------------------------------------- #
# 3. 统计表真的多出这两行
# --------------------------------------------------------------------------- #
def test_usage_status_lists_all_web_functions(tmp_path):
    store = MembershipStore(path=Path(tmp_path) / "m.json")
    rows = feature_usage_status(store, "today")
    names = [r["name"] for r in rows]
    for must in ("下载视频", "在线转码", "音乐转换", "图片转换", "在线拼接",
                 "图片去水印", "PDF 去水印", "字幕提取", "字幕烧录", "字幕翻译",
                 # 2026-10-19 同轮补全：分享的两个子页签（详见 test_share_quota_split.py）
                 "生成二维码", "生成网页"):
        assert must in names, f"使用统计缺少「{must}」：{names}"
    assert len(rows) == 12, f"网页版使用统计应为 12 行，实际 {len(rows)}：{names}"


# --------------------------------------------------------------------------- #
# 4. 真能限次（不在配额表里 ⇒ unknown ⇒ fail-open，等于没限）
# --------------------------------------------------------------------------- #
def test_quota_state_actually_limits(tmp_path):
    store = MembershipStore(path=Path(tmp_path) / "q.json")
    for r in ("convert_audio", "convert_image"):
        q = store.quota_state(r)
        assert not q.get("unknown"), f"{r} 不在配额表里，会被 fail-open 放行"
        assert q["limit"] == 5, f"{r} 免费档应为 5 次/日，实际 {q}"
    for _ in range(5):
        assert store.use_daily("convert_audio")["ok"] is True
    over = store.use_daily("convert_audio")
    assert over["ok"] is False, "第 6 次应被拦（免费 5 次/日）"
    # 拆池核心语义：音乐转换用尽不影响图片转换
    assert store.use_daily("convert_image")["ok"] is True


# --------------------------------------------------------------------------- #
# 5. 源码级接线
# --------------------------------------------------------------------------- #
def test_all_convert_gates_use_dispatch():
    src = (Path(_SERVER_DIR) / "routers" / "convert.py").read_text(encoding="utf-8")
    assert src.count("resource=_convert_resource(") == 3, (
        "/api/convert、/api/upload-convert、upload-chunk/finish 三处都必须按目标格式分派")
    # 转码类闸门不得再写死 cloud_convert（拼接是另一条链路，保留 cloud_concat）
    assert 'cloud_quota_gate(request, resource="cloud_convert")' not in src


if __name__ == "__main__":
    import tempfile
    import traceback

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            if fn.__code__.co_argcount == 1:
                fn(tempfile.mkdtemp(prefix="vdl_split_"))
            else:
                fn()
            print(f"  ok  {fn.__name__}")
        except Exception:
            failed += 1
            print(f"  FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} 通过")
    sys.exit(1 if failed else 0)
