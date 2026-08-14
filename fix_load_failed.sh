#!/bin/bash
# 修复 VideoDownloader 在「手机热点 / 无 WPAD 的网络」下出现的 "Load failed"
#
# 根因：
#   macOS 系统代理开启了「代理自动发现 (WPAD)」，会去拉取 http://wpad/wpad.dat 这个 PAC。
#   在公司 WiFi 上 wpad 可达，pywebview 的 WKWebView 能正常加载 http://127.0.0.1:<port>；
#   切到手机热点后 wpad 不可达（502），WKWebView 无法解析 PAC，主框架加载失败 → 报 "Load failed"。
#   后端 FastAPI 本身完全正常（curl 127.0.0.1:8321 返回 200）。
#
# 修复：关闭「代理自动发现」并清空残留的 PAC URL，让 WKWebView 直连 localhost。
# 注意：本脚本需要 sudo（改的是系统网络配置，可随时恢复）。
set -e

IFACE=$(route -n get default 2>/dev/null | awk '/interface:/{print $2}')
# 默认路由接口通常就是当前上网接口；映射成 networksetup 的服务名
SERVICE="Wi-Fi"
if [ "$IFACE" = "en4" ] || [ "$IFACE" = "en5" ]; then
  SERVICE="iPhone USB"   # 若用 USB 共享热点，服务名不同
fi

echo "活跃接口: $IFACE  ->  网络服务: $SERVICE"
echo "[1/2] 关闭「代理自动发现 (Proxy Auto Discovery)」..."
sudo networksetup -setproxyautodiscovery "$SERVICE" off
echo "[2/2] 清空残留的「自动代理配置 (PAC)」URL..."
sudo networksetup -setautoproxyurl "$SERVICE" "" 2>/dev/null || true

echo
echo "--- 修复后系统代理状态 ---"
scutil --proxy
echo
echo "完成。请彻底退出 VideoDownloader.app 后重新打开验证。"
echo "若仍出现 Load failed，请到：系统设置 → 网络 → $SERVICE → 详细信息 → 代理"
echo "手动取消勾选「自动代理发现」「自动代理配置」，再重启 App。"
echo "(恢复方法：sudo networksetup -setproxyautodiscovery \"$SERVICE\" on)"
