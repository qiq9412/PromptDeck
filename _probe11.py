# -*- coding: utf-8 -*-
"""定点探测：在反代页面上复现 verify_ui.py [11] 那一步，把原始返回值打出来。"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import verify_ui as V  # noqa: E402

APP = V.APP
PORT = 9347
EDGE = V.EDGE

tmp = tempfile.mkdtemp(prefix="pd_probe11")
proc = subprocess.Popen([
    EDGE, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
    "--remote-debugging-port=%d" % PORT, "--user-data-dir=" + tmp,
    "--window-size=1600,1000", "about:blank",
], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

try:
    ws = None
    for _ in range(60):
        time.sleep(0.5)
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/json/version" % PORT, timeout=3) as r:
                ws = json.loads(r.read().decode())["webSocketDebuggerUrl"]
            break
        except Exception:
            pass
    c = V.CDP(ws)
    tid = c.send("Target.createTarget", {"url": "about:blank"}, session=False)["targetId"]
    c.sid = c.send("Target.attachToTarget", {"targetId": tid, "flatten": True},
                   session=False)["sessionId"]
    c.send("Runtime.enable")
    c.send("Page.enable")

    print("APP =", APP)
    c.go(APP + "prompt.html", wait=5)

    print("snapshot ->", c.ev(V.JS_PB_SNAPSHOT))
    print("pick server 硅基流动 ->", repr(c.ev(V.JS_PB_PICK_SERVER % json.dumps("硅基流动"))))
    print("pick type 对话     ->", repr(c.ev(V.JS_PB_PICK_TYPE % json.dumps("对话"))))

    # 菜单里到底有哪些 combo-item、data-value 长什么样
    print("menuModel items ->", c.ev("""JSON.stringify(Array.prototype.map.call(
        document.querySelectorAll('#menuModel .combo-item'), function(e){
          return {v: e.getAttribute('data-value'), disp: e.style.display};
        }))"""))

    name = "zai-org/GLM-5.3"
    print("querySelector 命中 ->", c.ev(
        "String(!!document.querySelector('#menuModel .combo-item[data-value=' + "
        + json.dumps(name) + " + ']'))"))
    raw = c.ev(V.JS_PB_PICK_MODEL % json.dumps(name))
    print("PICK_MODEL raw ->", repr(raw))
finally:
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
