#!/usr/bin/env python3
"""代理 IP 爬取 + 测速筛选器（proxy_scout）

从公开免费代理源爬取代理，或读取你提供的代理列表，并发测试哪些能访问
腾讯视频（v.qq.com），输出按速度排序的可用代理，直接填进 app「高级选项 → 代理」即可。

⚠️ 诚实提示：免费代理对国内站（腾讯）大概率无效——免费池几乎全是海外 IP，
访问腾讯会被地域拒绝；少数国内 IP 是死代理或秒失效。本脚本真正的价值是
「批量测速筛选」机制：你有付费机场节点时，把节点列表丢进来筛最快的最实用。

用法：
    python3 proxy_scout.py                  # 爬免费源 + 测速筛选
    python3 proxy_scout.py --file p.txt     # 只测你提供的列表（每行 ip:port 或 http://ip:port）
    python3 proxy_scout.py --top 10         # 只输出最快的 10 个
    python3 proxy_scout.py --timeout 6      # 每个代理测试超时秒数（默认 6）
    python3 proxy_scout.py --workers 50     # 并发数（默认 50）

仅用标准库，无第三方依赖。
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

# 测试目标：腾讯视频首页（能访问它 ≈ 能访问腾讯；同时测下载速率）
TARGET_URL = "https://v.qq.com/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 公开免费代理源（文本：每行 ip:port；JSON：直接解析）
FREE_SOURCES = [
    "https://api.proxyscrape.com/v2/?request=getproxies&protocol=http&timeout=10000&country=all&ssl=all&anonymity=all",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://www.proxy-list.download/api/v1/get?type=http",
]


def _fetch_text(url: str, timeout: int = 15) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def crawl_free_proxies() -> list[str]:
    """从公开源爬取免费代理，去重后返回 ip:port 列表。"""
    proxies: list[str] = []
    for src in FREE_SOURCES:
        try:
            raw = _fetch_text(src)
        except Exception as e:
            print(f"  [warn] 源不可用 {src.split('/')[2]}: {e}", file=sys.stderr)
            continue
        count = 0
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                # JSON 源可能返回 {"ip": "...", "port": 80} 形式
                obj = json.loads(line)
                ip, port = obj.get("ip"), obj.get("port")
                if ip and port:
                    line = f"{ip}:{port}"
            except (json.JSONDecodeError, AttributeError):
                pass  # 非 JSON，直接当 ip:port 处理
            if ":" in line and not line.startswith(("#", "//")):
                proxies.append(line)
                count += 1
        print(f"  [ok] {src.split('/')[2]}: +{count} 条")
    # 去重保序
    seen: set[str] = set()
    uniq: list[str] = []
    for p in proxies:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def _read_file(path: str) -> list[str]:
    out: list[str] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            out.append(line)
    return out


def _normalize(proxy: str) -> str:
    """统一成 http://ip:port 形式。"""
    proxy = proxy.strip()
    if "://" not in proxy:
        proxy = "http://" + proxy
    return proxy


def test_proxy(proxy: str, window: float) -> dict | None:
    """通过指定代理访问腾讯并测速。返回 {proxy, speed_bps, speed_label}，失败返回 None。"""
    proxy = _normalize(proxy)
    try:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
    except Exception:
        return None
    req = urllib.request.Request(TARGET_URL, headers={"User-Agent": UA})
    total = [0]
    stop_at = time.time() + window

    try:
        with opener.open(req, timeout=8) as resp:
            # 只关心能拿到响应体；测窗口内下载字节
            while time.time() < stop_at:
                chunk = resp.read(8192)
                if not chunk:
                    break
                total[0] += len(chunk)
    except Exception:
        return None
    speed = total[0] / max(window, 0.5)
    if speed <= 0:
        return None
    return {"proxy": proxy, "speed_bps": round(speed), "speed_label": _fmt(speed)}


def _fmt(bps: float) -> str:
    v = float(bps)
    for unit in ("KB", "MB", "GB"):
        v /= 1024
        if v < 1024:
            return f"{v:.1f} {unit}/s"
    return f"{v:.1f} GB/s"


def main() -> None:
    ap = argparse.ArgumentParser(description="代理爬取 + 测速筛选器")
    ap.add_argument("--file", default="", help="代理列表文件（每行 ip:port）")
    ap.add_argument("--top", type=int, default=20, help="输出前 N 个最快代理")
    ap.add_argument("--timeout", type=float, default=6.0, help="每个代理测速窗口秒数")
    ap.add_argument("--workers", type=int, default=50, help="并发数")
    args = ap.parse_args()

    if args.file:
        proxies = _read_file(args.file)
        print(f"从文件读入 {len(proxies)} 条代理")
    else:
        print("正在爬取免费代理源…")
        proxies = crawl_free_proxies()
        print(f"共 {len(proxies)} 条去重代理")

    if not proxies:
        print("没有代理可测，退出", file=sys.stderr)
        sys.exit(1)

    print(f"\n开始并发测速（{args.workers} 并发 × {args.timeout}s/个），目标 {TARGET_URL} …\n")
    results: list[dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(test_proxy, p, args.timeout): p for p in proxies}
        for fut in as_completed(futs):
            done += 1
            r = fut.result()
            if r:
                results.append(r)
            if done % 100 == 0 or done == len(proxies):
                print(f"  进度 {done}/{len(proxies)}，可用 {len(results)}", flush=True)

    results.sort(key=lambda x: -x["speed_bps"])
    print("\n" + "=" * 60)
    print(f"✅ 可用代理 {len(results)} 个（按速度排序）\n")
    for i, r in enumerate(results[: args.top], 1):
        print(f"  {i:>2}. {r['proxy']:<28} {r['speed_label']}")
    if not results:
        print("  （无）— 免费代理池对腾讯基本全军覆没，符合预期。")
    print("\n" + "=" * 60)
    print("把上面任一代理填进 app「高级选项 → 代理」框即可，")
    print("例如：http://1.2.3.4:8080")
    print("提示：免费代理几乎都是海外 IP，访问腾讯大概率被拒；建议优先用付费国内中转节点。")


if __name__ == "__main__":
    main()
