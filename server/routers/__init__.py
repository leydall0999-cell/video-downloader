"""server/routers — Phase 1 按域抽取的功能路由包。

各模块由 server/app.py 抽取，保持原 handler 逻辑不变；通过 `app.<name>` 访问共享内核。
所有 profile 均挂载，网页版行为零变化。app 端新功能只改本目录下的对应文件。
"""
