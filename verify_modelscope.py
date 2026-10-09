# -*- coding: utf-8 -*-
"""把打包产物当容器真跑一遍 —— 本地复刻「魔搭创空间 Docker 容器」的启动语义。

为什么要它：
  pack_modelscope.py 只保证「包长得对」（文件齐、无密钥、fetch 相对化），
  但**能不能起来**是另一回事：容器入口是 start.sh，它自己决定用哪个库、
  要不要用种子重建、监听哪个端口。只做静态检查等于没验。
  本脚本直接 `sh start.sh`（不是另写一套模拟逻辑），把它的每条分支都走一遍。

覆盖：
  [1] 静态：复用 verify_upload.check_local（上传包口径 + 全包密钥扫描）
  [2] 冷启动：没有任何库 -> 用 data/seed.sqlite 建库 -> 服务起来
  [3] 应用：首页跳转 + 9 个页面 + 6 个接口逐一核对
  [4] 子路径反代：把 /studio/app/xxx 剥掉前缀转发到容器端口，
      验证「相对路径」写法在平台前缀下真的通（这是 fetch 相对化的唯一理由）
  [5] 旧版库自愈：只有 srv/mdl 的 Starlette 遗留库 -> 判不可用 -> 用种子重建
  [6] 种子版本变化 -> 刷新持久化库 + 旧库备份成 .bak
  [7] PROMPTDECK_SEED_REFRESH=off -> 不刷新（线上手工数据不被冲掉）
  [8] 收尾：进程 / 临时目录全部清理

运行：
    python verify_modelscope.py            # 用系统 Python 3.14（Django 装在那儿）
    报告落在 _ms_check.txt，同时打印到屏幕；退出码 0 = 全绿。

⚠️ start.sh 里写的是裸 `python`（容器里就是镜像的 Python）。
   本机裸 python 指向没装 Django 的 3.13，所以本脚本会把 Python314 的目录插到 PATH 最前面，
   模拟「容器里的 python 就是装了依赖的那个」。
"""
import http.client as _hclient      # 用别名：下面有个同名辅助函数 http()，会把标准库 http 包盖住
import http.server as _hserver
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PKG = os.path.join(ROOT, "promptdeck_modelscope")
REPORT = os.path.join(HERE, "_ms_check.txt")

PORT = 7861          # 假装是容器的 7860（本地避开占用）
PROXY_PORT = 7862    # 子路径反代
BASE = "http://127.0.0.1:%d" % PORT

PAGES = ["Table_Structure_Extraction.html", "foundation1_models.html",
         "list_server.html", "model_add.html", "new_server.html",
         "prompt.html", "prompt_scheme.html", "prompt_test.html",
         "prompt_test_results_list.html"]
SEED_TABLES = ["srv", "mdl", "tst_plan", "tst_plan_prompt",
               "tst_api_key", "tst_rec", "tst_rec_dtl"]

N_PASS = N_FAIL = 0
LINES = []
LOG_LOCK = threading.Lock()


def w(s=""):
    with LOG_LOCK:
        LINES.append(s)
        print(s)


def chk(name, ok, detail=""):
    global N_PASS, N_FAIL
    if ok:
        N_PASS += 1
        w("  [PASS] %s" % name)
    else:
        N_FAIL += 1
        w("  [FAIL] %s    %s" % (name, detail))
    return ok


# ---------------------------------------------------------------- 进程工具
def start_sh(workdir, db, seed, seed_ver_file, port=PORT, extra_env=None):
    """跑产物里的 start.sh。返回 (Popen, 日志路径)。

    环境变量就按容器语义给：库落点 / 种子 / 种子版本文件 / 端口。
    PATH 前面插上 Python314 —— 模拟容器里那个「装了依赖的 python」。
    """
    log = os.path.join(workdir, "start_%d.log" % int(time.time() * 1000))
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(os.path.abspath(sys.executable)) + os.pathsep + env.get("PATH", "")
    env["PROMPTDECK_DB"] = db.replace("\\", "/")
    env["PROMPTDECK_SEED"] = seed.replace("\\", "/")
    env["PROMPTDECK_SEED_VER_FILE"] = seed_ver_file.replace("\\", "/")
    env["PORT"] = str(port)
    env["PYTHONIOENCODING"] = "utf-8"
    # 与容器一致：不写 .pyc、不缓冲输出。
    # （Dockerfile 里就有 PYTHONUNBUFFERED=1；本地不设的话 stdout 重定向到文件是块缓冲，
    #   Django 的启动横幅迟迟不刷盘，日志断言会假失败 —— 真踩过。）
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    for k, v in (extra_env or {}).items():
        env[k] = v
    fp = open(log, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(["sh", "start.sh"], cwd=PKG, env=env,
                            stdout=fp, stderr=subprocess.STDOUT)
    return proc, log


def stop(proc, log):
    """结束进程，返回日志文本。"""
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=15)
        except Exception:                                        # noqa: BLE001
            try:
                proc.kill()
            except Exception:                                    # noqa: BLE001
                pass
    time.sleep(0.6)
    try:
        with open(log, encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:                                            # noqa: BLE001
        return ""


def wait_health(timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(BASE + "/api/health", timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:                                        # noqa: BLE001
            time.sleep(0.5)
    return False


def wait_db(cond, timeout=60):
    """等库满足条件（start.sh 的播种发生在起服务之前，所以很快）。"""
    end = time.time() + timeout
    while time.time() < end:
        try:
            if cond():
                return True
        except Exception:                                        # noqa: BLE001
            pass
        time.sleep(0.4)
    return False


# ---------------------------------------------------------------- HTTP 工具
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def http(path, raw=False, base=None):
    """返回 (状态码, 正文, 响应头 dict)。raw=True 时不跟随跳转（用来验首页跳转）。"""
    req = urllib.request.Request((base or BASE) + path,
                                 headers={"User-Agent": "pd-verify"})
    opener = urllib.request.build_opener(_NoRedirect) if raw \
        else urllib.request.build_opener()
    try:
        with opener.open(req, timeout=25) as r:
            return r.status, r.read().decode("utf-8", "replace"), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), dict(e.headers)
    except Exception as e:                                       # noqa: BLE001
        return 0, "请求失败：%s" % e, {}


def jget(path, base=None):
    code, body, _ = http(path, base=base)
    try:
        return code, json.loads(body)
    except Exception:                                            # noqa: BLE001
        return code, None


# ---------------------------------------------------------------- 子路径反代
class StripProxy(_hserver.BaseHTTPRequestHandler):
    """把 /studio/app/xxx 剥掉前缀转发到容器端口 —— 模拟创空间的子路径反代。"""
    PREFIX = "/studio/app"
    protocol_version = "HTTP/1.1"

    def _fwd(self):
        path = self.path
        if path.startswith(self.PREFIX):
            path = path[len(self.PREFIX):] or "/"
        body = None
        cl = self.headers.get("Content-Length")
        if cl:
            body = self.rfile.read(int(cl))
        hdrs = {k: v for k, v in self.headers.items()
                if k.lower() not in ("host", "connection")}
        try:
            conn = _hclient.HTTPConnection("127.0.0.1", PORT, timeout=25)
            conn.request(self.command, path, body=body, headers=hdrs)
            r = conn.getresponse()
            data = r.read()
            self.send_response(r.status)
            for k, v in r.getheaders():
                if k.lower() in ("transfer-encoding", "connection", "content-length"):
                    continue
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            conn.close()
        except Exception as e:                                   # noqa: BLE001
            self.send_response(502)
            self.send_header("Content-Length", "0")
            self.end_headers()
            w("    （代理转发失败：%s）" % e)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _fwd

    def log_message(self, *a):
        pass


# ---------------------------------------------------------------- 库工具
def db_counts(db):
    c = sqlite3.connect(db)
    try:
        return {t: c.execute("select count(*) from %s" % t).fetchone()[0]
                for t in SEED_TABLES}
    finally:
        c.close()


def scalar(db, sql):
    c = sqlite3.connect(db)
    try:
        return c.execute(sql).fetchone()[0]
    finally:
        c.close()


def make_legacy_db(db):
    """造一个「上一版 Starlette 遗留库」：只有 srv / mdl，没有 tst_*。"""
    if os.path.exists(db):
        os.remove(db)
    c = sqlite3.connect(db)
    c.execute("create table srv (id integer primary key, plat_name text, comp_name text,"
              " base_url text, home_url text)")
    c.execute("create table mdl (id integer primary key, srv_id integer, mdl_name text)")
    c.execute("insert into srv values (1,'遗留平台','遗留公司','','')")
    c.execute("insert into mdl values (1,1,'遗留模型')")
    c.commit()
    c.close()


def rm_tree(p):
    """逐层删除（不用 shutil.rmtree：某些沙箱会把它改道到回收站而失败）。"""
    if not os.path.isdir(p):
        return
    for dp, dns, fns in os.walk(p, topdown=False):
        for f in fns:
            try:
                os.remove(os.path.join(dp, f))
            except OSError:
                pass
        for d in dns:
            try:
                os.rmdir(os.path.join(dp, d))
            except OSError:
                pass
    try:
        os.rmdir(p)
    except OSError:
        pass


# ---------------------------------------------------------------- 主体
def main():
    global N_PASS, N_FAIL
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:                                            # noqa: BLE001
        pass

    w("=" * 78)
    w("PromptDeck · 打包产物「容器语义」彩排（直接执行产物里的 start.sh）")
    w("  包目录：%s" % PKG)
    w("  系统 Python：%s" % sys.executable)
    w("=" * 78)

    seed = os.path.join(PKG, "data", "seed.sqlite")
    seed_ver_file = os.path.join(PKG, "data", "seed.version")
    seed_ver = open(seed_ver_file, encoding="utf-8").read().strip()
    # ⚠️ 期望行数一律从**这份种子的真身**现场取，绝不写死 17 / 45。
    #    踩过：清洗规则一个字没改，但开发库里多了一行「同平台不重名」的模型，
    #    种子从 45 变成 46，[2] / [5] / /api/srv / /api/mdl / /api/schema 五处断言集体假失败 ——
    #    这就是「基线一律现场取、禁止硬编码」那条硬约束的现场。
    EXP = db_counts(seed)

    work = tempfile.mkdtemp(prefix="pd_ms_rehearsal_")
    db = os.path.join(work, "prompt_test_DB.db")
    mark = os.path.join(work, ".promptdeck.seed.version")
    proc = log = None
    proxy = None
    try:
        # ---------------------------------------------- [1] 静态：上传包口径
        w("")
        w("■ [1] 上传包静态检查（复用 verify_upload.check_local）")
        sys.path.insert(0, HERE)
        import verify_upload
        errs, notes = verify_upload.check_local(PKG)
        for n in notes:
            w("    · " + n)
        chk("上传包口径全通过（结构 / 上线补丁 / 9 页 / 种子 / 无密钥）", not errs,
            "；".join(errs[:6]))
        chk("能从开发目录 settings.py 读到密钥做比对（否则「无泄漏」是空断言）",
            bool(verify_upload.settings_secrets()))

        # ---------------------------------------------- [2] 冷启动
        w("")
        w("■ [2] 冷启动：没有任何库 -> 用 data/seed.sqlite 建库")
        for p in (db, mark, db + ".bak"):
            if os.path.exists(p):
                os.remove(p)
        chk("起始状态：库不存在", not os.path.exists(db))

        proc, log = start_sh(work, db, seed, seed_ver_file)
        up = wait_health()
        chk("服务在 90 秒内起来（/api/health 返回 200）", up)
        if not up:
            w("    —— start.sh 日志 ——")
            w(stop(proc, log))
            return 1
        chk("持久化库已建出：%s" % os.path.basename(db), os.path.exists(db))
        cnt = db_counts(db)
        chk("建出的库表结构完整（7 张业务表都在）", all(t in cnt for t in SEED_TABLES),
            "实际 %s" % cnt)
        chk("库里就是种子数据：srv=%d / mdl=%d / 密钥 0 行" % (EXP["srv"], EXP["mdl"]),
            cnt["srv"] == EXP["srv"] and cnt["mdl"] == EXP["mdl"] and cnt["tst_api_key"] == 0,
            "实际 %s" % cnt)
        got_mark = open(mark, encoding="utf-8").read().strip() if os.path.exists(mark) else ""
        chk("库旁写下了种子版本标记 = %s" % seed_ver, got_mark == seed_ver,
            "实际 %r" % got_mark)

        # ---------------------------------------------- [3] 应用
        w("")
        w("■ [3] 应用：首页跳转 + 9 个页面 + 6 个接口")
        code, _b, hdrs = http("/", raw=True)
        loc = hdrs.get("Location")
        chk("「/」是跳到页面的重定向（3xx）", code in (301, 302, 303, 307, 308),
            "实际 %s" % code)
        chk("跳转目标是**相对**路径 prompt.html（平台子路径前缀下才不错）",
            (loc or "").strip() == "prompt.html", "实际 Location=%r" % loc)

        page_marker = {
            "prompt.html": "提示词",
            "prompt_scheme.html": "方案",
            "prompt_test.html": "调试",
            "prompt_test_results_list.html": "结果",
            "list_server.html": "服务器",
            "new_server.html": "服务器",
            "foundation1_models.html": "模型",
            "model_add.html": "模型",
            "Table_Structure_Extraction.html": "数据库",
        }
        bad_pages = []
        for p in PAGES:
            st, body, hdrs = http("/" + p)
            ct = hdrs.get("Content-Type", "")
            if st != 200 or "text/html" not in ct:
                bad_pages.append("%s=%s/%s" % (p, st, ct))
            elif page_marker[p] not in body:
                bad_pages.append("%s 缺关键词「%s」" % (p, page_marker[p]))
        chk("9 个页面全部 200 且 Content-Type 是 text/html、内容含关键词",
            not bad_pages, "；".join(bad_pages))

        st, body, _ = http("/api/health")
        chk("/api/health = ok", st == 200 and body.strip() == "ok", "%s %r" % (st, body[:40]))

        st, srv = jget("/api/srv")
        chk("/api/srv 返回 %d 条且字段齐全" % EXP["srv"],
            st == 200 and isinstance(srv, list) and len(srv) == EXP["srv"]
            and set(("id", "platform", "company", "api", "cUrl")) <= set(srv[0]),
            "status=%s len=%s" % (st, len(srv) if isinstance(srv, list) else srv))

        st, mdl = jget("/api/mdl")
        chk("/api/mdl 返回 %d 条且字段齐全" % EXP["mdl"],
            st == 200 and isinstance(mdl, list) and len(mdl) == EXP["mdl"]
            and set(("id", "srvId", "platform", "model", "type", "inPrice",
                     "outPrice", "unit", "url")) <= set(mdl[0]),
            "status=%s len=%s" % (st, len(mdl) if isinstance(mdl, list) else mdl))

        st, tp = jget("/api/tp")
        chk("/api/tp 可访问（方案表；种子为空则给空列表）",
            st == 200 and isinstance(tp, (list, dict)), "status=%s" % st)

        st, rec = jget("/api/rec")
        chk("/api/rec 可访问（测试记录表）", st == 200 and isinstance(rec, (list, dict)),
            "status=%s" % st)

        st, sch = jget("/api/schema")
        names = [t.get("name") for t in sch.get("tables", [])] if isinstance(sch, dict) else []
        chk("/api/schema 读到真实库结构（7 张业务表都在）",
            st == 200 and isinstance(sch, dict) and bool(sch.get("ok"))
            and all(t in names for t in SEED_TABLES),
            "status=%s names=%s" % (st, names))
        if isinstance(sch, dict) and sch.get("tables"):
            by = {t["name"]: t for t in sch["tables"]}
            chk("/api/schema 报的行数与库内一致（srv=%d / mdl=%d）" % (EXP["srv"], EXP["mdl"]),
                by.get("srv", {}).get("rows") == EXP["srv"]
                and by.get("mdl", {}).get("rows") == EXP["mdl"],
                "srv=%s mdl=%s" % (by.get("srv", {}).get("rows"),
                                   by.get("mdl", {}).get("rows")))

        hits = [p for p in PAGES
                if verify_upload.KEY_RE.search(http("/" + p)[1] or "")]
        chk("9 个页面的响应里没有任何明文密钥", not hits, "；".join(hits))

        running_log = open(log, encoding="utf-8", errors="replace").read()
        chk("运行日志显示监听 0.0.0.0:%d" % PORT, ("0.0.0.0:%d" % PORT) in running_log,
            running_log[-300:])

        # ---------------------------------------------- [4] 子路径反代
        w("")
        w("■ [4] 子路径反代（模拟创空间把应用挂在 /studio/app/… 前缀下）")
        proxy = _hserver.ThreadingHTTPServer(("127.0.0.1", PROXY_PORT), StripProxy)
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        time.sleep(0.5)
        pbase = "http://127.0.0.1:%d/studio/app" % PROXY_PORT

        st, body, _ = http("/prompt.html", base=pbase)
        chk("带前缀能打开页面（/studio/app/prompt.html）", st == 200 and "提示词" in body,
            "status=%s" % st)
        st, arr = jget("/api/srv", base=pbase)
        chk("带前缀能调接口（/studio/app/api/srv 返回 %d 条）" % EXP["srv"],
            st == 200 and isinstance(arr, list) and len(arr) == EXP["srv"],
            "status=%s len=%s" % (st, len(arr) if isinstance(arr, list) else arr))
        code, _b, hdrs = http("/", raw=True, base=pbase)
        loc = hdrs.get("Location")
        chk("带前缀时首页跳转仍是相对路径（浏览器会解析成 /studio/app/prompt.html）",
            code in (301, 302, 303, 307, 308) and (loc or "").strip() == "prompt.html",
            "status=%s Location=%r" % (code, loc))
        chk("页面里的接口地址确实是相对的（fetch(\"api/…）",
            'fetch("/api/' not in body and 'fetch("api/' in http("/prompt.html", base=pbase)[1])

        proxy.shutdown()
        proxy = None

        logtxt = stop(proc, log)
        proc = None

        # ---------------------------------------------- [5] 旧版库自愈
        w("")
        w("■ [5] 旧版库自愈：只有 srv/mdl 的 Starlette 遗留库 -> 用种子重建")
        make_legacy_db(db)
        if os.path.exists(mark):
            os.remove(mark)
        chk("已就位一个旧版库（1 平台 / 1 模型 / 无 tst_* 表）",
            scalar(db, "select count(*) from srv") == 1)

        proc, log = start_sh(work, db, seed, seed_ver_file)
        ok = wait_db(lambda: db_counts(db)["srv"] == EXP["srv"]
                     and db_counts(db)["mdl"] == EXP["mdl"])
        logtxt = stop(proc, log)
        proc = None
        chk("旧版库被判不可用并换成种子数据（srv 1->%d / mdl 1->%d）"
            % (EXP["srv"], EXP["mdl"]), ok,
            "日志尾部：%s" % logtxt[-300:])
        chk("start.sh 日志里说明了重建原因",
            ("旧版" in logtxt) or ("不可用" in logtxt), logtxt[-200:])

        # ---------------------------------------------- [6] 种子版本变化 -> 刷新
        w("")
        w("■ [6] 种子版本变化 -> 刷新持久化库 + 旧库备份 .bak")
        c = sqlite3.connect(db)
        c.execute("insert into srv (id, plat_name, comp_name) values"
                  " (999, 'REHEARSAL_SENTINEL', '')")
        c.commit()
        c.close()
        with open(mark, "w", encoding="utf-8") as f:
            f.write("OLDMARK")
        if os.path.exists(db + ".bak"):
            os.remove(db + ".bak")

        # ⚠️ 这里等的是「版本标记写成」而不是「哨兵行没了」。
        #    rebuild_from_seed 的顺序是 cp 种子 -> db_ok（要起一次 python，约 200ms）-> 写标记。
        #    若只等哨兵行消失就在那个窗口里读到还没写的标记 ⇒ 假失败（真踩过）。
        proc, log = start_sh(work, db, seed, seed_ver_file)

        def mark_updated():
            try:
                return open(mark, encoding="utf-8").read().strip() == seed_ver
            except Exception:                                    # noqa: BLE001
                return False

        ok = wait_db(mark_updated)
        stop(proc, log)
        proc = None
        chk("start.sh 已按新版种子重建持久化库（版本标记写成新值）", ok)
        gone = scalar(db, "select count(*) from srv where plat_name="
                          "'REHEARSAL_SENTINEL'") == 0
        chk("哨兵行被新种子冲掉（说明真的刷新了）", gone)
        bak_has = False
        if os.path.isfile(db + ".bak"):
            try:
                bak_has = scalar(db + ".bak",
                                 "select count(*) from srv where plat_name="
                                 "'REHEARSAL_SENTINEL'") == 1
            except Exception:                                    # noqa: BLE001
                bak_has = False
        chk("旧库已备份为 .bak，且备份里保留了哨兵行", bak_has)
        new_mark = open(mark, encoding="utf-8").read().strip() if os.path.exists(mark) else ""
        chk("版本标记已更新为新种子版本", new_mark == seed_ver, "实际 %r" % new_mark)

        # ---------------------------------------------- [7] SEED_REFRESH=off
        w("")
        w("■ [7] PROMPTDECK_SEED_REFRESH=off -> 不动线上库")
        c = sqlite3.connect(db)
        c.execute("insert into srv (id, plat_name, comp_name) values"
                  " (998, 'REHEARSAL_KEEP', '')")
        c.commit()
        c.close()
        with open(mark, "w", encoding="utf-8") as f:
            f.write("OLDMARK2")

        proc, log = start_sh(work, db, seed, seed_ver_file,
                             extra_env={"PROMPTDECK_SEED_REFRESH": "off"})
        # 先等服务真起来 —— 只有它走完了「要不要刷新」这段判断，才能说 off 生效了；
        # 起来就杀等于什么都没验。
        up = wait_health()
        stop(proc, log)
        proc = None
        ok = scalar(db, "select count(*) from srv where plat_name='REHEARSAL_KEEP'") == 1
        chk("设了 off 之后线上库原样保留（哨兵行还在，且服务照常起来）", ok and up,
            "sentinel_kept=%s server_up=%s" % (ok, up))
    finally:
        try:
            if proxy:
                proxy.shutdown()
        except Exception:                                        # noqa: BLE001
            pass
        stop(proc, log)
        rm_tree(work)
        # 保险：万一还是有 __pycache__ 落到上传包里（比如手滑单跑了 manage.py），一并清掉
        for dp, dns, _fn in os.walk(PKG):
            for d in list(dns):
                if d == "__pycache__":
                    rm_tree(os.path.join(dp, d))
                    dns.remove(d)

    w("")
    w("=" * 78)
    w("结果：PASS=%d  FAIL=%d" % (N_PASS, N_FAIL))
    w("=" * 78)
    with open(REPORT, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(LINES) + "\n")
    w("报告已写入 %s" % REPORT)
    leaks = [s for s in open(REPORT, encoding="utf-8", errors="replace").read().split()
             if s.startswith("sk-") and len(s) > 23]
    if leaks:
        w("!! 报告里出现了明文密钥，请检查")
    return 1 if N_FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
