"""VDL 免费用户配额闸门（云端按量计费的护栏）。

规则（docs/llm-cloud-fallback-design.md §12.4，用户已拍板）：
- 终身云端事件：3 次（不可恢复；auto 回落云端 / cloud_only 各耗 1 次）
- 每日 auto 运行：1 次（自然日重置；每天第 1 次 auto 运行不论本机成败都占 1 次）
- 单条上传视频时长：≤ 30 分钟（免费用户；会员不限）
会员：解除全部限制（云端无限 + 时长不限）。

持久化：~/.video-downloader/quota.json（与 membership 同目录）。
设计要点（对齐 membership.py）：纯标准库、零外部依赖；now_fn / path / is_member_fn
全部可注入，测试无需 mock 系统时钟、无需真实会员引擎。
"""
from __future__ import annotations

import json
import os
import hashlib
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

try:                                    # 跨进程锁：POSIX 用 flock，Windows 用 msvcrt
    import fcntl as _fcntl
except ImportError:                     # pragma: no cover
    _fcntl = None
try:
    import msvcrt as _msvcrt
except ImportError:                     # pragma: no cover
    _msvcrt = None

# ── 配额常量（唯一真源）────────────────────────────────────────────────────── #
LIFETIME_CLOUD_EVENTS = 3          # 终身云端事件上限（不可恢复）
DAILY_AUTO_RUNS = 1                # 每日 auto 运行额度（自然日重置）
FREE_MAX_DURATION_SEC = 30 * 60    # 免费用户单条视频时长上限（秒）

DEFAULT_BASE_DIR = Path(os.path.expanduser("~/.video-downloader"))

# ── Keychain 影子计数（2026-10-06）───────────────────────────────────────── #
# 🔴 为什么需要：`quota.json` 是**可删的普通文件**。实测的口子 ——
#   联网把 3 次用满（中心记 3）→ 断网 → 删掉 quota.json → 计数归零 → 又白嫖 3 次。
# 断网时本机没有中心可问，只能读本地文件；文件可删就等于计数可重置。
# 修法：在 macOS Keychain 里另存一份**删不掉**的影子计数（系统级 ACL 保护，
# 其他进程读不到、删不掉），断网时取「文件与影子的**较大值**」。
#
# 设计原则（与 credential_store 一致）：**fail-safe 优先于加固** ——
# Keychain 不可用（非 macOS / security 命令缺失 / 被拒）时静默回落到纯文件口径，
# 绝不让「加固失败」变成「用户用不了」。
_KEYCHAIN_SERVICE = "com.videodownloader.desktop"
_KEYCHAIN_ACCOUNT = "cloud_quota_shadow"
_KEYCHAIN_TIMEOUT = 3


def _keychain_service() -> str:
    """Keychain service 名；测试隔离时切独立命名空间。

    ⚠️ 与 credential_store._service() 同理：Keychain 是**系统级**的，不受
    VDL_DATA_DIR 隔离 —— 测试桩会覆盖真实条目。测试环境必须换 service。
    """
    if os.environ.get("VDL_DATA_DIR", "").strip() or os.environ.get("VDL_KEYCHAIN_STUB"):
        return _KEYCHAIN_SERVICE + ".test"
    return _KEYCHAIN_SERVICE


def _keychain_account(base_dir: Any = None) -> str:
    """影子条目的 account 名：**按数据目录分桶**。

    🔴 为什么按 base_dir 而不是全局一份（2026-10-06 修）：全局一份会让
    同一台机器上「不同数据目录 = 不同用户/不同实例」互相污染额度 —— 最典型的是
    离线测试用固定时间戳跑 `now=1000.0`，而影子里的终身计数是上一次别的用例
    留下的（实测把 test_quota 的跨日重置用例打成 deny）。按 base_dir 分桶后，
    每个数据目录一份影子，互不干扰，测试也天然隔离。
    """
    if base_dir is None:
        return _KEYCHAIN_ACCOUNT
    try:
        h = hashlib.sha256(str(Path(base_dir).resolve()).encode("utf-8")).hexdigest()[:16]
        return f"{_KEYCHAIN_ACCOUNT}.{h}"
    except Exception:
        return _KEYCHAIN_ACCOUNT


def _keychain_read(account: str) -> Optional[dict[str, Any]]:
    """读影子计数；读不到（非 macOS / 条目不存在 / 被拒）返回 None。"""
    if sys.platform != "darwin":
        return None
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", _keychain_service(),
             "-a", account, "-w"],
            capture_output=True, timeout=_KEYCHAIN_TIMEOUT)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    try:
        raw = (r.stdout or b"").decode("utf-8").strip()
        data = json.loads(raw) if raw.startswith("{") else json.loads(
            (r.stdout or b"").decode("utf-8"))
    except Exception:
        # 旧版可能只存了一个纯数字（早期只影子终身计数）
        try:
            return {"lifetime_cloud_used": int((r.stdout or b"").decode("utf-8").strip())}
        except Exception:
            return None
    if not isinstance(data, dict):
        return None
    out = {}
    for k in ("lifetime_cloud_used", "daily_auto_used"):
        try:
            out[k] = max(0, int(data.get(k) or 0))
        except (TypeError, ValueError):
            out[k] = 0
    out["daily_date"] = str(data.get("daily_date") or "")
    return out


def _keychain_write(state: dict[str, Any], today: str, account: str) -> None:
    """写影子计数；失败静默（加固是可选的，不阻断主流程）。"""
    if sys.platform != "darwin":
        return
    payload = {}
    for k in ("lifetime_cloud_used", "daily_auto_used"):
        try:
            payload[k] = max(0, int(state.get(k) or 0))
        except (TypeError, ValueError):
            payload[k] = 0
    # 日配额必须带日期：否则次日读到的还是「昨天用尽」的计数（跨日不重置）
    payload["daily_date"] = today or str(state.get("daily_date") or "")
    try:
        data = json.dumps(payload, ensure_ascii=False)
        # delete 先行：已存在时 add 会报错；不存在时忽略错误继续 add
        subprocess.run(["security", "delete-generic-password", "-s",
                        _keychain_service(), "-a", account],
                       capture_output=True, timeout=_KEYCHAIN_TIMEOUT)
        subprocess.run(["security", "add-generic-password", "-U", "-s",
                        _keychain_service(), "-a", account,
                        "-w", data],
                       capture_output=True, timeout=_KEYCHAIN_TIMEOUT)
    except Exception:
        pass


def _shadow_merge(state: dict[str, Any], today: str = "",
                  account: str = "") -> dict[str, Any]:
    """把影子计数**合并进** state：终身取 max，日配额按日期取。

    取 max 而非信任文件 —— 文件可能被用户手改小，也可能在 Keychain 不可用
    时是唯一来源。max 保证「已经用掉的额度不会因为任一侧丢失而复原」。

    🔴 日配额必须**带日期**参与比较（2026-10-06 修）：影子只存一个数字的话，
    次日读到的还是昨天用尽的计数，用户一上来就看到「今天已用满」。
    日配额本身按自然日重置，所以只在**同一天**才取 max，跨日忽略影子。

    `today` 由调用方按注入的 `now_fn` 算出（不读真实系统时间）—— 否则
    用固定时间戳的测试会与影子日期错位。
    """
    sh = _keychain_read(account or _KEYCHAIN_ACCOUNT)
    if not sh:
        return state
    try:
        cur_life = int(state.get("lifetime_cloud_used") or 0)
    except (TypeError, ValueError):
        cur_life = 0
    state["lifetime_cloud_used"] = max(cur_life, int(sh.get("lifetime_cloud_used") or 0))
    day = today or str(state.get("daily_date") or "")
    sdate = str(sh.get("daily_date") or "")
    if day and sdate == day:
        try:
            cur_daily = int(state.get("daily_auto_used") or 0)
        except (TypeError, ValueError):
            cur_daily = 0
        state["daily_auto_used"] = max(cur_daily, int(sh.get("daily_auto_used") or 0))
    return state


def _shadow_write(state: dict[str, Any], today: str = "",
                   account: str = "") -> None:
    """把当前计数同步进影子（每次成功计数后调用）。"""
    _keychain_write(state, today, account or _KEYCHAIN_ACCOUNT)

# ── 落盘：唯一临时名原子写 + 「载入 → 改 → 写回」临界区 ───────────────────── #
# 为什么（2026-09-16 同类隐患清查）：quota.json 有**两个进程**的写者 —— 子进程
# worker（llm_script 扣额度）与父进程（任务失败时退额度）。此前是**直接
# write_text**（先截断再写），于是：
#   · 并发的另一方、以及 /api/quota/status 的读者，会读到「写了一半」的文件；
#     `_load()` 解析失败后按空状态返回 → 计数归零 = 免费云端额度**整体复原**；
#   · 进程被杀 / 断电则永久留下半截文件，此后每次都「额度复原」。
# 现改为同目录唯一临时名 + os.replace 原子换入，并用 flock 把「载入 → 改 → 写回」
# **整段**串起来（只锁落盘等于没锁：两个进程照样各自读到同一份旧快照）。
# ⚠️ 本文件与解说管线 `scripts/quota.py` **逐字节同源**（diff 校验守护），故这些
#    helper 就地实现，不 import app 侧的 atomic_io（那个模块不随管线分发）。

_TMP_MARK = ".tmp"
_seq_guard = threading.Lock()
_seq = 0
_LOCKS: dict = {}
_LOCKS_GUARD = threading.Lock()
_reentry = threading.local()


def _next_seq() -> int:
    global _seq
    with _seq_guard:
        _seq += 1
        return _seq


def _write_atomic(path: Path, text: str) -> None:
    """唯一临时名（pid + 线程 id + 序号）+ os.replace：绝不就地截断目标文件。"""
    tmp = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.{_next_seq()}{_TMP_MARK}"
    )
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    finally:
        if tmp.exists():                # 失败路径不留垃圾
            try:
                tmp.unlink()
            except OSError:
                pass


def _path_lock(path: Path) -> threading.RLock:
    """按绝对路径共享的进程内锁。

    ⚠️ 必须按**路径**而不是按实例：`routers/quota.py` 每个请求都新建一个
    QuotaManager，实例级锁互相看不见 = 等于没锁。
    """
    key = os.path.abspath(str(path))
    with _LOCKS_GUARD:
        lk = _LOCKS.get(key)
        if lk is None:
            lk = threading.RLock()
            _LOCKS[key] = lk
        return lk


def _flock_acquire(lock_path: Path, timeout: float = 2.0):
    """取跨进程独占锁；平台不支持 / 超时 → 返回 None（降级为仅进程内锁）。"""
    if _fcntl is None and _msvcrt is None:
        return None
    try:
        fh = open(lock_path, "a+")
    except OSError:
        return None
    deadline = time.time() + timeout
    while True:
        try:
            if _fcntl is not None:
                _fcntl.flock(fh.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            else:
                _msvcrt.locking(fh.fileno(), _msvcrt.LK_NBLCK, 1)   # type: ignore[union-attr]
            return fh
        except OSError:
            if time.time() >= deadline:
                # 绝不为了记账把用户卡死：降级为仅进程内锁
                try:
                    fh.close()
                except OSError:
                    pass
                return None
            time.sleep(0.02)


def _flock_release(fh) -> None:
    if fh is None:
        return
    try:
        if _fcntl is not None:
            _fcntl.flock(fh.fileno(), _fcntl.LOCK_UN)
        elif _msvcrt is not None:
            _msvcrt.locking(fh.fileno(), _msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    finally:
        try:
            fh.close()
        except OSError:
            pass


class QuotaManager:
    """免费用户配额状态机。

    并发：三个写入口（扣终身 / 扣每日 / 退还）都在 `_mutation()` 临界区内
    （进程内按路径 RLock + 跨进程 flock）；只读查询不加锁。
    """

    def __init__(
        self,
        base_dir: Optional[Path | str] = None,
        now_fn: Optional[Callable[[], float]] = None,
        is_member_fn: Optional[Callable[[], bool]] = None,
        token_fn: Optional[Callable[[], str]] = None,
    ) -> None:
        self.base_dir = Path(base_dir) if base_dir else DEFAULT_BASE_DIR
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.base_dir / "quota.json"
        self._now = now_fn or time.time
        self._is_member = is_member_fn or (lambda: False)
        # 🔴 2026-10-06 跨端记账：取云端账号 token。注入而非 import，是为了不把
        # quota.py（及其管线同源副本）绑死在 membership/app 上，也便于测试。
        self._token = token_fn or (lambda: "")
        # 影子条目按数据目录分桶：不同用户/实例互不污染（见 _keychain_account）
        self._shadow_acct = _keychain_account(self.base_dir)

    # ── 云端记账（跨端唯一真源；断网 fail-open 降级到本机计数）───────────── #
    def _cloud(self, lifetime: int = 0, daily: int = 0,
               refund: bool = False) -> Optional[dict]:
        """调授权中心原子扣减/退还/查询。**返回 None 表示「没拿到权威结果」**。

        三种 None 情形一律 fail-open（沿用本机 quota.json 计数）：
          · 没登录云端账号（token 为空）→ 本机专属用户，不该被云端拦
          · 网络异常 / 中心不可达
          · 中心返回体异常
        ⚠️ 绝不因为「查不到」就当成「额度已用尽」——那会把断网用户和免费用户
        一起误伤，且旧口径下断网本来是放行的。
        """
        try:
            import license_client
        except Exception:
            return None
        try:
            tok = str(self._token() or "").strip()
        except Exception:
            return None
        if not tok:
            return None
        try:
            r = license_client.cloud_quota_remote(tok, lifetime=lifetime,
                                                  daily=daily, refund=refund)
        except Exception:
            return None
        if not isinstance(r, dict) or not r.get("ok"):
            return None
        return r

    def _apply_cloud_view(self, cq: Any) -> bool:
        """把云端快照**覆盖**写回本机 quota.json（计数 + 日切日期）。

        🔴 `daily_date` 必须一起写（2026-10-06 修）：只写 `daily_auto_used` 会
        留下「计数是新的、日期还是旧的」的不一致状态 —— 次日本机惰性日切按旧日期
        重置后算出 remaining=0，在中心已经放行的情况下**本地反而拒绝**（实测
        「次日恢复」用例挂）。故日期与计数必须同源落下。

        用覆盖而非 max：云端是唯一真源，本机计数在中心可达时一律以云端为准
        （否则「重装系统 → 本机归零 → 又变 3 次」的口子会重新出现）。
        """
        if not isinstance(cq, dict):
            return False
        try:
            st = self._state()
            st["lifetime_cloud_used"] = max(0, int(cq.get("lifetime_used") or 0))
            st["daily_auto_used"] = max(0, int(cq.get("daily_auto_used") or 0))
            cdate = str(cq.get("date") or "").strip()
            if cdate:
                # 以云端（北京）日切日期为准：本机 _roll_daily 按本机时区算，
                # 与云端可能差一天，差值会让「每日 auto」在错误的边界重置。
                st["daily_date"] = cdate
                # 🔴 2026-10-07：`daily_date_src` 记下「这个日期是中心给的」，
                # 读侧日切凭它判断该不该信任（详见 _cloud_day_fresh）。没有它，
                # 回灌的计数会在下一次 _state() 被本机日切抹掉。
                st["daily_date_src"] = cdate
            # 🔴 roll=False：云端是日切的真源，这里再按本机时区 roll 一次会把
            # 刚回灌的「中心当天已用次数」清零（跨时区用户必现）。详见 _persist。
            self._persist(st, roll=False)
            return True
        except Exception:
            return False

    def _sync_from_cloud(self) -> Optional[dict]:
        """把云端余额**覆盖**写回本机 quota.json（登录/心跳后回灌）。"""
        r = self._cloud()
        cq = (r or {}).get("cloud_quota")
        if not self._apply_cloud_view(cq):
            return None
        return cq

    def _consume_cloud(self, lifetime: int = 0, daily: int = 0,
                       refund: bool = False) -> Optional[bool]:
        """中心原子扣减。返回 True=已扣、False=中心明确拒绝、None=拿不到结果。"""
        r = self._cloud(lifetime=lifetime, daily=daily, refund=refund)
        if r is None:
            return None
        # 回灌本机（计数 + 云端日切日期），保证两端显示与日切边界一致
        self._apply_cloud_view(r.get("cloud_quota"))
        return bool(r.get("allowed"))

    # ── 持久化 ──────────────────────────────────────────────────────────── #
    def _load(self) -> dict:
        """读取状态。解析失败 → 空 dict（等价「额度全部复原」）。

        ⚠️ 这个兜底看着无害，其实很贵：只要文件出现半截内容，用户就会白拿回
        3 次终身云端额度。改为唯一临时名原子写之后「读到半截」这条路径已不存在，
        所以**不要**把 `_save` 改回就地 write_text。

        🔴 2026-10-06：读到之后合并 Keychain 影子计数（终身取 max、日配额按日期
        取 max）。否则「联网用满 → 断网 → 删 quota.json」会把计数归零，白嫖 3 次。
        """
        today = self._today()
        if self.path.exists():
            try:
                return _shadow_merge(
                    json.loads(self.path.read_text(encoding="utf-8") or "{}"),
                    today, self._shadow_acct)
            except Exception:
                return {}
        # 文件不存在（重装/删文件）：仍要读影子，否则删文件就等于清零额度
        try:
            return _shadow_merge({}, today, self._shadow_acct)
        except Exception:
            return {}

    def _save(self, st: dict) -> None:
        _write_atomic(self.path, json.dumps(st, ensure_ascii=False, indent=2))
        # 影子同步到系统 Keychain：让「删掉 quota.json」不再是重置手段
        _shadow_write(st, self._today(), self._shadow_acct)

    @contextmanager
    def _mutation(self) -> Iterator[None]:
        """「载入 → 改 → 写回」临界区（进程内 RLock + 跨进程 flock）。

        跨进程锁只在**最外层**取一次：同一线程重入时若再开一个 fd 去 flock，
        会和自己的第一把锁死锁（flock 按 open file description 计）。
        """
        with _path_lock(self.path):
            depth = int(getattr(_reentry, "depth", 0))
            fh = None
            if depth == 0:
                fh = _flock_acquire(self.base_dir / ".quota.lock")
            _reentry.depth = depth + 1
            try:
                yield
            finally:
                _reentry.depth = depth
                if fh is not None:
                    _flock_release(fh)

    def _today(self) -> str:
        return time.strftime("%Y-%m-%d", time.localtime(self._now()))

    def _cloud_day_fresh(self, st: dict, today: str, cur: str) -> bool:
        """盘上的日切日期是不是「中心刚给的、且还没过期」。

        中心按**北京时间**日切，本机 `_today()` 按**本机时区**算 —— 境外用户
        （或系统时区非 UTC+8）两边会差一天。若一律按本机日期 roll，中心刚回灌的
        「今天已用几次」就会被清掉，本机显示「还剩 1 次」而中心其实已用满。

        判定：盘上日期 == 中心最近下发的日期（`daily_date_src`，由回灌写入），
        且与本机今天相差 ≤1 天（跨时区最多差一天）。超过一天说明是很久以前的
        云端快照（长期断网），此时不再信任 —— 否则日切永不触发会把用户卡死。
        """
        src = str(st.get("daily_date_src") or "")
        if not src or src != cur:
            return False
        try:
            from datetime import date as _date
            gap = abs((_date.fromisoformat(src) - _date.fromisoformat(today)).days)
        except (ValueError, TypeError):
            return False
        return gap <= 1

    def _roll_daily(self, st: dict) -> None:
        today = self._today()
        cur = str(st.get("daily_date") or "")
        if cur == today:
            return
        # 🔴 2026-10-07：云端日界优先。详见 _cloud_day_fresh。
        if self._cloud_day_fresh(st, today, cur):
            return
        st["daily_date"] = today
        st["daily_auto_used"] = 0

    def _state(self) -> dict:
        st = self._load()
        self._roll_daily(st)
        return st

    def _persist(self, st: dict, roll: bool = True) -> None:
        """写盘。`roll=False` 用于**云端回灌**路径 —— 见下方说明。

        🔴 2026-10-07 实测抓到：回灌（`_apply_cloud_view`）写入的每日计数会被
        这里的 `_roll_daily` 当场抹掉，导致回灌形同没做过。
        触发条件：中心日切日期 ≠ 本机 `_today()` —— 中心按**北京时间**日切，
        本机的「今天」由**本机时区**决定，用户在境外（或系统时区非 UTC+8）时
        两者会差一天。后果：本机显示「今天还剩 1 次」而中心其实已用满。

        修法：日切只在**读侧**（`_state()`）做就够了 —— 所有扣减路径都先读
        （已 roll），写盘再 roll 一次属重复且有害。回灌路径显式关闭。
        """
        if roll:
            self._roll_daily(st)
        self._save(st)

    # ── 查询 ────────────────────────────────────────────────────────────── #
    def _admin_exempt(self) -> bool:
        """管理员豁免（2026-09-19）：作者本机自用不受免费配额约束。

        满足任一即豁免：环境变量 VDL_QUOTA_ADMIN=1，或 base_dir 下存在
        .admin_exempt 标记文件。标记文件是主通道——桌面 App 由用户双击启动，
        无法可靠注入环境变量；文件标记对任何启动方式都生效，删除即收回。
        分发用户机器上不存在该文件，不受影响。
        """
        if (os.environ.get("VDL_QUOTA_ADMIN") or "").strip() == "1":
            return True
        try:
            return (self.base_dir / ".admin_exempt").exists()
        except Exception:
            return False

    def is_member(self) -> bool:
        if self._admin_exempt():
            return True
        try:
            return bool(self._is_member())
        except Exception:
            return False

    def lifetime_cloud_remaining(self) -> int:
        if self.is_member():
            return 10 ** 9
        return max(0, LIFETIME_CLOUD_EVENTS - int(self._state().get("lifetime_cloud_used", 0)))

    def daily_auto_remaining(self) -> int:
        if self.is_member():
            return 10 ** 9
        return max(0, DAILY_AUTO_RUNS - int(self._state().get("daily_auto_used", 0)))

    def can_upload_video(self, duration_sec: float) -> bool:
        """免费用户单条视频 ≤ 30 分钟才放行；会员 / 时长未知(≤0) 不拦（前端主拦，服务端兜底宽松）。"""
        if self.is_member():
            return True
        if not duration_sec or duration_sec <= 0:
            return True
        return duration_sec <= FREE_MAX_DURATION_SEC

    # ── 变更 ────────────────────────────────────────────────────────────── #
    def consume_cloud_event(self) -> bool:
        """扣 1 次终身云端事件。额度不足返回 False。

        🔴 2026-10-06：先问授权中心（账号级原子扣减，跨端共享）。中心明确拒绝
        （allowed=False）即**真的没有额度了** → 返回 False，堵住「换电脑重置」
        的漏洞。中心拿不到结果（未登录 / 断网 / 中心异常）→ 降级用本机计数，
        保持旧口径不误伤（fail-open）。

        ⚠️ 顺序：必须**先问中心再看本机**。开头的本地前置检查会造成假拒 ——
        本机缓存可能比中心旧（另一台机器刚用掉额度，本机还显示有余额，反之亦然），
        本地判定等于用缓存覆盖真源。本地判定只作中心不可达时的兜底。
        """
        if self.is_member():
            return True
        remote = self._consume_cloud(lifetime=1)
        if remote is not None:
            return remote
        with self._mutation():
            st = self._state()
            if self.lifetime_cloud_remaining() <= 0:
                return False
            st["lifetime_cloud_used"] = int(st.get("lifetime_cloud_used", 0)) + 1
            self._persist(st)
            return True

    # ── 退还（任务失败不该算用户头上）──────────────────────────────────── #
    def snapshot(self) -> dict:
        """当前用量快照，供「失败了要不要退」比对。"""
        st = self._state()
        return {
            "lifetime_cloud_used": int(st.get("lifetime_cloud_used", 0)),
            "daily_auto_used": int(st.get("daily_auto_used", 0)),
        }

    def refund(self, lifetime: int = 0, daily: int = 0) -> dict:
        """退还已扣额度（失败任务 / 异常终止的补偿）。

        用户视角的铁律：**没出片就不该扣额度**。管线在云端调用前就扣了 1 次
        终身额度，若之后整条任务失败（网络、超时、模型返回空内容），用户既没
        拿到成片、又少了 1 次机会——体感等同「花了钱买失败」。故在父进程侧按
        实际增量退还，绝不少扣也绝不超退（clamp 到 0）。

        🔴 2026-10-06：退还也要同步到中心，否则「任务失败退还」只退本机，
        云端计数不变 → 用户换台电脑后额度已被扣光却从未退过。先试中心，
        拿不到结果才退回纯本机退还。
        """
        if self.is_member():
            return self.snapshot()
        if lifetime <= 0 and daily <= 0:
            return self.snapshot()
        remote = self._consume_cloud(lifetime=lifetime, daily=daily, refund=True)
        if remote is not None:
            return self.snapshot()
        with self._mutation():
            st = self._state()
            cur_life = int(st.get("lifetime_cloud_used", 0))
            cur_daily = int(st.get("daily_auto_used", 0))
            new_life = max(0, cur_life - int(lifetime))
            new_daily = max(0, cur_daily - int(daily))
            if new_life == cur_life and new_daily == cur_daily:
                return self.snapshot()   # 无变化不落盘：避免无谓写文件与 mtime 抖动
            st["lifetime_cloud_used"] = new_life
            st["daily_auto_used"] = new_daily
            self._persist(st)
            return self.snapshot()

    def consume_daily_auto(self) -> bool:
        """扣 1 次每日 auto 额度。额度不足返回 False。

        🔴 2026-10-06 顺序铁律：**先问中心，再看本机**。
        早先放在函数开头的 `if self.daily_auto_remaining() <= 0: return False`
        会在中心可达时造成假拒 —— 本机缓存可能停留在昨天（计数 1、日切日期也
        停在昨天），次日真实额度已恢复，本机却算出 remaining=0 直接拒绝，
        中心根本没被问到（实测「次日恢复」用例挂）。本机计数是缓存，中心才是
        真源，故本地判定只能作为**中心不可达时**的兜底。
        """
        if self.is_member():
            return True
        remote = self._consume_cloud(daily=1)
        if remote is not None:
            return remote
        # fail-open 兜底：断网 / 未登录云端账号 → 沿用本机计数
        with self._mutation():
            st = self._state()
            if self.daily_auto_remaining() <= 0:
                return False
            st["daily_auto_used"] = int(st.get("daily_auto_used", 0)) + 1
            self._persist(st)
            return True

    # ── 前置可行性预检（在「开始前」拦住做不完的任务）────────────────────── #
    def precheck_commentary(
        self,
        duration_sec: float,
        local_engine_ready: bool = False,
        engine: str = "auto",
    ) -> dict:
        """解说任务开始前的可行性预检，返回结构化结论供调用方转人话提示。

        为什么必须前置（2026-09-15 用户实测反馈）：免费用户跑一条超出本机能力
        的片子，会在转写与脚本生成**全部跑完之后**才因云端额度不足失败——用户
        白等十几分钟，拿到的是废片。故所有解说入口在「开始前」统一过这道闸门，
        不通过就立刻拒绝并说明两条出路，绝不进入执行阶段。

        判定顺序（免费用户）：
        1. 时长为 0/未知 → fail-open 放行（探测失败不应误拦）。
        2. 时长 > FREE_MAX_DURATION_SEC → 拒绝。这条片必然要走云端，而免费档位
           的上限就是 30 分钟，没有可用的执行路径。
        3. 时长合法 → 本机引擎就绪则放行（零消耗）；否则需要云端，
           终身云端额度为 0 时拒绝。

        返回 {allowed, code, reason, hint, will_use_cloud}。
        字段名用 allowed 而非 ok：前端 request() 会把「HTTP 成功」统一标成 ok，
        两者同名会让调用方分不清「请求成功」与「业务放行」。
        """
        if self.is_member():
            return {"allowed": True, "code": "member", "reason": "", "hint": "",
                    "will_use_cloud": False}
        if not duration_sec or duration_sec <= 0:
            return {"allowed": True, "code": "unknown_duration", "reason": "", "hint": "",
                    "will_use_cloud": False}

        limit_min = int(FREE_MAX_DURATION_SEC // 60)
        dur_min = int(round(float(duration_sec) / 60.0))
        if duration_sec > FREE_MAX_DURATION_SEC:
            return {
                "allowed": False,
                "code": "duration_exceeded",
                "reason": f"视频约 {dur_min} 分钟，超过免费版单条 {limit_min} 分钟上限",
                "hint": (
                    f"免费版单条解说视频最长 {limit_min} 分钟，当前视频约 {dur_min} 分钟，"
                    f"超出 {dur_min - limit_min} 分钟。请换用 {limit_min} 分钟以内的素材，"
                    f"或开通会员解锁长视频解说。"
                ),
                "will_use_cloud": False,
                "duration_sec": float(duration_sec),
                "limit_sec": FREE_MAX_DURATION_SEC,
                "over_sec": float(duration_sec) - FREE_MAX_DURATION_SEC,
            }

        # 走云端的两种来源语义不同，提示文案必须区分，否则会误导用户
        # （明明是自己选的「纯云端」，却被提示「本机引擎不可用」）。
        _explicit_cloud = str(engine or "").strip().lower() == "cloud"
        need_cloud = _explicit_cloud or (not local_engine_ready)
        if need_cloud and self.lifetime_cloud_remaining() <= 0:
            return {
                "allowed": False,
                "code": "cloud_quota_exhausted",
                "reason": "本机引擎不可用，且免费云端额度已用完",
                "hint": (
                    "这条视频需要云端生成解说词，但免费云端额度（终身 "
                    f"{LIFETIME_CLOUD_EVENTS} 次）已用完。开通会员即可解锁无限云端解说。"
                ),
                "will_use_cloud": True,
                "duration_sec": float(duration_sec),
            }

        if not need_cloud:
            _hint = ""
        elif _explicit_cloud:
            _hint = "你选择的是「纯云端」，本次将消耗 1 次云端额度。"
        else:
            _hint = "本机 AI 引擎不可用，本次将由云端生成解说词，消耗 1 次云端额度。"

        return {
            "allowed": True,
            "code": "will_use_cloud" if need_cloud else "local",
            "reason": "",
            "hint": _hint,
            "will_use_cloud": need_cloud,
            "duration_sec": float(duration_sec),
        }

    # ── 决策（供 llm_script 云端回落使用）────────────────────────────────── #
    def decide_cloud_fallback(self, mode: str = "auto") -> str:
        """云端闸门决策（两种云端来源语义不同，必须分开判）。

        mode='auto'       引擎 auto，本机跑不动/失败时**回落**云端：
                          受「终身额度」+「每日 auto 运行额度」双重约束
                          （每日额度是给自动回落行为设的频次护栏）。
        mode='cloud_only' 用户在设置里**明确选云端**：只受「终身额度」约束。
                          每日 auto 额度不该拦显式选择，否则当天跑过 1 次后
                          第 2 次会以「额度已用完」被误拒（实际终身还剩额度）。

        返回 'allow'（放行）/ 'deny'（禁止，调用方给人话提示）。会员永远 allow。
        """
        if self.is_member():
            return "allow"
        if self.lifetime_cloud_remaining() <= 0:
            return "deny"          # 终身云端额度耗尽
        if mode == "cloud_only":
            return "allow"         # 显式选云端：不检查也不消耗每日 auto 额度
        if self.daily_auto_remaining() <= 0:
            return "deny"          # 当日 auto 已用满 → 强制 local_only
        return "allow"

    def status(self) -> dict:
        st = self._state()
        admin = self._admin_exempt()
        member = self.is_member() and not admin   # 管理员豁免≠会员，如实分开报告
        return {
            "is_member": member,
            "admin_exempt": admin,
            "lifetime_cloud_used": 0 if member else int(st.get("lifetime_cloud_used", 0)),
            "lifetime_cloud_limit": LIFETIME_CLOUD_EVENTS,
            "lifetime_cloud_remaining": self.lifetime_cloud_remaining(),
            "daily_auto_used": 0 if member else int(st.get("daily_auto_used", 0)),
            "daily_auto_limit": DAILY_AUTO_RUNS,
            "daily_auto_remaining": self.daily_auto_remaining(),
            "free_max_duration_sec": FREE_MAX_DURATION_SEC,
        }

    def sync_from_cloud(self) -> Optional[dict]:
        """登录/心跳成功后调用：把云端余额覆盖回本机，使两端显示一致。

        失败静默返回 None —— 展示类回灌不值得打断流程（记账路径另有 fail-open）。
        """
        return self._sync_from_cloud()


# 模块级单例（默认非会员；is_member 由调用方或 server 端注入）
_default_manager: Optional[QuotaManager] = None


def get_manager() -> QuotaManager:
    global _default_manager
    if _default_manager is None:
        _default_manager = QuotaManager()
    return _default_manager


def set_member_fn(fn: Callable[[], bool]) -> None:
    """注入会员判定（server 端调用，避免管线层依赖会员引擎）。"""
    global _default_manager
    if _default_manager is None:
        _default_manager = QuotaManager(is_member_fn=fn)
    else:
        _default_manager._is_member = fn  # type: ignore[assignment]


def set_token_fn(fn: Callable[[], str]) -> None:
    """注入云端账号 token 取值（server 端 / 管线 env 注入后调用）。

    🔴 2026-10-06 跨端云端额度记账：没有它，QuotaManager 拿不到云端身份 →
    只能 fail-open 用本机计数 → 「换电脑重置额度」的口子照旧存在。
    server 端经 `routers.quota.get_quota_manager` 构造时直接传 token_fn；
    管线侧是独立进程，改用 `VDL_CLOUD_TOKEN` 环境变量（见 llm_script.py）。
    """
    global _default_manager
    if _default_manager is None:
        _default_manager = QuotaManager(token_fn=fn)
    else:
        _default_manager._token = fn  # type: ignore[assignment]
