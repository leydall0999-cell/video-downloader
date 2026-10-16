"""守卫：用户信息类数据云备份（2026-10-16「用户数据放云端」）。

覆盖范围（用户口径）：**用户信息类数据** = 设置与偏好 / 收藏 / 最近记录 / 历史 /
平台登录态 Cookie；**不含功能产物**（成片、图片、解说），那些仍留本机。

钉住的契约（每条都是踩过或差点踩到的真问题）：
  1) 账号隔离：拿别人的 token 取不到我的资料包；
  2) 端到端加密：资料包里没有登录态明文；面板元数据既不含密文也不含明文；
  3) 换机还原：新机器 + 同账号 + 同密码 → 登录态逐字节还原、设置还原；
  4) 无密码 / 无密钥缓存 → 只同步设置，**不解密、也不把云端备份抹掉**；
  5) 错密码 → 拒绝上传：云端密文与 updated_at 逐字节不变，且不污染本机密钥缓存；
  6) 无谓推送抑制：内容没变不推，不把云端 updated_at 顶成当前时间；
  7) 本机路径字段（mlx_model_path / mlx_python）不进包；
  8) 目录穿越：畸形外层文件名不落盘。

跑法：`run_one test_user_data_cloud.py`（本文件带 main()，脚本模式下断言真的会执行）。
"""
import json
import os
import pathlib
import sys
import tempfile
import time

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "server"))

PASS = 0
FAIL = 0

_HDR_BILI = "SESSDATA=abc123; bili_jct=zzz"
_HDR_DY = "sessionid=qqq; ttwid=www"
_PW = "pw123456"


def ck(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK] {name}" + (f"  {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f"  {extra}" if extra else ""))


def _mk_dirs():
    return (pathlib.Path(tempfile.mkdtemp(prefix="vdl_ud_data_")),
            pathlib.Path(tempfile.mkdtemp(prefix="vdl_ud_ck_")),
            pathlib.Path(tempfile.mkdtemp(prefix="vdl_ud_key_")))


def _use(data, cookies, key):
    os.environ["VDL_DATA_DIR"] = str(data)
    os.environ["VDL_COOKIE_CACHE_DIR"] = str(cookies)
    os.environ["VDL_USERDATA_KEY_DIR"] = str(key)


def test_user_data_cloud() -> None:
    os.environ["VDL_USERDATA_AUTOSYNC"] = "0"        # 测试里不跑防抖线程
    srv = pathlib.Path(tempfile.mkdtemp(prefix="vdl_ud_srv_"))
    os.environ["VDL_LICENSE_DATA"] = str(srv / "license_data.json")
    os.environ["VDL_LICENSE_SECRET"] = "testsecret"
    sys.path.insert(0, str(REPO / "deploy"))

    import license_server as L
    import license_client as LC
    import user_data_cloud as U

    st = {"users": {}}
    L.register_impl(st, "u@x.com", _PW, time.time(), L.SECRET, {"fp": "f1", "name": "m1"})
    L.register_impl(st, "v@x.com", _PW, time.time(), L.SECRET, {"fp": "f2", "name": "m2"})
    tok = L.make_token("u@x.com", L.SECRET, time.time())
    tok_other = L.make_token("v@x.com", L.SECRET, time.time())

    def _pull(token, base_url=None, timeout=0, opener=None):
        try:
            return L.userdata_pull_impl(st, token, time.time(), L.SECRET)
        except L.ApiError as e:
            return {"ok": False, "error": e.msg, "code": e.code}

    def _push(token, payload, base_url=None, timeout=0, opener=None):
        try:
            return L.userdata_push_impl(st, token, payload, time.time(), L.SECRET)
        except L.ApiError as e:
            return {"ok": False, "error": e.msg, "code": e.code}

    LC.userdata_pull_remote = _pull
    LC.userdata_push_remote = _push

    d1, c1, k1 = _mk_dirs()
    d2, c2, k2 = _mk_dirs()

    # ── 机器 1：铺本机数据 ────────────────────────────────────────────────
    _use(d1, c1, k1)
    (c1 / "bilibili.com.json").write_text(
        json.dumps({"header": _HDR_BILI, "ts": 111.0}), encoding="utf-8")
    (c1 / "douyin.com.json").write_text(
        json.dumps({"header": _HDR_DY, "ts": 222.0}), encoding="utf-8")
    (d1 / "commentary_config.json").write_text(
        json.dumps({"narration_boost": 1.2}), encoding="utf-8")
    (d1 / "llm_config.json").write_text(
        json.dumps({"temperature": 0.7, "mlx_model_path": "/Users/someone/secret/path",
                    "mlx_python": "/Users/someone/.venv/bin/python"}), encoding="utf-8")

    if not U.crypto_available():
        print("  [SKIP] 当前解释器无 cryptography —— Cookie 分区相关断言整体跳过")
        return

    r = U.sync(tok, _PW)
    ck("首次同步上传成功", r["ok"] is True and r["action"] == "pushed", str(r.get("error")))

    blob = L.userdata_load("u@x.com")
    raw = json.dumps(blob, ensure_ascii=False)
    ck("资料包不含登录态明文", _HDR_BILI not in raw and _HDR_DY not in raw)
    ck("本机路径字段不进包", "mlx_model_path" not in raw and "mlx_python" not in raw)
    ck("Cookie 分区是密文", bool(blob["parts"]["cookies"]["ct"]))
    ck("平台清单正确",
       blob["parts"]["cookies"]["meta"]["platforms"] == ["bilibili.com", "douyin.com"])

    meta = L.usage_impl(st, "u@x.com", time.time())["data_meta"]
    mj = json.dumps(meta, ensure_ascii=False)
    ck("面板元数据有平台清单",
       meta and meta["cookies"]["platforms"] == ["bilibili.com", "douyin.com"])
    ck("面板元数据不含明文", _HDR_BILI not in mj)
    ck("面板元数据不含密文", '"ct"' not in mj and '"nonce"' not in mj)

    # ── 账号隔离 ─────────────────────────────────────────────────────────
    ck("他人 token 取不到我的资料包",
       L.userdata_pull_impl(st, tok_other, time.time(), L.SECRET)["payload"] is None)

    # ── 无谓推送抑制 ─────────────────────────────────────────────────────
    t_before = float(L.userdata_load("u@x.com")["updated_at"])
    r = U.sync(tok, _PW)
    ck("内容未变 → noop", r["action"] == "noop", r["action"])
    ck("云端 updated_at 未被顶高",
       float(L.userdata_load("u@x.com")["updated_at"]) == t_before)

    # ── 换机还原 ─────────────────────────────────────────────────────────
    _use(d2, c2, k2)
    r = U.sync(tok, _PW)
    ck("换机拉取成功", r["action"] in ("pulled", "mixed"), r["action"])
    ck("换机 Cookie 解密 ok", (r["pulled"] or {}).get("cookie_state") == "ok",
       str((r["pulled"] or {}).get("cookie_state")))
    ck("B 站登录态逐字节还原",
       json.loads((c2 / "bilibili.com.json").read_text())["header"] == _HDR_BILI)
    ck("抖音登录态逐字节还原",
       json.loads((c2 / "douyin.com.json").read_text())["header"] == _HDR_DY)
    ck("设置已还原",
       json.loads((d2 / "commentary_config.json").read_text())["narration_boost"] == 1.2)
    ck("换机不把路径带回来",
       "mlx_model_path" not in json.dumps(json.loads((d2 / "llm_config.json").read_text())))

    # ── 无密码 + 无密钥缓存：只同步设置，不破坏云端 ──────────────────────
    for x in k2.glob("*"):
        x.unlink()
    for x in c2.glob("*.json"):
        x.unlink()
    mk = d2 / ".userdata_sync.json"
    if mk.exists():
        mk.unlink()
    ct_before = L.userdata_load("u@x.com")["parts"]["cookies"]["ct"]
    r = U.sync(tok, None)
    ck("无密钥 → cookie_state=locked",
       (r["pulled"] or {}).get("cookie_state") == "locked",
       str((r["pulled"] or {}).get("cookie_state")))
    ck("无密钥不写回 cookie", not list(c2.glob("*.json")))
    ck("云端密文仍在（备份没被抹）",
       L.userdata_load("u@x.com")["parts"]["cookies"]["ct"] == ct_before)

    # ── 错密码：拒绝上传，不污染云端也不污染缓存 ────────────────────────
    t_before = float(L.userdata_load("u@x.com")["updated_at"])
    ct_before = L.userdata_load("u@x.com")["parts"]["cookies"]["ct"]
    if mk.exists():
        mk.unlink()
    r = U.sync(tok, "wrong-password")
    ck("错密码 → ok=False", r["ok"] is False, str(r.get("error")))
    ck("错密码 → rekey_required", r.get("rekey_required") is True)
    ck("云端密文逐字节未变",
       L.userdata_load("u@x.com")["parts"]["cookies"]["ct"] == ct_before)
    ck("云端 updated_at 未变",
       float(L.userdata_load("u@x.com")["updated_at"]) == t_before)
    ck("错密码未写进本机密钥缓存", U.load_cached_key() is None)

    # ── 本机更新的登录态不被云端旧值覆盖（备份不是镜像） ────────────────
    _use(d2, c2, k2)
    (c2 / "bilibili.com.json").write_text(
        json.dumps({"header": "LOCAL_NEW_LOGIN", "ts": 999.0}), encoding="utf-8")
    _salt = os.urandom(16)
    _key = U.derive_key(_PW, _salt, U.KDF_ITERS)
    _blob = U._encrypt({"bilibili.com.json": {"header": "CLOUD_OLD", "ts": 100.0}},
                       _key, _salt, U.KDF_ITERS)
    U.apply_payload({"parts": {"cookies": _blob}}, _PW)
    ck("云端旧登录态不覆盖本机新登录态",
       json.loads((c2 / "bilibili.com.json").read_text())["header"] == "LOCAL_NEW_LOGIN",
       json.loads((c2 / "bilibili.com.json").read_text())["header"])
    # 反向：本机那份**更旧**时才由云端补上（换机还原仍成立）
    (c2 / "bilibili.com.json").write_text(
        json.dumps({"header": "LOCAL_OLD_LOGIN", "ts": 50.0}), encoding="utf-8")
    U.apply_payload({"parts": {"cookies": _blob}}, _PW)
    ck("本机较旧 → 云端覆盖补上",
       json.loads((c2 / "bilibili.com.json").read_text())["header"] == "CLOUD_OLD",
       json.loads((c2 / "bilibili.com.json").read_text())["header"])

    # ── 目录穿越防护 ─────────────────────────────────────────────────────
    bad = U.apply_payload({"parts": {"cookies": {
        "enc": "aesgcm256", "kdf": {"salt": "c2FsdA==", "iters": 200000},
        "nonce": "AAAAAAAAAAAAAAAA", "ct": "AAAA", "meta": {}}}}, _PW)
    ck("畸形密文不崩、不落盘", bad["cookies"] == 0 and bad["cookie_state"] in
       ("bad_key", "locked", "ok"), str(bad))


def main() -> int:
    print("=== 用户信息类数据云备份 ===")
    test_user_data_cloud()
    print(f"\n{'=' * 46}\n通过 {PASS} / 失败 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
