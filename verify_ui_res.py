# -*- coding: utf-8 -*-
"""PromptDeck（Django 版）· 「测试结果列表」页真实浏览器端到端校验（第二次迭代 · 第五轮口径）。

本轮口径（用户两轮拍板：「只在折叠态截断、展开后给全文」+「所有题一律折叠态截 30 字」）：
    · 查看弹窗的正文**只截折叠态** —— 逐题默认只露「问题」，此时问题取前 30 字
      （超出补「…」，恰好 30 字不加省略号）；点「展开」后问题换回全文；
    · 标准答案 / 模型输出本身只存在于展开区（.q-more），按「展开后给全文」一律给全文；
    · **所有题一律截**：没有展开入口的题（标准答案与模型输出都为空）同样截 30 字，只是无处可展；
    · 字段 / 排版 / CSS 零改动。

本页**没有后端**（数据全在浏览器 localStorage + 页内 SEED_RESULTS），所以校验不碰接口，
只做两件事：① 渲染口径对不对；② 有没有把库 / 页面带坏。

    [1] 页面能打开，夹具能落进 localStorage（pd_test_results）
    [2] 点操作列「查看」→ 弹窗打开；点「展开明细」→ 逐题列表出现
    [3] 折叠态：问题截到 30 字 + 「…」，尾部原文消失；.q-more 仍 hidden
    [4] 展开后：问题回到全文、标准答案 / 模型输出给全文；按钮变「收起」
    [5] 再点「收起」：问题回到截断版（证明是双向切换，不是一次性）
    [6] 边界：恰好 30 字 → 原样显示、不加省略号（两态一致）
    [7] 不足 30 字 → 原样显示（两态一致）
    [8] 空问题 + 无标准答案：占位仍在、不该渲染的「标准答案」行不渲染
    [9] 无展开入口（答案与输出都为空）的题：同样截 30 字、没有展开按钮、点不出展开
    [10] 全程无 JS 异常
    [11] 收尾：库表计数回到基线（本页不写库，确认没被带脏）

用真实 Edge + CDP 操作 DOM，不依赖 playwright（脚手架与 verify_ui_tp / verify_ui_pt 同源）。
不需要外部应用已启动 —— 本脚本自己在 8504 起一个实例（避开 8502 / 8503）。

前置：pip install websocket-client；本机装有 Edge；已跑过 build_static.py。
结果写 _ui_res_check.txt；退出码 0 = 全部通过，1 = 有失败项。
"""
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request

try:
    import websocket  # websocket-client
except ImportError:
    print("跳过：未安装 websocket-client（pip install websocket-client）")
    sys.exit(0)

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "_ui_res_check.txt")
DB_PATH = os.environ.get("PROMPTDECK_DB") or os.path.join(HERE, "data", "prompt_test_DB_twe.db")

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
CDP_PORT = 9352
APP_PORT = 8504

THUMB_MAX = 30                 # 与原型里的 PROB_THUMB_MAX 对齐
ELLIPSIS = "…"

buf = io.StringIO()
FAILS = []
events = []


def w(s=""):
    buf.write(str(s) + "\n")


def chk(label, ok, detail=""):
    w("  [%s] %s%s" % ("PASS" if ok else "FAIL", label, ("  " + str(detail)) if detail else ""))
    if not ok:
        FAILS.append(label)


# ==================================================================== 夹具
# 用「可识别的唯一尾巴」构造超长串：截断后尾巴必须消失，才能证明是真截断而非巧合；
# 展开后尾巴必须回来，才能证明「展开给全文」不是只放开了 .q-more。
Q_LONG = "问题" + "あ" * 28 + "【Q尾】"          # 34 字
ANS_LONG = "答案" + "い" * 28 + "【A尾】"        # 34 字
OUT_LONG = "输出" + "う" * 28 + "【O尾】"        # 34 字

Q_EXACT = "问题" + "か" * 28                     # 30 字（边界：不该加省略号）
ANS_EXACT = "答案" + "き" * 28
OUT_EXACT = "输出" + "く" * 28

Q_SHORT = "短问题"
ANS_SHORT = "短答案"
OUT_SHORT = "短输出"

Q_EMPTY = ""

Q_NOEXP = "无答" + "さ" * 28 + "【B尾】"         # 34 字，答案与输出都为空 ⇒ 无展开入口（照样要截 30 字）


def build_fixture():
    return [{
        "id": 9001,
        "name": "截断校验方案",
        "problems": [
            # P1 三处都超 30 字
            {"q": Q_LONG, "ans": ANS_LONG, "out": OUT_LONG, "ai": "right", "judge": "right"},
            # P2 三处都恰 30 字（边界）
            {"q": Q_EXACT, "ans": ANS_EXACT, "out": OUT_EXACT, "ai": "wrong", "judge": "wrong"},
            # P3 三处都短
            {"q": Q_SHORT, "ans": ANS_SHORT, "out": OUT_SHORT, "ai": "right", "judge": "right"},
            # P4 空问题（占位）+ 无标准答案（该行不渲染）
            {"q": Q_EMPTY, "ans": "", "out": OUT_LONG, "ai": "", "judge": ""},
            # P5 超长问题 + 无标准答案 + 无模型输出 ⇒ 没有展开入口
            {"q": Q_NOEXP, "ans": "", "out": "", "ai": "right", "judge": "right"},
        ],
    }]


N_PROB = 5


# ==================================================================== CDP
class CDP:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=30,
                                              http_proxy_host=None, suppress_origin=True)
        self.i = 0
        self.sid = None

    def send(self, method, params=None, session=True):
        self.i += 1
        mid = self.i
        msg = {"id": mid, "method": method, "params": params or {}}
        if session and self.sid:
            msg["sessionId"] = self.sid
        self.ws.send(json.dumps(msg))
        deadline = time.time() + 30
        while time.time() < deadline:
            raw = self.ws.recv()
            if not raw:
                continue
            data = json.loads(raw)
            if data.get("id") == mid:
                if "error" in data:
                    raise RuntimeError("%s -> %s" % (method, data["error"]))
                return data.get("result", {})
            if "method" in data:
                events.append(data)
        raise TimeoutError(method)

    def pump(self, seconds):
        end = time.time() + seconds
        self.ws.settimeout(0.4)
        while time.time() < end:
            try:
                raw = self.ws.recv()
            except Exception:
                continue
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except Exception:
                continue
            if "method" in data:
                events.append(data)
        self.ws.settimeout(30)

    def go(self, url, wait=4):
        self.send("Page.navigate", {"url": url})
        self.pump(wait)

    def ev(self, expr):
        r = self.send("Runtime.evaluate",
                      {"expression": expr, "returnByValue": True, "awaitPromise": True})
        if "exceptionDetails" in r:
            return "__JSERROR__: %s" % str(
                r["exceptionDetails"].get("exception", {}).get("description"))[:200]
        return r.get("result", {}).get("value")

    def ev_json(self, expr, tag):
        v = self.ev(expr)
        if isinstance(v, dict):
            raise RuntimeError("[%s] CDP 返回了 dict（JS 片段末尾多半漏了 ()）：%s" % (tag, v))
        if not isinstance(v, str) or v.startswith("__JSERROR__"):
            raise RuntimeError("[%s] 页面侧报错：%s" % (tag, v))
        return json.loads(v)


HOOK = """window.__pdErrors = [];
window.addEventListener('error', function(e){ window.__pdErrors.push(String(e.message)); });
window.addEventListener('unhandledrejection', function(e){
  window.__pdErrors.push('unhandledrejection: ' + String(e.reason));
});
"""

JS_SET_FIXTURE = """(function(){
  var list = %s;
  try{ localStorage.setItem('pd_test_results', JSON.stringify(list)); }catch(e){ return 'ERR:' + e; }
  return JSON.stringify({ n: list.length, rawLen: (localStorage.getItem('pd_test_results')||'').length });
})()"""

JS_OPEN_VIEW = """(function(){
  var b = document.querySelector('#tbody [data-view]');
  if (!b) return JSON.stringify({ err: '__NOBTN__' });
  b.click();
  return JSON.stringify({
    dataRows:    document.querySelectorAll('#tbody tr[data-id]').length,
    maskShown:   document.getElementById('viewMask').classList.contains('show'),
    listHidden:  document.getElementById('vwList').hidden,
    qCount:      document.querySelectorAll('#vwList .q-item').length
  });
})()"""

JS_EXPAND_LIST = """(function(){
  var t = document.getElementById('vwQToggle');
  t.click();
  return JSON.stringify({
    listHidden: document.getElementById('vwList').hidden,
    label:      t.textContent.trim()
  });
})()"""

# 折叠态快照：不点任何逐题的「展开」，只读默认渲染出来的东西
JS_SNAP_FOLDED = """(function(){
  var out = [];
  var items = document.querySelectorAll('#vwList .q-item');
  for (var i = 0; i < items.length; i++){
    var it   = items[i];
    var more = it.querySelector('.q-more');
    var tog  = it.querySelector('[data-qtoggle]');
    var qt   = it.querySelector('.q-text');
    out.push({
      q:         qt ? qt.textContent : '',
      full:      (qt && qt.hasAttribute('data-full')) ? qt.getAttribute('data-full') : null,
      hasToggle: !!tog,
      moreHidden: more ? !!more.hidden : null
    });
  }
  return JSON.stringify({
    n: items.length,
    items: out,
    errors: (window.__pdErrors || []).slice()
  });
})()"""

# 点第 idx 题的「展开 / 收起」，把问题正文与明细行一起取回来
JS_TOGGLE = """(function(idx){
  var it = document.querySelectorAll('#vwList .q-item')[idx];
  if (!it) return JSON.stringify({ err: '__NOITEM__' });
  var tog = it.querySelector('[data-qtoggle]');
  if (!tog) return JSON.stringify({ err: '__NOTOG__' });
  tog.click();
  var more  = it.querySelector('.q-more');
  var lines = [];
  if (more){
    var ls = more.querySelectorAll('.q-line');
    for (var j = 0; j < ls.length; j++){
      lines.push({
        k: ((ls[j].querySelector('.k2') || {}).textContent || '').trim(),
        v: ((ls[j].querySelector('.v2') || {}).textContent || '')
      });
    }
  }
  return JSON.stringify({
    q:          it.querySelector('.q-text').textContent,
    label:      tog.textContent.trim(),
    moreHidden: more ? !!more.hidden : null,
    lines:      lines
  });
})(%d)"""


def db_count(table):
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]


def wait_http(url, seconds=40):
    end = time.time() + seconds
    while time.time() < end:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def check_text(label, got, want, tail_forbidden=None, tail_required=None):
    """断言一处渲染文本 = 期望值。
    tail_forbidden：该尾巴必须**不在**（证明真截断）；tail_required：必须**在**（证明给的是全文）。"""
    ok = (got == want)
    detail = "len(got)=%d len(want)=%d" % (len(got), len(want))
    if ok and tail_forbidden:
        gone = tail_forbidden not in got
        ok = ok and gone
        detail += " / 尾部 %r 已消失=%s" % (tail_forbidden, gone)
    if ok and tail_required:
        here = tail_required in got
        ok = ok and here
        detail += " / 尾部 %r 在=%s" % (tail_required, here)
    if not ok:
        detail += " / got=%r want=%r" % (got[:40], want[:40])
    chk(label, ok, detail)


def field(item, k):
    """从明细行里取某个标签的值（没有该行 → None）。"""
    for ln in item.get("lines") or []:
        if ln.get("k") == k:
            return ln.get("v")
    return None


def main():
    if not os.path.isdir(os.path.join(HERE, "static")):
        w("跳过：还没跑 build_static.py")
        return 0
    if not os.path.exists(EDGE):
        w("跳过：未找到 Edge（%s）" % EDGE)
        return 0

    base = "http://127.0.0.1:%d" % APP_PORT

    w("=" * 78)
    w("PromptDeck · 「测试结果列表」页端到端校验（真实 Edge + CDP）")
    w("库文件：%s" % DB_PATH)
    w("应用：%s（本脚本自起）  本页无后端，夹具走 localStorage" % base)
    w("口径：只截**折叠态** —— 问题前 %d 字 + 「%s」（所有题一律截）；展开后问题 / 标准答案 / 模型输出一律全文"
      % (THUMB_MAX, ELLIPSIS))
    w("=" * 78)

    tables = ("srv", "mdl", "tst_plan", "tst_plan_prompt", "tst_api_key", "tst_rec", "tst_rec_dtl")
    before = {t: db_count(t) for t in tables}

    app_log = open(os.path.join(HERE, "_ui_res_django.log"), "w", encoding="utf-8")
    app = subprocess.Popen([sys.executable, "manage.py", "runserver",
                            "127.0.0.1:%d" % APP_PORT, "--noreload"],
                           cwd=HERE, stdout=app_log, stderr=subprocess.STDOUT)

    tmp = tempfile.mkdtemp(prefix="pd_ui_res")
    edge = None
    try:
        chk("自起的应用实例已就绪", wait_http("%s/api/health" % base), base)

        # ------------------------------------------------------------ 起 Edge
        edge = subprocess.Popen([
            EDGE, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
            "--remote-debugging-port=%d" % CDP_PORT, "--user-data-dir=" + tmp,
            "--window-size=1920,1080", "about:blank",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        ws_browser = None
        for _ in range(60):
            time.sleep(0.5)
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d/json/version" % CDP_PORT,
                                            timeout=3) as r:
                    ws_browser = json.loads(r.read().decode())["webSocketDebuggerUrl"]
                break
            except Exception:
                pass
        chk("Edge devtools 已就绪", bool(ws_browser), "port=%d" % CDP_PORT)
        if not ws_browser:
            return 1

        c = CDP(ws_browser)
        tid = c.send("Target.createTarget", {"url": "about:blank"}, session=False)["targetId"]
        c.sid = c.send("Target.attachToTarget", {"targetId": tid, "flatten": True},
                       session=False)["sessionId"]
        c.send("Runtime.enable")
        c.send("Page.enable")
        c.send("Page.addScriptToEvaluateOnNewDocument", {"source": HOOK})

        # ------------------------------------------------------------ [1] 打开页面 + 灌夹具
        page = "%s/prompt_test_results_list.html" % base
        w("")
        w("■ [1] 打开页面并灌入夹具（localStorage: pd_test_results）")
        w("    页面：%s" % page)
        c.go(page, 5)                       # 先加载一次：拿到同源 origin（此时页面自己会落 SEED_RESULTS）

        fix = build_fixture()
        r = c.ev_json(JS_SET_FIXTURE % json.dumps(fix, ensure_ascii=False), "set_fixture")
        chk("夹具已写进 pd_test_results", r.get("n") == 1 and r.get("rawLen", 0) > 100, "%s" % r)

        c.go(page, 5)                       # 重新加载：键已存在 ⇒ 不会重新播种，渲染读到夹具

        # ------------------------------------------------------------ [2] 打开查看弹窗
        w("")
        w("■ [2] 点操作列「查看」→ 弹窗打开 → 展开明细")
        snap = c.ev_json(JS_OPEN_VIEW, "open_view")
        chk("列表页渲染出夹具那一行", snap.get("dataRows") == 1, "dataRows=%s" % snap.get("dataRows"))
        chk("点「查看」后弹窗打开", snap.get("maskShown") is True, "%s" % snap)
        chk("明细默认折叠（第二十一轮口径不变）", snap.get("listHidden") is True, "%s" % snap)
        chk("明细里 %d 道题都在" % N_PROB, snap.get("qCount") == N_PROB,
            "qCount=%s" % snap.get("qCount"))

        ex = c.ev_json(JS_EXPAND_LIST, "expand_list")
        chk("点「展开明细」后列表出现、按钮变「收起明细」",
            ex.get("listHidden") is False and ex.get("label") == "收起明细", "%s" % ex)

        # ------------------------------------------------------------ [3] 折叠态
        w("")
        w("■ [3] 折叠态：问题截到 %d 字（展开区仍 hidden）" % THUMB_MAX)
        fold = c.ev_json(JS_SNAP_FOLDED, "snap_folded")
        items = fold.get("items") or []
        chk("折叠态快照拿到 %d 条" % N_PROB, len(items) == N_PROB, "实际 %d 条" % len(items))

        if len(items) == N_PROB:
            f1, f2, f3, f4, f5 = items
            check_text("P1 问题 截到 30 字 + 「…」", f1["q"], Q_LONG[:THUMB_MAX] + ELLIPSIS,
                       tail_forbidden="【Q尾】")
            chk("P1 折叠态 .q-more 仍 hidden", f1.get("moreHidden") is True, "%s" % f1.get("moreHidden"))
            chk("P1 有展开按钮", f1.get("hasToggle") is True)
            chk("P1 问题的 data-full 存的是全文",
                f1.get("full") == Q_LONG, "len(full)=%s" % (len(f1.get("full") or "")))

        # ------------------------------------------------------------ [4] 展开后
        w("")
        w("■ [4] 点 P1「展开」→ 问题回全文、标准答案 / 模型输出 全文")
        t1 = c.ev_json(JS_TOGGLE % 0, "toggle_p1")
        check_text("P1 展开后 问题 = 全文", t1.get("q"), Q_LONG, tail_required="【Q尾】")
        check_text("P1 展开后 标准答案 = 全文", field(t1, "标准答案"), ANS_LONG,
                   tail_required="【A尾】")
        check_text("P1 展开后 模型输出 = 全文", field(t1, "模型输出"), OUT_LONG,
                   tail_required="【O尾】")
        chk("P1 展开后 .q-more 已显示、按钮变「收起」",
            t1.get("moreHidden") is False and t1.get("label") == "收起", "%s" % t1)

        # ------------------------------------------------------------ [5] 再收起
        w("")
        w("■ [5] 再点「收起」→ 问题回到截断版（双向切换）")
        t1b = c.ev_json(JS_TOGGLE % 0, "toggle_p1_back")
        check_text("P1 收起后 问题 又截到 30 字 + 「…」", t1b.get("q"), Q_LONG[:THUMB_MAX] + ELLIPSIS,
                   tail_forbidden="【Q尾】")
        chk("P1 收起后 .q-more 又 hidden、按钮变「展开」",
            t1b.get("moreHidden") is True and t1b.get("label") == "展开", "%s" % t1b)

        if len(items) == N_PROB:
            f2, f3, f4, f5 = items[1], items[2], items[3], items[4]

            # -------------------------------------------------------- [6] 边界
            w("")
            w("■ [6] 边界：恰好 %d 字 → 原样显示、不加省略号" % THUMB_MAX)
            check_text("P2 折叠态 问题 = 原样 30 字", f2["q"], Q_EXACT)
            chk("P2 折叠态 问题 不含省略号", ELLIPSIS not in (f2["q"] or ""), "%r" % f2["q"])
            t2 = c.ev_json(JS_TOGGLE % 1, "toggle_p2")
            check_text("P2 展开后 问题 仍是 30 字全文", t2.get("q"), Q_EXACT)
            check_text("P2 展开后 标准答案 = 30 字全文", field(t2, "标准答案"), ANS_EXACT)
            check_text("P2 展开后 模型输出 = 30 字全文", field(t2, "模型输出"), OUT_EXACT)
            chk("P2 两态都不含省略号",
                ELLIPSIS not in (t2.get("q") or "")
                and ELLIPSIS not in (field(t2, "标准答案") or "")
                and ELLIPSIS not in (field(t2, "模型输出") or ""))

            # -------------------------------------------------------- [7] 短路
            w("")
            w("■ [7] 不足 %d 字 → 原样显示（两态一致）" % THUMB_MAX)
            check_text("P3 折叠态 问题 = 短文本", f3["q"], Q_SHORT)
            t3 = c.ev_json(JS_TOGGLE % 2, "toggle_p3")
            check_text("P3 展开后 问题 = 短文本", t3.get("q"), Q_SHORT)
            check_text("P3 展开后 标准答案 = 短文本", field(t3, "标准答案"), ANS_SHORT)
            check_text("P3 展开后 模型输出 = 短文本", field(t3, "模型输出"), OUT_SHORT)

            # -------------------------------------------------------- [8] 空问题
            w("")
            w("■ [8] 空问题 + 无标准答案")
            check_text("P4 折叠态 空问题仍显示「（空问题）」", f4["q"], "（空问题）")
            t4 = c.ev_json(JS_TOGGLE % 3, "toggle_p4")
            chk("P4 无标准答案的那题不渲染「标准答案」行", field(t4, "标准答案") is None,
                "行=%s" % [(x.get("k"), (x.get("v") or "")[:12]) for x in (t4.get("lines") or [])])
            check_text("P4 模型输出 = 全文", field(t4, "模型输出"), OUT_LONG, tail_required="【O尾】")

            # -------------------------------------------------------- [9] 无展开入口
            w("")
            w("■ [9] 无展开入口（标准答案与模型输出都为空）→ 同样截 30 字，但没有展开按钮")
            chk("P5 没有展开按钮", f5.get("hasToggle") is False, "%s" % f5)
            chk("P5 折叠态 .q-more 不存在", f5.get("moreHidden") is None, "%s" % f5)
            check_text("P5 问题 仍截到 30 字 + 「…」（一律截）",
                       f5["q"], Q_NOEXP[:THUMB_MAX] + ELLIPSIS, tail_forbidden="【B尾】")
            chk("P5 data-full 里仍留着全文（将来若给它加展开入口可直接回全文）",
                f5.get("full") == Q_NOEXP, "len(full)=%s" % len(f5.get("full") or ""))
            chk("P5 点不出展开（无 [data-qtoggle]）",
                c.ev_json(JS_TOGGLE % 4, "toggle_p5").get("err") == "__NOTOG__")

        # ------------------------------------------------------------ [10] JS 异常
        w("")
        w("■ [10] JS 异常")
        errs = fold.get("errors") or []
        w("    页面 error：%s" % errs)
        chk("无页面 JS 异常", not errs, "%s" % errs)

    finally:
        try:
            if edge:
                edge.terminate()
        except Exception:
            pass
        try:
            app.terminate()
            app.wait(timeout=10)
        except Exception:
            try:
                app.kill()
            except Exception:
                pass
        try:
            app_log.close()
        except Exception:
            pass

    # ------------------------------------------------------------ [11] 收尾
    w("")
    w("■ [11] 收尾：库表计数回到基线（本页不写库）")
    after = {t: db_count(t) for t in tables}
    for t in tables:
        chk("%s 回到基线" % t, after[t] == before[t], "%d -> %d" % (before[t], after[t]))

    w("")
    w("=" * 78)
    w("结果：%s" % ("全部通过" if not FAILS else "失败项 -> " + "; ".join(FAILS)))
    w("=" * 78)

    text = buf.getvalue()
    with open(OUT, "w", encoding="utf-8") as fp:
        fp.write(text)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print(text)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
