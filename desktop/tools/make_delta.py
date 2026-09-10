#!/usr/bin/env python3
"""make_delta.py — 生成「文件级增量更新包」（替代整包 bsdiff，避免 730MB bsdiff OOM）。

对 NEW_APP 与 OLD_APP 逐文件比较：
  - 内容/大小相同        -> 忽略（用户端保留已装副本）
  - 新增 / 变小文件变更   -> 原样打包（op=new）
  - 大文件（>BSDIFF_MIN）变更 -> 对该文件单独 bsdiff（op=bsdiff），仅几 MB
  - OLD 有而 NEW 无       -> 删除（op=del）

产出 zip 包：
  manifest.json  : [{"op","idx","path"}, ...]
  <idx>          : 该条目的原始字节 或 bsdiff 补丁字节

用法：python3 make_delta.py <old.app> <new.app> <out.delta> [bsdiff_bin]
"""
import os
import sys
import json
import hashlib
import subprocess
import zipfile

BSDIFF_MIN = 1 * 1024 * 1024  # 大于此体积的变更文件走 bsdiff（Mach-O 等，几十 MB 级也能压到几百 KB）
BSDIFF_MAX = 64 * 1024 * 1024  # 超过此体积不走 bsdiff（O(n^2) 内存开销）。实测 41MB 主二进制 bsdiff 仅需 10s / 0.45MB 补丁，
                               # 故上限放到 64MB 足以覆盖包内所有文件（最大单文件 ~41MB），避免主二进制被原样打包导致增量膨胀到 40MB。
SKIP_PREFIX = ("_CodeSignature",)  # 签名资源由客户端套用后重新 ad-hoc 签名生成，无需下发
# 打包期产物 / 测试数据，用户端无意义且体积大，不参与增量
SKIP_ANY = ("test", "__pycache__", ".pyc")


def sha256_of(p: str) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def walk(app: str) -> dict:
    files = {}
    for root, dirs, names in os.walk(app):
        for n in names:
            p = os.path.join(root, n)
            if os.path.isfile(p) or os.path.islink(p):
                rel = os.path.relpath(p, app)
                top = rel.split("/")[0]
                if any(top == s or ("/" + s + "/") in ("/" + rel + "/") for s in SKIP_PREFIX):
                    continue
                if any(("/" + s + "/") in ("/" + rel + "/") for s in SKIP_ANY):
                    continue
                files[rel] = p
    return files


def main(old_app: str, new_app: str, out: str, bsdiff: str) -> None:
    old = walk(old_app)
    new = walk(new_app)
    manifest = []
    idx = 0
    n_bsdiff = 0
    total = len(new)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for i, rel in enumerate(sorted(new), 1):
            np_ = new[rel]
            if i % 100 == 0 or i == total:
                print("[make_delta] %d/%d files processed (bsdiff=%d)" % (i, total, n_bsdiff), flush=True)
            if rel in old:
                try:
                    same = (os.path.getsize(old[rel]) == os.path.getsize(np_)
                            and sha256_of(old[rel]) == sha256_of(np_))
                except OSError:
                    same = False
                if same:
                    continue  # 未变更
                if BSDIFF_MIN < os.path.getsize(np_) <= BSDIFF_MAX and os.path.isfile(np_) and os.path.isfile(old[rel]):
                    patch = "/tmp/vdl_delta_patch_%d.bin" % idx
                    try:
                        subprocess.run([bsdiff, old[rel], np_, patch], check=True, timeout=180)
                    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                        # bsdiff 失败（体积过大/超时/二进制缺失）时回退原样打包，保证增量包一定能生成
                        if os.path.exists(patch):
                            os.remove(patch)
                    else:
                        z.write(patch, str(idx))
                        os.remove(patch)
                        manifest.append({"op": "bsdiff", "idx": idx, "path": rel,
                                         "mode": os.stat(np_).st_mode & 0o777})
                        idx += 1
                        n_bsdiff += 1
                        continue
            # 新增 / 小文件变更：原样打包
            if os.path.islink(np_):
                # 符号链接：记下目标，原样重建
                manifest.append({"op": "link", "idx": -1, "path": rel, "target": os.readlink(np_)})
            else:
                z.write(np_, str(idx))
                manifest.append({"op": "new", "idx": idx, "path": rel,
                                 "mode": os.stat(np_).st_mode & 0o777})
                idx += 1
        for rel in old:
            if rel not in new:
                manifest.append({"op": "del", "idx": -1, "path": rel})
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("usage: make_delta.py <old.app> <new.app> <out.delta> [bsdiff]", file=sys.stderr)
        sys.exit(2)
    bsdiff_bin = sys.argv[4] if len(sys.argv) > 4 else "bsdiff"
    main(sys.argv[1], sys.argv[2], sys.argv[3], bsdiff_bin)
