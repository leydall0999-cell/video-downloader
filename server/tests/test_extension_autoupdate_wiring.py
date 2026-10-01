#!/usr/bin/env python3
"""扩展「零点击自动更新」回归测试（2026-10-02，纯离线）。

背景（用户问「扩展程序更新怎么办」）：
解压版（Load unpacked）扩展 Chrome **不会自动更新**——它以本地文件夹为源，改了文件
也必须到 chrome://extensions 点一次 ↻（官方行为 + 本项目实测：把加载目录覆盖成 1.0.43
后，扩展心跳仍自报 1.0.42，直到用户点 ↻）。官方文档同时写明「解压版被 reload 视为
一次 update」，`chrome.runtime.reload()` 同样有效 → 于是把「更新」拆成两半：

    ① 桌面端把内置新版**写进 App 自己维护的扩展目录**
       （server/extension_sync.py + server/routers/extension.py）
    ② 心跳响应回一句 reload_to → 扩展自己 `chrome.runtime.reload()`
       （extension/background.js::maybeAutoReload）

⚠️ 落点为什么是 App 自己的目录而不是用户原来那个（2026-10-02 真机实测）：
用户那个加载目录通常在 `~/Downloads`，属 macOS 隐私保护（TCC）目录；本项目 ad-hoc
签名每次重建 cdhash 都变 → 每次都重新弹授权框，后台线程里没人点 → `open()` 在内核里
**无限期阻塞**（该端点 90s 不返回）。详见 §D 与 extension_sync 模块 docstring。

本测试分四层，**重点是真跑**（模块/路由真被调用、文件真落盘、边界真被拒），源码契约
只用于钉住那些「改坏了会静默失效」的点：

  A. extension_sync 模块行为：安全默认 / manifest.name 判据 / 拒写他人目录 / 只增不删 /
     排除 tests / 幂等 / 脏数据不抛 / 受管目录初始化
  B. 路由行为：maybe_sync 三态（未开不动盘 · 落后才写并催重载 · 已最新不瞎催）
     + 重启续催 + 降级保护 + 受管模式 + needs_setup + update-status/config/now
  C. 源码契约：心跳回传字段（且包在 try 里）、扩展自重载与防循环、**不得新增权限**、
     面板接线（复制目录路径 / 一次性加载引导）、桌面桥提示语
  D. **绝不碰 macOS 隐私保护目录**：源码里不得再有扫描实现，write/config 两条路都拒

运行：
    cd server && python tests/test_extension_autoupdate_wiring.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 测试隔离：绝不写用户真实数据目录（配置就存在数据目录里）
_TMP = tempfile.mkdtemp(prefix="vdl_test_extauto_")
os.environ["VDL_DATA_DIR"] = _TMP
# 受管扩展目录也必须隔离：默认是 ~/视频工坊浏览器扩展，测试里绝不能真去建它
os.environ["VDL_EXTENSION_DIR"] = os.path.join(_TMP, "ext-managed")
os.environ.setdefault("VDL_LOGIN_GATE", "0")

import cdp_sniffer  # noqa: E402
import extension_sync as es  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from routers import extension as rext  # noqa: E402

_fail: list[str] = []
_total = 0


def _real_name() -> str:
    """从内置源 manifest 取**真实**扩展名（别硬编码：本次初稿就因漏了个空格而误报）。

    这个名字同时是「目标目录是不是本扩展」的判据（extension_sync.is_our_extension），
    所以测试必须与它逐字一致。
    """
    got = str(rext._manifest().get("name") or "")
    assert got, "内置源 manifest 取不到 name，测试前提不成立"
    return got


_NAME = _real_name()


def check(name, cond, detail=""):
    global _total
    _total += 1
    if cond:
        print("  ✅ " + name)
    else:
        print("  ❌ " + name + (("  → " + str(detail)) if detail else ""))
        _fail.append(name)


def _write_ext(dirpath: Path, name: str, version: str, code: str = "code") -> Path:
    """造一个「像扩展」的目录：manifest.json + background.js。"""
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / "manifest.json").write_text(
        json.dumps({"name": name, "version": version, "manifest_version": 3}),
        encoding="utf-8")
    (dirpath / "background.js").write_text("// " + code + "\n", encoding="utf-8")
    return dirpath


def _reset_throttle():
    """maybe_sync 有 20s 节流；每个子用例前清掉，否则断言会被上一次的缓存结果骗过。"""
    rext._sync_throttle.update(key="", ts=0.0, result=None)


def _ver_ge(v, want) -> bool:
    try:
        got = tuple(int(x) for x in str(v or "").split("."))
    except ValueError:
        return False
    return got >= tuple(want)


# ---------------------------------------------------------------- A 模块行为
def section_a_module():
    print("\n① extension_sync 模块（真跑，落盘可验证）")
    root = Path(tempfile.mkdtemp(prefix="extsync_", dir=_TMP))
    src = _write_ext(root / "src", _NAME, "9.9.9", "new code")
    tgt = _write_ext(root / "loaded", _NAME, "1.0.1", "old code")
    (tgt / "keepme.txt").write_text("用户自己的文件", encoding="utf-8")
    (src / "tests").mkdir(exist_ok=True)
    (src / "tests" / "t.js").write_text("x", encoding="utf-8")
    (src / ".DS_Store").write_bytes(b"junk")

    es.save_config(load_dir="", auto=False)          # 先归零，确保看的是默认值
    cfg0 = es.get_config()
    check("默认配置是「未开启 + 无目录」（绝不可能默认去动用户文件夹）",
          cfg0.get("auto") is False and cfg0.get("load_dir") == "", cfg0)

    es.save_config(load_dir=str(tgt), auto=True)
    cfg1 = es.get_config()
    check("配置写入后可原样读回",
          cfg1.get("auto") is True and cfg1.get("load_dir") == str(tgt), cfg1)

    foreign = _write_ext(root / "other", "别人的扩展", "9.9.9")
    check("is_our_extension 按 manifest.name 认得自己",
          es.is_our_extension(tgt, _NAME) is True)
    check("is_our_extension 拒绝别的扩展；expected_name 为空时也不认",
          es.is_our_extension(foreign, _NAME) is False
          and es.is_our_extension(tgt, "") is False)

    check("is_newer：只有严格更新才为真（相等 / 更旧都不算）",
          es.is_newer("1.0.44", "1.0.40") and not es.is_newer("1.0.40", "1.0.44")
          and not es.is_newer("1.0.44", "1.0.44"))
    check("is_newer：非法/空版本一律 False（脏数据绝不能把旧版写回去）",
          not es.is_newer("abc", "1.0.40") and not es.is_newer("1.0.44", "")
          and not es.is_newer("1.0", "1.0.0"))

    res = es.sync_to(tgt, src, _NAME)
    check("同步：真写出新文件，且目标 manifest 版本变成源版本",
          res.get("ok") is True and es.read_version(tgt) == "9.9.9", res)
    check("同步：覆盖掉旧的 background.js",
          "new code" in (tgt / "background.js").read_text(encoding="utf-8"))
    check("同步：目标目录里用户自己的文件一个都不能删",
          (tgt / "keepme.txt").read_text(encoding="utf-8") == "用户自己的文件")
    check("同步：tests/ 与 .DS_Store 不进目标目录",
          not (tgt / "tests").exists() and not (tgt / ".DS_Store").exists())

    res2 = es.sync_to(tgt, src, _NAME)
    check("同步幂等：内容一致时不再重复写（written=0 且 unchanged>0）",
          res2.get("ok") is True and res2.get("written") == 0
          and res2.get("unchanged") >= 2, res2)

    bad1 = es.sync_to(foreign, src, _NAME)
    check("拒写「别人的扩展」目录（选错目录也不能写坏它）",
          bad1.get("ok") is False and "拒绝" in str(bad1.get("error")), bad1)
    bad2 = es.sync_to(root / "not-exist", src, _NAME)
    check("拒写不存在的目录", bad2.get("ok") is False, bad2)
    fake_app = _write_ext(root / "Fake.app" / "Contents" / "Resources" / "extension", _NAME, "1.0.1")
    bad3 = es.sync_to(fake_app, src, _NAME)
    check("拒写 .app 包内路径（写进去会让 app 签名失效）",
          bad3.get("ok") is False and "签名" in str(bad3.get("error")), bad3)

    # —— 受管目录（零点击的真正落点）：首次初始化 + 幂等 ——
    md = es.managed_dir()
    check("受管目录默认不在任何 TCC 保护目录内（App 才能自由读写）",
          es.is_tcc_protected(md) is False, str(md))
    check("受管目录名不带版本号（路径恒定 → Chrome 派生的扩展 ID 稳定）",
          not any(ch.isdigit() for ch in md.name), md.name)

    empty = root / "empty-managed"
    empty.mkdir()
    bad_empty = es.sync_to(empty, src, _NAME)               # 默认不允许初始化空目录
    check("默认不允许往「空目录」里写（用户手选一个乱目录时不该被写）",
          bad_empty.get("ok") is False, bad_empty)

    mres = es.sync_to(empty, src, _NAME, allow_empty=True)
    check("受管目录首次初始化：空目录允许写入（allow_empty=True）且版本就位",
          mres.get("ok") is True and es.read_version(empty) == "9.9.9", mres)
    mres2 = es.sync_to(empty, src, _NAME, allow_empty=True)
    check("受管目录同步幂等（第二次 written=0）",
          mres2.get("ok") is True and mres2.get("written") == 0, mres2)

    bad_foreign_empty = es.sync_to(foreign, src, _NAME, allow_empty=True)
    check("即使 allow_empty=True 也绝不写「别人的扩展」目录",
          bad_foreign_empty.get("ok") is False and "拒绝" in str(bad_foreign_empty.get("error")),
          bad_foreign_empty)

    # —— ensure_managed：真建受管目录并落盘（测试里已用 VDL_EXTENSION_DIR 隔离）——
    ens = es.ensure_managed(src, _NAME)
    check("ensure_managed 真把扩展落进受管目录",
          ens.get("ok") is True and es.read_version(md) == "9.9.9", ens)
    check("ensure_managed 落盘排除 tests/ 与 .DS_Store",
          not (md / "tests").exists() and not (md / ".DS_Store").exists())
    check("受管目录里没有写进受保护的 Downloads（隔离生效）",
          es.is_tcc_protected(md) is False)

    broken = root / "broken"
    broken.mkdir()
    (broken / "manifest.json").write_text("{ not json", encoding="utf-8")
    check("目标 manifest 损坏时只是「不认」，不抛异常",
          es.is_our_extension(broken, _NAME) is False and es.read_version(broken) == "")


# ---------------------------------------------------------------- B 路由行为
def section_b_router():
    print("\n② 路由层 /api/extension/*（真调用，含磁盘副作用断言）")
    src_ver = str(rext._manifest().get("version") or "")
    check("内置扩展源可解析出版本号", bool(src_ver), src_ver)

    root = Path(tempfile.mkdtemp(prefix="extrtr_", dir=_TMP))
    loaded = _write_ext(root / "loaded", _NAME, "0.0.1", "old code")

    # —— 未开启自动更新：绝不能碰盘（默认安全）——
    es.save_config(load_dir=str(loaded), auto=False)
    _reset_throttle()
    r = rext.maybe_sync("0.0.1")
    check("未开启自动更新：不判定、不写盘、不催重载",
          r.get("auto") is False and r.get("reload_to") == ""
          and es.read_version(loaded) == "0.0.1", r)

    # —— 开启 + 磁盘落后 + 扩展旧 → 写盘 + 催重载 ——
    es.save_config(auto=True)
    _reset_throttle()
    r = rext.maybe_sync("0.0.1")
    check("开启且落后：真把新版写进目录，并回 reload_to=源版本",
          r.get("auto") is True and r.get("reload_to") == src_ver
          and r.get("synced") is True and es.read_version(loaded) == src_ver, r)

    # —— 扩展已是最新 → 不瞎催（否则每次心跳都重启扩展）——
    _reset_throttle()
    r = rext.maybe_sync(src_ver)
    check("扩展已是最新：reload_to 为空（不制造无意义的重载）",
          r.get("reload_to") == "", r)

    # —— 磁盘已是新版但扩展还是旧的（App 重启后 / 上次没重载成功）→ 不重复写但仍催 ——
    loaded2 = _write_ext(root / "loaded2", _NAME, "0.0.1", "old code")
    es.sync_to(loaded2, rext._source_dir(), _NAME)
    es.save_config(load_dir=str(loaded2), auto=True)
    _reset_throttle()
    r = rext.maybe_sync("0.0.1")
    check("磁盘已新版而扩展仍旧（重启续催）：不重复写，但仍回 reload_to",
          r.get("reload_to") == src_ver and r.get("synced") is False, r)

    # —— 降级保护：扩展比内置还新（用户自己装了更新的）→ 绝不催重载 ——
    _reset_throttle()
    r = rext.maybe_sync("99.0.0")
    check("扩展比内置新（降级保护）：绝不催重载",
          r.get("reload_to") == "", r)

    # —— 目录被删（用户挪走了）→ 返错误但不抛，且不催重载 ——
    es.save_config(load_dir=str(root / "gone"), auto=True)
    _reset_throttle()
    r = rext.maybe_sync("0.0.1")
    check("目录不存在：返错误信息、不抛异常、不催重载",
          r.get("reload_to") == "" and bool(r.get("error")), r)

    # —— update-status 结构 ——
    es.save_config(load_dir=str(loaded), auto=True)
    cdp_sniffer.SNIFFER.mark_ext_seen("0.0.1")
    st = rext.extension_update_status()
    need = {"auto", "load_dir", "installed_version", "source_version",
            "on_disk_version", "pending", "explicit_dir", "managed_dir",
            "using_managed", "needs_setup", "error"}
    check("update-status 暴露所需的全部字段", need <= set(st.keys()), sorted(st.keys()))
    check("update-status：源码版本 == 包内 manifest 版本，且磁盘/已装/待重载判定一致",
          st.get("source_version") == src_ver and st.get("installed_version") == "0.0.1"
          and st.get("on_disk_version") == src_ver and st.get("pending") is True, st)
    check("update-status：显式目录时 using_managed=False（不偷偷改成受管目录）",
          st.get("using_managed") is False and st.get("explicit_dir") == str(loaded), st)

    # —— 零配置路径：目录留空 → 一律用「App 维护的受管目录」 ——
    es.save_config(load_dir="", auto=True)
    check("_effective_load_dir：目录留空 → 受管目录",
          rext._effective_load_dir(es.get_config()) == str(es.managed_dir()))
    st2 = rext.extension_update_status()
    check("update-status 在受管模式下会把受管目录补齐（用户一眼能看到路径）",
          st2.get("using_managed") is True and st2.get("load_dir") == str(es.managed_dir())
          and es.read_version(es.managed_dir()) == src_ver, st2)

    # —— 受管模式下心跳能把目录刷成最新并催重载 ——
    es.save_config(load_dir="", auto=True)
    _reset_throttle()
    r = rext.maybe_sync("0.0.1")
    check("受管模式下开启自动更新：心跳真把受管目录写成最新并回 reload_to",
          r.get("auto") is True and r.get("reload_to") == src_ver
          and es.read_version(es.managed_dir()) == src_ver, r)

    # —— auto 开但「浏览器里跑的还不是受管目录那个」→ needs_setup，让 UI 引导一次性加载 ——
    _reset_throttle()
    st3 = rext.extension_update_status()
    check("浏览器版本落后于内置 → needs_setup=True（面板据此给出一次性加载引导）",
          st3.get("needs_setup") is True, st3)
    cdp_sniffer.SNIFFER.mark_ext_seen(src_ver)
    st4 = rext.extension_update_status()
    check("浏览器版本已等于内置 → needs_setup=False（不再打扰用户）",
          st4.get("needs_setup") is False, st4)

    # —— update-config：非法目录必须 400 ——
    try:
        rext.extension_update_config({"load_dir": str(root / "other-missing")})
        raised = False
    except HTTPException as exc:
        raised = exc.status_code == 400
    check("update-config 指向「不是本扩展」的目录 → 400 拒绝",
          raised is True)

    # —— update-config：合法目录 → 立即同步 ——
    _reset_throttle()
    r = rext.extension_update_config({"load_dir": str(loaded), "auto": True})
    check("update-config 合法目录 → 立即同步一次并回报",
          r.get("ok") is True and isinstance(r.get("sync"), dict)
          and r["sync"].get("auto") is True, r)

    # —— update-config：关掉（空串）→ auto=False 且清空目录 ——
    r = rext.extension_update_config({"load_dir": ""})
    cfg = es.get_config()
    check("update-config 传空串 → 关闭并清空目录（显式动作）",
          r.get("closed") is True and cfg.get("auto") is False
          and cfg.get("load_dir") == "", cfg)

    # —— update-now：真同步 ——
    es.save_config(load_dir=str(loaded), auto=True)
    r = rext.extension_update_now()
    check("update-now：真同步并回报版本",
          r.get("ok") is True and (r.get("result") or {}).get("version") == src_ver, r)

    # —— 心跳透传（路由层真调一次，确认响应带新字段）——
    from routers import sniffer as rs
    resp = rs.sniffer_ext_ping(payload={"version": "0.0.1", "captured": 1})
    check("心跳响应带回 reload_to / auto_update（扩展据此自重载）",
          resp.get("ok") is True and "reload_to" in resp and "auto_update" in resp
          and resp.get("auto_update") is True, resp)


# ---------------------------------------------------------------- C 源码契约
def section_c_contracts():
    print("\n③ 源码契约（钉住「改坏了会静默失效」的点）")
    repo = Path(__file__).resolve().parents[2]
    sniffer_py = (repo / "server" / "routers" / "sniffer.py").read_text(encoding="utf-8")
    ext_py = (repo / "server" / "routers" / "extension.py").read_text(encoding="utf-8")
    app_py = (repo / "server" / "app.py").read_text(encoding="utf-8")
    bg = (repo / "extension" / "background.js").read_text(encoding="utf-8")
    man = json.loads((repo / "extension" / "manifest.json").read_text(encoding="utf-8"))
    launcher = (repo / "desktop" / "desktop_launcher.py").read_text(encoding="utf-8")
    js = (repo / "web" / "js" / "desktop-app.js").read_text(encoding="utf-8")

    check("心跳响应真的回传 reload_to / auto_update",
          '"reload_to": reload_to' in sniffer_py and '"auto_update": auto_on' in sniffer_py)
    check("心跳里的自动更新包在 try 内（自动更新失败绝不能把心跳搞成 500）",
          "自动更新是尽力而为" in sniffer_py and "except Exception:" in sniffer_py)
    check("extension 路由已挂载（新增端点才可达）",
          "from routers import extension as" in app_py and "_extension_rtr.router" in app_py)
    check("extension 路由暴露三个自动更新端点",
          "/api/extension/update-status" in ext_py and "/api/extension/update-config" in ext_py
          and "/api/extension/update-now" in ext_py)
    check("同步只在磁盘版本 != 源码版本时真的写（避免每次心跳都写盘）",
          "if src_ver and disk_ver != src_ver:" in ext_py)
    check("只有磁盘确实已是新版才回 reload_to（否则会把扩展重载到旧版）",
          "if src_ver and disk_ver == src_ver and extension_sync.is_newer(src_ver, installed_version):" in ext_py)

    check("扩展心跳读响应并接到 maybeAutoReload",
          "return r.json()" in bg and ".then(maybeAutoReload)" in bg)
    check("扩展会用 chrome.runtime.reload() 自更新",
          "chrome.runtime.reload()" in bg)
    check("扩展有防重启死循环的记账（同一目标只试一次）",
          "autoReloadTried" in bg and "if (tried[target]) return;" in bg)
    check("扩展只在目标确实更新时重载（绝不自降级）",
          "if (!_verNewer(target, own)) return;" in bg)
    check("扩展没有引入新权限相关 API（新增权限会让静默更新变成需用户重新授权）",
          "chrome.management" not in bg and "chrome.permissions" not in bg)
    check("manifest 权限没被新增（仍是 webRequest/storage/alarms）",
          sorted(man.get("permissions") or []) == ["alarms", "storage", "webRequest"],
          man.get("permissions"))
    check("manifest 版本已 bump（≥1.0.44，否则包内版本不前进就永远不会触发更新）",
          _ver_ge(man.get("version"), (1, 0, 44)), man.get("version"))

    check("桌面桥 choose_extension_dir 存在，且提示语与用途一致（不提「剪映」）",
          "def choose_extension_dir" in launcher
          and "请选择浏览器扩展的加载目录" in launcher)
    check("前端面板：有自动更新行 + 三处请求接线",
          'id="sniffAutoRow"' in js and "'/api/extension/update-status'" in js
          and "'/api/extension/update-config'" in js and "'/api/extension/update-now'" in js)
    check("前端面板打开时会刷新自动更新状态",
          "refreshAutoStatus();" in js and "let autoSt = null;" in js)
    check("更新横幅在「已开启自动更新」时改成自动说明（不再要求用户覆盖目录）",
          "已开启自动更新：新文件会自动写入扩展目录" in js)
    # update-config / update-now 把同步结果放在**顶层** sync（与 status 平级）；
    # 初稿写成 st.sync.reload_to 于是「已更新到 vX」提示永远不触发（真机自检发现）。
    check("开启/同步后的 Toast 读的是顶层 sync（不是 status.sync）",
          "const synced = (r && r.sync) || st.sync || {};" in js
          and "st.sync && st.sync.reload_to" not in js)

    # —— 落点改成受管目录后，前端必须带上「复制路径 + 一次性加载引导」——
    check("前端有「复制目录路径」按钮并走原生剪贴板桥",
          'id="sniffAutoCopy"' in js and "copyText(dir, autoCopyBtn, '目录路径')" in js)
    check("前端有一次性加载引导（needs_setup → SETUP_STEPS）",
          "const SETUP_STEPS = " in js and "st.needs_setup" in js)
    check("前端不再对外声称「自动识别目录」（识别已移除，落点是固定受管目录）",
          "开启（自动识别目录）" not in js and "开启（自动维护目录）" in js)
    check("前端在受管模式下即使 load_dir 为空也能显示目录（兜底 managed_dir）",
          "st.load_dir || st.managed_dir || ''" in js)
    check("更新横幅在 needs_setup 时改口（否则用户会等一个永远不会来的自动重载）",
          "但浏览器里当前加载的还不是 App 维护的那个目录" in js)


def section_d_no_tcc_scan():
    """D. 「绝不碰 macOS 隐私保护目录」的回归守卫（2026-10-02 真机实测抓到的假死 bug）。

    现象：第一版把落点定在用户在 ~/Downloads 里那个加载目录，冻结成 .app 后一碰就
    **永久阻塞** —— /api/extension/update-status 90 秒不返回，进程栈全是
    os_scandir → __opendir2 → open$NOCANCEL；把同一段代码换成 /tmp 路径则 13 毫秒返回。

    根因：macOS 隐私保护（TCC）+ 本项目 ad-hoc 签名（每次重建 cdhash 都变）→ 每次都
    重新弹「访问下载文件夹」授权框，而后台线程里没人点 → open() 在内核里无限期阻塞。
    与 app.py::_tcc_safe_config_path 记的是同一根因（那条注释说已被咬过 3 次）。

    处置：落点改到 App 自己的受管目录（不属保护范围），并**彻底删掉**「扫描
    Downloads/Desktop/Documents 找候选目录」那套实现——它正是卡死来源。本段钉住：
      ① 源码里不得再出现扫描实现（防好心加回来）；
      ② 受保护目录的判定正确；
      ③ 真调用时 write 与 config 两条路都必须拒绝。
    """
    repo = Path(__file__).resolve().parents[2]
    src_py = (repo / "server" / "extension_sync.py").read_text(encoding="utf-8")

    # ① 不得再有任何「扫描用户目录」的实现
    check("D1 已彻底移除扫描候选目录的实现（_iter_candidate_dirs / detect_load_dir）",
          "_iter_candidate_dirs" not in src_py and "def detect_load_dir(" not in src_py)
    check("D1 不再出现扫描根/规模上限常量（那是下载/桌面/文稿三类目录的扫描器）",
          "SCAN_ROOTS" not in src_py and "_SCAN_MAX" not in src_py)
    check("D1 受保护目录只作为「拒绝名单」存在",
          "_TCC_PROTECTED" in src_py and "隐私保护目录" in src_py)
    check("D1 受管目录可被 VDL_EXTENSION_DIR 覆盖（单测隔离 / 用户换盘）",
          'os.environ.get("VDL_EXTENSION_DIR")' in src_py)

    # ② 判定正确
    home = Path.home()
    if sys.platform == "darwin":
        check("D2 macOS：桌面 / 文稿 / 下载 都判定为受保护",
              all(es.is_tcc_protected(home / n / "probe")
                  for n in ("Desktop", "Documents", "Downloads")))
    else:
        check("D2 非 macOS：不判定为受保护（这套限制是 macOS 特有的）",
              es.is_tcc_protected(home / "Downloads" / "probe") is False)
    check("D2 App 自己的受管目录**不是**受保护目录（否则自己把自己锁死）",
          es.is_tcc_protected(es.managed_dir()) is False, str(es.managed_dir()))

    # ③ 真调用被拒：写路径。刻意用一个**不存在**的下载目录路径来探测，
    #    这样既能验证「危险路径优先拦」，又不会真往用户的下载目录里建东西。
    root = Path(tempfile.mkdtemp(prefix="exttcc_", dir=_TMP))
    src_dir = _write_ext(root / "src", _NAME, "9.9.9")
    probe = home / "Downloads" / "_vdl_tcc_guard_probe_not_exist"
    res = es.sync_to(probe, src_dir, _NAME, allow_empty=True)
    check("D3 写受保护目录被拒（且先于「目录不存在」判定：危险路径优先拦）",
          res.get("ok") is False and "隐私保护目录" in str(res.get("error")), res)
    check("D3 拒绝信息里直接给出受管目录（用户知道该改成哪儿）",
          str(es.managed_dir()) in str(res.get("error")), res.get("error"))

    # ③ 真调用被拒：config 路径（否则用户手选一个下载目录就会把 App 配到卡死）
    try:
        rext._resolve_load_dir(str(home / "Downloads" / "anything"))
        raised = False
    except HTTPException as exc:
        raised = exc.status_code == 400
    check("D3 update-config 指向受保护目录 → 400 拒绝（不让用户把 App 配到卡死）",
          raised is True)

    # ③ 对照：非保护目录仍可正常写入（守卫不能误伤正常路径）
    okdir = root / "okdir"
    okdir.mkdir()
    ok_res = es.sync_to(okdir, src_dir, _NAME, allow_empty=True)
    check("D3 对照：非保护目录仍可正常写入（守卫没有误伤正常路径）",
          ok_res.get("ok") is True and es.read_version(okdir) == "9.9.9", ok_res)


def main():
    print("=" * 68)
    print("扩展零点击自动更新 回归测试（2026-10-02 用户「扩展程序更新怎么办」）")
    print("=" * 68)
    section_a_module()
    section_b_router()
    section_c_contracts()
    section_d_no_tcc_scan()
    print("\n" + "-" * 68)
    if _fail:
        print(f"通过: {_total - len(_fail)}  失败: {len(_fail)}")
        for n in _fail:
            print("  ❌ " + n)
        return 1
    print(f"通过: {_total}  失败: 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
