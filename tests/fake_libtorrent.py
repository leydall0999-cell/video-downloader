"""测试用 mock libtorrent：实现 server/torrent.py 实际调用的 API 表面。

只覆盖 torrent.py 用到的子集（session / handle / torrent_info / status / file_progress ...），
足以驱动 TorrentManager 的全部管理逻辑（增删改查、文件优先级、完成侧车）而无须真实内核。
"""

from __future__ import annotations


class FakeHash:
    def __init__(self, h: str) -> None:
        self._h = h

    def __str__(self) -> str:
        return self._h


class FakeErr:
    value = 0

    def __str__(self) -> str:
        return ""


class FakeStatus:
    def __init__(self, handle: "FakeHandle") -> None:
        self._h = handle

    @property
    def state(self):
        return self._h._state

    @property
    def total_wanted(self):
        return self._h._total

    @property
    def total_wanted_done(self):
        return self._h._done

    @property
    def download_rate(self):
        return self._h._dl

    @property
    def upload_rate(self):
        return self._h._ul

    @property
    def num_peers(self):
        return self._h._peers

    @property
    def num_seeds(self):
        return self._h._seeds

    @property
    def paused(self):
        return self._h._paused

    @property
    def save_path(self):
        return self._h._save_path

    @property
    def error(self):
        return FakeErr()

    @property
    def is_finished(self):
        return self._h._total > 0 and self._h._done >= self._h._total

    def eta(self):
        return self._h._eta


class FakeTI:
    def __init__(self, name: str, files: list[tuple[str, int]]) -> None:
        self._name = name
        self._files = files  # list of (relpath, size)

    def name(self) -> str:
        return self._name

    def num_files(self) -> int:
        return len(self._files)

    def files(self) -> "FakeTI":
        return self

    def file_path(self, i: int) -> str:
        return self._files[i][0]

    def file_name(self, i: int) -> str:
        return self._files[i][0].split("/")[-1]

    def file_size(self, i: int) -> int:
        return self._files[i][1]


class FakeHandle:
    def __init__(self, ti: FakeTI, save_path: str) -> None:
        self._ti = ti
        self._save_path = save_path
        self._state = 2  # downloading
        self._total = 1000
        self._done = 500
        self._dl = 12345
        self._ul = 678
        self._peers = 5
        self._seeds = 2
        self._paused = False
        self._eta = 42
        n = ti.num_files()
        self._prio = [4] * n
        self._fp = [f[1] for f in ti._files]

    def status(self) -> FakeStatus:
        return FakeStatus(self)

    def has_metadata(self) -> bool:
        return True

    def torrent_file(self) -> FakeTI:
        return self._ti

    def file_progress(self) -> list[int]:
        return list(self._fp)

    def file_priority(self, i: int) -> int:
        return self._prio[i]

    def set_file_priority(self, i: int, p: int) -> None:
        self._prio[i] = p

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def info_hash(self) -> FakeHash:
        return FakeHash("a" * 40)


class FakeSession:
    def __init__(self) -> None:
        self._handles: list[FakeHandle] = []

    def listen_on(self, a, b) -> None:
        pass

    def apply_settings(self, d) -> None:
        pass

    def add_torrent(self, params: dict):
        ti = params.get("ti")
        if ti is None:
            ti = FakeTI("MagnetTorrent", [("movie.mkv", 2000), ("subtitle.srt", 100)])
        h = FakeHandle(ti, params["save_path"])
        self._handles.append(h)
        return h

    def post_torrent_updates(self) -> None:
        pass

    def pop_alerts(self) -> list:
        return []

    def pause(self) -> None:
        pass

    def remove_torrent(self, h: FakeHandle, opts: int = 0) -> None:
        if h in self._handles:
            self._handles.remove(h)


class FakeLT:
    storage_mode_t = type("S", (), {"storage_mode_sparse": 1})()
    options_t = type("O", (), {"delete_files": 1})()

    class torrent_status:
        class states:
            pass

    @staticmethod
    def session():
        return FakeSession()

    @staticmethod
    def torrent_info(d):
        if isinstance(d, FakeTI):
            return d
        return FakeTI("FromFile", [("clip.mkv", 100)])

    @staticmethod
    def bdecode(b):
        return {"info": {}}
