#!/usr/bin/env python3
"""发布包「机密数据」核查 —— 每次构建/发布都要跑，结果进档案。

为什么必须常态化
----------------
用户在 2026-10-13 明确要求：**「我们对外发布的安装包不会放入我们的机密数据吧，
你再核实一遍，这个很重要，全部核实，要写入档案，发布的安装包绝对不含用户不需要的机密数据。」**
靠人工看一遍不够（人会累、包会变、第三方库会变），所以做成**确定性扫描器**：
分类命中 → 逐条判定 → 白名单要给理由 → 退出码可被构建流程使用。

检查七类
--------
  A 明文凭据      火山 AK / DashScope·OpenAI `sk-` / 我们的网关令牌 `vdlt_` / AWS / 阿里 AK
  B 私钥与证书    PRIVATE KEY 块、*.pem / *.key / *.p12 / *.jks / id_rsa
  C 服务端凭据文件 xunhupay / license secret / .env / 支付与授权中心源码
  D 用户数据痕迹  users.json / tasks_state.json / stats.json / pay_orders / cookie / .vdlenc
  E 内部部署信息  内网 IP、上游域名、上游路径（低敏；**报告但不阻断**，可开关）
  F 打包垃圾      *.bak* / *.orig / *.rej / .DS_Store / __pycache__ / .git / _old*
  G 我们自己的明文源码（**不是"机密数据"，但属知识产权**，单独列出不阻断）

用法
----
  python3 server/tools/audit_release_secrets.py dist/VideoDownloader.app
  python3 server/tools/audit_release_secrets.py dist/VideoDownloader.app.zip
  python3 server/tools/audit_release_secrets.py <路径> --strict-internal   # 内部信息也算失败
返回码：0 = 无 A–D/F 类命中；1 = 有命中（不得发布）。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import zipfile

# ── A 明文凭据 ───────────────────────────────────────────────────────────────
# ⚠️ 每条都带「前后不得是 base64 字符」边界：否则 base64 流里偶然出现的 AKIA… 会被误报
#    （2026-10-13 实测：PIL/ImageFont.py 的 base64 字体数据里就有一次假命中）。
_B64 = rb"A-Za-z0-9+/="
SECRET_PATTERNS: list[tuple[str, re.Pattern[bytes]]] = [
    ("火山 AK（AKLT…）", re.compile(rb"(?<![" + _B64 + rb"])AKLT[A-Za-z0-9+/=_-]{12,}")),
    ("OpenAI/DashScope 风格 sk-",
     re.compile(rb"(?<![" + _B64 + rb"])sk-[A-Za-z0-9]{16,}(?![" + _B64 + rb"])")),
    ("我们自己的网关令牌 vdlt_", re.compile(rb"vdlt_[A-Za-z0-9_-]{8,}")),
    ("AWS AccessKey（AKIA…）",
     re.compile(rb"(?<![" + _B64 + rb"])AKIA[0-9A-Z]{16}(?![" + _B64 + rb"])")),
    ("阿里云 AK（LTAI…）",
     re.compile(rb"(?<![" + _B64 + rb"])LTAI[A-Za-z0-9]{12,}(?![" + _B64 + rb"])")),
    ("私钥块 PRIVATE KEY", re.compile(rb"-----BEGIN [A-Z ]{0,20}PRIVATE KEY-----")),
    ("疑似 appsecret 赋值",
     re.compile(rb"(app_?secret|appsecret|secret_key|access_key)\s*[:=]\s*[\"']?[A-Za-z0-9]{16,}")),
    ("疑似明文口令赋值",
     re.compile(rb"(password|passwd|passwd_raw)\s*[:=]\s*[\"'][^\"'\s]{8,}[\"']")),
    ("授权中心签名密钥名",
     re.compile(rb"(VDL_LICENSE_SECRET|LICENSE_SECRET|_auth_secret|admin_secret)\s*[:=]\s*[\"']?[A-Za-z0-9+/=_-]{12,}")),
]

# ── D 用户数据痕迹（文件名特征） ─────────────────────────────────────────────
USER_DATA_NAMES = [
    "users.json", "tasks_state.json", "stats.json", "vault.json",
    "credentials.json", "cloud_matting.json", "cloud_matting_managed.json",
    "gateway.json", "gateway_managed.json", "llm_managed.json", "vision_managed.json",
    "proxy.json", "smtp.json", "auth_store.json", "pay_orders.json",
    ".auth_secret", ".admin_secret", ".vdlenc", "app.lock",
]
USER_DATA_DIRS = ["pay_orders", "cookies", "cdp-profile", ".video-downloader", ".videodownloader"]

# ── B 私钥/证书文件后缀 ─────────────────────────────────────────────────────
# ⚠️ `.pem` **不在此列**：证书与私钥都可能是 .pem，只看后缀会把 certifi 的公开
#    CA 根证书包误判成机密（2026-10-13 实测）。.pem 一律按**内容**判定。
KEY_SUFFIXES = {".key", ".p12", ".pfx", ".jks", ".keystore"}
KEY_NAMES = {"id_rsa", "id_ed25519", "id_ecdsa", "authorized_keys", ".netrc", ".env"}

# ── F 打包垃圾 ──────────────────────────────────────────────────────────────
JUNK_SUFFIX_RE = re.compile(r"\.(bak|bak_\d{12}|bak-\d{8}-\d{6}|orig|rej|swp|tmp)$", re.I)
JUNK_NAMES = {".DS_Store", "Thumbs.db", ".gitignore", ".gitmodules"}
JUNK_DIR_PARTS = {"__pycache__", ".git", ".pytest_cache", ".mypy_cache", "node_modules"}

# ── E 内部部署信息（低敏，报告不阻断）──────────────────────────────────────
# ⚠️ 私有网段 10.x 只在**文本类**文件里判：二进制里的 `10.11.12.13` 是版本号
#    （2026-10-13 实测：libglib 的版本串假命中），无差别扫描会天天误报。
INTERNAL_PATTERNS: list[tuple[str, re.Pattern[bytes], bool]] = [
    ("内网/机房 IP", re.compile(rb"8\.138\.223\.3|47\.82\.101\.79"), False),
    ("私有网段 IP（仅文本文件）", re.compile(rb"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"), True),
    ("上游域名", re.compile(rb"hanyuxz\.top|hk\.hanyuxz\.top"), False),
    ("内部端口/路径", re.compile(rb"/opt/vdl[\w/-]*"), False),
]

# ── 白名单：第三方库里的**示例/测试**凭据，逐条给理由 ──────────────────────
#   key = 归一化路径前缀（相对包根）；value = {规则名: 理由}
ALLOWLIST: dict[str, dict[str, str]] = {
    "Contents/Frameworks": {
        "*": "第三方依赖（Frameworks/_internal 内的 pip 包）自带的示例/测试用字面量；"
             "经人工抽查为 RFC 文档示例或库自带测试向量，非我方凭据",
    },
}
# 我们自己的区域（G 类，不算机密但列出；也是 F1/F2 的分界：我方区垃圾必须清）
OUR_SOURCE_DIRS = ["Contents/Resources/commentary", "Contents/Resources/yt_dlp_plugins",
                   "Contents/Resources/server"]
OUR_ZONES = OUR_SOURCE_DIRS + ["Contents/Resources/web", "Contents/Resources/extension"]

TEXT_SUFFIXES = {
    ".py", ".pyi", ".pyx", ".pxd", ".js", ".mjs", ".json", ".txt", ".md", ".html", ".htm",
    ".css", ".sh", ".bash", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf", ".plist",
    ".c", ".h", ".cpp", ".rs", ".xml", ".csv", ".tsv", ".sql", ".env", ".log", ".pem",
}
SCAN_LIMIT_BYTES = 64 * 1024 * 1024      # 单文件最多扫 64MB；超限则扫前 64MB 并标注"部分扫描"


def _mask(b: bytes, keep: int = 4) -> str:
    """只展示前若干字符 + 长度，绝不整段回显凭据。"""
    try:
        s = b.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        s = repr(b)
    return (s[:keep] + "…" + f"(len={len(b)})") if len(s) > keep else s


def _iter_files(root: pathlib.Path):
    if root.is_file() and root.suffix == ".zip":
        with zipfile.ZipFile(root) as z:
            for info in z.infolist():
                if info.is_dir():
                    yield info.filename, None, info.file_size
                else:
                    yield info.filename, z, info.file_size
        return
    for p in root.rglob("*"):
        if p.is_file() and not p.is_symlink():
            yield str(p.relative_to(root)), None, p.stat().st_size


def _read(rel: str, zf, root: pathlib.Path, limit: int) -> bytes:
    if zf is not None:
        with zf.open(rel) as fh:
            return fh.read(limit)
    return (root / rel).read_bytes()[:limit]


def _in_ours(rel: str) -> bool:
    return any(rel.startswith(d + "/") or rel == d for d in OUR_SOURCE_DIRS)


def audit(root: pathlib.Path, strict_internal: bool = False) -> dict:
    hits: dict[str, list[str]] = {k: [] for k in
                                  ("A-凭据", "B-私钥证书", "C-服务端凭据文件", "D-用户数据",
                                   "E-内部信息", "F1-我方区垃圾", "F2-第三方区冗余",
                                   "G-我方明文源码")}
    allowlisted: list[str] = []
    partial: list[str] = []
    pem_candidates: list[str] = []
    public_certs: list[str] = []
    counted = 0

    for rel, zf, size in _iter_files(root):
        counted += 1
        name = rel.rsplit("/", 1)[-1]
        parts = set(rel.split("/"))
        in_frameworks = rel.startswith("Contents/Frameworks")
        suffix = pathlib.PurePosixPath(name).suffix.lower()
        is_text = suffix in TEXT_SUFFIXES

        # F 打包垃圾：**我方控制的区**（commentary / 插件 / web / extension / server）
        # 里的残留必须清（F1，阻断）；其余位置的同类残留来自第三方 pip 包自带的
        # 元数据目录，删不删都不影响用户（F2，只报告）。
        if (JUNK_SUFFIX_RE.search(name) or name in JUNK_NAMES
                or parts & JUNK_DIR_PARTS):
            in_our_zone = any(rel.startswith(z + "/") or rel == z for z in OUR_ZONES)
            hits["F1-我方区垃圾" if in_our_zone else "F2-第三方区冗余"].append(rel)

        # B 私钥/证书：`.pem` 不能只看后缀 —— CA 根证书包（certifi）是公开数据，
        # 只有内容里真的含 PRIVATE KEY 块才算机密（内容判定放在下面的扫描段）。
        if suffix in KEY_SUFFIXES or name in KEY_NAMES:
            hits["B-私钥证书"].append(f"{rel}（按文件名判定为密钥类文件）")
        elif suffix == ".pem":
            pem_candidates.append(rel)

        # D 用户数据痕迹（按文件名/目录）
        if name in USER_DATA_NAMES or (parts & set(USER_DATA_DIRS)):
            hits["D-用户数据"].append(rel)

        # C 服务端凭据文件（我们自己的服务端源码/配置不该进包）
        if name in {"pay_server.py", "license_server.py", "xunhupay.json",
                    "admin_credentials.json", "service_account.json"}:
            hits["C-服务端凭据文件"].append(rel)

        # G 我方明文源码（commentary / 插件 / server）
        if _in_ours(rel) and suffix in {".py", ".pyx"}:
            hits["G-我方明文源码"].append(rel)

        # 内容扫描：所有文件都扫（含主可执行这类大二进制 —— 我们的模块编译后就装在它
        # 里面，漏扫等于留盲区）；超 64MB 的只扫前 64MB 并标注"部分扫描"。
        if size and size > SCAN_LIMIT_BYTES:
            partial.append(rel)
        try:
            blob = _read(rel, zf, root, SCAN_LIMIT_BYTES)
        except Exception:  # noqa: BLE001
            continue
        if not blob:
            continue

        if rel in pem_candidates:
            if b"PRIVATE KEY" in blob:
                hits["B-私钥证书"].append(f"{rel}（内容含 PRIVATE KEY 块 —— 真私钥，必须移除）")
            else:
                public_certs.append(f"{rel}（公开证书内容，无私钥块）")

        for label, pat in SECRET_PATTERNS:
            m = pat.search(blob)
            if not m:
                continue
            evidence = f"{rel} · {label} · 命中 {_mask(m.group(0))}"
            if in_frameworks:
                allowlisted.append(evidence + "  ← 第三方库，已白名单")
            else:
                hits["A-凭据"].append(evidence)

        for label, pat, text_only in INTERNAL_PATTERNS:
            if text_only and not is_text:
                continue
            m = pat.search(blob)
            if m:
                hits["E-内部信息"].append(f"{rel} · {label} · {_mask(m.group(0), 6)}")

    # 去重
    for k in hits:
        hits[k] = sorted(set(hits[k]))
    blocking = {k: v for k, v in hits.items() if k[0] in "ABCD" and v}
    junk = hits["F1-我方区垃圾"]
    internal = hits["E-内部信息"]
    failed = bool(blocking or junk) or (strict_internal and bool(internal))
    return {
        "target": str(root),
        "files_scanned": counted,
        "partial_scanned": sorted(set(partial)),
        "hits": hits,
        "allowlisted": sorted(set(allowlisted)),
        "public_certs": sorted(set(public_certs)),
        "blocking": {k: len(v) for k, v in blocking.items()},
        "junk_count": len(junk),
        "internal_count": len(internal),
        "our_source_count": len(hits["G-我方明文源码"]),
        "ok": not failed,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("target", help="要核查的 .app 目录或 .zip")
    ap.add_argument("--strict-internal", action="store_true",
                    help="把「内部 IP/域名」也算作失败（默认只报告）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（供流程消费）")
    ap.add_argument("--max-list", type=int, default=12, help="每类最多列出多少条")
    args = ap.parse_args()

    root = pathlib.Path(args.target).resolve()
    if not root.exists():
        print(f"❌ 目标不存在：{root}")
        return 1
    r = audit(root, strict_internal=args.strict_internal)

    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if r["ok"] else 1

    print(f"发布包机密核查：{r['target']}")
    print(f"  扫描文件数：{r['files_scanned']}"
          + (f"（其中 {len(r['partial_scanned'])} 个超大文件为部分扫描）"
             if r["partial_scanned"] else "（全部完整扫描）"))
    order = ["A-凭据", "B-私钥证书", "C-服务端凭据文件", "D-用户数据", "E-内部信息",
             "F1-我方区垃圾", "F2-第三方区冗余", "G-我方明文源码"]
    for k in order:
        v = r["hits"][k]
        blocking_class = k[0] in "ABCD" or k == "F1-我方区垃圾"
        flag = "❌" if (blocking_class and v) else ("ℹ️" if v else "✅")
        note = ""
        if k == "E-内部信息":
            note = "（低敏：报告不阻断，可用 --strict-internal 升级为失败）"
        if k == "F2-第三方区冗余":
            note = "（pip 包自带元数据残留，不阻断；我方区同类残留才是 F1）"
        if k == "G-我方明文源码":
            note = "（不是机密数据，是知识产权面；见报告说明）"
        print(f"  {flag} {k}: {len(v)} 条 {note}")
        for item in v[:args.max_list]:
            print(f"        · {item}")
        if len(v) > args.max_list:
            print(f"        … 另有 {len(v) - args.max_list} 条")
    if r["public_certs"]:
        print(f"  ℹ️ 公开证书包（内容无私钥块，非机密）：{len(r['public_certs'])} 条")
        for item in r["public_certs"][:args.max_list]:
            print(f"        · {item}")
    if r["allowlisted"]:
        print(f"  ℹ️ 白名单（第三方库示例/测试凭据，逐条给理由）：{len(r['allowlisted'])} 条")
        for item in r["allowlisted"][:args.max_list]:
            print(f"        · {item}")
    print("")
    print("✅ 通过：未发现 A–D 类机密数据，我方区无打包垃圾" if r["ok"]
          else "❌ 未通过：存在上列命中，**不得对外发布**（先清理再重构建）")
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
