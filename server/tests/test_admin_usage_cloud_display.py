"""server/tests/test_admin_usage_cloud_display.py — 后台「使用详情」必须看见云端用量（2026-10-18）

事故复现：网页版账号 `guodong9@foxmail.com`（设备指纹 `web-*`）在授权中心有当日用量
`authority.usage = {"download": 2}`，但后台点「使用详情」显示
「这个账号还没有任何使用记录」，同时「云端授权」又写着「云端有记录」——自相矛盾。

根因：面板的判空条件与「配额消耗」区块**只读本机 store 的 usage_history**，
`cloud.data.authority.usage` 从未被渲染。网页版的消耗记在授权中心，不进本机。

本文件钉死三件事，防回退：
  1) `_usageCloudUsage()` 真能从 `cloud.data.authority` 抽出 usage（用 node 实跑产品代码段，
     不是只 grep 字符串——提取不到 node 时退化为静态断言，并明确打印跳过原因）；
  2) 判空条件必须把云端当日总量算进去（缺 `!cu0.total` ＝ 网页版用户继续被误报为空）；
  3) 正常视图必须单独渲染「云端用量」，并说明云端只有当天计数、无逐日历史。
"""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_APP_JS = _ROOT / "web" / "app.js"

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ✓ " if cond else "  ❌ ") + name + ("" if cond else "  " + detail[:300]))
    if not cond:
        FAILED.append(name)


def _strict(fn):
    """把「收集式断言」翻译成 pytest 能看见的失败。

    本项目的离线套件既可能用 `run_one`（当脚本跑，走 main()）也可能用
    `run_pytest_root`（pytest 收集 test_* 函数）。若只收集不抛异常，
    pytest 会认为一切通过 —— 那是**假绿**。这里在函数结束时统一抛。
    """
    def wrapper(*args, **kwargs):
        before = len(FAILED)
        fn(*args, **kwargs)
        if len(FAILED) > before:
            raise AssertionError("失败项：" + "；".join(FAILED[before:]))
    wrapper.__name__ = getattr(fn, "__name__", "wrapper")
    return wrapper


def _src() -> str:
    return _APP_JS.read_text(encoding="utf-8")


def _snippet() -> str:
    """截出 `_usageCloudUsage` … `_USAGE_VIA_LABELS` 之间的产品代码（含两个函数）。"""
    src = _src()
    start = src.index("const _usageCloudUsage = (u) => {")
    end = src.index("const _USAGE_VIA_LABELS", start)
    return src[start:end]


@_strict
def test_cloud_usage_extraction_behaviour() -> None:
    """用 node 实跑产品代码段：云端 usage 必须被正确抽取成 {date, items, total}。"""
    node = shutil.which("node") or shutil.which("node.exe")
    if not node:
        # 没有 node 时不阻塞（CI 的 windows 跑器有 node，本地 mac 也有），但要显式提示
        print("  ⚠ 未找到 node，跳过行为级校验（静态断言仍然生效）")
        return

    stub = ("const _USAGE_RES_LABELS = { download: '下载任务', matting: '一键抠图' };\n"
            "const _usageResLabel = (k) => _USAGE_RES_LABELS[k] || k;\n")
    tail = """
const cases = [
  {cloud:{ok:true,data:{authority:{usage_date:'2026-10-18',usage:{download:2}}}}},
  {cloud:{ok:true,data:{authority:{usage_date:'2026-10-18',usage:{download:2,matting:1}}}}},
  {cloud:{ok:true,data:{authority:{usage_date:'2026-10-18',usage:{download:0}}}}},
  {cloud:{ok:true,data:{authority:{}}}},
  {cloud:{ok:false,reason:'云端不可达'}},
  {},
  null
];
const histCases = [
  {cloud:{data:{usage_history:[
    {date:'2026-10-17', counts:{download:3, matting:1}},
    {date:'2026-10-16', counts:{download:1}},
    {date:'2026-10-15', counts:{}},
    {date:'2026-10-14', counts:{download:0}}
  ]}}},
  {cloud:{data:{}}},
  {cloud:{ok:false, reason:'云端不可达'}},
  {}
];
console.log(JSON.stringify({
  rows: cases.map((c) => _usageCloudUsage(c)),
  texts: cases.map((c) => _usageCloudUsageText(_usageCloudUsage(c))),
  hists: histCases.map((c) => _usageCloudHistory(c))
}));
"""
    script = stub + _snippet() + tail
    try:
        out = subprocess.run([node, "-e", script], capture_output=True,
                             text=True, timeout=30)
    except Exception as e:  # noqa: BLE001
        check(f"node 执行产品代码段（异常：{e!r}）", False)
        return
    check("node 执行产品代码段无 stderr", out.returncode == 0)
    if out.returncode != 0:
        print("    stderr:", (out.stderr or "")[:500])
        return

    data = json.loads((out.stdout or "").strip().splitlines()[-1])
    rows = data["rows"]
    texts = data["texts"]

    check("单一下载 2 次 → total=2", rows[0]["total"] == 2 and rows[0]["items"] == {"download": 2})
    check("日期原样透出（2026-10-18）", rows[0]["date"] == "2026-10-18")
    check("多资源求和 → total=3", rows[1]["total"] == 3)
    check("0 值不计入（total=0、items 为空）", rows[2]["total"] == 0 and rows[2]["items"] == {})
    check("空 authority → total=0 不抛异常", rows[3]["total"] == 0)
    check("云端不可达 → total=0 不抛异常", rows[4]["total"] == 0)
    check("无 cloud 字段 / null 入参不抛异常", rows[5]["total"] == 0 and rows[6]["total"] == 0)
    check("摘要用中文资源名（下载任务 2）", texts[0] == "下载任务 2")
    check("无用量的摘要为空串", texts[3] == "" and texts[4] == "")

    # ---- 按天历史（2026-10-18 新增：授权中心归档近一年，面板要能列出来）----
    hists = data["hists"]
    check("历史只保留有消耗的天（空 counts / 全 0 被滤掉）", len(hists[0]) == 2, str(hists[0]))
    check("历史第一条是最新日期", hists[0][0]["date"] == "2026-10-17", str(hists[0]))
    check("历史行 total 求和正确（3+1=4）", hists[0][0]["total"] == 4, str(hists[0]))
    check("历史明细用原键聚合", hists[0][0]["items"] == {"download": 3, "matting": 1}, str(hists[0]))
    check("云端无 usage_history → 空数组", hists[1] == [] and hists[2] == [] and hists[3] == [],
          str(hists[1:]))


@_strict
def test_guard_against_regression() -> None:
    """静态断言：判空与渲染都必须覆盖云端口径，防有人只删一行就回退。"""
    src = _src()

    check("存在 _usageCloudUsage 辅助函数", "const _usageCloudUsage = (u) => {" in src)
    check("从 cloud.data.authority 取值",
          re.search(r"\.cloud[\s\S]{0,80}?\.data[\s\S]{0,40}?\.authority", src) is not None)
    check("读取 authority.usage", "const raw = auth.usage || {};" in src)
    check("判空条件包含 !cu0.total", "!us0.total && !cu0.total" in src)
    check("空状态文案仍保留（只是条件更严）", "这个账号还没有任何使用记录" in src)
    check("正常视图渲染「云端用量」区块", "云端用量（桌面 App + 网页版 / 其它设备）" in src)
    check("「本机配额消耗」与云端口径区分", "本机配额消耗" in src)
    check("存在 _usageCloudHistory（按天历史）", "const _usageCloudHistory = (u) => {" in src)
    check("历史读 cloud.data.usage_history", "usage_history" in src)
    check("历史按日期分列渲染（日期/次数/明细表头）",
          "<th>日期</th><th>次数</th><th>明细</th>" in src)
    check("说明保留近一年 + 上线前无法补录", ("近一年" in src) and ("无法补录" in src))
    check("不再用「云端有记录」这种误导措辞", "云端有记录" not in src)

    # ── 2026-10-18 错号事故（点 2649348580@qq.com 却显示 guodong9@foxmail.com）──
    # 根因：弹窗单例复用，账号头只在「有使用记录」分支里写，空状态分支 return 后
    # 顶部残留上一个账号。这里锁住三件事，任何一条被改回去都会变红。
    i_empty = src.find("<h4>这个账号还没有任何使用记录</h4>")
    i_req = src.find("const r = await adminRequest('/api/admin/users/")
    i_head_after = src.find("dlg.querySelector('.aud-user').innerHTML", max(i_req, 0))
    check("账号头在空状态分支之前刷新（不再残留上一个账号）",
          i_empty > 0 and i_head_after >= 0 and i_head_after < i_empty)
    check("进入弹窗时先清掉上一次的账号头",
          re.search(r"dlg\.querySelector\('\.aud-user'\)\.innerHTML\s*=\s*`<b>\$\{_uidEsc\}", src) is not None)
    check("请求返回后校验 uid 未被切换（丢弃过期响应）",
          "dlg.dataset.uid !== (uid || '')" in src)
    check("空状态分支不再单独依赖列表行补齐账号头",
          src.count("dlg.querySelector('.aud-user').innerHTML") == 2)


def main() -> int:
    print("== test_admin_usage_cloud_display ==")
    try:
        test_cloud_usage_extraction_behaviour()
        test_guard_against_regression()
    except AssertionError as e:
        print("\n" + str(e))
        return 1
    if FAILED:
        print(f"\n{len(FAILED)} 项失败：")
        for f in FAILED:
            print("  -", f)
        return 1
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
