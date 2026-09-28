# -*- coding: utf-8 -*-
"""校验「魔搭创空间（Docker 型）」部署产物 promptdeck_modelscope/ 是否真的可用。

做三件事：
  1. 按容器入口的方式把产物跑起来（python server.py --host 0.0.0.0 --port 7860；
     若 7860 已在运行则直接复用）；
  2. 起一个「子路径反代」代理：把 /studio/app/xxx 剥掉前缀后转发到 7860
     —— 模拟创空间可能以子路径暴露应用的情形，验证相对路径写法真的通；
  3. 逐项断言平台硬性要求与业务链路（读 / 写 / 删 都过一遍）。

用法：python verify_modelscope.py
结果写 _ms_check.txt；退出码 0 = 全部通过。
"""
import http.client
import http.server
import io
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.join(os.path.dirname(HERE), "promptdeck_modelscope")
OUT = os.path.join(HERE, "_ms_check.txt")
APP_PORT = 7860
PROXY_PORT = 8799
PREFIX = "/studio/app"          # 模拟平台前缀
PROXY_BASE = "http://127.0.0.1:%d%s" % (PROXY_PORT, PREFIX)

buf = io.StringIO()
FAILS = []


def w(s=""):
    buf.write(str(s) + "\n")


def chk(label, ok, detail=""):
    w("  [%s] %s%s" % ("PASS" if ok else "FAIL", label, ("  " + str(detail)) if detail else ""))
    if not ok:
        FAILS.append(label)


def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def strip_comments(text):
    """去掉整行注释与行尾注释，用于「是否真的用了 8080」这类判断（注释里提到不算）。"""
    out = []
    for line in text.splitlines():
        s = line.split("#", 1)[0]
        out.append(s)
    return "\n".join(out)


def cleanup_test_rows():
    """清掉本脚本可能留下的测试数据（删 srv 会级联删掉它的 mdl）。"""
    base = "http://127.0.0.1:%d" % APP_PORT
    try:
        _, _, body = req("GET", base + "/api/srv")
        ids = [r["id"] for r in json.loads(body)
               if str(r.get("platform", "")).startswith("MS校验")]
        if ids:
            req("DELETE", base + "/api/srv", {"ids": ids})
        _, _, body = req("GET", base + "/api/mdl")
        ids = [r["id"] for r in json.loads(body)
               if str(r.get("model", "")).startswith("MS校验")]
        if ids:
            req("DELETE", base + "/api/mdl", {"ids": ids})
    except Exception:
        pass


# ------------------------------------------------------------------ 子路径反代
class PrefixProxy(http.server.BaseHTTPRequestHandler):
    """把 {PREFIX}/xxx 原样转发到后端 /xxx（剥掉前缀），保留状态码与 Location。"""
    protocol_version = "HTTP/1.1"

    def _fwd(self, method):
        if not self.path.startswith(PREFIX):
            self._send(404, b"prefix mismatch", "text/plain; charset=utf-8")
            return
        tail = self.path[len(PREFIX):] or "/"
        if not tail.startswith("/"):
            tail = "/" + tail
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else None
        hdrs = {}
        if body is not None:
            hdrs["Content-Type"] = self.headers.get("Content-Type") or "application/json"
        try:
            conn = http.client.HTTPConnection("127.0.0.1", APP_PORT, timeout=30)
            conn.request(method, tail, body=body, headers=hdrs)
            resp = conn.getresponse()
            data = resp.read()
            self.send_response(resp.status)
            self.send_header("Content-Type", resp.getheader("Content-Type") or "application/octet-stream")
            loc = resp.getheader("Location")
            if loc:
                self.send_header("Location", loc)     # 不重写：应用给的就是相对路径
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            conn.close()
        except Exception as exc:
            self._send(502, ("proxy error: %r" % (exc,)).encode(), "text/plain")

    def _send(self, code, data, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._fwd("GET")

    def do_POST(self):
        self._fwd("POST")

    # 列表页的「修改」走 PUT：代理漏了它就会回 501 Unsupported method，
    # 表现为「页面上改动看起来生效了、其实没落库」（前端先改内存数组再 render）。
    def do_PUT(self):
        self._fwd("PUT")

    def do_PATCH(self):
        self._fwd("PATCH")

    def do_DELETE(self):
        self._fwd("DELETE")

    def log_message(self, *a):
        pass


def req(method, url, obj=None, follow=True):
    """返回 (status, location, text)。默认跟随重定向，follow=False 时不跟随。"""
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8") if obj is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        r.add_header("Content-Type", "application/json")
    if follow:
        op = urllib.request.build_opener()
    else:
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        op = urllib.request.build_opener(NoRedirect)
    try:
        with op.open(r, timeout=20) as resp:
            return resp.status, resp.headers.get("Location"), resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Location"), e.read().decode("utf-8", "ignore")


def wait_health(url, seconds=40):
    for _ in range(seconds * 2):
        try:
            s, _, b = req("GET", url + "/api/health")
            if s == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    # 只起反代、常驻前台：供真实浏览器端到端（verify_ui.py）经前缀跑时使用，
    # 例如：python verify_modelscope.py --proxy-foreground
    if "--proxy-foreground" in sys.argv:
        print("prefix proxy: %s/... -> http://127.0.0.1:%d/  (Ctrl+C 结束)"
              % (PROXY_BASE, APP_PORT))
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", PROXY_PORT), PrefixProxy)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        return 0

    if not os.path.isdir(PKG):
        w("!! 找不到部署产物目录：%s（先跑 python pack_modelscope.py）" % PKG)
        return 1

    proc = None
    proxy = None
    try:
        # ---------------- 0. 静态检查：平台要求 ----------------
        w("=" * 74)
        w("[0] 平台硬性要求（静态检查）")
        dockerfile = read(os.path.join(PKG, "Dockerfile"))
        startsh = read(os.path.join(PKG, "start.sh"))
        msjson = json.loads(read(os.path.join(PKG, "ms_deploy.json")))
        deps = read(os.path.join(PKG, "requirements.txt"))
        serverpy = read(os.path.join(PKG, "server.py"))

        chk("Dockerfile 在项目根目录", os.path.isfile(os.path.join(PKG, "Dockerfile")))
        chk("ms_deploy.json：sdk_type=docker", msjson.get("sdk_type") == "docker", msjson.get("sdk_type"))
        chk("ms_deploy.json：port=7860", msjson.get("port") == 7860, msjson.get("port"))
        chk("启动命令监听 0.0.0.0", "--host 0.0.0.0" in startsh and "0.0.0.0" in startsh)
        chk("启动端口为 7860", "7860" in startsh and "7860" in dockerfile)
        chk("未使用平台禁用的 8080 端口",
            "8080" not in strip_comments(dockerfile)
            and "8080" not in strip_comments(startsh)
            and "8080" not in strip_comments(serverpy))
        chk("EXPOSE 7860", "EXPOSE 7860" in dockerfile)
        chk("未读取平台保留请求头（入站 Authorization / X-modelscope- / X-studio-）",
            'headers.get("Authorization")' not in serverpy
            and "X-modelscope" not in serverpy and "X-studio" not in serverpy)
        chk("依赖仅运行必需（无 streamlit / 无 websocket-client）",
            "streamlit" not in deps and "websocket-client" not in deps)
        chk("持久化：容器内库路径指向 /mnt/workspace",
            "PROMPTDECK_DB" in startsh and "/mnt/workspace" in startsh)
        chk("容器内库路径可用环境变量覆盖", "PROMPTDECK_DB" in read(os.path.join(PKG, "db.py")))

        pages = [f for f in sorted(os.listdir(os.path.join(PKG, "static"))) if f.endswith(".html")]
        n_fetch = sum(read(os.path.join(PKG, "static", f)).count('fetch("api/') for f in pages)
        bad_abs = [f for f in pages if 'fetch("/api' in read(os.path.join(PKG, "static", f))]
        chk("页面接口调用全为相对路径（与部署前缀无关）", not bad_abs and n_fetch >= 9,
            "相对 %d 处 / 绝对残留 %s" % (n_fetch, bad_abs))
        chk("页面导航链接为相对路径", 'href="foundation1_models.html"' in
            read(os.path.join(PKG, "static", "prompt.html")))
        chk("首页跳转为相对路径", 'RedirectResponse(HOME_PAGE)' in serverpy)

        # ---------------- 1. 起服务 ----------------
        w("")
        w("[1] 按容器入口方式启动产物（0.0.0.0:7860）")
        if wait_health("http://127.0.0.1:%d" % APP_PORT, seconds=2):
            w("    7860 已有实例在跑，直接复用")
        else:
            env = dict(os.environ)
            env["PYTHONUNBUFFERED"] = "1"
            proc = subprocess.Popen([sys.executable, "-u", "server.py",
                                     "--host", "0.0.0.0", "--port", str(APP_PORT)],
                                    cwd=PKG, env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if not wait_health("http://127.0.0.1:%d" % APP_PORT):
                chk("服务在 7860 启动成功", False, "超时")
                return 1
        chk("服务在 7860 启动成功", True)

        # ---------------- 2. 起子路径反代 ----------------
        w("")
        w("[2] 起子路径反代 %s -> 后端 /（模拟平台前缀）" % PREFIX)
        proxy = http.server.ThreadingHTTPServer(("127.0.0.1", PROXY_PORT), PrefixProxy)
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        chk("反代就绪", req("GET", PROXY_BASE + "/api/health")[0] == 200)

        # ---------------- 3. 走前缀访问 ----------------
        w("")
        w("[3] 经前缀访问（模拟创空间线上）")
        s, loc, _ = req("GET", PROXY_BASE + "/", follow=False)
        w("    GET %s/ -> %s Location=%r" % (PREFIX, s, loc))
        chk("首页 307 跳转到相对地址", s in (302, 307) and loc == "foundation1_models.html", (s, loc))

        s, _, body = req("GET", PROXY_BASE + "/foundation1_models.html")
        chk("经前缀能打开模型列表页", s == 200 and 'fetch("api/mdl"' in body, "status=%s" % s)

        s, _, body = req("GET", PROXY_BASE + "/api/srv")
        n_srv = len(json.loads(body)) if s == 200 else -1
        w("    GET %s/api/srv -> %s 共 %s 行" % (PREFIX, s, n_srv))
        chk("经前缀能读服务器接口", s == 200 and n_srv > 0, "rows=%s" % n_srv)

        s, _, body = req("GET", PROXY_BASE + "/api/mdl")
        n_mdl = len(json.loads(body)) if s == 200 else -1
        w("    GET %s/api/mdl -> %s 共 %s 行" % (PREFIX, s, n_mdl))
        chk("经前缀能读模型接口", s == 200 and n_mdl > 0, "rows=%s" % n_mdl)

        # ---------------- 4. 写入链路（新增 + 删除） ----------------
        w("")
        w("[4] 经前缀写入：新增平台/模型 -> 回读 -> 删除")
        stamp = str(int(time.time()))
        plat = "MS校验平台-" + stamp
        mdl = "MS校验模型-" + stamp
        s, _, body = req("POST", PROXY_BASE + "/api/srv",
                         {"plat_name": plat, "base_url": "https://example.com/v1",
                          "comp_name": "校验用公司", "home_url": "https://example.com"})
        chk("经前缀新增服务器成功（201）", s == 201, "status=%s %s" % (s, body[:80]))
        s, _, body = req("POST", PROXY_BASE + "/api/mdl",
                         {"plat_name": plat, "mdl_name": mdl, "mdl_type": "对话",
                          "in_price": "1.5", "out_price": "3", "unit": "K", "url": ""})
        chk("经前缀新增模型成功（201）", s == 201, "status=%s %s" % (s, body[:80]))

        s, _, body = req("GET", PROXY_BASE + "/api/mdl")
        rows = json.loads(body) if s == 200 else []
        hit = [r for r in rows if r.get("model") == mdl]
        chk("新模型已落库且可回读", len(hit) == 1, hit[:1])
        if hit:
            chk("回读字段与写入一致（类型/单价/单位/归属平台）",
                hit[0].get("type") == "对话" and abs(float(hit[0].get("inPrice")) - 1.5) < 1e-6
                and abs(float(hit[0].get("outPrice")) - 3.0) < 1e-6
                and hit[0].get("unit") == "K" and hit[0].get("platform") == plat, hit[0])
        chk("回读条数 == 原条数 + 1", len(rows) == n_mdl + 1, "%s vs %s" % (len(rows), n_mdl + 1))

        # ---- 修改链路（列表页「操作」列的「修改」按钮走 PUT）----
        if hit:
            mid = hit[0]["id"]
            mdl2 = mdl + "-改"
            s, _, body = req("PUT", PROXY_BASE + "/api/mdl",
                             {"id": mid, "plat_name": plat, "mdl_name": mdl2,
                              "mdl_type": "对话", "in_price": "4.135",
                              "out_price": "0.0005", "unit": "K", "url": ""})
            chk("经前缀修改模型成功（PUT 被反代透传）", s == 200 and '"ok"' in body,
                "status=%s %s" % (s, body[:80]))
            s, _, body = req("GET", PROXY_BASE + "/api/mdl")
            hit2 = [r for r in json.loads(body) if r.get("id") == mid]
            chk("修改按主键落库且没新增行", len(hit2) == 1, hit2[:1])
            if hit2:
                chk("改后名称生效", hit2[0].get("model") == mdl2, hit2[0].get("model"))
                chk("计费按原文保存（4.135 / 0.0005 未被四舍五入）",
                    hit2[0].get("inPrice") == "4.135" and hit2[0].get("outPrice") == "0.0005",
                    (hit2[0].get("inPrice"), hit2[0].get("outPrice")))
            s, _, body = req("PUT", PROXY_BASE + "/api/mdl", {"mdl_name": "缺id"})
            chk("PUT 缺 id -> 400", s == 400, "status=%s" % s)
            hit = hit2 or hit

        s, _, body = req("DELETE", PROXY_BASE + "/api/mdl", {"ids": [r["id"] for r in hit]})
        chk("经前缀删除模型成功", s == 200, "status=%s" % s)
        s, _, body = req("GET", PROXY_BASE + "/api/mdl")
        chk("删除后条数回落", len(json.loads(body)) == n_mdl, len(json.loads(body)))

        # ---------------- 5. 容器内不该有的东西 ----------------
        w("")
        w("[5] 产物清洁度")
        files = []
        for d, _, fs in os.walk(PKG):
            for f in fs:
                files.append(os.path.relpath(os.path.join(d, f), PKG))
        chk("不含开发期脚本 / 旧 Streamlit 版", not [f for f in files if f.startswith(("verify_", "build_", "app.py"))
                                              or f.startswith("comp/")], files)
        biggest = max(os.path.getsize(os.path.join(PKG, f)) for f in files)
        chk("无超过 100MB 的文件（平台要求，否则需 Git LFS）", biggest < 100 * 1024 * 1024,
            "最大 %.1f KB" % (biggest / 1024.0))

        w("")
        w("=" * 74)
        w("结果：%s" % ("全部通过" if not FAILS else "失败项 -> " + "; ".join(FAILS)))
        return 0 if not FAILS else 1

    except Exception as exc:
        import traceback
        w("!! 校验脚本异常: %r" % exc)
        w(traceback.format_exc())
        return 1
    finally:
        cleanup_test_rows()          # 先清测试数据（需要应用还活着）
        if proxy is not None:
            try:
                proxy.shutdown()
            except Exception:
                pass
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        open(OUT, "w", encoding="utf-8").write(buf.getvalue())
        print("written: %s" % OUT)


if __name__ == "__main__":
    sys.exit(main())
