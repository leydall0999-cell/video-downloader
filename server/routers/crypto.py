"""routers/crypto.py — 由 server/app.py 抽取（Phase 1）。
保持原 handler 逻辑不变；通过 `app.<name>` 访问共享内核（globals/helper/模块导入）。
所有 profile 均挂载，网页版行为零变化。
"""
from __future__ import annotations

import app  # 共享内核（模块级符号通过 app.<name> 访问）
from fastapi import APIRouter

router = APIRouter()


@router.get("/api/crypto/status")
def crypto_status() -> dict:
    app._require_crypto()
    return {
        "enabled": app.CRYPTO_ENABLED,
        "has_pass": bool(app._vault_load()),
        "locked": app.VAULT_KEY is None,
    }

@router.post("/api/crypto/set-pass")
def crypto_set_pass(req: app.CryptoSetPassRequest) -> dict:
    app._require_crypto()
    vault = app._vault_load()
    if vault is not None:
        # 已有密码：必须提供正确的旧密码方可修改
        if not req.old or not app.crypto_mod.verify_passphrase(req.old, vault):
            raise app.HTTPException(status_code=400, detail="旧密码错误")
    else:
        if req.passwd != req.confirm:
            raise app.HTTPException(status_code=400, detail="两次输入的密码不一致")
    if len(req.passwd) < 4:
        raise app.HTTPException(status_code=400, detail="密码至少 4 位")
    new_vault = app.crypto_mod.new_vault(req.passwd)
    app._vault_save(new_vault)
    # 设完即解锁，立即可用
# (global/nonlocal removed: 通过 app.<name> 访问模块全局)
    app.VAULT_KEY = app.crypto_mod.unlock_key(req.passwd, new_vault)
    return {"has_pass": True, "locked": False}


@router.post("/api/crypto/unlock")
def crypto_unlock(req: app.CryptoUnlockRequest) -> dict:
    app._require_crypto()
    vault = app._vault_load()
    if not vault:
        raise app.HTTPException(status_code=400, detail="尚未设置保险箱密码")
    if not app.crypto_mod.verify_passphrase(req.passwd, vault):
        raise app.HTTPException(status_code=401, detail="密码错误")
# (global/nonlocal removed: 通过 app.<name> 访问模块全局)
    app.VAULT_KEY = app.crypto_mod.unlock_key(req.passwd, vault)
    return {"locked": False}

@router.post("/api/crypto/lock")
def crypto_lock() -> dict:
    app._require_crypto()
# (global/nonlocal removed: 通过 app.<name> 访问模块全局)
    app.VAULT_KEY = None
    return {"locked": True}

@router.post("/api/crypto/encrypt")
def crypto_encrypt(req: app.CryptoIdsRequest) -> dict:
    app._require_crypto()
    app._require_unlocked()
    if not req.lib_ids:
        raise app.HTTPException(status_code=400, detail="未选择文件")
    job_id = "cry_" + app.uuid.uuid4().hex[:12]
    with app.CRYPTO_LOCK:
        app.CRYPTO_JOBS[job_id] = {"status": "queued", "done": 0, "total": len(req.lib_ids),
                               "errors": [], "cancel": False, "mode": "encrypt"}
    app.CRYPTO_EXECUTOR.submit(app._run_crypto_job, job_id, list(req.lib_ids), "encrypt")
    app._prune_crypto_jobs()
    return {"job_id": job_id}


@router.post("/api/crypto/decrypt")
def crypto_decrypt(req: app.CryptoIdsRequest) -> dict:
    app._require_crypto()
    app._require_unlocked()
    if not req.lib_ids:
        raise app.HTTPException(status_code=400, detail="未选择文件")
    job_id = "cry_" + app.uuid.uuid4().hex[:12]
    with app.CRYPTO_LOCK:
        app.CRYPTO_JOBS[job_id] = {"status": "queued", "done": 0, "total": len(req.lib_ids),
                               "errors": [], "cancel": False, "mode": "decrypt"}
    app.CRYPTO_EXECUTOR.submit(app._run_crypto_job, job_id, list(req.lib_ids), "decrypt")
    app._prune_crypto_jobs()
    return {"job_id": job_id}


@router.get("/api/crypto/job/{job_id}")
def crypto_job(job_id: str) -> dict:
    app._require_crypto()
    return app._crypto_job_status(job_id)


@router.post("/api/crypto/cancel/{job_id}")
def crypto_cancel(job_id: str) -> dict:
    app._require_crypto()
    with app.CRYPTO_LOCK:
        job = app.CRYPTO_JOBS.get(job_id)
        if not job:
            raise app.HTTPException(status_code=404, detail="任务不存在")
        job["cancel"] = True
    return {"canceled": True}

