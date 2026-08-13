import json, sys, time, urllib.request

BASE = "http://localhost:8321"
URL = "https://v.qq.com/x/cover/mcv8hkc8zk8lnov/t0043wtgjyt.html"


def post(path, payload):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=15) as r:
        return json.load(r)


print("=== submit download (slow video, best) ===")
sub = post("/api/download", {"url": URL, "quality": "best"})
tid = sub["task_id"]
print("TID=", tid)

print("=== poll up to 60s for slow_warning ===")
triggered = False
for i in range(20):
    d = get(f"/api/tasks/{tid}")
    sw = d.get("slow_warning") or {}
    msg = (sw.get("message") or "")[:42]
    sug = sw.get("suggested_quality_keys")
    print(f"[{d['status']}] prog={d['progress']:.1f}% speed={d['speed']/1024:.1f}KB/s "
          f"warn={bool(sw)} msg={msg!r} sug={sug}")
    if sw:
        triggered = True
        print(">>> SLOW WARNING TRIGGERED")
        print("full slow_warning =", json.dumps(sw, ensure_ascii=False))
        break
    time.sleep(3)

print("=== cleanup ===")
try:
    urllib.request.urlopen(urllib.request.Request(BASE + f"/api/tasks/{tid}", method="DELETE"), timeout=10)
except Exception as e:
    print("cancel err:", e)
print("done, triggered=", triggered)
