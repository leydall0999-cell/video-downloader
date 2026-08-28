"""server/routers/baidu_dlink.py — 由 server/app.py 按域抽取（Phase 1）。
handler 通过 `app.<name>` 访问共享内核（globals/helper/导入）。
所有 profile 均挂载，网页版行为零变化。app 端新功能只改本目录对应文件。
"""
import app
from fastapi import APIRouter

router = APIRouter()

@router.post("/api/baidu_dlink")
def add_baidu_dlink(payload: app.BaiduDlinkRequest, request: app.Request) -> dict:
    # SSRF 护栏：仅允许百度域名直链
    if "pan.baidu.com" not in payload.dlink and "baidu.com" not in payload.dlink:
        raise app.HTTPException(status_code=400, detail="仅支持百度网盘下载直链")
    # 文件名安全化
    raw = (payload.filename or "baidu_download").strip()
    fname = app.re.sub(r'[^\w\-\.\(\)\u4e00-\u9fff ]', '_', raw) or "baidu_download"
    if not fname.lower().endswith((".apk", ".zip", ".rar", ".mp4", ".pdf", ".7z", ".tar", ".gz", ".exe", ".dmg", ".iso", ".txt", ".json")):
        fname += ".bin"
    dest_dir = app.DOWNLOAD_DIR / "baidu"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / fname
    # 防重名
    if dest.exists():
        base, ext = app.os.path.splitext(fname)
        i = 1
        while dest.exists():
            dest = dest_dir / f"{base}_{i}{ext}"
            i += 1
    task_id = "bd_" + str(int(app.time.time() * 1000))[-10:]

    def _run():
        try:
            clouddrive._aria2c_download(dlink=payload.dlink, dest=dest, total=0, concurrency=8)
            app.logger.info("百度直链下载完成: %s", dest)
        except Exception as e:
            app.logger.error("百度直链下载失败: %s", e)

    _t2 = app.threading.Thread(target=_run, name="vdl-baidu-dlink", daemon=True)
    _t2.start()
    return {"ok": True, "task_id": task_id, "dest": str(dest), "message": "已提交 aria2c 下载"}


# --------------------------------------------------------------------------- #
# 纯 curl 方案：BDUSS 直链通道（砍掉 WebView，零浏览器依赖）
# 前端分享下载直接走 app.clouddrive 的「策略 A：BDUSS + /api/sharedownload」，
# 只要本机 ~/.vdl/baidu_bduss.txt 有有效 BDUSS 即可拿直链，无需任何 WebView 登录。
# --------------------------------------------------------------------------- #
@router.post("/api/baidu/save_bduss")
async def save_bduss(request: app.Request):
    """保存用户提供的百度网盘 BDUSS（从浏览器 F12 复制），供纯 curl 直链使用。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    bduss = (body.get("bduss") or "").strip()
    if not bduss:
        return {"ok": False, "message": "BDUSS 为空"}
    # 兼容用户粘贴 "BDUSS=xxxx" 或纯值
    if bduss.startswith("BDUSS="):
        bduss = bduss[6:].strip()
    # 粗略校验：真实 BDUSS 通常较长（>=40 字符）
    if len(bduss) < 20:
        return {"ok": False, "message": "BDUSS 过短，疑似复制不完整（应从浏览器 Cookie 复制完整值）"}
    try:
        app.clouddrive._save_bduss(bduss)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": "保存失败: " + str(e)}
    return {"ok": True, "message": "✓ BDUSS 已保存，现在可直接下载百度分享链接（纯 curl 直链通道，无需 WebView 登录）"}


@router.get("/api/baidu/bduss_status")
def bduss_status():
    """返回本机是否已配置 BDUSS（供前端显示）。"""
    from pathlib import Path
    import json
    p = Path.home() / ".vdl" / "baidu_bduss_info.json"
    if p.exists():
        try:
            return {"configured": True, "info": json.loads(p.read_text("utf-8"))}
        except Exception:
            pass
    return {"configured": False}
