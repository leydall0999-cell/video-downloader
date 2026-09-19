"""server/tests/test_commentary_precheck.py — 解说「前置可行性闸门」回归测试（2026-09-15）。

背景（用户实测反馈）：
免费用户跑一条超出当前档位能力的片子时，系统会在**转写与脚本全部跑完之后**才
因云端额度不足失败——用户白等十几分钟，拿到的是废片。历史实现只在两个上传入口
做了时长校验，而本地拖拽（stash → script-only）与下载库入口**完全没有闸门**。

修复要求：所有会启动解说任务的入口，都必须在**开始前**判定完「时长上限 + 云端
额度」，不通过就立刻拒绝并给出两条出路（换素材 / 开通会员）。

覆盖：
  precheck_member_always_allowed            会员不受限
  precheck_duration_exceeded_blocks         超时长在开始前就拦住（回归核心）
  precheck_duration_boundary                等于上限放行、超一点即拦
  precheck_unknown_duration_fail_open       时长未知时 fail-open，不误拦
  precheck_local_ready_no_cloud_needed      本机就绪 → 放行且不消耗云端
  precheck_cloud_required_with_quota        需云端但仍有额度 → 放行并提示消耗
  precheck_cloud_exhausted_blocks           需云端且额度为 0 → 开始前拒绝
  precheck_cloud_engine_ignores_daily_auto  显式纯云端不受「每日 auto」限制
  effective_duration_subtracts_trim         预检按裁剪后的真实时长判定
  precheck_or_raise_403_shape               拒绝时返回结构化 403（含 hint/subscribe）
  precheck_endpoint_returns_allowed_flag    HTTP 端点返回 allowed 而非 ok
  precheck_endpoint_honors_ui_engine        端点采信「界面上当前档位」而非已保存配置

设计约束：绝不读写用户真实 `~/.video-downloader` 的配额状态——所有用例都用
临时 base_dir，并把真实的家目录探测（ffprobe / 本机引擎）替换为替身。

运行：
    cd server && python tests/test_commentary_precheck.py
    cd server && python -m pytest tests/test_commentary_precheck.py -v
"""
import os
import shutil
import sys
import tempfile

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import app as server_app  # noqa: E402  先完成 app 初始化，避免 routers 循环导入
from quota import QuotaManager, FREE_MAX_DURATION_SEC, LIFETIME_CLOUD_EVENTS  # noqa: E402
from routers import quota as quota_router  # noqa: E402
from routers import commentary as cm  # noqa: E402


def _mgr(member=False, base=None):
    return QuotaManager(
        base_dir=base or tempfile.mkdtemp(prefix="vdl_precheck_"),
        is_member_fn=lambda: member,
    )


# ── 1. 预检分支 ─────────────────────────────────────────────────────── #
def test_precheck_member_always_allowed():
    q = _mgr(member=True)
    r = q.precheck_commentary(99999, local_engine_ready=False, engine="auto")
    assert r["allowed"] is True and r["code"] == "member"


def test_precheck_duration_exceeded_blocks():
    """核心回归：超时长的片子必须在「开始前」就被拦住。"""
    q = _mgr()
    r = q.precheck_commentary(
        FREE_MAX_DURATION_SEC + 60, local_engine_ready=False, engine="auto"
    )
    assert r["allowed"] is False
    assert r["code"] == "duration_exceeded"
    assert r["reason"], "必须给出可读的拒绝原因"
    assert "分钟" in r["hint"], "提示里要写清时长上限，用户才知道换多长的素材"
    assert r["over_sec"] >= 60


def test_precheck_duration_boundary():
    q = _mgr()
    # 恰好等于上限：放行（边界含等号）
    assert q.precheck_commentary(FREE_MAX_DURATION_SEC, local_engine_ready=True)["allowed"] is True
    # 超一点点就拦
    assert q.precheck_commentary(FREE_MAX_DURATION_SEC + 0.1, local_engine_ready=True)["allowed"] is False


def test_precheck_unknown_duration_fail_open():
    """探测失败（0 / 负数）不得误拦——宁可放过，由任务入口兜底。"""
    q = _mgr()
    assert q.precheck_commentary(0, local_engine_ready=False)["allowed"] is True
    assert q.precheck_commentary(-5, local_engine_ready=False)["allowed"] is True


def test_precheck_local_ready_no_cloud_needed():
    q = _mgr()
    r = q.precheck_commentary(600, local_engine_ready=True, engine="auto")
    assert r["allowed"] is True
    assert r["will_use_cloud"] is False, "本机就绪时不该消耗云端额度"


def test_precheck_cloud_required_with_quota():
    q = _mgr()
    r = q.precheck_commentary(600, local_engine_ready=False, engine="auto")
    assert r["allowed"] is True
    assert r["will_use_cloud"] is True
    assert r["hint"], "要消耗额度时必须提前告知用户"


def test_precheck_cloud_hint_distinguishes_explicit_choice():
    """提示要区分「用户主动选纯云端」与「本机不可用被迫走云端」，否则会误导。"""
    q = _mgr()
    r = q.precheck_commentary(600, local_engine_ready=True, engine="cloud")
    assert "纯云端" in r["hint"], r["hint"]
    assert "本机 AI 引擎不可用" not in r["hint"], r["hint"]
    # 反过来：本机不可用而用户没显式选云端
    r2 = q.precheck_commentary(600, local_engine_ready=False, engine="auto")
    assert "本机 AI 引擎不可用" in r2["hint"], r2["hint"]


def test_precheck_cloud_exhausted_blocks():
    """时长合法、但必须走云端且额度为 0 → 开始前拒绝（否则白跑一整场）。"""
    d = tempfile.mkdtemp(prefix="vdl_precheck_")
    try:
        q = _mgr(base=d)
        for _ in range(LIFETIME_CLOUD_EVENTS):
            assert q.consume_cloud_event() is True
        r = q.precheck_commentary(600, local_engine_ready=False, engine="auto")
        assert r["allowed"] is False
        assert r["code"] == "cloud_quota_exhausted"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_precheck_cloud_engine_ignores_daily_auto():
    """显式选「纯云端」：本机是否就绪不影响判定，且不受每日 auto 额度限制。"""
    d = tempfile.mkdtemp(prefix="vdl_precheck_")
    try:
        q = _mgr(base=d)
        q.consume_daily_auto()
        r = q.precheck_commentary(600, local_engine_ready=True, engine="cloud")
        assert r["allowed"] is True and r["will_use_cloud"] is True
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ── 2. 按「真正会被处理」的时长判定 ──────────────────────────────────── #
def test_effective_duration_uses_actual_range():
    """口径 = [起点, 终点] 区间 ∩ 正剧范围（与 app._fold_commentary_range 同源）。

    ⚠️ 2026-09-19 语义修正：旧实现把 trim_start/trim_end 当「从头/从尾裁掉的秒数」
    （dur − (ts + te)），而 app._commentary_run 把它们当**绝对起止秒** —— 前端传的是
    整片终点（如 0~2734），于是旧实现恒算出 0，免费档时长闸门形同失效。
    """
    orig = server_app._probe_video_duration
    server_app._probe_video_duration = lambda p: 2000.0
    try:
        assert cm._effective_duration("/x.mp4") == 2000.0
        # [100, 200] 是绝对秒 → 只有 100 秒参与
        assert cm._effective_duration("/x.mp4", 100.0, 200.0) == 100.0
        # 起点超出片长 → 不裁（fail-open，绝不因参数问题把任务判死）
        assert cm._effective_duration("/x.mp4", 5000.0, 0.0) == 2000.0
        # 前端常态：trim 恒为整片（0~2000），真正决定区间的是「正剧范围」
        assert cm._effective_duration("/x.mp4", 0.0, 2000.0,
                                     None, 900.0) == 900.0
        assert cm._effective_duration("/x.mp4", 0.0, 2000.0,
                                     300.0, 900.0) == 600.0
        # 正剧范围覆盖整片 → 仍是整片时长
        assert cm._effective_duration("/x.mp4", 0.0, 2000.0, 0.0, 2000.0) == 2000.0
    finally:
        server_app._probe_video_duration = orig


def test_fold_commentary_range_skips_full_span():
    """覆盖整片时必须返回 (0, 片长)：调用方据此跳过无谓的整片重编码。"""
    assert server_app._fold_commentary_range(2733.5, 0.0, 2733.5) == (0.0, 2733.5)
    # drama 精确到片尾也算整片（容差 0.5s）
    assert server_app._fold_commentary_range(2733.5, 0.0, 2733.5, 0.0, 2733.0) == (0.0, 2733.5)
    # 只做中间 15 分钟：区间就是它
    assert server_app._fold_commentary_range(2733.5, 0.0, 2733.5, 0.0, 900.0) == (0.0, 900.0)
    assert server_app._fold_commentary_range(2733.5, 0.0, 2733.5, 1234.0, 2134.0) == (1234.0, 2134.0)
    # 终点早于起点（用户把两块拖反了）→ 不裁，绝不产出废片
    assert server_app._fold_commentary_range(2733.5, 0.0, 2733.5, 2000.0, 300.0) == (0.0, 2733.5)


# ── 3. 路由层：结构化 403 与预检端点 ─────────────────────────────────── #
class _Patch:
    """把配额管理器与本机引擎探测替换为替身，避免触碰真实家目录与 MLX。"""

    def __enter__(self):
        self._qm = quota_router.get_quota_manager
        self._ready = quota_router._local_engine_ready
        quota_router.get_quota_manager = lambda request=None: _mgr()
        quota_router._local_engine_ready = lambda engine="": False
        return self

    def __exit__(self, *exc):
        quota_router.get_quota_manager = self._qm
        quota_router._local_engine_ready = self._ready
        return False


def test_precheck_or_raise_403_shape():
    from fastapi import HTTPException

    with _Patch():
        try:
            quota_router.precheck_or_raise(None, FREE_MAX_DURATION_SEC + 60)
            raise AssertionError("超时长时必须抛 403")
        except HTTPException as e:
            assert e.status_code == 403
            assert isinstance(e.detail, dict), "detail 必须是结构化对象，前端才能显示 hint"
            assert e.detail["category"] == "quota"
            assert e.detail["subscribe"] is True, "要引导用户订阅"
            assert e.detail["hint"] and e.detail["message"]


def test_precheck_or_raise_passes_when_allowed():
    with _Patch():
        res = quota_router.precheck_or_raise(None, 600.0)
        assert res["allowed"] is True


def test_precheck_endpoint_returns_allowed_flag():
    """端点用 allowed 表达业务结论：ok 已被前端 request() 占用为「HTTP 成功」。"""
    with _Patch():
        r = cm.commentary_precheck(None, duration_sec=3600.0)
        assert r["allowed"] is False
        assert r["code"] == "duration_exceeded"
        assert "engine" in r and "free_max_duration_sec" in r
        # 通过场景
        ok = cm.commentary_precheck(None, duration_sec=600.0)
        assert ok["allowed"] is True


def test_precheck_endpoint_uses_actual_range():
    """端点按「真正会被处理的区间」折算后再判（外层裁剪 ∩ 正剧范围）。

    语义（2026-09-19 统一）：trim_start/trim_end 是**绝对起止秒**，与 app._commentary_run
    一致；前端常态是 trim 传整片、真正决定区间的是「正剧范围」drama_start_sec/drama_end_sec。
    """
    with _Patch():
        # 32 分钟源片，只做 150~1830s 这段（1680s）→ 应当放行
        r = cm.commentary_precheck(None, duration_sec=1920.0, trim_start=150.0, trim_end=1830.0)
        assert r["allowed"] is True
        # 整片（trim 传整片、正剧范围=整片）→ 拦下
        r2 = cm.commentary_precheck(None, duration_sec=1920.0, trim_end=1920.0)
        assert r2["allowed"] is False
        # 前端常态：trim 整片 + 只做前 15 分钟的正剧范围 → 按 900s 判，放行
        r3 = cm.commentary_precheck(None, duration_sec=1920.0, trim_end=1920.0,
                                    drama_start_sec=0.0, drama_end_sec=900.0)
        assert r3["allowed"] is True


def test_precheck_endpoint_honors_ui_engine():
    """端点必须采信前端传来「界面上当前选中的档位」，而不是只读已保存配置。

    真实场景（2026-09-15 实测发现）：用户先把档位切到「本机优先」，紧接着去选素材
    ——此刻档位尚未落盘。若端点只按已保存的「纯云端」判定，本机明明可用却提示
    「本次将消耗 1 次云端额度」；更糟的是终身额度为 0 时会直接被判
    cloud_quota_exhausted 拦下，用户在本机明明能跑的情况下拿到「不能用」的结论。
    """
    _qm = quota_router.get_quota_manager
    _ready = quota_router._local_engine_ready
    _resolve = quota_router.resolve_engine
    try:
        quota_router.get_quota_manager = lambda request=None: _mgr()
        # 本机就绪度与档位相关：auto → 本机可用；cloud → 需要云端
        quota_router._local_engine_ready = lambda engine="": engine != "cloud"
        # 已保存配置是「纯云端」，模拟「界面已切档但还没保存」
        quota_router.resolve_engine = lambda request=None: "cloud"

        ui_auto = cm.commentary_precheck(None, duration_sec=600.0, engine="auto")
        assert ui_auto["allowed"] is True
        assert ui_auto["engine"] == "auto", "端点必须采信界面档位，而非已保存配置"
        assert ui_auto["will_use_cloud"] is False, "本机就绪时不该提示会消耗云端额度"
        assert ui_auto["code"] == "local"

        # 不传 engine 时回落到已保存配置（纯云端），提示相应改变
        saved = cm.commentary_precheck(None, duration_sec=600.0)
        assert saved["engine"] == "cloud"
        assert saved["will_use_cloud"] is True
        assert "纯云端" in saved["hint"]
    finally:
        quota_router.get_quota_manager = _qm
        quota_router._local_engine_ready = _ready
        quota_router.resolve_engine = _resolve


_TESTS = [
    test_precheck_member_always_allowed,
    test_precheck_duration_exceeded_blocks,
    test_precheck_duration_boundary,
    test_precheck_unknown_duration_fail_open,
    test_precheck_local_ready_no_cloud_needed,
    test_precheck_cloud_required_with_quota,
    test_precheck_cloud_hint_distinguishes_explicit_choice,
    test_precheck_cloud_exhausted_blocks,
    test_precheck_cloud_engine_ignores_daily_auto,
    test_effective_duration_uses_actual_range,
    test_fold_commentary_range_skips_full_span,
    test_precheck_or_raise_403_shape,
    test_precheck_or_raise_passes_when_allowed,
    test_precheck_endpoint_returns_allowed_flag,
    test_precheck_endpoint_uses_actual_range,
    test_precheck_endpoint_honors_ui_engine,
]

if __name__ == "__main__":
    failed = 0
    for t in _TESTS:
        try:
            t()
            print(f"  ✓ {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  ✗ {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ✗ {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(_TESTS) - failed}/{len(_TESTS)} 通过")
    raise SystemExit(1 if failed else 0)
