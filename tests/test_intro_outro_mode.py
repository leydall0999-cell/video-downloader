"""tests/test_intro_outro_mode.py — 「固定选择项」2-mode radio 映射与后端参数测试。

背景：
  旧版（极早）两个 checkbox 视觉上像独立开关但语义互斥（skip 优先 keep），UX 混乱。
  中间版尝试 3-mode radio（keep_no_narrate / keep_narrate_all / skip），但用户认为
  keep_narrate_all 多余、实际场景极少用到，回退为 2-mode：
      ● 保留片头片尾·不解说（默认，片头片尾绝对不解说）
      ○ 去片头片尾（自动检测边界后剪掉）
  radio 单选天然互斥，无歧义。

本测试覆盖：
  ① JS 选项拼装：2 种 mode 映射到正确的 skip_intro_outro / no_narrate_intro_outro
  ② 后端 _commentary_option_args：skip/no_narrate 任意组合的命令行参数正确性
  ③ UI 默认行为：HTML 默认选中 keep_no_narrate，旧 checkbox ID 完全清除
  ④ 后端 keep_narrate_all 功能仍保留（外部/cli 调用路径），UI 不再暴露
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "server"))

import app as srv  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# ① 后端 _commentary_option_args：skip/no_narrate 组合 → 命令行参数
# ─────────────────────────────────────────────────────────────────────────────

class TestCommentaryOptionArgsIntroOutro:
    """验证 _commentary_option_args 对 (skip, no_narrate) 组合的参数拼装。

    后端仍支持 3 种 CLI 组合（外部/CLI 调用路径仍可用 keep_narrate_all，
    即 skip=False + no_narrate=False），但 UI 不再提供这一选项。
    """

    def _args(self, *, skip: bool, no_narrate: bool) -> list[str]:
        return srv._commentary_option_args(
            skip_intro_outro=skip,
            no_narrate_intro_outro=no_narrate,
        )

    def test_keep_no_narrate_default(self):
        """默认 mode：保留片头片尾且不解说 → 不加 --skip-intro-outro，也不加 --narrate-all"""
        args = self._args(skip=False, no_narrate=True)
        assert "--skip-intro-outro" not in args
        assert "--narrate-all" not in args

    def test_keep_narrate_all_backend_still_works(self):
        """保留片头片尾但要全片解说（CLI/外部调用场景）→ 不加 --skip-intro-outro，但加 --narrate-all

        注：UI 已不再暴露该选项，但后端实现保留以兼容外部调用。"""
        args = self._args(skip=False, no_narrate=False)
        assert "--skip-intro-outro" not in args
        assert "--narrate-all" in args

    def test_skip(self):
        """去片头片尾（UI 现在唯一另一选项）→ 加 --skip-intro-outro"""
        args = self._args(skip=True, no_narrate=True)
        assert "--skip-intro-outro" in args
        # no_narrate=True 守卫：--narrate-all 不应出现
        assert "--narrate-all" not in args


# ─────────────────────────────────────────────────────────────────────────────
# ② JS 端 mode 映射（不依赖 DOM，纯逻辑模拟）
# ─────────────────────────────────────────────────────────────────────────────

class TestIntroOutroModeMapping:
    """模拟 app.js comGetOptions 中的 mode → (skip, no_narrate) 映射逻辑。

    这是从 app.js 抽出来的纯函数，便于单测。如未来 JS 改动，请同步更新此函数。
    """

    @staticmethod
    def _map(mode: str) -> tuple[bool, bool]:
        """mode → (skip_intro_outro, no_narrate_intro_outro)

        2-mode 简化版：
          - 'skip' → skip=True, no_narrate=True（剪掉，自然不解说）
          - 其他（包括 keep_no_narrate / 未知 / 空） → skip=False, no_narrate=True
        """
        skip = mode == "skip"
        no_narrate = True  # 两个 mode 都不解说片头片尾（skip 模式已剪掉）
        return skip, no_narrate

    def test_keep_no_narrate(self):
        assert self._map("keep_no_narrate") == (False, True)

    def test_skip(self):
        assert self._map("skip") == (True, True)

    def test_unknown_mode_defaults_to_keep_no_narrate(self):
        """未知值（前端容错）应回退到默认行为：保留不解说"""
        assert self._map("garbage") == (False, True)
        assert self._map("") == (False, True)
        assert self._map("keep_narrate_all") == (False, True)  # 已废弃但安全降级

    def test_two_modes_produce_distinct_backend_args(self):
        """2 种 mode 映射出的 (skip, no_narrate) 组合在 _commentary_option_args 中产生不同的命令行参数"""
        combos = [self._map(m) for m in ("keep_no_narrate", "skip")]
        arg_sets = [
            set(srv._commentary_option_args(skip_intro_outro=s, no_narrate_intro_outro=n))
            for s, n in combos
        ]
        # 两组参数必须不同（保证后端能区分）
        assert len({frozenset(s) for s in arg_sets}) == 2, (
            f"2 种 mode 应产生不同的命令行参数集，实际: {arg_sets}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# ③ UI 默认值契约：HTML 默认 keep_no_narrate，旧 checkbox ID 完全清除
# ─────────────────────────────────────────────────────────────────────────────

class TestDefaultsContract:
    """校验前后端默认值一致：默认行为 = 「保留片头片尾·不解说」。"""

    def test_routers_default_no_narrate_true(self):
        """server/routers/commentary.py create_script_only_upload 默认 no_narrate=True

        注：create_commentary_upload（直接上传渲染版）暂未开放这两个参数，
        走的是简化路径（用户默认行为）。本测试只覆盖脚本审核路径。"""
        from routers import commentary as rcom
        import inspect
        sig = inspect.signature(rcom.create_script_only_upload)
        # FastAPI Form(True) 包装了 True，从 .default 属性取原值
        no_narrate_default = sig.parameters["no_narrate_intro_outro"].default.default
        skip_default = sig.parameters["skip_intro_outro"].default.default
        assert no_narrate_default is True, f"期望 no_narrate 默认 True，实际 {no_narrate_default!r}"
        assert skip_default is False, f"期望 skip 默认 False，实际 {skip_default!r}"

    def test_html_has_exactly_two_radios(self):
        """web/index.html 必须恰好有 2 个 comIntroOutroMode radio，且默认选 keep_no_narrate"""
        html = (REPO / "web" / "index.html").read_text(encoding="utf-8")
        import re
        radios = re.findall(
            r'<input type="radio" name="comIntroOutroMode" value="([^"]+)"(\s+checked)?',
            html,
        )
        assert len(radios) == 2, f"应恰好 2 个 radio，实际 {len(radios)}: {radios}"
        modes = [v for v, _ in radios]
        assert modes == ["keep_no_narrate", "skip"], (
            f"2 个 radio 顺序应为 keep_no_narrate/skip，实际 {modes}"
        )
        # 默认 checked
        assert "checked" in radios[0][1], "keep_no_narrate 必须默认 checked"
        # 另一个不能 checked
        assert "checked" not in radios[1][1]

    def test_html_no_keep_narrate_all_radio(self):
        """keep_narrate_all radio 必须从 HTML 删除（用户认为多余）"""
        html = (REPO / "web" / "index.html").read_text(encoding="utf-8")
        assert 'value="keep_narrate_all"' not in html, (
            "HTML 仍含已废弃的 keep_narrate_all radio 选项，应删除"
        )

    def test_app_py_no_checkbox_ids_remain(self):
        """旧的 comKeepNoNarrate / comSkipIntroOutro checkbox 必须从 web/ 完全删除"""
        for f in ("web/index.html", "web/app.js"):
            content = (REPO / f).read_text(encoding="utf-8")
            assert "comKeepNoNarrate" not in content, f"{f} 残留旧 ID"
            assert "comSkipIntroOutro" not in content, f"{f} 残留旧 ID"


# ─────────────────────────────────────────────────────────────────────────────
# ④ radio 单选天然互斥 + 默认 opening 高亮测试
# ─────────────────────────────────────────────────────────────────────────────

class TestRadioMutuallyExclusive:
    """HTML radio 浏览器层天然互斥；JS 只需读取选中值即可。"""

    def test_html_uses_radio_not_checkbox(self):
        """必须是 radio，确保浏览器层互斥（无需 JS 额外守卫）"""
        html = (REPO / "web" / "index.html").read_text(encoding="utf-8")
        assert 'type="radio" name="comIntroOutroMode"' in html
        # 不应有同名 checkbox
        assert 'type="checkbox" name="comIntroOutroMode"' not in html

    def test_js_mapper_is_consistent_with_backend(self):
        """JS 映射 + 后端命令行参数，端到端一致性"""
        for mode in ("keep_no_narrate", "skip"):
            skip, no_narr = self._test_js_map(mode)
            args = srv._commentary_option_args(
                skip_intro_outro=skip,
                no_narrate_intro_outro=no_narr,
            )
            if skip:
                assert "--skip-intro-outro" in args, f"mode={mode} 应含 --skip-intro-outro"
            else:
                assert "--skip-intro-outro" not in args, f"mode={mode} 不应含 --skip-intro-outro"

    @staticmethod
    def _test_js_map(mode: str) -> tuple[bool, bool]:
        return TestIntroOutroModeMapping._map(mode)


# ─────────────────────────────────────────────────────────────────────────────
# ⑤ Pydantic 修复回归测试：CommentaryConfigRequest POST 端点不再抛 TypeAdapter 错误
# ─────────────────────────────────────────────────────────────────────────────

class TestPydanticConfigSave:
    """修复 server/app.py CommentaryConfigRequest.narration_loudness 字段从 Any 改为
    Union[int,float,str] 后的回归测试。

    根因：from __future__ import annotations（PEP 563）下，Pydantic v2 的 TypeAdapter
    无法为 Any 字段构建完全定义的 core schema，导致 POST 抛
    'is not fully defined; you should define' 错误。
    """

    def _client(self):
        from fastapi.testclient import TestClient
        return TestClient(srv.app)

    def test_post_config_numeric_loudness(self):
        """POST /api/commentary/config 用 numeric loudness 应该 200 OK"""
        c = self._client()
        r = c.post("/api/commentary/config", json={
            "narration_loudness": -14,
            "original_duck": 0.10,
            "narration_boost": 1.0,
        })
        assert r.status_code == 200, f"期望 200，实际 {r.status_code} body={r.text[:200]}"
        body = r.json()
        assert body.get("ok") is True
        assert body["config"]["narration_loudness"] == -14

    def test_post_config_off_string_loudness(self):
        """POST /api/commentary/config 用 'off' 字符串 loudness 应该 200 OK"""
        c = self._client()
        r = c.post("/api/commentary/config", json={
            "narration_loudness": "off",
            "original_duck": 0.10,
            "narration_boost": 1.2,
        })
        assert r.status_code == 200, f"期望 200，实际 {r.status_code} body={r.text[:200]}"
        body = r.json()
        assert body.get("ok") is True
        assert body["config"]["narration_loudness"] == "off"

    def test_model_field_annotation_is_union_not_any(self):
        """CommentaryConfigRequest.narration_loudness 必须是具体 Union 类型，不能是 Any"""
        # 在 PEP 563 (from __future__ import annotations) 下，__annotations__ 里的值是字符串
        ann = srv.CommentaryConfigRequest.__annotations__.get("narration_loudness")
        assert ann is not None, "narration_loudness 字段必须存在"
        ann_str = str(ann)
        assert "Any" not in ann_str, (
            f"narration_loudness 注解不能含 Any（会触发 Pydantic v2 TypeAdapter 错误），实际: {ann_str}"
        )
