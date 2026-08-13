"""解说视频一键工作台

把视频丢进 input/ 后, 这里一条命令跑完整流程:
    转写 -> (自动解说词草稿 / 交给 WorkBuddy 精修) -> 带字幕旁白的成片

用法(都在 commentary-pipeline/ 目录下运行, 用 managed python):
----------------------------------------------------------------
# 1) 全自动(解说词由 LLM 生成, 需配置 LLM_API_KEY, 适合快速出片看效果)
python process.py input/素材.mp4 --auto

# 2) 只转写, 把 transcript 交给 WorkBuddy 写爆款解说词, 然后精修出片
python process.py input/素材.mp4                 # 只转写, 停下来等你给解说词
python process.py input/素材.mp4 --edit-only work/素材.script.json   # 用精修稿出片

# 可选参数
--voice zh-CN-YunxiNeural    指定 AI 旁白音色(男声沉稳/女声温柔)
--vertical                   输出竖屏 9:16(抖音/视频号)
--original-speed             画面原速(不快进): 窗口跟随旁白时长, 不拉伸画面
--own-voice                  用 work/<i>.mp3(你自己录的)替换 AI 旁白
--moviepy                    回退到 moviepy 渲染(更慢, 仅兼容旧路径); 默认已用 ffmpeg 原生渲染(快约16倍)

# 剪辑选项（与 llm_script / edit_ffmpeg 共用 commentary_options 模型）
--commentary-type            解说类型: deep_hl(高光深度) / normal_hl(高光普通) /
                            full_normal(全片普通) / full_deep(全片深入)；默认 deep_hl
--highlight-source           ai(AI 自动挑高光) / manual(人工在审核面板挑)；默认 ai
--intro-highlight            片头插入最精彩片段当钩子
--skip-intro-outro           跳过片头片尾(各砍约 8%，上限 90s)
--retain-pct 50              保留全片时长百分比(10~100，不填=不裁剪)
--web                        联网搜索资料辅助发挥(任何解说类型都可开)
--one-click                  一键生成: 全片深入解说 + AI 联网 + 片头插精彩片段
（单行字幕/同行显示/只羽化原字幕/弱化原声 属默认铁律，渲染层强制生效）
----------------------------------------------------------------

依赖(managed python venv):
/Users/suixindelang/.workbuddy/binaries/python/envs/default/bin/python
"""
import os
import sys
import json
import shutil
import argparse
import subprocess
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "scripts"))
from config import INPUT, OUTPUT, WORK, ensure_dirs
import transcribe
# 注意: edit.py 依赖 moviepy(重依赖, 桌面打包版已排除)，改为用到时再惰性导入。


# ---- 版本自报家门（供 video-downloader 自检/更新检查调用）----
__version__ = "1.1.0"


def _packaged_version():
    """打包分发版没有 .git，改读随包 VERSION 文件（构建脚本生成）。

    格式: 第一行 commit 短哈希，第二行分支名（缺失则为空）。
    """
    path = os.path.join(HERE, "VERSION")
    try:
        with open(path, encoding="utf-8") as f:
            lines = [ln.strip() for ln in f.read().splitlines() if ln.strip()]
    except OSError:
        return None, None
    commit = lines[0] if lines else None
    branch = lines[1] if len(lines) > 1 else None
    return commit, branch


def _git(*args):
    """在管线根目录执行 git 子命令，失败/无 git 时返回 None。

    用 shutil.which + 绝对路径兜底，避免管线依赖 import 改变 PATH 后找不到 git。
    """
    exe = shutil.which("git")
    if not exe:
        for cand in ("/usr/bin/git", "/usr/local/bin/git", "/opt/homebrew/bin/git"):
            if os.path.isfile(cand):
                exe = cand
                break
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "-C", HERE, *args],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return None

def _git_head():
    commit, _ = _packaged_version()
    if commit:
        return commit
    h = _git("rev-parse", "--short", "HEAD")
    return h or "unknown"

def _git_branch():
    _, branch = _packaged_version()
    if branch:
        return branch
    b = _git("rev-parse", "--abbrev-ref", "HEAD")
    return b or "unknown"

def self_version_json():
    """供 `--version` 与桌面版 /api/commentary/diagnostics 解析的版本信息。"""
    return json.dumps({
        "version": __version__,
        "commit": _git_head(),
        "branch": _git_branch(),
        "modes": ["deep_hl", "normal_hl", "full_normal", "full_deep"],
        "modes_since": {"deep_hl": "1.1.0",
                        "normal_hl": "1.1.0",
                        "full_normal": "1.1.0",
                        "full_deep": "1.1.0"},
    }, ensure_ascii=False)


def auto_script(transcript_path, script_path, voice=None, title="", mode=None,
                commentary_type=None, highlight_source=None, intro_highlight=None,
                web=None, retain_pct=None, skip_intro_outro=None, style="none"):
    """生成解说词：调用 LLM 生成（强依赖 LLM_API_KEY）。

    剪辑选项（与 llm_script / edit_ffmpeg 共用 commentary_options 模型）：
      commentary_type：deep_hl / normal_hl / full_normal / full_deep
      highlight_source：ai(AI 自动挑高光) / manual(人工在审核面板挑)
      intro_highlight：是否把最精彩一段剪到片头当钩子
      web：是否联网搜索资料辅助发挥
      retain_pct / skip_intro_outro：保留时长百分比 / 跳过片头片尾
      mode：旧版三选一字段，仅用于兼容老调用方（会被新选项覆盖）
    """
    if not (os.environ.get("LLM_APIKEY") or os.environ.get("LLM_API_KEY")):
        raise RuntimeError(
            "AI 解说依赖大模型，请先配置 LLM_API_KEY 后重试。")
    try:
        from llm_script import llm_script
        return llm_script(transcript_path, script_path, title=title,
                          voice=voice, mode=mode, commentary_type=commentary_type,
                          highlight_source=highlight_source,
                          intro_highlight=intro_highlight, web=web,
                          retain_pct=retain_pct, skip_intro_outro=skip_intro_outro,
                          style=style)
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"LLM 调用失败，解说词生成中止: {e}")


def _run_resume_loop(video_path, script_path, out_path, build_fn, **kwargs):
    """断点续作守护: 反复调用 build(), 直到成片生成。

    只有以下情况才真正停止(符合“人工删除才结束”):
      - 解说词脚本被手动删除
      - 工作目录被手动删除
      - 在工作目录放置 <视频id>.stop 标记文件
    其余任何失败/中断都会 8 秒后自动续作, 已完成的分段靠进度清单跳过, 不重做。
    """
    vid = os.path.splitext(os.path.basename(script_path))[0]
    if vid.endswith(".script"):
        vid = vid[:-len(".script")]
    stop_file = os.path.join(WORK, vid + ".stop")
    round_no = 0
    while True:
        round_no += 1
        if not os.path.exists(script_path):
            print(f"[中止] 解说词脚本已被删除({script_path}), 渲染结束")
            return False
        if not os.path.exists(os.path.dirname(os.path.abspath(script_path))):
            print("[中止] 工作目录已被删除, 渲染结束")
            return False
        if os.path.exists(stop_file):
            print(f"[中止] 检测到停止标记 {stop_file}, 渲染结束(删除该文件可恢复)")
            return False
        print(f"\n========== 渲染轮次 #{round_no} ({'断点续作' if round_no > 1 else '首次'}) ==========")
        try:
            ok = build_fn(video_path, script_path, out_path, **kwargs)
        except SystemExit:
            ok = False
        except Exception as e:
            print(f"[异常] 本轮渲染出错: {e}")
            ok = False
        if ok and os.path.exists(out_path):
            return True
        print("[续作] 本轮未全部完成, 8 秒后自动断点续作 (人工删除脚本或放置 .stop 文件可随时中止)")
        time.sleep(8)


def main():
    # 版本自报：绕过 video 位置必填，避免 `python process.py --version` 报缺参
    if "--version" in sys.argv[1:]:
        print(self_version_json())
        return
    ensure_dirs()
    ap = argparse.ArgumentParser(description="解说视频一键工作台")
    ap.add_argument("video", help="视频路径(相对/绝对)")
    ap.add_argument("--auto", action="store_true",
                    help="全自动: 转写 + 自动解说词草稿 + 出片")
    ap.add_argument("--mode", choices=["highlights", "highlights_intro", "full_web"],
                    default=None,
                    help="(旧版兼容) 三选一解说模式，会被下面的新选项覆盖；"
                         "新用法请用 --commentary-type")
    ap.add_argument("--commentary-type",
                    choices=["deep_hl", "normal_hl", "full_normal", "full_deep"],
                    default="deep_hl",
                    help="解说类型: deep_hl=高光处叠加深度解说; normal_hl=高光部分普通解说; "
                         "full_normal=全片普通解说; full_deep=全片深入解说")
    ap.add_argument("--highlight-source", choices=["ai", "manual"], default="ai",
                    help="高光来源: ai=AI 自动挑高光; manual=人工在审核面板挑")
    ap.add_argument("--intro-highlight", action="store_true",
                    help="片头插入最精彩片段当钩子")
    ap.add_argument("--skip-intro-outro", action="store_true",
                    help="跳过片头片尾(各砍约 8%%，上限 90s)")
    ap.add_argument("--retain-pct", type=float, default=None,
                    help="保留全片时长百分比(10~100，不填=不裁剪)")
    ap.add_argument("--web", action="store_true",
                    help="联网搜索资料辅助发挥(任何解说类型都可开)")
    ap.add_argument("--one-click", action="store_true",
                    help="一键生成: 全片深入解说 + AI 联网 + 片头插精彩片段"
                         "(单行字幕/同行显示/只羽化原字幕/弱化原声 为默认铁律)")
    ap.add_argument("--edit-only", metavar="SCRIPT",
                    help="跳过转写, 直接用指定 script.json 剪辑出片")
    ap.add_argument("--voice", default=None, help="AI 旁白音色, 如 zh-CN-YunxiNeural")
    ap.add_argument("--style", default="none",
                    choices=["none", "funny", "serious", "domineering", "angry",
                             "suspense", "healing", "sarcastic"],
                    help="解说口吻风格: none=默认; funny=搞笑; serious=严肃; domineering=霸道; "
                         "angry=愤青; suspense=悬疑; healing=治愈; sarcastic=毒舌")
    ap.add_argument("--vertical", action="store_true", help="输出竖屏 9:16")
    ap.add_argument("--original-speed", action="store_true",
                    help="画面原速(不快进): 窗口跟随旁白时长, 不拉伸画面")
    ap.add_argument("--own-voice", action="store_true",
                    help="用 work/<i>.mp3(自己录的)替换 AI 旁白")
    ap.add_argument("--moviepy", action="store_true",
                    help="回退到 moviepy 渲染(更慢, 仅兼容旧路径); 默认已用 ffmpeg 原生渲染(快约16倍)")
    ap.add_argument("--script-only", action="store_true",
                    help="只做转写+解说词生成，不渲染成片(方便审核/人工修改后再渲染)")
    ap.add_argument("--output", default=None,
                    help="自定义成片输出路径(默认按命名规则生成)")
    ap.add_argument("--loop", action="store_true",
                    help="断点续作守护模式: 反复渲染直到成片生成; 只有人工删除脚本/工作目录或放置 <视频id>.stop 才停止")
    args = ap.parse_args()

    # ---- 剪辑选项归一（one-click > commentary-type > 旧 mode > 默认）----
    if args.one_click:
        args.commentary_type = "full_deep"
        args.highlight_source = "ai"
        args.intro_highlight = True
        args.web = True
        args.skip_intro_outro = False
        args.retain_pct = None
    elif args.mode and args.commentary_type == "deep_hl":
        # 旧版 --mode 兜底（仅当用户没显式用新选项）
        _legacy_map = {"highlights": "deep_hl", "highlights_intro": "deep_hl",
                       "full_web": "full_deep", "full": "deep_hl"}
        args.commentary_type = _legacy_map.get(args.mode, "deep_hl")
        if args.mode == "highlights_intro":
            args.intro_highlight = True
        if args.mode == "full_web":
            args.web = True

    video_path = args.video
    if not os.path.isabs(video_path):
        video_path = os.path.join(HERE, video_path)
    if not os.path.exists(video_path):
        print(f"[错误] 找不到视频: {video_path}")
        sys.exit(1)

    name = os.path.splitext(os.path.basename(video_path))[0]
    transcript_path = os.path.join(WORK, name + ".transcript.json")
    audio_path = os.path.join(WORK, name + ".wav")

    # ---- 步骤1: 转写 ----
    # --script-only 未指定 --auto 或 --edit-only 时自动启用 --auto
    if args.script_only and not args.auto and not args.edit_only:
        args.auto = True
    if not args.edit_only:
        print("=== [1/2] 转写 ===")
        if os.path.exists(transcript_path) and os.path.getsize(transcript_path) > 0:
            print(f"  [缓存] 已存在转写，复用: {transcript_path}")
        else:
            # 音轨也做缓存：已抽取则跳过（长视频抽音频也耗时）
            if os.path.exists(audio_path) and os.path.getsize(audio_path) > 0:
                print(f"  [缓存] 已存在音轨，复用: {audio_path}")
            else:
                transcribe.extract_audio(video_path, audio_path)
            segs, info = transcribe.transcribe(audio_path)
            json.dump(segs, open(transcript_path, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=2)
            print(f"  转写完成: {len(segs)} 段 -> {transcript_path}")

    # ---- 步骤2: 解说词 ----
    if args.edit_only:
        script_path = args.edit_only
        if not os.path.exists(script_path):
            print(f"[错误] 找不到解说词: {script_path}")
            sys.exit(1)
        print(f"=== 使用指定解说词: {script_path} ===")
    elif args.auto:
        print("=== [2/2] 自动解说词（LLM）===")
        script_path = os.path.join(WORK, name + ".script.json")
        auto_script(transcript_path, script_path, voice=args.voice,
                    commentary_type=args.commentary_type,
                    highlight_source=args.highlight_source,
                    intro_highlight=args.intro_highlight, web=args.web,
                    retain_pct=args.retain_pct,
                    skip_intro_outro=args.skip_intro_outro,
                    style=args.style)
    else:
        print("\n✅ 转写完成。下一步二选一:")
        print(f"   A) 全自动草稿:  python process.py {args.video} --auto"
              + (" --vertical" if args.vertical else "")
              + (f" --voice {args.voice}" if args.voice else ""))
        print(f"   B) 精修解说词:  把 {transcript_path} 交给 WorkBuddy 写解说词,")
        print(f"                   保存为 work/{name}.script.json 后运行:")
        print(f"                   python process.py {args.video} --edit-only work/{name}.script.json")
        print("   (推荐 B: 机器草稿只是转写稿, 没有钩子/节奏; WorkBuddy 写的才有爆款感)")
        return

    # ---- 步骤3: 剪辑出片 ----
    if args.script_only:
        print(f"\n✅ 脚本已生成: {script_path}")
        print(f"   转写: {transcript_path}")
        print(f"   分镜数: {len(json.load(open(script_path, encoding='utf-8'))['segments'])} 段")
        return
    print("=== 剪辑成片 ===")
    use_ffmpeg = not args.moviepy  # 默认 ffmpeg 原生渲染(快约16倍); --moviepy 回退
    if args.vertical:
        suffix = "_竖屏成片.mp4"
    elif args.original_speed:
        suffix = "_原速成片.mp4"
    else:
        suffix = "_成片.mp4"
    out_path = args.output or os.path.join(OUTPUT, name + suffix)
    build_kwargs = dict(vertical=args.vertical, own_voice=args.own_voice,
                        voice_override=args.voice, original_speed=args.original_speed,
                        mode=args.mode, commentary_type=args.commentary_type,
                        highlight_source=args.highlight_source,
                        intro_highlight=args.intro_highlight,
                        skip_intro_outro=args.skip_intro_outro,
                        retain_pct=args.retain_pct)
    if use_ffmpeg:
        import edit_ffmpeg
        if args.loop:
            ok = _run_resume_loop(video_path, script_path, out_path,
                                  build_fn=edit_ffmpeg.build, **build_kwargs)
            if not ok:
                sys.exit(1)
        else:
            ok = edit_ffmpeg.build(video_path, script_path, out_path, **build_kwargs)
            if not ok:
                sys.exit(1)
    else:
        # moviepy 渲染是历史回退路径，依赖包体积巨大，桌面分发版已从打包中排除。
        try:
            import edit as _edit_moviepy
        except ImportError as exc:
            print("[错误] --moviepy 回退渲染需要 moviepy，但当前环境未安装。")
            print(f"       原因: {exc}")
            print("       解决: 去掉 --moviepy 用默认 ffmpeg 原生渲染（更快），")
            print("             或执行 pip install -r requirements-moviepy.txt 后重试。")
            sys.exit(2)
        # moviepy 旧渲染路径只认旧参数，过滤掉新选项以免 TypeError
        _moviepy_kwargs = {k: v for k, v in build_kwargs.items()
                          if k in ("vertical", "own_voice", "voice_override",
                                   "original_speed", "mode")}
        _edit_moviepy.build(video_path, script_path, out_path, **_moviepy_kwargs)
    print(f"\n🎬 全部完成! 成片在: {out_path}")


if __name__ == "__main__":
    main()
