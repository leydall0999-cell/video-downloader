# -*- coding: utf-8 -*-
"""包内核验：把 PYZ 里的字节码与本地源码编译结果做递归结构指纹比对。

用法：.build_venv/bin/python tools/verify_pyz_fingerprint.py <app可执行文件> <源码根>
判定：指纹一致 = 包内代码与源码树同代；不一致 = 包是旧代码或源码有未构建改动。
"""
import marshal
import struct
import sys
import zlib


def code_fp(code, acc):
    """递归结构指纹：函数名/引用名/字符串常量/嵌套 code 的拓扑。"""
    acc.append(("C", code.co_name, tuple(code.co_names)))
    for c in code.co_consts:
        if isinstance(c, str):
            acc.append(("s", c))
        elif isinstance(c, tuple):
            acc.append(("t", tuple(x for x in c if isinstance(x, (str, int, float, bool, type(None))))))
        elif hasattr(c, "co_code"):
            code_fp(c, acc)
    return acc


def fp_hex(code):
    import hashlib
    h = hashlib.sha256()
    for item in code_fp(code, []):
        h.update(repr(item).encode("utf-8", "replace"))
    return h.hexdigest()[:24]


def load_pyz(path):
    data = open(path, "rb").read()
    p = data.find(b"PYZ\x00")
    if p < 0:
        raise SystemExit(f"❌ {path} 里找不到 PYZ 魔数")
    toc_off = struct.unpack("!I", data[p + 8:p + 12])[0]
    toc = dict(marshal.loads(data[p + toc_off:]))
    cache = {}

    def get(name):
        if name in cache:
            return cache[name]
        typ, pos, length = toc[name]
        raw = data[p + pos:p + pos + length]
        try:
            payload = zlib.decompress(raw)
        except zlib.error:
            payload = raw
        cache[name] = marshal.loads(payload)
        return cache[name]

    return get, sorted(toc.keys())


def main():
    exe, src_root = sys.argv[1], sys.argv[2]
    import pathlib
    get, names = load_pyz(exe)
    print(f"PYZ 条目总数：{len(names)}")
    checks = [
        ("image_download", f"{src_root}/server/image_download.py"),
        ("routers.image", f"{src_root}/server/routers/image.py"),
        ("tasks", f"{src_root}/server/tasks.py"),
        ("routers.core", f"{src_root}/server/routers/core.py"),
        ("app", f"{src_root}/server/app.py"),
    ]
    bad = 0
    for mod, src in checks:
        if mod not in names:
            print(f"❌ {mod}: 不在 PYZ 里（漏打包！）")
            bad += 1
            continue
        pkg_code = get(mod)
        pkg_fp = fp_hex(pkg_code)
        local = compile(pathlib.Path(src).read_text(encoding="utf-8"), src, "exec")
        loc_fp = fp_hex(local)
        ok = pkg_fp == loc_fp
        print(f"{'✅' if ok else '❌'} {mod}: 包 {pkg_fp} / 源码 {loc_fp}"
              f"{'' if ok else '  ← 不一致'}")
        if not ok:
            bad += 1
    # 新功能符号抽检（防「指纹算法太粗」）
    for sym in ("_SafeRedirectHandler", "normalize_image_url", "run_image_download",
                "sniff_image_ext", "derive_filename"):
        code = get("image_download")
        found = sym in code.co_names or any(
            sym in c.co_names for c in code.co_consts if hasattr(c, "co_code"))
        print(f"{'✅' if found else '❌'} image_download.{sym} 在包内可见")
        if not found:
            bad += 1
    img_router = get("routers.image")
    print(f"{'✅' if '/api/image/download' in img_router.co_consts or '/api/image/download' in [c for c in img_router.co_consts if isinstance(c, str)] else '❌'}"
          f" routers.image 路由路径 /api/image/download 在包内")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
