"""集中设置测试前提环境（功能开关）。

问题根因
--------
`server/app.py` 是进程级单例：第一次 `import app` 时读取 `VDL_*` 环境变量，
决定各功能模块（订阅 / 媒体库 / 时效清理 / 归档 / 解说 / AI 去水印 / 桌面专属）
是否启用。

此前各测试文件在**各自的模块顶层**设置这些 env，但当多个测试文件一起运行时，
pytest 按文件名顺序收集并 import，第一个 import app 的文件（如按字母序靠前的
`test_web_contract.py`）会锁定 app 的初始状态；后续文件（如
`test_merged_modules_verify.py`）再设置 `VDL_SUBSCRIPTIONS_ENABLED=1` 等已经
来不及（app 单例早已初始化完），于是 subscriptions / retention 测试在批量运行时
偶发失败，但孤立运行却 PASS——典型的全局状态污染型 flaky。

修复
----
本 conftest 由 pytest 在**加载任何测试文件之前**导入，其模块顶层代码先于所有
`import app` 执行。在这里集中、确定性地打开全部测试需要的功能开关，app 首次
导入即处于预期状态，彻底消除顺序依赖。

各测试文件内原有的 env 设置保留为冗余兜底（无害）：conftest 已先设好，文件中
的赋值重复设相同值、setdefault 不覆盖，互不影响。
"""
import os

# ---- 公共前提：所有测试均运行在网页版 profile 下 ----
os.environ["VDL_PLATFORM"] = "web"

# ---- 合并系统核对（订阅监控 / 媒体库 / 时效清理 / AI 去水印）----
os.environ["VDL_SUBSCRIPTIONS_ENABLED"] = "1"
os.environ["VDL_LIBRARY_ENABLED"] = "1"
os.environ["VDL_RETENTION_ENABLED"] = "1"
os.environ["VDL_AI_DEWATERMARK_MODE"] = "http"
os.environ["VDL_AI_DEWATERMARK_ENDPOINT"] = "http://fake-worker.local"

# ---- 归档 / 解说路由测试 ----
os.environ["VDL_ARCHIVE_ENABLED"] = "1"
os.environ["VDL_COMMENTARY_ENABLED"] = "true"
os.environ["VDL_COMMENTARY_MODE"] = "http"
os.environ["VDL_COMMENTARY_ENDPOINT"] = "http://fake-worker.local"

# ---- 脚本式测试排除 ----
# test_crypto_routes / test_crypto_unit / test_torrent_routes 是独立脚本式测试
# （模块顶层 check() 失败时 raise SystemExit），设计上单文件运行
# （python3 tests/test_xxx.py）。pytest 收集它们会在导入期触发 SystemExit，
# 导致整个套件 INTERNALERROR。此处显式忽略，让 pytest 只收集 pytest 风格用例。
import pytest as _pytest

collect_ignore = [
    "test_crypto_routes.py",
    "test_crypto_unit.py",
    "test_torrent_routes.py",
]
