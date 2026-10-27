# -*- coding: utf-8 -*-
"""守卫：服务端日志必须落盘。

背景（2026-10-27）：网盘失败的用户可见文案已统一成一句话
（cloud_drives/baidu.py:USER_ERROR_TEXT），真实原因只剩 logger 一个出口。
而打包版是 console=False（runw.exe），desktop_launcher._ensure_stdio()
会把 sys.stdout/stderr 换成 devnull —— logging 的 StreamHandler 写进去等于丢弃。
所以 app.py 必须额外挂一个文件 handler，否则线上永远查不到根因。

这里只做静态守卫（不 import app，避免拉起整个应用），
真实落盘行为由部署后的冒烟验证（触发一次解析失败 → 看 logs/vdl.log）。
"""
from pathlib import Path

APP_PY = Path(__file__).resolve().parents[1] / "app.py"


def test_app_attaches_rotating_file_handler():
    src = APP_PY.read_text(encoding="utf-8")
    assert "def _attach_file_logging(" in src, "app.py 缺少文件日志挂载函数"
    assert "LOG_FILE = _attach_file_logging()" in src, "文件日志函数定义了但没调用"
    assert "RotatingFileHandler" in src, "文件日志必须是轮转的（否则无限膨胀）"
    # 兜底：日志初始化失败绝不能拖垮启动
    assert "except Exception" in src


def test_log_dir_is_user_writable_not_bundle_dir():
    """日志目录必须在用户可写位置（LOCALAPPDATA / home），不能写包内目录。

    包目录可能装在 Program Files（只读），写进去会静默失败。
    """
    src = APP_PY.read_text(encoding="utf-8")
    assert 'os.environ.get("LOCALAPPDATA")' in src
    assert '"VideoDownloader" / "logs"' in src
