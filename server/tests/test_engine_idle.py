"""引擎空闲自动卸载（server/engine_idle.py）离线测试。

覆盖三条最关键的保证：
  1. 空闲超过 TTL 真的会被释放；
  2. 开关关掉时**绝不**释放（用户明确选择常驻）；
  3. touch（刚用过）不该被误释放 —— 否则刚加载就被回收，功能等于坏了。

⚠️ 配置隔离：经 VDL_DATA_DIR 把配置文件重定向到 tmp，绝不碰用户真实家目录
   （与 Keychain 那次事故的教训同源：外部存储必须可隔离）。
"""
import os
import sys
import tempfile
import time

SERVER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

import engine_idle  # noqa: E402


def _isolate():
    """把配置文件重定向到 tmp（经 VDL_DATA_DIR），返回 tmp 目录。"""
    tmp = tempfile.mkdtemp(prefix="vdl_idle_")
    os.environ["VDL_DATA_DIR"] = tmp
    # 清掉进程内可能残留的注册表，避免用例互相干扰
    engine_idle._ENGINES.clear()
    return tmp


class FakeEngine:
    """一个假的「引擎」：只记会话数，被释放时清零。"""

    def __init__(self, n=0):
        self.n = n

    def count(self):
        return self.n

    def release(self):
        freed = self.n
        self.n = 0
        return freed


def test_idle_over_ttl_gets_released():
    _isolate()
    engine_idle.save_config({"enabled": True, "ttl_seconds": 180})
    eng = FakeEngine(n=3)
    engine_idle.register("_t1", eng.release, eng.count)
    engine_idle._ENGINES["_t1"]["last_used"] = time.time() - 600  # 推到 10 分钟前
    freed = engine_idle._sweep_once()
    assert "_t1" in freed, f"应被释放，实际 freed={freed}"
    assert eng.n == 0, "会话数应清零"


def test_recently_used_is_not_released():
    _isolate()
    engine_idle.save_config({"enabled": True, "ttl_seconds": 180})
    eng = FakeEngine(n=2)
    engine_idle.register("_t2", eng.release, eng.count)
    engine_idle.touch("_t2")  # 刷新为「刚用过」
    freed = engine_idle._sweep_once()
    assert "_t2" not in freed, "刚用过不该被释放"
    assert eng.n == 2


def test_disabled_never_releases():
    _isolate()
    engine_idle.save_config({"enabled": False, "ttl_seconds": 180})
    assert engine_idle.get_config()["enabled"] is False
    eng = FakeEngine(n=1)
    engine_idle.register("_t3", eng.release, eng.count)
    engine_idle._ENGINES["_t3"]["last_used"] = time.time() - 99999
    freed = engine_idle._sweep_once()
    assert freed == [], f"关闭状态下不应释放，实际 freed={freed}"
    assert eng.n == 1


def test_default_config_is_three_minutes_and_on():
    _isolate()
    # 不写任何配置文件 → 走默认值
    cfg = engine_idle.get_config()
    assert cfg["enabled"] is True, "默认应开启（目标就是帮内存吃紧的用户）"
    assert cfg["ttl_seconds"] == 180, f"默认 TTL 应为 180，实际 {cfg['ttl_seconds']}"


def test_env_can_override_config():
    _isolate()
    os.environ["VDL_ENGINE_IDLE_UNLOAD"] = "off"
    try:
        cfg = engine_idle.get_config()
        assert cfg["enabled"] is False, "环境变量应可关掉开关"
    finally:
        del os.environ["VDL_ENGINE_IDLE_UNLOAD"]


def test_ttl_is_clamped():
    _isolate()
    engine_idle.save_config({"enabled": True, "ttl_seconds": 0})
    assert engine_idle.get_config()["ttl_seconds"] >= engine_idle.MIN_TTL
    engine_idle.save_config({"enabled": True, "ttl_seconds": 999999})
    assert engine_idle.get_config()["ttl_seconds"] <= engine_idle.MAX_TTL


def test_release_all():
    _isolate()
    a = FakeEngine(n=2)
    b = FakeEngine(n=0)
    engine_idle.register("_t6a", a.release, a.count)
    engine_idle.register("_t6b", b.release, b.count)
    freed = engine_idle.release_all()
    assert freed == 1, f"只有 a 有会话，应释放 1 个，实际 {freed}"
    assert a.n == 0


def test_status_reports_remaining_seconds():
    _isolate()
    engine_idle.save_config({"enabled": True, "ttl_seconds": 180})
    eng = FakeEngine(n=1)
    engine_idle.register("_t7", eng.release, eng.count)
    engine_idle.touch("_t7")
    st = engine_idle.status()
    item = [x for x in st["engines"] if x["name"] == "_t7"][0]
    assert item["loaded"] == 1
    assert item["releases_in"] is not None and item["releases_in"] > 0


if __name__ == "__main__":
    funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    fail = 0
    for fn in funcs:
        try:
            fn()
            print(f"  ✔ {fn.__name__}")
        except AssertionError as e:
            fail += 1
            print(f"  ✘ {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            fail += 1
            print(f"  ✘ {fn.__name__}: 异常 {type(e).__name__}: {e}")
    if fail:
        print(f"\n❌ 失败 {fail} 项")
        sys.exit(1)
    print(f"\n🎉 引擎空闲卸载测试全部通过（{len(funcs)} 项）")
