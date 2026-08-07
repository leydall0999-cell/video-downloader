import os, sys, time, pathlib, json, shutil, tempfile
from pathlib import Path
SERVER = str(pathlib.Path(__file__).resolve().parent.parent / 'server')
sys.path.insert(0, SERVER)
import archive as A
import library as L

DAY = 86400
now = time.time()
ok = []
def check(label, cond):
    ok.append(cond); print(f'  {"PASS" if cond else "FAIL"}  {label}')

root = Path(tempfile.mkdtemp())
cfgdir = Path(tempfile.mkdtemp())

def mk(rel, size=1024, age_days=1.0, meta=None):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b'x' * size)
    t = now - age_days * DAY
    os.utime(p, (t, t))
    if meta:
        p.with_name(p.stem + '.vdlmeta.json').write_text(json.dumps(meta, ensure_ascii=False), encoding='utf-8')
    return p

mk('老片.mp4', 4000, 10, {'title': '老片子', 'platform': 'youtube', 'uploader': '张三'})
mk('新片.mp4', 3000, 0.0001)        # 刚写完 -> min_age 拦住
mk('音乐.mp3', 2000, 5, {'title': '歌', 'platform': 'bilibili', 'uploader': '李四'})
mk('封面.jpg', 300, 5)              # image 默认不传
mk('巨物.mp4', 5000, 5)             # 用 max_file_gb 极小值时被拦

print('--- 1. 路径模板渲染 ---')
item = {'name': 'a b/c:d.mp4', 'title': 'Ti/tle', 'ext': 'mp4', 'platform': 'youtube',
        'uploader': '张三', 'kind': 'video', 'mtime': int(now)}
d = A.render_dest('VDL/{platform}/{uploader}/{filename}', item)
print('   ', d)
check('非法字符被替换', ':' not in d and '\\' not in d)
check('平台/作者分层正确', d.startswith('VDL/youtube/张三/'))
d2 = A.render_dest('VDL/{platform}/{date}', item)   # 模板没带文件名
print('   ', d2)
check('模板缺文件名时自动补', d2.endswith('a b_c_d.mp4'))
d3 = A.render_dest('../../etc/{filename}', item)
print('   ', d3)
check('.. 穿越被清洗', '..' not in d3.split('/'))
d4 = A.render_dest('{platform}/{title}', {'name': 'x.mp4', 'title': '', 'platform': '', 'mtime': int(now)})
print('   ', d4)
check('空字段有兜底', '未知平台' in d4)

print('--- 2. 待归档筛选 ---')
store = A.ArchiveStore(cfgdir / 'archive.json')
cfg = A.ArchiveConfig()
items = L.scan_library(root)
print('    媒体库共', len(items), '项')
pend = A.pending_items(items, cfg, store)
names = sorted(p['name'] for p in pend)
print('    待归档:', names)
check('图片默认不归档', '封面.jpg' not in names)
check('刚写完的文件被静置拦住', '新片.mp4' not in names)
check('老视频+音频入选', set(names) == {'老片.mp4', '巨物.mp4', '音乐.mp3'})
check('旧的排前面', pend[0]['name'] == '老片.mp4')
check('带上目标路径', pend[0]['dest'].startswith('VideoDownloader/youtube/'))

cfg_img = A.ArchiveConfig(include_image=True)
check('开图片后封面入选', any(p['name'] == '封面.jpg' for p in A.pending_items(items, cfg_img, store)))
cfg_small = A.ArchiveConfig(max_file_gb=4500 / 1024**3)   # ~4500B
check('超限文件被跳过', not any(p['name'] == '巨物.mp4' for p in A.pending_items(items, cfg_small, store)))
cfg_noage = A.ArchiveConfig(min_age_minutes=0)
check('静置期设0后新片入选', any(p['name'] == '新片.mp4' for p in A.pending_items(items, cfg_noage, store)))

print('--- 3. 去重记录 ---')
p0 = pend[0]
store.record(p0['fp'], p0['rel'], 'remote/x.mp4', 'webdav', p0['size'])
pend2 = A.pending_items(items, cfg, store)
check('已归档不再重复', not any(p['name'] == '老片.mp4' for p in pend2))
# 文件变了 -> 指纹变 -> 重新归档
f = root / '老片.mp4'
f.write_bytes(b'y' * 9999)
os.utime(f, (now - 10 * DAY, now - 10 * DAY))
items3 = L.scan_library(root)
check('文件改动后重新入选', any(p['name'] == '老片.mp4' for p in A.pending_items(items3, cfg, store)))
check('forget 清记录', store.forget(p0['rel']) == 1)

print('--- 4. 凭据脱敏与留空沿用 ---')
store.set_creds('webdav', {'url': 'https://dav.example.com/x', 'user': 'bob', 'pass': 'SuperSecret123'})
m = store.creds_masked()
print('   ', m['webdav'])
check('密码不回显明文', 'SuperSecret123' not in json.dumps(m))
check('标记已设置', m['webdav']['pass_set'] is True)
store.set_creds('webdav', {'url': 'https://dav.example.com/y', 'user': 'bob', 'pass': ''})
check('留空沿用旧密码', store.get_creds('webdav')['pass'] == 'SuperSecret123')
check('URL 被更新', store.get_creds('webdav')['url'].endswith('/y'))
check('has_creds webdav', store.has_creds('webdav') is True)
check('has_creds baidu 未配', store.has_creds('baidu') is False)
mode = oct(os.stat(cfgdir / 'archive.json').st_mode)[-3:]
print('    文件权限:', mode)
check('配置文件 0600', mode == '600')

print('--- 5. 配置持久化 + 损坏降级 ---')
store.update(auto_enabled=True, dest_template='X/{filename}', interval_hours=2)
s2 = A.ArchiveStore(cfgdir / 'archive.json')
check('配置读回', s2.get().auto_enabled is True and s2.get().dest_template == 'X/{filename}')
check('凭据读回', s2.get_creds('webdav')['pass'] == 'SuperSecret123')
(cfgdir / 'archive.json').write_text('{{{broken')
s3 = A.ArchiveStore(cfgdir / 'archive.json')
check('损坏降级为默认', s3.get().auto_enabled is False and s3.get().dest_template == A.DEFAULT_TEMPLATE)

print('--- 6. 执行：正常上传 ---')
st = A.ArchiveStore(Path(tempfile.mkdtemp()) / 'a.json')
uploaded = []
def fake_up(path, dest, creds, progress=None):
    uploaded.append((path.name, dest))
    if progress:
        progress(len(path.read_bytes()) // 2, len(path.read_bytes()))
        progress(len(path.read_bytes()), len(path.read_bytes()))
    return dest
items6 = L.scan_library(root)
pend6 = A.pending_items(items6, cfg, st)
prog = []
res = A.run_archive(root, pend6, cfg, st, uploader=fake_up, creds={}, on_progress=lambda p: prog.append(p))
print('   ', {k: res[k] for k in ('uploaded', 'failed', 'skipped', 'deleted')}, res['bytes_text'])
check('全部上传成功', res['uploaded'] == len(pend6) and res['failed'] == 0)
check('本地文件没被删（delete_after 默认关）', (root / '音乐.mp3').exists())
check('有进度回调', len(prog) > 0 and prog[-1]['total'] == len(pend6))
check('去重记录已写入', not A.pending_items(L.scan_library(root), cfg, st))

print('--- 7. 单条失败不中断整批 ---')
st7 = A.ArchiveStore(Path(tempfile.mkdtemp()) / 'a.json')
calls = []
def flaky(path, dest, creds, progress=None):
    calls.append(path.name)
    if path.name == '音乐.mp3':
        raise A_err
    return dest
class _E(Exception):
    def __init__(self): super().__init__('网盘 500'); self.message='网盘 500'; self.hint='稍后重试'
A_err = _E()
pend7 = A.pending_items(L.scan_library(root), cfg, st7)
res7 = A.run_archive(root, pend7, cfg, st7, uploader=flaky, creds={})
print('   ', res7['uploaded'], '成功 /', res7['failed'], '失败 |', res7['errors'])
check('失败1个但其余继续', res7['failed'] == 1 and res7['uploaded'] == len(pend7) - 1)
check('尝试了全部文件', len(calls) == len(pend7))
check('错误带中文提示', any('网盘 500' in e for e in res7['errors']))
check('失败的没写去重记录', any(p['name'] == '音乐.mp3' for p in A.pending_items(L.scan_library(root), cfg, st7)))

print('--- 8. 归档后删本地：回收站不可用 -> 只传不删 ---')
st8 = A.ArchiveStore(Path(tempfile.mkdtemp()) / 'a.json')
cfg8 = A.ArchiveConfig(delete_after=True)
pend8 = A.pending_items(L.scan_library(root), cfg8, st8)
res8 = A.run_archive(root, pend8, cfg8, st8, uploader=fake_up, creds={},
                     trash=lambda p: False, trash_ok=lambda: False)
print('   ', res8['uploaded'], '上传 /', res8['deleted'], '删除 |', res8['errors'])
check('上传成功', res8['uploaded'] == len(pend8))
check('一个都没删', res8['deleted'] == 0)
check('本地文件都在', (root / '音乐.mp3').exists() and (root / '老片.mp4').exists())
check('有明确提示', any('回收站' in e for e in res8['errors']))

print('--- 9. 归档后删本地：回收站可用 -> 本体+侧车一起走 ---')
st9 = A.ArchiveStore(Path(tempfile.mkdtemp()) / 'a.json')
trashed = []
def fake_trash(p):
    trashed.append(Path(p).name); Path(p).unlink(); return True
pend9 = A.pending_items(L.scan_library(root), cfg8, st9)
res9 = A.run_archive(root, pend9, cfg8, st9, uploader=fake_up, creds={},
                     trash=fake_trash, trash_ok=lambda: True)
print('    进回收站:', sorted(trashed))
check('删除计数正确', res9['deleted'] == len(pend9))
check('侧车一起走', '音乐.vdlmeta.json' in trashed)
check('本地确实没了', not (root / '音乐.mp3').exists())

print('--- 10. 穿越防护：目标指向下载目录外 ---')
outside_dir = Path(tempfile.mkdtemp())
victim = outside_dir / 'victim.mp4'
victim.write_bytes(b'z' * 100)
st10 = A.ArchiveStore(Path(tempfile.mkdtemp()) / 'a.json')
evil = [{'id': 'x', 'rel': '../' * 8 + str(victim).lstrip('/'), 'name': 'victim.mp4',
         'size': 100, 'mtime': int(now), 'fp': 'fake', 'dest': 'v.mp4'}]
res10 = A.run_archive(root, evil, cfg, st10, uploader=fake_up, creds={})
print('   ', res10['skipped'], '跳过 |', res10['errors'])
check('目录外文件未被上传', res10['uploaded'] == 0 and res10['skipped'] == 1)

print('--- 11. 取消 ---')
st11 = A.ArchiveStore(Path(tempfile.mkdtemp()) / 'a.json')
mk('c1.mp4', 100, 5); mk('c2.mp4', 100, 5); mk('c3.mp4', 100, 5)
pend11 = A.pending_items(L.scan_library(root), cfg, st11)
cnt = {'n': 0}
def stop_after_1():
    cnt['n'] += 1
    return cnt['n'] > 1
res11 = A.run_archive(root, pend11, cfg, st11, uploader=fake_up, creds={}, should_stop=stop_after_1)
print('   ', res11['uploaded'], '上传 /', res11['skipped'], '跳过')
check('取消后停止', res11['uploaded'] == 1 and res11['skipped'] == len(pend11) - 1)

print('--- 12. human_size ---')
check('human_size', A.human_size(0) == '0 B' and A.human_size(1536) == '1.5 KB')

for d in (root, cfgdir, outside_dir):
    shutil.rmtree(d, ignore_errors=True)
print('\n' + ('ALL_PASS' if all(ok) else f'SOME_FAIL ({ok.count(False)} 项失败)'))
