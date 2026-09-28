# -*- coding: utf-8 -*-
"""Streamlit 版冒烟自测（装好 streamlit 后运行）。

做三件事：
  1. 在 streamlit 安装目录里核对「组件协议报文名」是否与我自研的桥一致；
  2. 用 st.testing.v1.AppTest 无头跑一遍 app.py，确认脚本无异常、列表分支真的读了库；
  3. 起一个 headless 服务，探 / 与 /_stcore/health，并尝试直接取组件静态文件。
结果写 _smoke.txt（UTF-8），避免依赖被吞掉的标准输出。
"""
import glob
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
PORT = 8577
out = []


def w(s=""):
    out.append(str(s))


# ---------------------------------------------------------------- 1. 协议核对
TOKENS = [
    "streamlit:componentReady",
    "streamlit:setComponentValue",
    "streamlit:setFrameHeight",
    "streamlit:render",
    "isStreamlitMessage",
    "apiVersion",
]

w("=" * 68)
w("[1] 核对自研组件桥的报文名是否与 streamlit 官方实现一致")
try:
    import streamlit
    w("    streamlit 版本: %s" % streamlit.__version__)
    root = os.path.dirname(streamlit.__file__)
    found = {t: [] for t in TOKENS}
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            if not fn.lower().endswith((".js", ".py", ".mjs", ".ts")):
                continue
            p = os.path.join(dirpath, fn)
            try:
                if os.path.getsize(p) > 6 * 1024 * 1024:
                    continue
                with open(p, "r", encoding="utf-8", errors="ignore") as fp:
                    blob = fp.read()
            except Exception:
                continue
            for t in TOKENS:
                if t in blob and len(found[t]) < 3:
                    found[t].append(os.path.relpath(p, root))
    for t in TOKENS:
        w("    %-30s -> %s" % (t, found[t] if found[t] else "!! 未找到"))
except Exception as exc:
    w("    !! 导入 streamlit 失败: %r" % exc)

# ---------------------------------------------------------------- 2. AppTest
w("")
w("=" * 68)
w("[2] AppTest 无头运行 app.py")
try:
    from streamlit.testing.v1 import AppTest
    import db as appdb

    at = AppTest.from_file(os.path.join(HERE, "app.py"), default_timeout=120)
    at.run()
    w("    首次运行异常数: %d" % len(at.exception))
    for e in at.exception:
        w("      !! %s" % e.value)
    w("    session_state.page = %r" % at.session_state.get("page"))
    w("    数据库 srv 行数 = %d" % appdb.count_srv())

    # 模拟组件回传 save 事件（组件值落在 session_state 的组件 key 上）
    at2 = AppTest.from_file(os.path.join(HERE, "app.py"), default_timeout=120)
    at2.session_state["pd_list"] = {"event": "save", "nonce": "smoke-1",
                                    "data": {"plat_name": "冒烟测试平台",
                                             "base_url": "https://smoke.test/v1",
                                             "comp_name": "冒烟测试公司",
                                             "home_url": "https://smoke.test"}}
    at2.run()
    w("    [save 事件] 异常数: %d" % len(at2.exception))
    for e in at2.exception:
        w("      !! %s" % e.value)
    w("    [save 事件] session_state.page = %r" % at2.session_state.get("page"))
    w("    [save 事件] srv 行数 = %d（应比上一步 +1）" % appdb.count_srv())

    # 落库结果核对 + 清理
    rows = [r for r in appdb.list_srv() if r["plat_name"] == "冒烟测试平台"]
    w("    [save 事件] 落库回读: %s" % rows)
    if rows:
        appdb.delete_srv([r["id"] for r in rows])
    w("    清理后 srv 行数 = %d" % appdb.count_srv())

    # 组件注册结果：注册名应为 "<模块名>.<name>"，并解析出真实目录
    import streamlit.runtime as st_runtime
    reg = st_runtime.Runtime.instance().component_registry
    w("    已注册组件: %s" % [c.name for c in reg.get_components()])
    for cname in ("app.pd_list", "app.pd_new"):
        p = reg.get_component_path(cname)
        idx = os.path.join(p, "index.html") if p else None
        w("    get_component_path(%-12s) -> %s    index.html 存在: %s"
          % (cname, p, bool(idx and os.path.isfile(idx))))
except Exception as exc:
    import traceback
    w("    !! AppTest 失败: %s" % exc)
    w(traceback.format_exc())

# ---------------------------------------------------------------- 3. 起服务
w("")
w("=" * 68)
w("[3] 启动 headless 服务并探活")
proc = subprocess.Popen(
    [sys.executable, "-m", "streamlit", "run", os.path.join(HERE, "app.py"),
     "--server.headless", "true", "--server.port", str(PORT),
     "--browser.gatherUsageStats", "false"],
    cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
)
base = "http://127.0.0.1:%d" % PORT
try:
    ok = False
    for _ in range(60):
        time.sleep(1)
        try:
            with urllib.request.urlopen(base + "/_stcore/health", timeout=3) as r:
                if r.status == 200:
                    w("    /_stcore/health -> %s %s" % (r.status, r.read().decode().strip()))
                    ok = True
                    break
        except Exception:
            pass
    if not ok:
        w("    !! 服务在 60s 内未就绪")

    w("    注：组件路由要等某个会话真正跑过脚本才会注册，无浏览器连接时下面预期 404")
    for path in ["/", "/component/app.pd_list/", "/component/app.pd_list/index.html",
                 "/component/app.pd_new/index.html"]:
        try:
            with urllib.request.urlopen(base + path, timeout=8) as r:
                body = r.read().decode("utf-8", "ignore")
                w("    GET %-34s -> %s  len=%d" % (path, r.status, len(body)))
                for mark in ['"streamlit:componentReady"', "window.PD", "onPDArgs"]:
                    if mark in body:
                        w("        含标记 %s" % mark)
        except urllib.error.HTTPError as e:
            w("    GET %-34s -> HTTP %s" % (path, e.code))
        except Exception as e:
            w("    GET %-34s -> !! %r" % (path, e))
finally:
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except Exception:
        proc.kill()

with open(os.path.join(HERE, "_smoke.txt"), "w", encoding="utf-8") as fp:
    fp.write("\n".join(out))
print("written: _smoke.txt")
