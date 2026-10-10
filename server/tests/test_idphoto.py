"""证件照模块离线回归测试（2026-10-10 竞品对标「水印云」certified）。

证件照 = 抠图 + 换底 + 构图 + 排版。抠图本身由 matting_ai 负责（已有覆盖），
本文件只钉死**本模块新增的合成逻辑**与「前端三处接线是否真的通」：

后端（纯 PIL，无模型 / 无网络）：
  - _bg_rgba          底色解析（透明 / 预设名 / 十六进制 / 非法回退）
  - compose_id_photo  单张尺寸与底色、透明底、构图边界（不裁头不贴底）
  - compose_id_photo  排版（4 张 = 2×2 白底整版）
  - compose_id_photo  空抠图结果必须报错（不能静默出黑图）

前端接线（静态）：
  - 侧栏 / 首页卡片 / 视图 section / switchView 四处必须齐全
    （「函数级单测绿」≠「用户点得到」，缺任何一处就是点了没反应）

运行：
    cd server && python tests/test_idphoto.py
    cd server && python -m pytest tests/test_idphoto.py -v
"""
import os
import sys
import io
import tempfile
from pathlib import Path

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)
_REPO_DIR = os.path.dirname(_SERVER_DIR)
_WEB_DIR = os.path.join(_REPO_DIR, "web")

from PIL import Image  # noqa: E402

import app as server_app  # noqa: E402  （触发内核初始化，routers.idphoto 依赖 app.* 命名空间）
from routers import idphoto as idp  # noqa: E402


# ---------------------------------------------------------------- 工具
def _subject_rgba(size=(400, 600), margin=40):
    """造一张「透明背景 + 中间不透明主体」的假抠图结果。"""
    im = Image.new("RGBA", size, (0, 0, 0, 0))
    px = im.load()
    for y in range(margin, size[1] - margin):
        for x in range(margin, size[0] - margin):
            px[x, y] = (30, 60, 90, 255)
    return im


def _tmp_png(im, name="subject.png"):
    """落到系统临时目录（不脏仓库）——测试只做一次性读写的中间图。"""
    p = Path(tempfile.gettempdir()) / f"_vdl_test_idp_{name}"
    im.save(p, format="PNG")
    return str(p)


# ---------------------------------------------------------------- 后端纯逻辑
def test_bg_rgba_mapping():
    """底色解析：预设名 / 十六进制 / 非法值回退透明（绝不静默变黑）。"""
    assert idp._bg_rgba("blue") == (62, 84, 185, 255)
    assert idp._bg_rgba("white") == (255, 255, 255, 255)
    assert idp._bg_rgba("transparent") == (0, 0, 0, 0)
    assert idp._bg_rgba("#FF0000") == (255, 0, 0, 255)
    assert idp._bg_rgba("#ff0000") == (255, 0, 0, 255)
    # 非法输入必须回退透明，不能变成黑色背景（用户会拿到一张黑底证件照）
    assert idp._bg_rgba("") == (0, 0, 0, 0)
    assert idp._bg_rgba("not-a-color") == (0, 0, 0, 0)
    assert idp._bg_rgba("#GGG") == (0, 0, 0, 0)
    print("✅ 底色解析：预设 / 十六进制 / 非法回退透明")


def test_compose_single_size_and_bg():
    """单张：画布严格等于规格像素，底色铺满，主体落在画布内。"""
    src = _tmp_png(_subject_rgba(), "subj1.png")
    out = str(Path(tempfile.gettempdir()) / "_vdl_test_idp_out1.png")
    info = idp.compose_id_photo(src, out, 295, 413, "blue", 1)
    assert (info["width"], info["height"]) == (295, 413)
    im = Image.open(out)
    assert im.size == (295, 413)
    rgb = im.convert("RGB")
    # 左上角必须是底色（蓝），不是黑 / 不是透明残留
    assert rgb.getpixel((2, 2)) == (62, 84, 185), rgb.getpixel((2, 2))
    # 中央必须有主体（构图居中）
    cx, cy = 295 // 2, int(413 * 0.6)
    assert rgb.getpixel((cx, cy)) != (62, 84, 185)
    print("✅ 单张：尺寸 / 底色 / 主体居中构图正确")


def test_compose_transparent_bg():
    """透明底：画布带 alpha，四角必须透明（用户才能贴到任意背景）。"""
    src = _tmp_png(_subject_rgba(), "subj2.png")
    out = str(Path(tempfile.gettempdir()) / "_vdl_test_idp_out2.png")
    idp.compose_id_photo(src, out, 295, 413, "transparent", 1)
    im = Image.open(out).convert("RGBA")
    assert im.getpixel((1, 1))[3] == 0, "透明底四角 alpha 必须为 0"
    print("✅ 透明底：四角 alpha=0")


def test_compose_layout_sheet():
    """4 张排版：白底整版，尺寸 = 2 列 ×(w+gap) + 边距，且明显大于单张。"""
    src = _tmp_png(_subject_rgba(), "subj3.png")
    out = str(Path(tempfile.gettempdir()) / "_vdl_test_idp_out3.png")
    info = idp.compose_id_photo(src, out, 295, 413, "blue", 4)
    single_w, single_h = 295, 413
    assert info["width"] > single_w * 1.9 and info["height"] > single_h * 1.9
    im = Image.open(out).convert("RGB")
    # 网格间隙处（第 1 行与第 2 行之间的横缝）必须是白色纸张
    gap_y = single_h + max(8, int(single_w * 0.06)) // 2
    assert im.getpixel((5, gap_y)) == (255, 255, 255), "排版整版间隙应为白纸"
    print("✅ 排版：4 张 2×2 白底整版尺寸与间隙正确")


def test_compose_rejects_empty_matting():
    """抠图结果全透明 → 必须报错，不能出一张纯色废图。"""
    empty = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    src = _tmp_png(empty, "empty.png")
    out = str(Path(tempfile.gettempdir()) / "_vdl_test_idp_out4.png")
    try:
        idp.compose_id_photo(src, out, 295, 413, "blue", 1)
    except RuntimeError as e:
        assert "抠图结果为空" in str(e)
    else:
        raise AssertionError("全透明抠图结果应抛 RuntimeError（静默出图 = 废图）")
    print("✅ 空抠图结果被拒绝（不静默出废图）")


def test_compose_subject_fits_canvas():
    """构图（回退分支）：非人像结构（矩形主体）必须等比缩放进画布，不能原尺寸直通。

    用「去掉底色后的主体 bbox」量化：宽 ≤ 画布 88%、高 ≤ 画布 90%，且四边都不贴边。
    只查单行像素（旧写法）钉不住缩放，变异测试已证实 scale=1.0 能存活。
    """
    src = _tmp_png(_subject_rgba(size=(400, 900)), "tall.png")
    out = str(Path(tempfile.gettempdir()) / "_vdl_test_idp_out5.png")
    idp.compose_id_photo(src, out, 295, 413, "white", 1)
    rgb = Image.open(out).convert("RGB")
    # 非白像素的 bbox = 主体实际占位
    xs, ys = [], []
    px = rgb.load()
    for y in range(rgb.height):
        for x in range(rgb.width):
            r, g, b = px[x, y]
            if not (r > 250 and g > 250 and b > 250):
                xs.append(x)
                ys.append(y)
    assert xs and ys, "画布里没有主体（合成失败）"
    bw, bh = max(xs) - min(xs) + 1, max(ys) - min(ys) + 1
    assert bw <= int(295 * 0.90), f"主体宽 {bw} 超出 88% 构图上限（未缩放 / 被横向裁切）"
    assert bh <= int(413 * 0.92), f"主体高 {bh} 超出 90% 构图上限（未缩放 / 被纵向裁切）"
    assert min(ys) > 0, "顶部必须留白，不能切到头顶"
    assert max(ys) < 413 - 1, "底部必须留白，主体不能贴底溢出"
    print("✅ 构图（回退）：主体等比缩放、四边留白（宽≤88% / 高≤90%）")


def _person_rgba(size=(600, 900), mark=False):
    """造一张「头 + 颈 + 肩」形状的假抠图结果（行宽呈窄→宽→窄→骤宽，像人像）。

    mark=True 时在右下角加一块**与主体不相连**的残留（模拟原图水印 / 签名被抠图
    误判为主体），用于验证掩码清洗与 bbox 不受残留影响。
    """
    from PIL import ImageDraw
    im = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.ellipse([240, 120, 360, 300], fill=(230, 190, 160, 255))   # 头（宽 120）
    d.rectangle([285, 295, 315, 340], fill=(230, 190, 160, 255))  # 颈（宽 30）
    d.rectangle([150, 340, 450, 860], fill=(40, 60, 110, 255))    # 肩 / 身（宽 300）
    if mark:
        d.rectangle([470, 800, 580, 830], fill=(255, 0, 0, 255))  # 不相连的残留
    return im


def _head_metrics(out):
    """量输出里「头部色」的占比：头宽 / 头高（含颈）/ 头顶留白。"""
    im = Image.open(out).convert("RGB")
    W, H = im.size
    px = im.load()
    hx, hy = [], []
    for y in range(H):
        for x in range(W):
            if px[x, y] == (230, 190, 160):
                hx.append(x)
                hy.append(y)
    assert hx, "输出里找不到头部（假人像没合成进去）"
    return (max(hx) - min(hx) + 1) / W, (max(hy) - min(hy) + 1) / H, min(hy) / H


def test_mask_cleanup_drops_residue():
    """掩码清洗：与主体不相连的抠图残留（水印 / 签名）必须被清零，且不影响构图。

    「残留把主体框撑大 ⇒ 人像被挤小、位置下移」是这次「人物偏小」的根因之一。
    分两层钉：
      ① 直接断言清洗结果——残留区在 strong/sel 里必须为 False，且主体框不含残留
         （只靠合成后的像素断言不可靠：残留可能被裁到画布外，变异会存活）；
      ② 断言有残留与无残留的输出构图一致。
    """
    import numpy as _np
    person = _person_rgba(mark=True)
    alpha = _np.array(person)[..., 3]
    strong, sel = idp._mask_region(alpha)
    # 残留块 (470~580, 800~830) 的中心：必须被排除出核心区，且不得保留像素
    assert not bool(strong[815, 525]), "残留区没被排除出主体核心区（bbox 会被撑大）"
    assert not bool(sel[815, 525]), "残留区像素没被清零（水印会印进证件照）"
    assert bool(strong[210, 300]), "头部主体被误删（清洗把人也滤掉了）"
    bbox = Image.fromarray((strong.astype(_np.uint8) * 255), "L").getbbox()
    assert bbox and bbox[2] <= 460, f"残留把主体框撑大了：{bbox}（应为 x≤451）"

    outs = []
    for tag, m in (("with", True), ("without", False)):
        src = _tmp_png(_person_rgba(mark=m), f"mark_{tag}.png")
        out = str(Path(tempfile.gettempdir()) / f"_vdl_test_idp_res_{tag}.png")
        idp.compose_id_photo(src, out, 295, 413, "white", 1)
        rgb = Image.open(out).convert("RGB")
        px = rgb.load()
        red = sum(1 for y in range(rgb.height) for x in range(rgb.width)
                  if px[x, y] == (255, 0, 0))
        assert red == 0, f"残留（红色块）没被清掉：{red} 像素"
        outs.append(_head_metrics(out))
    assert outs[0] == outs[1], f"残留影响了构图：{outs[0]} vs {outs[1]}"
    print("✅ 掩码清洗：不相连残留清零，且构图不受影响")


def test_compose_head_dominant_for_portrait():
    """构图（头主导）：人像结构可识别时，脸必须够大、头顶留白合规。

    旧实现在半身照上只按整体 bbox 缩放，脸被缩到画布宽的 ~20%（用户反馈「人太小」）。
    这里钉死：头宽 ≥45% 画布宽、头高（含颈）≥55% 画布高、头顶留白 ≥4%。
    """
    src = _tmp_png(_person_rgba(), "person.png")
    out = str(Path(tempfile.gettempdir()) / "_vdl_test_idp_person.png")
    idp.compose_id_photo(src, out, 295, 413, "white", 1)
    hw, hh, top = _head_metrics(out)
    assert hw >= 0.45, f"头宽只占 {hw:.2f}，脸太小（旧实现约 0.20）"
    assert hh >= 0.55, f"头高（含颈）只占 {hh:.2f}，脸太小"
    assert top >= 0.04, f"头顶留白只有 {top:.2f}，会显得切头"
    print(f"✅ 构图（头主导）：头宽 {hw:.2f} / 头高 {hh:.2f} / 头顶留白 {top:.2f}")


def test_find_head_rows_rejects_non_portrait():
    """结构判据：矩形主体（无脖子收窄）必须判不出头，保证能落到回退构图。"""
    import numpy as _np
    rect = _np.zeros((400, 200), dtype=bool)
    rect[20:380, 40:160] = True
    assert idp._find_head_rows(rect) is None, "矩形主体不该被认成人像（回退分支会失效）"
    person = _np.zeros((400, 200), dtype=bool)
    person[20:150, 60:140] = True      # 头
    person[150:190, 92:108] = True     # 颈（明显收窄）
    person[190:380, 20:180] = True     # 肩
    got = idp._find_head_rows(person)
    assert got is not None, "头 + 颈 + 肩的结构应被识别"
    print("✅ 结构判据：矩形拒绝 / 人像结构识别")


def test_head_detected_without_neck_by_proportion():
    """长发 / 近景特写（行宽一路变宽、没有脖子收窄）也必须按头构图。

    这是「人物偏小」投诉的主战场：真人长发照的行宽从发顶单调增到肩，脖子判据
    漏判 ⇒ 落到回退构图 ⇒ 脸只有画布高的一成出头。兜底按人体比例（头宽≈肩宽
    0.42）反推头，命中后肩以下被自然裁切 = 证件照该有的头肩特写。
    判据：头主导时人像会**裁到画布底边**；回退构图底部必留白（3%）。
    """
    import numpy as _np
    h, w = 900, 600
    m = _np.zeros((h, w), dtype=bool)
    for y in range(h):                       # 半宽单调增：漏斗形（发顶→肩），无收窄
        half = int(30 + 250 * ((y / float(h)) ** 0.8))
        m[y, max(0, w // 2 - half):w // 2 + half] = True
    got = idp._find_head_rows(m)
    assert got is not None, "长发 / 近景人像被判成非人像（会落回退构图，脸被缩小）"
    assert len(got) == 3 and got[2] > 0, "兜底必须回传估算头宽（否则头宽取区间最大行宽会偏大）"

    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    px = im.load()
    for y in range(h):
        for x in range(w):
            if m[y, x]:
                px[x, y] = (230, 190, 160, 255)
    src = _tmp_png(im, "longhair.png")
    out = str(Path(tempfile.gettempdir()) / "_vdl_test_idp_longhair.png")
    idp.compose_id_photo(src, out, 295, 413, "white", 1)
    rgb = Image.open(out).convert("RGB")
    p = rgb.load()
    W, H = rgb.size

    def _fg(yy):
        return sum(1 for xx in range(W) if p[xx, yy] != (255, 255, 255))

    assert _fg(H - 1) > 0, "底部被留白了 ⇒ 走的是回退构图（脸还是小的）"
    assert _fg(H - 1) > W * 0.8, f"底边没撑满（{_fg(H - 1)}/{W}），不像头肩特写"
    assert _fg(int(H * 0.085) + 4) > 0, "头顶不在 8.5% 留白处（构图锚点不对）"
    print("✅ 长发/近景无脖子：按人体比例估头，头肩特写到底边（不再缩成小脸）")


def test_compose_zoom_changes_size():
    """「人物大小」可调：zoom 越大脸越大，且头顶留白（缩放锚点）不动。

    锚点必须稳——拖滑块时头顶位置若跟着漂，用户每调一下都要重新对位置，
    这个滑块就成了「能调但没法调准」。
    """
    src = _tmp_png(_person_rgba(), "person_zoom.png")
    got = {}
    for z in (0.7, 1.0, 1.4):
        out = str(Path(tempfile.gettempdir()) / f"_vdl_test_idp_zoom_{int(z * 100)}.png")
        info = idp.compose_id_photo(src, out, 295, 413, "white", 1, zoom=z)
        got[z] = (_head_metrics(out), info)
    (w7, _, t7), (w1, _, t1), (w14, _, t14) = got[0.7][0], got[1.0][0], got[1.4][0]
    assert w14 > w1 * 1.25, f"zoom=1.4 头宽 {w14:.2f} 没比 1.0（{w1:.2f}）明显变大"
    assert w7 < w1 * 0.85, f"zoom=0.7 头宽 {w7:.2f} 没比 1.0（{w1:.2f}）变小"
    assert abs(t14 - t1) < 0.02 and abs(t7 - t1) < 0.02, \
        f"缩放把头顶留白也带跑了：{t7:.2f} / {t1:.2f} / {t14:.2f}"
    assert got[1.4][1]["zoom"] == 1.4, "detail 没回传 zoom（前端拿不到当前倍率）"
    # 越界必须夹住：手改请求不能拉出马赛克大图或缩成一个点
    assert idp.clamp_zoom(9) == idp._ZOOM_MAX, "zoom 上限没夹住"
    assert idp.clamp_zoom(0.01) == idp._ZOOM_MIN, "zoom 下限没夹住"
    assert idp.clamp_zoom("abc") == 1.0 and idp.clamp_zoom(None) == 1.0, "非法 zoom 没回退默认"
    print(f"✅ 人物大小可调：0.7→{w7:.2f} / 1.0→{w1:.2f} / 1.4→{w14:.2f}，头顶留白 {t1:.2f} 不动")


def test_render_endpoint_reuses_matting():
    """拖滑块只重合成、**不重抠图**：抠图必须只被调用一次（否则滑块等于不能用）。

    抠图一次几十秒，若每拖一下都重抠，用户只会看到一直转圈。
    """
    from fastapi.testclient import TestClient
    import time
    calls = {"n": 0}

    def _fake_matting(src, out, **kw):
        calls["n"] += 1
        _person_rgba().save(out, format="PNG")

    def _head_w(resp):
        im = Image.open(io.BytesIO(resp.content)).convert("RGB")
        W, H = im.size
        px = im.load()
        hx = [x for y in range(H) for x in range(W) if px[x, y] == (230, 190, 160)]
        return (max(hx) - min(hx) + 1) / W

    orig = idp.mat.matting_image
    idp.mat.matting_image = _fake_matting
    try:
        c = TestClient(server_app.app)
        buf = io.BytesIO()
        Image.new("RGB", (600, 900), (170, 170, 170)).save(buf, format="PNG")
        r = c.post("/api/idphoto/make",
                   files={"file": ("me.png", buf.getvalue(), "image/png")},
                   data={"w": "295", "h": "413", "bg": "blue", "layout": "1",
                         "label": "一寸", "zoom": "1.0"})
        assert r.status_code == 200, r.text[:200]
        job = r.json()["job_id"]
        st = None
        for _ in range(60):
            st = c.get(f"/api/idphoto/{job}").json()
            if st["status"] in ("completed", "failed"):
                break
            time.sleep(0.2)
        assert st["status"] == "completed", f"任务失败：{st.get('error')}"
        assert st["detail"].get("zoom") == 1.0, "首次生成没带 zoom"
        w_before = _head_w(c.get(f"/api/idphoto/{job}/file"))
        rr = c.post(f"/api/idphoto/{job}/render", data={"zoom": "1.4"})
        assert rr.status_code == 200, rr.text[:200]
        assert rr.json()["detail"]["zoom"] == 1.4, "重新合成没吃到新 zoom"
        w_after = _head_w(c.get(f"/api/idphoto/{job}/file"))
        assert w_after > w_before * 1.25, f"重新出图没变大：{w_before:.2f} → {w_after:.2f}"
        assert calls["n"] == 1, f"重新出图又跑了一次抠图（共 {calls['n']} 次）"
        print(f"✅ 重新合成：头宽 {w_before:.2f} → {w_after:.2f}，抠图只跑 1 次（不重抠）")
    finally:
        idp.mat.matting_image = orig


def test_compose_offset_moves_and_clamps():
    """预览拖动（ox/oy）：真的移动人像，且越界会被夹住——不许把人拖出画面。

    用户拖到极限时若不做夹取，会导出一张纯底色空图，看起来像「生成坏了」。
    """
    src = _tmp_png(_person_rgba(), "person_off.png")
    tmp = Path(tempfile.gettempdir())
    paths = {k: str(tmp / f"_vdl_test_idp_off_{k}.png") for k in ("base", "right", "far")}
    idp.compose_id_photo(src, paths["base"], 295, 413, "white", 1)
    idp.compose_id_photo(src, paths["right"], 295, 413, "white", 1, ox=0.15)
    idp.compose_id_photo(src, paths["far"], 295, 413, "white", 1, ox=1.0)

    def fg_stats(path):
        im = Image.open(path).convert("RGB")
        W, H = im.size
        px = im.load()
        xs = [x for y in range(H) for x in range(W) if px[x, y] != (255, 255, 255)]
        return len(xs), (sum(xs) / float(len(xs)) / W if xs else 0.0)

    n_base, cx_base = fg_stats(paths["base"])
    n_right, cx_right = fg_stats(paths["right"])
    n_far, _ = fg_stats(paths["far"])
    assert cx_right > cx_base + 0.05, f"ox 没把人像右移：{cx_base:.2f} → {cx_right:.2f}"
    assert n_far < n_base, "越界后应被夹住只露一部分，不该完整保留"

    # 夹取要用「比画布小」的主体才测得出：上面那张头主导人像比画布还宽，
    # 移动一整幅画布的距离也还留在画面里，去掉夹取的变异会存活（踩过）。
    small = _tmp_png(_subject_rgba(), "small_off.png")
    s_base = str(tmp / "_vdl_test_idp_off_small_base.png")
    far2 = str(tmp / "_vdl_test_idp_off_far2.png")
    idp.compose_id_photo(small, s_base, 295, 413, "white", 1)
    idp.compose_id_photo(small, far2, 295, 413, "white", 1, ox=1.0)

    def _fg_of(path):
        im2 = Image.open(path).convert("RGB")
        W2, H2 = im2.size
        px2 = im2.load()
        return sum(1 for y in range(H2) for x in range(W2) if px2[x, y] != (255, 255, 255))

    n_far2, n_small = _fg_of(far2), _fg_of(s_base)
    assert n_far2 >= n_small * 0.3, \
        f"拖到极限后人像几乎被拖出画布（{n_far2} vs 复位 {n_small}）"
    assert idp._clamp_off(9.9) == 1.0 and idp._clamp_off("x") == 0.0, "偏移参数没夹住"
    print(f"✅ 位置拖动：重心 {cx_base:.2f} → {cx_right:.2f}，越界被夹住（仍留 {n_far} 像素）")


def test_offset_extreme_never_empties_canvas():
    """两个方向同时拖到极限也不能出空图。

    实测踩到：只夹「包围盒至少两成可见」时，右下角可见区正好落在主体轮廓的空隙里
    （人像右下方没内容）⇒ 导出一张纯底色图，用户以为「生成坏了」。所以要按**主体质心**
    仍落在画布内来兜底。
    """
    im = Image.new("RGBA", (200, 400), (0, 0, 0, 0))
    px = im.load()
    for y in range(20, 200):                 # 上部竖条
        for x in range(40, 160):
            px[x, y] = (30, 60, 90, 255)
    for y in range(200, 380):                # 下部偏左（右下角留空）
        for x in range(20, 120):
            px[x, y] = (30, 60, 90, 255)
    src = _tmp_png(im, "corner.png")
    base = str(Path(tempfile.gettempdir()) / "_vdl_test_idp_corner_base.png")
    out = str(Path(tempfile.gettempdir()) / "_vdl_test_idp_corner.png")
    idp.compose_id_photo(src, base, 295, 413, "white", 1)
    idp.compose_id_photo(src, out, 295, 413, "white", 1, ox=1.0, oy=1.0)

    def _fg(path):
        rgb = Image.open(path).convert("RGB")
        W, H = rgb.size
        p = rgb.load()
        return sum(1 for y in range(H) for x in range(W) if p[x, y] != (255, 255, 255))

    n0, n = _fg(base), _fg(out)
    assert n >= n0 * 0.3, f"极限拖动后只剩 {n}/{n0} 像素，人像基本被拖没了（应当还有一半左右）"
    print(f"✅ 极限拖动仍保住主体（{n}/{n0} 像素，约 {n / n0:.0%}）")


def test_precut_skips_matting_and_rejects_opaque():
    """「用作证件照」（precut）：必须跳过抠图；没有透明区域的图必须拒收。

    拒收很重要：用户可能把普通照片当「已抠好」传进来，那样会合成一张带原背景的
    矩形贴图，而用户会以为是我们抠错了。
    """
    from fastapi.testclient import TestClient
    import time
    calls = {"n": 0}

    def _fake_matting(src, out, **kw):
        calls["n"] += 1
        _person_rgba().save(out, format="PNG")

    orig = idp.mat.matting_image
    idp.mat.matting_image = _fake_matting
    try:
        c = TestClient(server_app.app)
        buf = io.BytesIO()
        _person_rgba().save(buf, format="PNG")
        r = c.post("/api/idphoto/make",
                   files={"file": ("cut.png", buf.getvalue(), "image/png")},
                   data={"w": "295", "h": "413", "bg": "blue", "layout": "1",
                         "precut": "true", "zoom": "1.0"})
        assert r.status_code == 200, r.text[:200]
        job = r.json()["job_id"]
        st = None
        for _ in range(60):
            st = c.get(f"/api/idphoto/{job}").json()
            if st["status"] in ("completed", "failed"):
                break
            time.sleep(0.2)
        assert st["status"] == "completed", f"precut 任务失败：{st.get('error')}"
        assert calls["n"] == 0, f"precut 竟然又抠了一次图（{calls['n']} 次）"

        # 位置拖动走的就是 render 接口 ⇒ ox/oy 必须透传
        rr = c.post(f"/api/idphoto/{job}/render",
                    data={"zoom": "1.0", "ox": "0.12", "oy": "-0.08"})
        assert rr.status_code == 200, rr.text[:200]
        d = rr.json()["detail"]
        assert abs(d["ox"] - 0.12) < 1e-6 and abs(d["oy"] + 0.08) < 1e-6, \
            f"render 没吃到 ox/oy：{d}"

        buf2 = io.BytesIO()
        Image.new("RGB", (300, 400), (120, 130, 140)).save(buf2, format="JPEG")
        r2 = c.post("/api/idphoto/make",
                    files={"file": ("photo.jpg", buf2.getvalue(), "image/jpeg")},
                    data={"w": "295", "h": "413", "precut": "true"})
        job2 = r2.json()["job_id"]
        st2 = None
        for _ in range(60):
            st2 = c.get(f"/api/idphoto/{job2}").json()
            if st2["status"] in ("completed", "failed"):
                break
            time.sleep(0.2)
        assert st2["status"] == "failed", "不透明图被当成抠图结果放行了"
        print("✅ 用作证件照：precut 跳过抠图（0 次）/ render 吃 ox,oy / 非透明图被拒")
    finally:
        idp.mat.matting_image = orig


# ---------------------------------------------------------------- 前端接线
def test_frontend_zoom_wiring_present():
    """「人物大小」三处接线：滑块控件 / JS 取值 / 重新合成接口调用。"""
    html = _web("index.html")
    js = _web("app.js")
    assert 'id="idpZoom"' in html, "缺少「人物大小」滑块控件"
    assert "el.idpZoom" in js, "滑块没接进 JS（拖了没反应）"
    assert "/render" in js, "JS 没调重新合成接口（拖滑块不会重新出图）"
    assert "form.append('zoom'" in js, "生成时没把 zoom 传给后端（首次出图不吃滑块值）"
    print("✅ 人物大小前端接线齐全（控件 / 取值 / 重合成）")


def test_frontend_drag_paste_and_matting_bridge_wiring():
    """拖动 / 滚轮 / 拖拽入页 / ⌘V 粘贴 / 一键抠图带入：五条通道缺一条用户就得多等面板。"""
    html = _web("index.html")
    js = _web("app.js")
    assert 'id="idpReset"' in html, "缺少「复位」按钮（拖歪了没法还原）"
    assert 'id="idpPicked"' in html, "缺少「已选图片」状态行（拖入后看不出载入了没有）"
    assert 'id="matToIdPhoto"' in html, "一键抠图缺少「用作证件照」按钮"
    assert "el.idpPreview.addEventListener('pointerdown'" in js, "预览没接按住拖动"
    assert "el.idpPreview.addEventListener('pointerup'" in js, "拖动松手没接（位置不会落盘）"
    assert "{ passive: false }" in js, "预览滚轮没拦默认滚动（会变成页面滚动）"
    assert "el.idphotoView.addEventListener('drop'" in js, "证件照页没接拖拽入页"
    assert "addEventListener('paste'" in js, "没接 ⌘V 粘贴"
    assert "switchView('idphoto')" in js, "「用作证件照」没切到证件照视图"
    assert "form.append('precut'" in js, "生成时没带 precut（从抠图带入会白抠一次）"
    print("✅ 拖动 / 滚轮 / 拖拽 / 粘贴 / 一键抠图带入 五条通道接线齐全")


def _web(name):
    return Path(_WEB_DIR, name).read_text(encoding="utf-8")


def test_frontend_wiring_present():
    """前端四处接线齐全：侧栏入口 / 首页卡片 / 视图 section / switchView 显隐。"""
    html = _web("index.html")
    js = _web("app.js")
    assert 'data-view="idphoto"' in html, "侧栏或首页卡片缺少 idphoto 入口"
    assert 'id="idphotoView"' in html, "缺少 idphotoView 视图容器"
    assert 'id="sTabIdPhoto"' in html, "缺少侧栏按钮 sTabIdPhoto"
    assert "el.idphotoView.hidden = !isIdPhoto" in js.replace("'", "'"), \
        "switchView 未接管 idphotoView 显隐（点了会没反应）"
    assert "[el.sTabIdPhoto, 'idphoto']" in js, "侧栏按钮未绑定 switchView（点了不切视图）"
    print("✅ 前端四处接线齐全（入口 / 视图 / 显隐 / 事件）")


def test_routes_mounted():
    """路由挂载：非法排版张数必须 400，未知任务必须 404（挂载失败会是 404/405）。"""
    from fastapi.testclient import TestClient
    c = TestClient(server_app.app)
    buf = io.BytesIO()
    Image.new("RGB", (200, 260), (180, 180, 180)).save(buf, format="PNG")
    r = c.post("/api/idphoto/make",
               files={"file": ("t.png", buf.getvalue(), "image/png")},
               data={"w": "295", "h": "413", "layout": "3"})
    assert r.status_code == 400, f"非法排版张数应 400，实际 {r.status_code}"
    r2 = c.get("/api/idphoto/nonexistent")
    assert r2.status_code == 404
    print("✅ 路由已挂载：非法参数 400 / 未知任务 404")


def test_job_pipeline_e2e():
    """端到端：POST 建任务 → 抠图（打桩）→ 合成 → 下载文件，全链路真的出图。

    抠图本身要下载/加载 ONNX 权重（慢且不稳定），这里打桩成一张「透明底 + 中间
    不透明方块」的假抠图结果，钉死的是**本模块的编排**：任务状态机、参数透传、
    结果文件落盘与下载。抠图质量由 matting_ai 自己的测试负责。
    """
    from fastapi.testclient import TestClient
    calls = {}

    def _fake_matting(src, out, **kw):
        calls["src"] = str(src)
        calls["kw"] = kw
        _subject_rgba(size=(400, 600)).save(out, format="PNG")

    orig = idp.mat.matting_image
    idp.mat.matting_image = _fake_matting
    try:
        c = TestClient(server_app.app)
        buf = io.BytesIO()
        Image.new("RGB", (400, 600), (170, 170, 170)).save(buf, format="PNG")
        r = c.post("/api/idphoto/make",
                   files={"file": ("me.png", buf.getvalue(), "image/png")},
                   data={"w": "295", "h": "413", "bg": "blue", "layout": "4", "label": "一寸"})
        assert r.status_code == 200, r.text[:200]
        job = r.json()["job_id"]
        assert calls.get("src"), "抠图没被调用（任务链路断了）"
        # 证件照必须显式声明人像并关掉 AI 图片类型识别：否则自动模式会先调 VLM
        # 判类型（timeout=45s）——白等 45 秒，还可能被判成非人像走错通道。
        assert calls["kw"].get("vision_label") == "人像", "证件照没声明人像标签（会白等 VLM 判断）"
        assert calls["kw"].get("auto_vlm") is False, "auto_vlm 应为 False（跳过图片类型识别）"
        st = None
        for _ in range(60):
            st = c.get(f"/api/idphoto/{job}").json()
            if st["status"] in ("completed", "failed"):
                break
            import time
            time.sleep(0.2)
        assert st["status"] == "completed", f"任务失败：{st.get('error')}"
        assert st["filename"].startswith("证件照_一寸_"), st["filename"]
        assert st["detail"]["layout"] == 4
        f = c.get(f"/api/idphoto/{job}/file")
        assert f.status_code == 200
        im = Image.open(io.BytesIO(f.content))
        # 排版 4 张 ⇒ 明显大于单张规格
        assert im.width > 295 * 1.9 and im.height > 413 * 1.9, im.size
        print("✅ 端到端：任务链路 / 参数透传 / 结果下载全通")
    finally:
        idp.mat.matting_image = orig


if __name__ == "__main__":
    test_bg_rgba_mapping()
    test_compose_single_size_and_bg()
    test_compose_transparent_bg()
    test_compose_layout_sheet()
    test_compose_rejects_empty_matting()
    test_compose_subject_fits_canvas()
    test_mask_cleanup_drops_residue()
    test_compose_head_dominant_for_portrait()
    test_find_head_rows_rejects_non_portrait()
    test_head_detected_without_neck_by_proportion()
    test_compose_zoom_changes_size()
    test_render_endpoint_reuses_matting()
    test_compose_offset_moves_and_clamps()
    test_offset_extreme_never_empties_canvas()
    test_precut_skips_matting_and_rejects_opaque()
    test_frontend_wiring_present()
    test_frontend_zoom_wiring_present()
    test_frontend_drag_paste_and_matting_bridge_wiring()
    test_routes_mounted()
    test_job_pipeline_e2e()
    print("\n全部通过 ✅")
