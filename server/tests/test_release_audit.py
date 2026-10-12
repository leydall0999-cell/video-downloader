#!/usr/bin/env python3
"""发布包机密核查工具的离线守卫 —— 2026-10-13 用户要求「发布的安装包绝对不含机密数据」。

本文件用**合成包**（临时目录/压缩包）验证扫描器的判定：
  ① 七类命中都能被抓到（凭据 / 私钥 / 服务端凭据文件 / 用户数据 / 内部信息 / 打包垃圾 / 我方明文源码）；
  ② 三个真实误报必须**不再**误报（都是 2026-10-13 实测踩到的）：
     · base64 流里偶然出现的 `AKIA…`（PIL/ImageFont.py 实际命中过）；
     · 公开 CA 根证书包 `certifi/cacert.pem`（内容无私钥块，不是机密）；
     · 二进制里的版本号 `10.11.12.13` 被当成私有网段 IP（libglib 实际命中过）；
  ③ 主可执行这类大二进制**不再被跳过**（34.5MB 的主程序曾整只漏扫）；
  ④ .zip 形态（发布用的压缩包）同样能扫。

运行：`.build_venv/bin/python server/tests/test_release_audit.py`
环境变量 `VDL_AUDIT_TOOL` 可指向别的副本（供变异测试用）。
"""
from __future__ import annotations

import importlib.util
import io
import os
import pathlib
import sys
import tempfile
import zipfile

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
TOOL = pathlib.Path(os.environ.get("VDL_AUDIT_TOOL") or (REPO / "server/tools/audit_release_secrets.py"))

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + name + (f" —— {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def _load_tool():
    spec = importlib.util.spec_from_file_location("vdl_audit_tool_under_test", TOOL)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["vdl_audit_tool_under_test"] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _write(root: pathlib.Path, rel: str, data: bytes) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def _clean_pkg(root: pathlib.Path) -> None:
    """一个「干净」的包：只有我们该有的东西，且不含任何机密。"""
    _write(root, "Contents/Resources/commentary/process.py", b"# pipeline\nprint(1)\n")
    _write(root, "Contents/Resources/yt_dlp_plugins/extractor/demo.py", b"# plugin\n")
    _write(root, "Contents/Resources/web/index.html", b"<html><a href='https://hanyuxz.top'>x</a></html>")
    _write(root, "Contents/Resources/server/build_info.txt", b"v1\n")
    _write(root, "Contents/Resources/certifi/cacert.pem", b"-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n")
    _write(root, "Contents/Frameworks/_internal/somepkg/__pycache__/a.pyc", b"\x00\x01")
    _write(root, "Contents/MacOS/VideoDownloader", b"#!/bin/sh\n" + b"\x00" * 1024)


def test_clean_package_passes() -> None:
    mod = _load_tool()
    with tempfile.TemporaryDirectory(prefix="vdl_audit_clean_") as td:
        root = pathlib.Path(td)
        _clean_pkg(root)
        r = mod.audit(root)
        check("干净包：A–D 全 0", all(not r["hits"][k] for k in
                                    ("A-凭据", "B-私钥证书", "C-服务端凭据文件", "D-用户数据")),
              str({k: r["hits"][k] for k in ("A-凭据", "B-私钥证书", "C-服务端凭据文件", "D-用户数据")}))
        check("干净包：我方区无垃圾（F1=0）", not r["hits"]["F1-我方区垃圾"], str(r["hits"]["F1-我方区垃圾"]))
        check("干净包：第三方区垃圾归 F2 不阻断",
              len(r["hits"]["F2-第三方区冗余"]) == 1 and r["ok"], str(r["hits"]["F2-第三方区冗余"]))
        check("干净包：公开 CA 包被识别为公开证书（非机密）",
              any("cacert.pem" in x for x in r["public_certs"]), str(r["public_certs"]))
        check("干净包：整体判定通过（可发布）", r["ok"] is True)
    print("✅ 干净包判定为可发布")


def test_each_class_is_caught() -> None:
    mod = _load_tool()
    with tempfile.TemporaryDirectory(prefix="vdl_audit_dirty_") as td:
        root = pathlib.Path(td)
        _clean_pkg(root)
        _write(root, "Contents/Resources/commentary/leak_cred.py",
               ('ak = "AKLT' + "A" * 40 + '"\nsk = "sk-' + "B" * 32 + '"\n').encode())
        _write(root, "Contents/Resources/commentary/leak_key.py",
               b"-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----\n")
        _write(root, "Contents/Resources/pay_server.py", "# 服务端源码不该进包\n".encode())
        _write(root, "Contents/Resources/users.json", b'{"a":{"is_admin":true}}\n')
        _write(root, "Contents/Resources/commentary/scripts/quota.py.bak_202610061902",
               "# 旧备份\n".encode())
        _write(root, "Contents/Resources/yt_dlp_plugins/__pycache__/x.cpython-313.pyc", b"\x00")
        _write(root, "Contents/Resources/commentary/deploy_note.py",
               b'VPS = "8.138.223.3"\nLIB = "/opt/vdl-update/x"\n')
        _write(root, "Contents/Resources/commentary/keys/id_ed25519", b"key material\n")

        r = mod.audit(root)
        check("抓到明文凭据（AKLT / sk-）", any("leak_cred" in x for x in r["hits"]["A-凭据"]),
              str(r["hits"]["A-凭据"]))
        check("抓到私钥块", any("leak_key" in x for x in r["hits"]["A-凭据"]), str(r["hits"]["A-凭据"]))
        check("抓到按文件名判定的密钥文件", any("id_ed25519" in x for x in r["hits"]["B-私钥证书"]),
              str(r["hits"]["B-私钥证书"]))
        check("抓到服务端凭据文件（pay_server.py）",
              any("pay_server" in x for x in r["hits"]["C-服务端凭据文件"]), str(r["hits"]["C-服务端凭据文件"]))
        check("抓到用户数据文件（users.json）", any("users.json" in x for x in r["hits"]["D-用户数据"]))
        check("抓到内部 IP 与内部路径",
              len(r["hits"]["E-内部信息"]) >= 2, str(r["hits"]["E-内部信息"]))
        check("抓到我方区垃圾（.bak + 插件 __pycache__）",
              len(r["hits"]["F1-我方区垃圾"]) == 2, str(r["hits"]["F1-我方区垃圾"]))
        check("我方明文源码被单列（不算机密但不隐身）",
              any("process.py" in x for x in r["hits"]["G-我方明文源码"]))
        check("脏包整体判定为不可发布", r["ok"] is False)
        check("凭据证据做了掩码（不回显完整凭据）",
              all("AKLT" not in x or "…" in x for x in r["hits"]["A-凭据"]),
              str(r["hits"]["A-凭据"])[:120])
    print("✅ 七类命中全部抓到，且证据已掩码")


def test_false_positives_stay_fixed() -> None:
    """三个真实误报的回归钉子 —— 谁把它们改回去，这里立刻红。"""
    mod = _load_tool()
    with tempfile.TemporaryDirectory(prefix="vdl_audit_fp_") as td:
        root = pathlib.Path(td)
        _clean_pkg(root)
        # ① base64 流里偶然出现 AKIA
        b64 = b"QAHAEwAAQAAAAAAAgAHAGQAAQAAAAAAAwAaAKIA" + b"B" * 40
        _write(root, "Contents/Resources/PIL/ImageFont.py", b64 + b"\n")
        # ② 二进制里的版本号 10.11.12.13
        _write(root, "Contents/Frameworks/cv2/libglib.dylib", b"\x7fELF" + b"\x00" * 32 + b"10.11.12.13")
        r = mod.audit(root)
        check("① base64 中的 AKIA 不再误报为凭据",
              not any("ImageFont" in x for x in r["hits"]["A-凭据"]), str(r["hits"]["A-凭据"]))
        check("② 二进制版本号 10.11.12.13 不再误报为内网 IP",
              not any("libglib" in x for x in r["hits"]["E-内部信息"]), str(r["hits"]["E-内部信息"]))
        check("两个误报都不影响「可发布」判定", r["ok"] is True)
    print("✅ 已知误报不会复发（base64 假 AKIA / 版本号假内网 IP）")


def test_large_binary_is_scanned() -> None:
    """大二进制不再被跳过：把凭据深埋进 20MB 的「主可执行」里必须被抓到。"""
    mod = _load_tool()
    with tempfile.TemporaryDirectory(prefix="vdl_audit_big_") as td:
        root = pathlib.Path(td)
        _clean_pkg(root)
        blob = b"\x00" * (20 * 1024 * 1024) + b'key = "AKLT' + b"C" * 40 + b'"'
        _write(root, "Contents/MacOS/VideoDownloader", blob)
        r = mod.audit(root)
        check("20MB 主可执行里的凭据被抓到",
              any("VideoDownloader" in x for x in r["hits"]["A-凭据"]), str(r["hits"]["A-凭据"])[:120])
        check("大文件计数为「部分扫描」或完整扫描均被记录",
              isinstance(r["partial_scanned"], list))
    print("✅ 大二进制（主可执行）不再漏扫")


def test_zip_form() -> None:
    mod = _load_tool()
    with tempfile.TemporaryDirectory(prefix="vdl_audit_zip_") as td:
        root = pathlib.Path(td)
        _clean_pkg(root)
        _write(root, "Contents/Resources/commentary/leak.py", b'x = "AKLT' + b"D" * 40 + b'"')
        zp = root.parent / "pkg.zip"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for p in root.rglob("*"):
                if p.is_file():
                    z.write(p, str(p.relative_to(root)))
        zp.write_bytes(buf.getvalue())
        try:
            r = mod.audit(zp)
            check("zip 形态同样能扫出凭据", any("leak.py" in x for x in r["hits"]["A-凭据"]),
                  str(r["hits"]["A-凭据"])[:120])
            check("zip 形态判定为不可发布", r["ok"] is False)
        finally:
            zp.unlink(missing_ok=True)
    print("✅ 发布用的 .zip 形态同样被核查")


def test_ditto_style_zip() -> None:
    """**发布包里真正的形态**：`ditto -c -k --keepParent` 打的包。

    它有三个与普通 zip 不同的特征，全都实测踩过（2026-10-13）：
      ① 每个条目都带 `VideoDownloader.app/` 前缀 ⇒ 不归一化会让「是否第三方/我方区」判定失效；
      ② 中文条目不置 UTF-8 标志 ⇒ 读出来是 CP437 乱码（「视频工坊」变 `ΦºåΘóæσ╖Ñσ¥è`）；
      ③ 为带扩展属性的文件生成 `._xxx` 伴生条目。
    本用例锁住：三者在核查里都被正确对待，且**不产生假命中**。
    """
    mod = _load_tool()
    with tempfile.TemporaryDirectory(prefix="vdl_audit_ditto_") as td:
        root = pathlib.Path(td)
        _clean_pkg(root)
        # 我方区里放一个中文名的泄露文件（必须被抓到，且证明 CP437 名不影响判定）
        _write(root, "Contents/Resources/commentary/泄露的密钥.py",
               ('k = "AKLT' + "E" * 40 + '"\n').encode())

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("VideoDownloader.app/", b"")                    # 目录条目
            for p in root.rglob("*"):
                if p.is_file():
                    z.writestr("VideoDownloader.app/" + str(p.relative_to(root)), p.read_bytes())
            z.writestr("VideoDownloader.app/Contents/Resources/commentary/._process.py",
                       b"\x00\x05\x16\x07")                              # AppleDouble 伴生
            _nm = "视频工坊".encode("utf-8").decode("cp437")            # ditto 风格中文名
            z.writestr("VideoDownloader.app/Contents/MacOS/" + _nm, b"\x7fELF" + b"\x00" * 16)
        zp = root.parent / "ditto_style.zip"
        zp.write_bytes(buf.getvalue())
        try:
            # ① CP437 名必须能被还原（否则中文名条目一律判错）。
            #    ⚠️ 无法用 zipfile.writestr 造出这种条目（它会强制置 UTF-8 标志），
            #    所以直接构造 ZipInfo 验证解码逻辑；真实形态已在发布包上实测过
            #    （ditto 打的包不置标志，`Contents/MacOS/视频工坊` 读出来是乱码）。
            zi = zipfile.ZipInfo()
            zi.filename = "视频工坊".encode("utf-8").decode("cp437")
            zi.flag_bits = 0
            check("CP437 中文条目名可还原为「视频工坊」",
                  mod._decode_zip_name(zi) == "视频工坊", mod._decode_zip_name(zi))
            zi2 = zipfile.ZipInfo(filename="ascii-name.py")
            check("已是 UTF-8/ASCII 的名字原样返回",
                  mod._decode_zip_name(zi2) == "ascii-name.py")

            r = mod.audit(zp)
            check("ditto 风格 zip：中文名条目走对了判定路径（凭据被抓到）",
                  any("泄露的密钥" in x for x in r["hits"]["A-凭据"]), str(r["hits"]["A-凭据"])[:120])
            check("ditto 风格 zip：前缀不影响我方区判定（G 有内容）",
                  len(r["hits"]["G-我方明文源码"]) >= 2, str(len(r["hits"]["G-我方明文源码"])))
            check("ditto 风格 zip：AppleDouble 伴生文件归噪音（不进源码/垃圾清单）",
                  any("._process" in x for x in r["hits"]["F2-第三方区冗余"])
                  and not any("._" in x for x in r["hits"]["G-我方明文源码"])
                  and not any("._" in x for x in r["hits"]["F1-我方区垃圾"]),
                  f"F2={r['hits']['F2-第三方区冗余'][:3]} G={r['hits']['G-我方明文源码'][:3]}")
            check("ditto 风格 zip：第三方库仍被白名单（不虚报 A 类）",
                  not any("Frameworks" in x for x in r["hits"]["A-凭据"]), str(r["hits"]["A-凭据"])[:120])
            check("ditto 风格 zip：判定为不可发布", r["ok"] is False)
        finally:
            zp.unlink(missing_ok=True)
    print("✅ ditto 风格 zip（前缀 / CP437 中文名 / AppleDouble）判定正确")


def test_our_zones_cover_the_real_areas() -> None:
    """棘轮：我方区清单必须覆盖构建真正 add-data 的目录（否则垃圾会悄悄变成"第三方区"）。"""
    mod = _load_tool()
    build = (REPO / "desktop" / "build_mac.sh").read_text(encoding="utf-8")
    for zone, needle in (
        ("Contents/Resources/commentary", "commentary"),
        ("Contents/Resources/yt_dlp_plugins", "yt_dlp_plugins"),
        ("Contents/Resources/server", "SERVER_STAGING:server"),
        ("Contents/Resources/web", "REPO/web:web"),
        ("Contents/Resources/extension", "REPO/extension:extension"),
    ):
        check(f"我方区清单含 {zone}", zone in mod.OUR_ZONES, zone)
        check(f"构建脚本确有 add-data/staging 对应 {needle}", needle in build, needle)
    check("构建脚本已把插件改为 staging（防 __pycache__ 进包）",
          "YDLP_STAGING" in build and "--exclude '__pycache__'" in build)
    check("构建脚本已排除注释管线里的 .bak 残留", "! -name '*.bak*'" in build)
    check("构建自验证里接了机密核查", "audit_release_secrets.py" in build)
    print("✅ 我方区清单与构建脚本接线一致")


def main() -> int:
    print(f"被测工具：{TOOL}")
    print("")
    print("▶ 发布包机密核查工具守卫")
    test_clean_package_passes()
    test_each_class_is_caught()
    test_false_positives_stay_fixed()
    test_large_binary_is_scanned()
    test_zip_form()
    test_ditto_style_zip()
    test_our_zones_cover_the_real_areas()
    print("")
    if FAILS:
        print(f"❌ 存在 {len(FAILS)} 项失败：")
        for f in FAILS:
            print("   -", f)
        return 1
    print("🎉 发布包机密核查工具守卫全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
