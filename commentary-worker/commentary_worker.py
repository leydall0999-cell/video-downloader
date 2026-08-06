"""独立解说 worker · HTTP 服务

把解说管线(process.py)包成一个可独立部署的服务，与主站 video-downloader 解耦：
主站把「下载好的视频」POST 过来 → 本服务跑 process.py --auto → 返回成片。

设计要点
--------
- 独立部署：跑在强机(多核/大内存)上，不挤占主站(Railway)资源。
- 接口：POST /render(传视频+vertical/voice) → 轮询 GET /status/{job_id}
        → 完成后 GET /file/{job_id} 下载成片。
- 串行渲染：默认 WORKER_MAX_CONCURRENCY=1(渲染吃 CPU，跑满一个再下一个)，
  超出返回 429，主站侧限流即可。
- 主站侧对应开关：VDL_COMMENTARY_MODE=http + VDL_COMMENTARY_ENDPOINT=<本服务地址>。

依赖：与解说管线共用 requirements.txt(fastapi / uvicorn 已含)。
启动：COMMENTARY_BASE=/path/to/pipeline PYTHON=/path/to/python \
      uvicorn commentary_worker:app --host 0.0.0.0 --port 8100
"""
import os
import sys
import uuid
import threading
import subprocess
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Depends, Header
from fastapi.responses import FileResponse, JSONResponse

PIPELINE = Path(os.environ.get("COMMENTARY_BASE") or Path(__file__).resolve().parent)
PYTHON = os.environ.get("COMMENTARY_PYTHON", sys.executable)
INPUT = PIPELINE / "input"
OUTPUT = PIPELINE / "output"
MAX_CONCURRENCY = int(os.environ.get("WORKER_MAX_CONCURRENCY", "1") or 1)
RENDER_TIMEOUT = int(os.environ.get("WORKER_TIMEOUT", "1800") or 1800)  # 长视频可能很久
# 公网暴露时必须设 WORKER_TOKEN，否则任何人都能调用本服务（消耗机器/滥用）
WORKER_TOKEN = (os.environ.get("WORKER_TOKEN") or "").strip()


def _require_token(x_worker_token: str = Header(None, alias="X-Worker-Token")) -> None:
    if WORKER_TOKEN and x_worker_token != WORKER_TOKEN:
        raise HTTPException(status_code=401, detail="invalid or missing X-Worker-Token")

for d in (INPUT, OUTPUT):
    d.mkdir(parents=True, exist_ok=True)

jobs: dict[str, dict] = {}
_lock = threading.Lock()
_running = 0

app = FastAPI(title="commentary-worker")


@app.get("/health")
def health():
    with _lock:
        busy = _running
    return {"ok": True, "running": busy, "max_concurrency": MAX_CONCURRENCY}


@app.post("/render", dependencies=[Depends(_require_token)])
async def render(
    video: UploadFile = File(...),
    vertical: bool = Form(False),
    voice: str = Form("zh-CN-XiaoxiaoNeural"),
):
    global _running
    with _lock:
        if _running >= MAX_CONCURRENCY:
            raise HTTPException(status_code=429, detail="渲染队列已满，请稍后重试")
        _running += 1

    job_id = uuid.uuid4().hex[:16]
    in_path = INPUT / f"{job_id}.mp4"
    out_path = OUTPUT / f"{job_id}.mp4"
    try:
        data = await video.read()
        in_path.write_bytes(data)

        args = [PYTHON, "process.py", str(in_path), "--auto", "--output", str(out_path)]
        if vertical:
            args.append("--vertical")
        if voice:
            args += ["--voice", voice]

        with _lock:
            jobs[job_id] = {
                "status": "running",
                "error": "",
                "output_path": str(out_path),
                "input_path": str(in_path),
            }

        threading.Thread(target=_run, args=(job_id, args), daemon=True).start()
        return JSONResponse({"job_id": job_id, "status": "running"})
    except Exception as exc:  # noqa: BLE001
        with _lock:
            _running -= 1
        if in_path.exists():
            in_path.unlink()
        raise HTTPException(status_code=500, detail=str(exc)[:500])


def _run(job_id: str, args: list[str]) -> None:
    global _running
    try:
        proc = subprocess.run(
            args, cwd=str(PIPELINE), capture_output=True, text=True,
            timeout=RENDER_TIMEOUT,
        )
        if proc.returncode != 0:
            stderr = (proc.stderr or proc.stdout or "")[-2000:]
            raise RuntimeError(f"process.py 失败(rc={proc.returncode}): {stderr}")
        with _lock:
            jobs[job_id]["status"] = "completed"
    except Exception as exc:  # noqa: BLE001
        with _lock:
            jobs[job_id]["status"] = "failed"
            jobs[job_id]["error"] = str(exc)[:1000]
    finally:
        with _lock:
            _running -= 1
        # 清理上传的源视频，避免无限堆积
        ip = jobs.get(job_id, {}).get("input_path")
        if ip and Path(ip).exists():
            try:
                Path(ip).unlink()
            except OSError:
                pass


@app.get("/status/{job_id}", dependencies=[Depends(_require_token)])
def status(job_id: str) -> dict:
    with _lock:
        job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="未知任务")
    return job


@app.get("/file/{job_id}", dependencies=[Depends(_require_token)])
def file(job_id: str) -> FileResponse:
    with _lock:
        job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="未知任务")
    if job["status"] != "completed":
        raise HTTPException(status_code=409, detail=f"状态: {job['status']}")
    p = Path(job["output_path"])
    if not p.exists():
        raise HTTPException(status_code=404, detail="成片缺失")
    return FileResponse(p, filename=f"{job_id}.mp4", media_type="video/mp4")
