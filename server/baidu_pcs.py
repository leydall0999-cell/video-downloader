"""baiduPCS-Go 适配器 —— 用成熟的开源命令行工具管理百度网盘登录态与下载。

为什么用这套替代之前的「app 内 WebView 注入百度内部接口」：
- 登录态写在 baiduPCS-Go 自己的配置目录里（固定 cwd 执行），持久、可靠，重启不丢。
- 没有 HttpOnly cookie 读不到的问题、没有 sign 动态计算、没有「点按钮触发浏览器下载」绕开拦截的幺蛾子。
- 所有命令在固定工作目录 PCS_HOME 下执行，登录态自然跨调用保留。

所有命令通过 subprocess 调用，stdout/stderr 实时捕获，便于前端展示与排错。
"""
from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Callable, Optional

import requests

logger = logging.getLogger("vdl.baidupcs")

# baiduPCS-Go 维护版（qjfoidnh fork）
REPO = "qjfoidnh/BaiduPCS-Go"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"

# 登录态 / 配置目录（固定 cwd，保证登录持久）
PCS_HOME = Path.home() / ".video-downloader" / "baidupcs"
PCS_BIN_DIR = PCS_HOME / "bin"
PCS_BINARY = PCS_BIN_DIR / "BaiduPCS-Go"

# 下载落点（与 app.py 的 DOWNLOAD_DIR 保持一致；直接取值避免循环导入）
DOWNLOAD_DIR = Path.home() / "Downloads" / "VideoDownloader"
PCS_DOWNLOAD_DIR = DOWNLOAD_DIR / "baidu" / "pcs"

ProgressCb = Callable[[dict], None]


# --------------------------------------------------------------------------- #
# 二进制获取
# --------------------------------------------------------------------------- #
def _detect_arch() -> str:
    m = platform.machine().lower()
    if m in ("arm64", "aarch64"):
        return "arm64"
    return "amd64"


def _find_existing_binary() -> Optional[Path]:
    # 1) 固定目录（用户目录，可执行）
    if PCS_BINARY.exists() and os.access(PCS_BINARY, os.X_OK):
        return PCS_BINARY
    # 2) PATH
    found = shutil.which("BaiduPCS-Go") or shutil.which("baidupcs-go")
    if found:
        return Path(found)
    # 3) 打包内置（PyInstaller 冻结的 _MEIPASS）
    meipass = os.environ.get("MEIPASS") or getattr(sys, "_MEIPASS", None)
    if meipass:
        # --add-binary 放到 MEIPASS/bin/ 下（可能直接是文件，也可能是子目录）
        for cand in [Path(meipass) / "bin" / "BaiduPCS-Go",
                      Path(meipass) / "bin" / "BaiduPCS-Go" / "BaiduPCS-Go"]:
            if cand.exists() and os.access(cand, os.X_OK):
                # macOS 安全策略：从 .app 包内执行非原始签名二进制会被拦截（Errno 13 Permission denied）。
                # 解决方案：复制到用户目录后从那里执行。
                try:
                    PCS_BIN_DIR.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(cand, PCS_BINARY)
                    PCS_BINARY.chmod(PCS_BINARY.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                    # 清除 quarantine（从 .app 内复制的文件可能继承隔离标记，导致执行被拦）
                    try:
                        subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(PCS_BINARY)],
                                        capture_output=True, timeout=5)
                    except Exception:
                        pass
                    logger.info("已将 baiduPCS-Go 从包内复制到 %s", PCS_BINARY)
                    return PCS_BINARY
                except Exception as e:
                    logger.error("复制 baiduPCS-Go 到用户目录失败: %s", e)
                    # 不回退！包内路径执行一定会 Errno 13（macOS 安全策略）
                    return None
    return None


def ensure_binary(progress: Optional[ProgressCb] = None) -> dict:
    """确保 baiduPCS-Go 二进制可用；缺失则下载。返回状态字典。"""
    existing = _find_existing_binary()
    if existing:
        return {"ok": True, "binary": str(existing), "from_cache": True, "message": "已存在"}

    PCS_BIN_DIR.mkdir(parents=True, exist_ok=True)
    arch = _detect_arch()
    if progress:
        progress({"stage": "resolve", "message": f"正在查询最新版本（{arch}）…"})

    try:
        resp = requests.get(API_LATEST, timeout=20)
        resp.raise_for_status()
        rel = resp.json()
        tag = rel.get("tag_name", "")
        assets = rel.get("assets", [])
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "binary": None, "message": f"查询最新版本失败：{e}"}

    target = None
    for a in assets:
        name = (a.get("name") or "").lower()
        if "darwin" in name and arch in name and name.endswith(".zip"):
            target = a
            break
    if not target:
        return {"ok": False, "binary": None,
                "message": f"未找到适配 macOS/{arch} 的发布包（tag={tag}）"}

    url = target.get("browser_download_url")
    if not url:
        return {"ok": False, "binary": None, "message": "发布包缺少下载链接"}

    if progress:
        progress({"stage": "download", "message": f"正在下载 {target.get('name')} …", "url": url})
    try:
        _download(url, PCS_BIN_DIR / "pcs.zip", progress)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "binary": None, "message": f"下载失败：{e}"}

    # 解压，找二进制
    try:
        with zipfile.ZipFile(PCS_BIN_DIR / "pcs.zip") as z:
            names = z.namelist()
            bin_name = next((n for n in names if n.endswith("BaiduPCS-Go") or n.endswith("baidupcs-go")), None)
            if not bin_name:
                return {"ok": False, "binary": None, "message": f"压缩包内未找到二进制：{names[:5]}"}
            z.extract(bin_name, PCS_BIN_DIR)
        # 可能在子目录里
        extracted = PCS_BIN_DIR / bin_name
        if extracted != PCS_BINARY:
            if PCS_BINARY.exists():
                PCS_BINARY.unlink()
            shutil.move(str(extracted), str(PCS_BINARY))
        PCS_BINARY.chmod(PCS_BINARY.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        (PCS_BIN_DIR / "pcs.zip").unlink(missing_ok=True)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "binary": None, "message": f"解压失败：{e}"}

    if progress:
        progress({"stage": "done", "message": "baiduPCS-Go 就绪 ✓"})
    return {"ok": True, "binary": str(PCS_BINARY), "from_cache": False, "message": "下载并安装完成"}


def _download(url: str, dest: Path, progress: Optional[ProgressCb] = None) -> None:
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0) or 0)
        done = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                if not chunk:
                    continue
                f.write(chunk)
                done += len(chunk)
                if progress and total:
                    progress({"stage": "download", "percent": round(done * 100 / total, 1),
                              "downloaded": done, "total": total})


# --------------------------------------------------------------------------- #
# 命令执行
# --------------------------------------------------------------------------- #
def _run(args: list[str], timeout: int = 120, cwd: Optional[Path] = None) -> dict:
    """执行 baiduPCS-Go 命令，返回 {ok, code, stdout, stderr}。"""
    bin_path = _find_existing_binary()
    if not bin_path:
        return {"ok": False, "code": -1, "stdout": "", "stderr": "baiduPCS-Go 未安装，请先调用 /api/pcs/install"}
    cmd = [str(bin_path)] + args
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd or PCS_HOME),
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "HOME": str(Path.home())},
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        return {
            "ok": proc.returncode == 0,
            "code": proc.returncode,
            "stdout": proc.stdout or "",
            "stderr": proc.stderr or "",
            "combined": out,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": -2, "stdout": "", "stderr": f"命令超时（>{timeout}s）", "combined": f"命令超时（>{timeout}s）"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "code": -3, "stdout": "", "stderr": str(e), "combined": str(e)}


def ensure_home() -> None:
    PCS_HOME.mkdir(parents=True, exist_ok=True)
    PCS_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- #
# 登录态
# --------------------------------------------------------------------------- #
def _detect_cookie_format(raw: str) -> str:
    """检测 cookie 字符串格式，返回: 'header' | 'netscape' | 'bduss' | 'unknown'"""
    raw_stripped = raw.strip()
    # Netscape 格式：每行 "name\tvalue\tdomain\t..." （含 tab 分隔，有 domain 列如 .baidu.com）
    if "\t" in raw_stripped and (".baidu.com" in raw_stripped or ".miao.baidu.com" in raw_stripped):
        return "netscape"
    # 标准 header 格式：BDUSS=xxx; STOKEN=yyy
    if re.search(r'\bBDUSS\s*=', raw_stripped):
        return "header"
    # 纯 BDUSS 值（192 位左右 base64 字符串，允许 = padding）
    if len(raw_stripped) > 100:
        return "bduss"
    return "unknown"


def _extract_netscape_bduss(raw: str) -> Optional[str]:
    """从 Netscape 格式 cookie 文本中尝试提取 BDUSS 值。"""
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].strip().upper() == "BDUSS":
            return parts[1].strip()
    return None


def _clean_bduss(raw: str) -> str:
    """清理 BDUSS 值：去掉换行、首尾空白、多余空格。"""
    return "".join(raw.split()).strip()


# baiduPCS-Go 在登录失败时往往仍返回退出码 0（仅在输出里打印错误），
# 所以不能只信 res["ok"]（= 退出码），必须扫描输出里的失败标记。
_FAIL_PATTERNS = re.compile(
    r"错误代码|errno|登录失败|系统繁忙|密码错误|验证码|账号或密码|"
    r"invalid account|account is not|登录过期|请重新登录|验证失败|用户名或密码",
    re.IGNORECASE,
)


def _login_failed(combined: str) -> bool:
    """根据 baiduPCS-Go 输出判断登录是否真的失败。"""
    if not combined:
        return False
    return bool(_FAIL_PATTERNS.search(combined))


def _extract_uid(who_output: str) -> int:
    """从 who 命令输出中提取 uid，提取失败返回 0。"""
    m = re.search(r'uid[:\s]+(\d+)', who_output, re.IGNORECASE)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    return 0


def _verify_login() -> tuple[bool, str]:
    """登录后验证：调 who 确认 uid > 0。返回 (是否有效, 提示文本)。"""
    try:
        w = who()
        if w.get("logged_in") and w.get("ok"):
            uid = _extract_uid(w.get("message", "") or w.get("raw", ""))
            if uid > 0:
                return True, w.get("message", "")
            return False, f"登录态无效（uid=0，BDUSS 可能已过期或无效）\nwho 输出: {w.get('message', '')}"
        return False, f"登录后验证失败: {w.get('message', '未知')}"
    except Exception as e:
        # who 调用异常不阻断（可能是网络问题），仅记录
        logger.warning("登录后 who 验证异常（不阻断）: %s", e)
        return True, ""  # 放行，避免误拦


def login(raw: str) -> dict:
    """用 cookie 串或纯 BDUSS 登录。统一走 -cookies=（最可靠，工具会自行解析 BDUSS/STOKEN）。"""
    try:
        raw = (raw or "").strip()
        if not raw:
            return {"ok": False, "message": "请输入 cookie 或 BDUSS"}

        # 统一清理：去掉换行和多余空白（用户从 DevTools 复制时可能带换行）
        raw = _clean_bduss(raw)

        fmt = _detect_cookie_format(raw)

        # Netscape 格式（用户从 DevTools Application > Cookies 表格复制的）
        if fmt == "netscape":
            bduss = _extract_netscape_bduss(raw)
            if bduss:
                cookie_str = f"BDUSS={_clean_bduss(bduss)}"
            else:
                return {
                    "ok": False,
                    "message": "Cookie 格式不对：你粘贴的是「表格原始数据」格式（含 Tab 和域名列），且其中没有 BDUSS。\n\n"
                               "✗ 错误做法：从 DevTools Cookies 表格直接 Ctrl+A/Cmd+A 全选复制\n"
                               "✓ 正确做法：在表格中找到 BDUSS 那一行，只复制「值」那一列的内容",
                    "hint": "netscape_no_bduss",
                }
        # 标准 header 格式
        elif fmt == "header":
            cookie_str = raw
        elif fmt == "bduss":
            # 纯值或已带 BDUSS= 前缀
            if raw.upper().startswith("BDUSS="):
                cookie_str = raw
            else:
                cookie_str = f"BDUSS={raw}"
        else:
            cookie_str = f"BDUSS={raw}"

        ensure_home()
        res = _run(["login", f"-cookies={cookie_str}"], timeout=60)
        combined = res.get("combined", res.get("stderr", "未知错误"))
        # 退出码 0 不代表成功：baiduPCS-Go 失败时也常返回 0，需看输出
        if res["ok"] and not _login_failed(combined):
            # 二次验证：确认 who 返回有效 uid（防止退出码 0 但实际未登录）
            verified, hint = _verify_login()
            if not verified:
                return {"ok": False, "message": f"登录失败（凭证无效）：{hint}", "raw": _tail(combined, 12)}
            return {"ok": True, "message": "登录成功 ✓", "raw": _tail(combined, 6)}
        return {"ok": False, "message": "登录失败：" + _tail(combined, 3).strip(), "raw": _tail(combined, 12)}
    except Exception as e:
        logger.exception("baidu_pcs.login 异常")
        return {"ok": False, "message": f"登录过程出错：{e}"}


def login_by_password(username: str, password: str) -> dict:
    """用百度账号+密码登录（最简单可靠，不需要复制 Cookie）。"""
    try:
        username = (username or "").strip()
        password = (password or "").strip()
        if not username:
            return {"ok": False, "message": "请输入百度账号（手机号/邮箱/用户名）"}
        if not password:
            return {"ok": False, "message": "请输入密码"}

        ensure_home()
        res = _run(["login", f"-username={username}", f"-password={password}"], timeout=60)
        combined = res.get("combined", res.get("stderr", "未知错误"))
        if res["ok"] and not _login_failed(combined):
            # 二次验证：确认 who 返回有效 uid
            verified, hint = _verify_login()
            if not verified:
                return {"ok": False, "message": f"登录失败（凭证无效）：{hint}", "raw": _tail(combined, 12)}
            return {"ok": True, "message": "登录成功 ✓", "raw": _tail(combined, 6)}
        return {"ok": False, "message": "登录失败：" + _tail(combined, 3).strip(), "raw": _tail(combined, 12)}
    except Exception as e:
        logger.exception("baidu_pcs.login_by_password 异常")
        return {"ok": False, "message": f"登录过程出错：{e}"}


def who() -> dict:
    """返回当前登录账号信息。uid=0 视为未登录（无效凭证）。"""
    ensure_home()
    res = _run(["who"], timeout=30)
    combined = res.get("combined", res.get("stderr", ""))
    if not res["ok"]:
        return {"ok": False, "logged_in": False, "message": "未登录或查询失败", "raw": _tail(combined, 8)}
    txt = combined.strip()
    uid = _extract_uid(txt)
    if uid is not None and uid <= 0:
        return {"ok": False, "logged_in": False, "message": f"凭证无效（uid={uid}），请重新登录", "raw": txt}
    return {"ok": True, "logged_in": True, "message": txt, "raw": txt}


def status() -> dict:
    bin_path = _find_existing_binary()
    if not bin_path:
        return {"binary_installed": False, "binary_path": None, "logged_in": False, "who": None}
    try:
        w = who()
        return {
            "binary_installed": True,
            "binary_path": str(bin_path),
            "logged_in": w.get("logged_in", False),
            "who": w.get("message") if w.get("logged_in") else None,
        }
    except Exception as e:
        return {"binary_installed": True, "binary_path": str(bin_path), "logged_in": False, "who": None, "error": str(e)}


# --------------------------------------------------------------------------- #
# 分享转存 / 列出 / 下载
# --------------------------------------------------------------------------- #
def transfer(share_url: str, pwd: str = "") -> dict:
    """转存分享文件到我的网盘（根目录）。返回 {ok, message, raw}。"""
    ensure_home()
    url = (share_url or "").strip()
    if not url:
        return {"ok": False, "message": "分享链接为空"}
    args = ["transfer", url]
    if pwd:
        args.append(pwd)
    res = _run(args, timeout=120)
    combined = res.get("combined", res.get("stderr", ""))
    if res["ok"]:
        return {"ok": True, "message": "转存成功 ✓", "raw": _tail(combined, 10)}
    return {"ok": False, "message": "转存失败", "raw": _tail(combined, 15)}


def ls(path: str = "/") -> dict:
    """列出网盘目录。优先用 -json，失败回退纯文本。返回 {ok, items, raw}。"""
    ensure_home()
    path = path or "/"
    # 先试 json（更可靠地解析文件名/大小/类型）
    res = _run(["ls", "-json", path], timeout=60)
    combined = res.get("combined", res.get("stderr", ""))
    if res["ok"] and res.get("stdout", "").strip():
        try:
            data = json.loads(res["stdout"])
            items = _parse_ls_json(data)
            if items is not None:
                return {"ok": True, "items": items, "raw": _tail(combined, 10), "json": True}
        except Exception:  # noqa: BLE001
            pass
    # 回退纯文本
    res2 = _run(["ls", path], timeout=60)
    combined2 = res2.get("combined", res2.get("stderr", ""))
    return {"ok": res2["ok"], "items": None, "raw": _tail(combined2, 30), "json": False}


def _parse_ls_json(data) -> Optional[list]:
    # baiduPCS-Go -json 返回结构不稳定，做多种兼容
    try:
        if isinstance(data, dict):
            # 常见: {"list": [...], "path":...} 或 {"errCode":0,"list":[...]}
            arr = data.get("list") or data.get("files") or data.get("items")
        elif isinstance(data, list):
            arr = data
        else:
            return None
        if not isinstance(arr, list):
            return None
        items = []
        for it in arr:
            if not isinstance(it, dict):
                continue
            name = it.get("filename") or it.get("name") or it.get("path") or ""
            if not name:
                continue
            size = it.get("size") or it.get("filesize") or 0
            is_dir = bool(it.get("isdir") or it.get("isDir") or "/" in str(it.get("type", "")))
            items.append({"name": str(name).split("/")[-1] or str(name), "size": int(size or 0), "is_dir": is_dir})
        return items
    except Exception:  # noqa: BLE001
        return None


def download(remote_path: str, dest_dir: Optional[Path] = None,
             progress: Optional[ProgressCb] = None,
             timeout: int = 3600) -> dict:
    """下载网盘文件/目录到本地。流式解析进度回调。"""
    ensure_home()
    dest = Path(dest_dir or PCS_DOWNLOAD_DIR)
    dest.mkdir(parents=True, exist_ok=True)
    # 设置保存目录（config set -savedir，README 明确支持；download 不再带 --saveto 以免版本差异报错）
    _run(["config", "set", "-savedir", str(dest)], timeout=30)
    bin_path = _find_existing_binary()
    if not bin_path:
        return {"ok": False, "message": "baiduPCS-Go 未安装"}
    cmd = [str(bin_path), "download", remote_path]
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(PCS_HOME), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, env={**os.environ, "HOME": str(Path.home())},
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": f"启动下载失败：{e}"}

    last = ""
    percent = 0.0
    for line in proc.stdout:
        line = line.rstrip("\n")
        if not line:
            continue
        last = line
        m = re.search(r"(\d{1,3}(?:\.\d+)?)\s*%", line)
        if m:
            try:
                percent = float(m.group(1))
            except ValueError:
                pass
        if progress:
            progress({"stage": "downloading", "percent": percent, "line": line})
    proc.wait(timeout=timeout)
    if proc.returncode == 0:
        return {"ok": True, "message": "下载完成 ✓", "last": last, "percent": 100.0}
    return {"ok": False, "message": "下载失败", "last": last, "code": proc.returncode}


def _tail(text: str, n: int) -> str:
    if not text:
        return ""
    lines = [l for l in text.splitlines() if l.strip()]
    return "\n".join(lines[-n:])
