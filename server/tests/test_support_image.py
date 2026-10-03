# -*- coding: utf-8 -*-
"""守卫：客服消息「发图片 + 搜历史」（2026-10-03）。

钉住三件事：
  1) 图片解码：白名单格式 + 大小上限 + 魔数二次校验（防伪装成图片的任意文件），
     非法输入一律抛 ValueError 而不是写盘；
  2) 附件路径只认「我们自己生成的 32 位十六进制文件名」，杜绝 ../ 穿越；
  3) 历史消息搜索：超管搜全部会话、普通用户只搜自己的；命中带会话/角色/片段，
     且按时间倒序。

全部在 VDL_DATA_DIR 临时目录内进行，绝不写真实家目录。
"""
from __future__ import annotations

import base64
import os
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

_tmp = tempfile.mkdtemp(prefix="vdl_support_img_")
os.environ["VDL_DATA_DIR"] = _tmp

from routers import support as S                            # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool) -> None:
    print(("  ✅ " if cond else "  ❌ ") + name)
    if not cond:
        FAILS.append(name)


def _png_b64(extra: bytes = b"") -> str:
    raw = b"\x89PNG\r\n\x1a\n" + extra
    return base64.b64encode(raw).decode("ascii")


def test_image_decode() -> None:
    print("\n[A] 图片解码：格式白名单 + 上限 + 魔数校验")
    name, data, mime = S._decode_image_payload({"mime": "image/png", "data_b64": _png_b64(b"fake")})
    check("[解码] 合法 PNG 返回 32 位十六进制文件名 + png 后缀",
          S._IMG_NAME_RE.match(name) is not None and name.endswith(".png") and mime == "image/png")
    check("[解码] 原始字节原样保留", data.startswith(b"\x89PNG\r\n\x1a\n"))

    jpg, _, _ = S._decode_image_payload({
        "mime": "image/jpeg",
        "data_b64": base64.b64encode(b"\xff\xd8\xff\xe0rest").decode("ascii"),
    })
    check("[解码] JPEG 落 .jpg", jpg.endswith(".jpg"))
    webp, _, _ = S._decode_image_payload({
        "mime": "image/webp",
        "data_b64": base64.b64encode(b"RIFF\x00\x00\x00\x00WEBPrest").decode("ascii"),
    })
    check("[解码] WebP 落 .webp", webp.endswith(".webp"))

    for bad, why in (
        ({"mime": "application/pdf", "data_b64": _png_b64()}, "非图片 mime 被拒"),
        ({"mime": "image/png", "data_b64": ""}, "空内容被拒"),
        ({"mime": "image/png", "data_b64": base64.b64encode(b"not-an-image").decode()}, "魔数不符被拒"),
    ):
        try:
            S._decode_image_payload(bad)
            check(f"[解码] {why}", False)
        except ValueError:
            check(f"[解码] {why}", True)

    big = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * (S._IMG_MAX_BYTES + 10)).decode("ascii")
    try:
        S._decode_image_payload({"mime": "image/png", "data_b64": big})
        check("[解码] 超限图片被拒", False)
    except ValueError:
        check("[解码] 超限图片被拒", True)

    durl = "data:image/png;base64," + _png_b64(b"x")
    n2, _, _ = S._decode_image_payload({"mime": "image/png", "data_b64": durl})
    check("[解码] data: 前缀自动剥离", S._IMG_NAME_RE.match(n2) is not None)


def test_media_path_guards() -> None:
    print("\n[B] 附件路径防穿越")
    check("[路径] 合法名可解析", S._media_path("a" * 32 + ".png") is not None)
    for bad in ("../../etc/passwd", "a" * 31 + ".png", "a" * 32 + ".exe", "a" * 32 + ".png/../x", ""):
        check(f"[路径] 非法名被拒：{bad[:24]!r}", S._media_path(bad) is None)


def test_search_messages() -> None:
    print("\n[C] 历史消息搜索")
    threads = [
        {
            "id": "t1",
            "user_id": "u1",
            "user_identifier": "a@x.com",
            "messages": [
                {"role": "user", "text": "这个按钮点了没反应", "ts": 100},
                {"role": "admin", "text": "请截图发我", "ts": 150},
            ],
        },
        {
            "id": "t2",
            "user_id": "u2",
            "user_identifier": "b@x.com",
            "messages": [
                {"role": "user", "text": "下载一直失败 error", "ts": 200},
                {"role": "user", "text": "带图的消息", "ts": 260, "image": "a" * 32 + ".png"},
            ],
        },
    ]
    adm = S._search_messages(threads, "u1", True, "截图")
    check("[搜索] 超管能搜到别人会话里的消息", len(adm) == 1 and adm[0]["thread_id"] == "t1")
    check("[搜索] 命中带会话归属与角色", adm[0]["user_identifier"] == "a@x.com" and adm[0]["role"] == "admin")
    check("[搜索] 命中带消息下标供定位高亮", adm[0]["index"] == 1)

    own = S._search_messages(threads, "u1", False, "下载")
    check("[搜索] 普通用户搜不到别人的会话", own == [])
    own2 = S._search_messages(threads, "u1", False, "按钮")
    check("[搜索] 普通用户能搜自己的会话", len(own2) == 1 and own2[0]["thread_id"] == "t1")

    many = S._search_messages(threads, "u1", True, "e", limit=1)
    check("[搜索] limit 生效", len(many) == 1)
    check("[搜索] 关键词大小写不敏感", len(S._search_messages(
        [{"id": "t", "user_id": "u", "messages": [{"role": "user", "text": "ERROR 500", "ts": 1}]}],
        "u", True, "error")) == 1)
    check("[搜索] 空关键词返回空", S._search_messages(threads, "u1", True, "   ") == [])

    ordered = S._search_messages([
        {"id": "t", "user_id": "u", "messages": [
            {"role": "user", "text": "报错", "ts": 10},
            {"role": "user", "text": "又报错", "ts": 99},
        ]},
    ], "u", True, "报错")
    check("[搜索] 结果按时间倒序", [r["ts"] for r in ordered] == [99, 10])

    ex = S._search_messages([
        {"id": "t", "user_id": "u", "messages": [
            {"role": "user", "text": "前置内容" * 20 + "关键词" + "后置内容" * 20, "ts": 1}]},
    ], "u", True, "关键词")
    check("[搜索] 长文返回带上下文的片段而非全文", 0 < len(ex[0]["excerpt"]) < 200 and "关键词" in ex[0]["excerpt"])


def test_image_field_flow() -> None:
    print("\n[D] 消息带图字段：校验文件名 + 归属判定")
    check("[字段] 空 image 视为不带图", S._image_name_of({"text": "x"}) == "")
    good = "b" * 32 + ".webp"
    check("[字段] 合法文件名原样取出", S._image_name_of({"image": good}) == good)
    try:
        S._image_name_of({"image": "../evil.png"})
        check("[字段] 非法文件名被拒", False)
    except ValueError:
        check("[字段] 非法文件名被拒", True)

    thread = {"messages": [{"role": "user", "text": "看图", "image": good}]}
    check("[归属] 会话确实引用了这张图", S._thread_owns_image(thread, good) is True)
    check("[归属] 未引用的图不算", S._thread_owns_image(thread, "c" * 32 + ".png") is False)


def test_media_dir_in_data_dir() -> None:
    print("\n[E] 附件落数据目录（避 TCC「下载」目录）")
    d = S._media_dir()
    check("[落点] media 目录在 VDL_DATA_DIR 内", str(d).startswith(_tmp))
    check("[落点] 不在 ~/Downloads / Documents / Desktop 下",
          not any(x in str(d) for x in ("/Downloads/", "/Documents/", "/Desktop/")))


def main() -> int:
    test_image_decode()
    test_media_path_guards()
    test_search_messages()
    test_image_field_flow()
    test_media_dir_in_data_dir()
    print("\n" + "=" * 46)
    if FAILS:
        print("❌ 失败 %d 项：" % len(FAILS))
        for f in FAILS:
            print("   - " + f)
        return 1
    print("✅ 客服发图与历史搜索守卫全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
