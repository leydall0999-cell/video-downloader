#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给香港节点（/opt/vdl，更老分支）定点加 YouTube 解析提速参数 player_skip=configs。

为什么补：
  香港节点是网页版解析海外站（YouTube 等）的实际出口。yt-dlp 默认会为每个
  非 webpage 的备用客户端**额外下载一次 ytcfg**（player_skip 语义见
  yt_dlp/extractor/youtube/_video.py:3054）。解析阶段我们并不需要这些备用
  客户端的配置，跳过可省一次请求。

  实测（2026-09-27，香港节点 /opt/vdl/.venv，yt-dlp 2026.8.19 + web_safari）：
    格式数 11→11、协议分布 mhtml×4 + m3u8_native×6 + https×1 不变
    耗时 5.63→4.52s / 4.83→4.49s
  ⇒ 少一次请求且不裁掉 HLS/DASH（skip 类参数最容易误伤格式列表，必须实测而非照抄）。

设计约束（老分支，风险压到最低）：
  1. 只改 _base_options() 里 YouTube 那一行，不动别处；
  2. 保留 player_client=web_safari（与 bgutil PO Token 同 client，改了会 403）；
  3. 带 VDL_YT_PLAYER_SKIP=0 回滚开关，线上可关不用发版；
  4. 幂等：已含 player_skip 则跳过；锚点命中数必须恰好 1，否则拒绝写入；
  5. 写前备份，写后 py_compile 自检。
"""
import io
import os
import shutil
import sys
import time


# 目标文件可传参：香港节点 /opt/vdl/server/downloader.py（默认），
# ECS 主站 /opt/vdl-worker/server/downloader.py。两地同构、同一份补丁。
DL = sys.argv[1] if len(sys.argv) > 1 else '/opt/vdl/server/downloader.py'
BACKUP_DIR = os.path.join(os.path.dirname(os.path.dirname(DL)), 'backup')

ANCHOR = (
    '        options.setdefault("extractor_args", {}).setdefault("youtube", {})'
    '["player_client"] = ["web_safari"]'
)

NEW = '''        _yt_args = options.setdefault("extractor_args", {}).setdefault("youtube", {})
        _yt_args["player_client"] = ["web_safari"]
        # 提速：player_skip=configs 跳过「为每个非 webpage 客户端额外下载一次 ytcfg」的请求。
        # 2026-09-27 本节点实测（yt-dlp 2026.8.19 + web_safari，同一视频跑两遍）：
        #   解析耗时 5.63s→4.52s、4.83s→4.49s
        #   格式数 11→11、协议分布 mhtml×4 + m3u8_native×6 + https×1 完全不变
        #   ⇒ 少一次请求，且不裁掉 HLS/DASH。
        # 紧急回滚开关：VDL_YT_PLAYER_SKIP=0（不需要重新发版）。
        if os.environ.get("VDL_YT_PLAYER_SKIP", "1").strip().lower() not in ("0", "false", "no", "off"):
            _yt_args["player_skip"] = ["configs"]
'''


def backup(path):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    dst = os.path.join(BACKUP_DIR, os.path.basename(path) + '.pre-player-skip.' + time.strftime('%Y%m%d-%H%M%S'))
    shutil.copy2(path, dst)
    print('  备份 →', dst)


def patch():
    src = io.open(DL, encoding='utf-8').read()
    if '"player_skip"' in src:
        print('[downloader.py] 已含 player_skip，跳过（幂等）')
        return False
    n = src.count(ANCHOR)
    if n != 1:
        raise SystemExit(f'[downloader.py] 锚点命中 {n} 次（期望 1），拒绝写入')
    backup(DL)
    src = src.replace(ANCHOR, NEW)
    io.open(DL, 'w', encoding='utf-8').write(src)
    print('[downloader.py] 已加 player_skip=configs（YouTube 解析提速）')
    return True


def main():
    print('== 香港节点 YouTube player_skip=configs 定点补丁 ==')
    before = len(io.open(DL, encoding='utf-8').read().splitlines())
    changed = patch()
    after = len(io.open(DL, encoding='utf-8').read().splitlines())
    print(f'downloader.py 行数 {before} → {after} (+{after - before})')
    if changed:
        print('== 语法自检 ==')
        import py_compile
        try:
            py_compile.compile(DL, doraise=True)
            print('  OK', DL)
        except Exception as exc:
            raise SystemExit(f'  语法错误 {DL}: {exc}')
        print('  下一步：systemctl restart vdl-web.service')
    print('== 完成 ==')


if __name__ == '__main__':
    main()
