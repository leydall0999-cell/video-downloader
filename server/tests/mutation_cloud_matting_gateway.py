#!/usr/bin/env python3
"""变异确认：云端抠图「网关代做」守卫是否真的咬得住。

为什么必须单独做一次
--------------------
离线守卫全绿只证明「当前代码能过」，不证明「改坏了会被拦」。本项目既有铁律：
新增守卫必须做变异确认（注入缺陷 → 必须变红），否则等于没写。

安全约定（比业务缺陷更重要）
--------------------------
🔴 变异**只在临时副本**上进行：`rsync` 把 `server/`、`web/`、`deploy/` 复制到临时根，
只改副本、只跑副本里的守卫，收尾再核对**真实源码** sha256 未变。
历史上（2026-10-12）吃过「变异改真源码、进程被杀后残留缺陷代码」的亏，本文件不重复。

运行：`.build_venv/bin/python server/tests/mutation_cloud_matting_gateway.py`
"""
from __future__ import annotations

import hashlib
import os
import pathlib
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PY = str(ROOT / ".build_venv" / "bin" / "python")
GUARD = "server/tests/test_cloud_matting_gateway.py"

# (名称, 相对路径, 原片段, 变异后片段)
MUTATIONS: list[tuple[str, str, str, str]] = [
    ("M1 抠图不再走网关（网关接入也直连本机）",
     "server/cloud_matting_mediakit.py",
     '    if _gateway_on():\n        data = _gw_call("/v1/image/remove-background",',
     '    if False:\n        data = _gw_call("/v1/image/remove-background",'),

    ("M2 增强不再走网关",
     "server/cloud_matting_mediakit.py",
     '    if _gateway_on():\n        data = _gw_call("/v1/image/enhance",',
     '    if False:\n        data = _gw_call("/v1/image/enhance",'),

    ("M3 网关开关恒为假（等于退回凭据下发模型）",
     "server/cloud_matting_mediakit.py",
     '        return bool(get_gateway_config().get("enabled"))',
     '        return False'),

    ("M4 CV 分割不走网关（AK/SK 又要落回本机签名）",
     "server/cloud_matting.py",
     '    if _gateway_on():\n        return _gw_segment(rgb, person=(action == "HumanSegment"), timeout=timeout)',
     '    if False:\n        return _gw_segment(rgb, person=(action == "HumanSegment"), timeout=timeout)'),

    ("M5 网关 token 校验被架空（错令牌也放行）",
     "deploy/gateway/gateway.py",
     '    raise HTTPException(status_code=401, detail="invalid token")',
     '    return {"name": "anonymous"}'),

    ("M6 health 泄露服务端密钥",
     "deploy/gateway/gateway.py",
     '        "matting": _matting_caps(),',
     '        "matting": {**_matting_caps(), "key": _matting_cfg().get("mediakit_api_key")},'),

    ("M7 人像也走通用分割（HumanSegment 失效）",
     "deploy/gateway/gateway.py",
     '    action = "HumanSegment" if person else "GeneralSegment"',
     '    action = "GeneralSegment"'),

    ("M8 凭据来源恒报 server（谎报钥匙在服务端）",
     "server/cloud_matting_config.py",
     '    if _gateway_matting_ready("mediakit") or _gateway_matting_ready("visual"):\n        return "server"',
     '    if False:\n        return "server"'),

    ("M9 探测失败时乐观放行（谎报就绪）",
     "server/cloud_matting_config.py",
     '    caps = matting_capability()\n    if caps is None:\n        return False',
     '    caps = matting_capability()\n    if caps is None:\n        return True'),

    ("M10 前端声明的「凭据来源」元素与接线脱钩（改了 id 没改引用）",
     "web/index.html",
     'id="cloudMattingSource"',
     'id="cloudMattingSourceX"'),
]

MUTATED_FILES = sorted({m[1] for m in MUTATIONS})


def _sha(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _stage() -> pathlib.Path:
    """把工作树复制到临时根（只动副本）。"""
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="vdl_mat_gw_mut_"))
    for sub in ("server", "web", "deploy"):
        subprocess.run(
            ["rsync", "-a", "--exclude", "__pycache__", "--exclude", ".pytest_cache",
             f"{ROOT / sub}/", f"{tmp / sub}/"],
            check=True, capture_output=True)
    return tmp


def run_guard(root: pathlib.Path) -> tuple[bool, str]:
    env = dict(os.environ)
    env["VDL_DATA_DIR"] = tempfile.mkdtemp(prefix="vdl_mut_data_")
    env.pop("VDL_GATEWAY_HOME", None)
    p = subprocess.run([PY, str(root / GUARD)], cwd=str(root), env=env,
                       capture_output=True, text=True, timeout=900)
    return p.returncode == 0, (p.stdout or "") + (p.stderr or "")


def main() -> int:
    pristine = {rel: _sha(ROOT / rel) for rel in MUTATED_FILES}
    print("真实源码开工前 sha：")
    for rel, sha in pristine.items():
        print(f"  {sha}  {rel}")

    print("\n▶ 基线自检：未变异必须先全绿")
    base_ok, base_out = run_guard(ROOT)
    if not base_ok:
        print("❌ 基线失败，本次变异结果全部作废：")
        for ln in [l for l in base_out.splitlines() if "❌" in l or "Traceback" in l][:12]:
            print("   ", ln)
        return 1
    print("✅ 基线全绿（未变异通过）")

    ok = True
    print("\n▶ 注入缺陷（只改临时副本）")
    for name, rel, old, new in MUTATIONS:
        root = _stage()
        try:
            target = root / rel
            src = target.read_text(encoding="utf-8")
            if old not in src:
                print(f"⚠️  {name}: 锚点未命中，跳过（请人工核对）")
                ok = False
                continue
            target.write_text(src.replace(old, new, 1), encoding="utf-8")
            passed, out = run_guard(root)
            if passed:
                print(f"❌ {name}: 注入缺陷后守卫仍全绿 —— 断言没咬住！")
                ok = False
            else:
                why = [l.strip() for l in out.splitlines() if l.strip().startswith("❌")]
                print(f"✅ {name}: 如期变红（{why[0][:60] if why else '非零退出'}）")
        except Exception as e:  # noqa: BLE001
            print(f"⚠️  {name}: harness 异常 {type(e).__name__}: {e}")
            ok = False
        finally:
            subprocess.run(["rm", "-rf", str(root)], capture_output=True)

    print("\n▶ 真实源码未被污染（sha 应与开工前一致）")
    after = {rel: _sha(ROOT / rel) for rel in MUTATED_FILES}
    for rel in MUTATED_FILES:
        same = after[rel] == pristine[rel]
        print(f"  {'✅' if same else '❌'} {rel}  {after[rel]}")
        ok = ok and same
    print("\n" + ("🎉 全部变异如期变红，真实源码零改动" if ok else "❌ 存在未咬住/被污染的项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
