# -*- coding: utf-8 -*-
"""PromptDeck（Django 版）· 「提示词测试」页真实浏览器端到端校验（第二次迭代）。

只盯本轮新做的那条链路，全程用真实 Edge + CDP 操作 DOM，不依赖 playwright：

    [1] 左栏「选择提示词方案」下拉的方案来自后端 /api/tp
        （库里新建一条临时方案 → 刷新页面 → 下拉里出现、左栏回显模型与提示词）
    [2] 点「开始调试」→ 每道题都真发一次 POST /api/chat
        （用选定方案的 模型 / API_key / 系统提示词，题目当用户提示词）
        →「测试反馈答案」列（= 模型答案列）显示模型返回的原文
    [3] 再逐题走一次 POST /api/judge →「判断」列写 AI 判定
        （题目没标准答案的那道不发判定请求，判定留空 = 未判定）
    [4] 统计标签栏按原型口径出数（准确率 = 最终判定为「对」的条数 ÷ 总题数）
    [5] 人工改判一道题 → 统计里的「人工矫正错误数量」+1
    [6] 点「保存结果」→ 落库 tst_rec（一条汇总）+ tst_rec_dtl（逐题明细），
        汇总的四个数字由服务端按明细现算，与页面显示一致
    [7] 全程无 JS 异常
    [8] 收尾：本次写入的方案 / 供应商 / 测试记录全部清掉，基础数据回到基线

判定模型固定走 DeepSeek（https://api.deepseek.com / deepseek-v4-pro），
为了让校验可重复，本脚本**自己起一个只监听 127.0.0.1 的 OpenAI 兼容 mock**，
并用 PD_JUDGE_BASE_URL 把**它自己起的那个应用实例**的判定地址指到 mock 上。

前置：
    1. pip install websocket-client；本机装有 Edge
    2. 已跑过 build_static.py（static/prompt_test.html 是新版）
    3. 不需要外部应用已启动 —— 本脚本自己在 8503 起一个实例

结果写 _ui_pt_check.txt；退出码 0 = 全部通过，1 = 有失败项。
"""
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import websocket  # websocket-client
except ImportError:
    print("跳过：未安装 websocket-client（pip install websocket-client）")
    sys.exit(0)

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "_ui_pt_check.txt")
DB_PATH = os.environ.get("PROMPTDECK_DB") or os.path.join(HERE, "data", "prompt_test_DB_twe.db")

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
CDP_PORT = 9351
MOCK_PORT = 8792
APP_PORT = 8503
MOCK_BASE = "http://127.0.0.1:%d/v1" % MOCK_PORT
JUDGE_MODEL = "deepseek-v4-pro"

buf = io.StringIO()
FAILS = []
events = []


def w(s=""):
    buf.write(str(s) + "\n")


def chk(label, ok, detail=""):
    w("  [%s] %s%s" % ("PASS" if ok else "FAIL", label, ("  " + str(detail)) if detail else ""))
    if not ok:
        FAILS.append(label)


# ==================================================================== mock 模型服务
MOCK_REQS = []


def _norm(s):
    return "".join(str(s or "").split()).lower()


class MockHandler(BaseHTTPRequestHandler):
    """OpenAI 兼容的最小实现：

    · 请求来自「开始调试」→ 返回固定可预期的回答：`模型回答：<题目原文>`
      （这样标准答案写成同样的串就是「一致」，写成别的就是「不一致」）
    · 请求来自「AI 判定」→ 按 mock 自己的朴素口径回 {"same": true|false}
      （判定模型返回 JSON，正是 core.chat.parse_judge 要解的形状）
    """

    protocol_version = "HTTP/1.1"

    def log_message(self, *a):        # 别往控制台刷日志
        pass

    def _send(self, obj, code=200):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception:
            body = {}

        msgs = body.get("messages") or []
        sys_txt = ""
        user_txt = ""
        for m in msgs:
            if m.get("role") == "system" and not sys_txt:
                sys_txt = m.get("content") or ""
            if m.get("role") == "user":
                user_txt = m.get("content") or ""

        MOCK_REQS.append({
            "path": self.path,
            "auth": self.headers.get("Authorization") or "",
            "model": body.get("model") or "",
            "stream": body.get("stream"),
            "system": sys_txt,
            "user": user_txt,
            "n_messages": len(msgs),
        })

        if "答案一致性判定器" in sys_txt:
            std, out = "", ""
            if "【模型答案】" in user_txt:
                left, out = user_txt.split("【模型答案】", 1)
                std = left.replace("【标准答案】", "").strip()
                out = out.strip()
            same = bool(std) and (_norm(std) in _norm(out) or _norm(out) in _norm(std)
                                  or _norm(std) == _norm(out))
            text = json.dumps({"same": same})
        else:
            text = "模型回答：" + user_txt

        self._send({
            "id": "mock", "object": "chat.completion", "model": body.get("model") or "mock",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
        })


def start_mock():
    srv = ThreadingHTTPServer(("127.0.0.1", MOCK_PORT), MockHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


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


# ==================================================================== 页面探针
JS_INJECT_TESTS = """(function(){
  var list = %s;
  try{ localStorage.setItem('pd_prompt_tests', JSON.stringify(list)); }catch(e){ return 'ERR:' + e; }
  return JSON.stringify({ n: list.length });
})()"""

JS_WAIT_SCHEMES = """JSON.stringify({
  n: document.querySelectorAll('#comboScheme .combo-item').length,
  empty: (document.querySelector('#comboScheme .combo-empty')||{}).textContent || ''
})"""

JS_PICK_SCHEME = """(function(){
  var items = document.querySelectorAll('#comboScheme .combo-item');
  var want = %s, el = null;
  for (var i=0;i<items.length;i++){
    if (items[i].getAttribute('data-value') === want){ el = items[i]; break; }
  }
  if (!el) return JSON.stringify({picked:null, n:items.length});
  el.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true}));
  return JSON.stringify({
    picked:  want,
    input:   document.getElementById('schemeInput').value,
    server:  document.getElementById('scServer').textContent.trim(),
    base:    document.getElementById('scBase').textContent.trim(),
    model:   document.getElementById('scModel').textContent.trim(),
    prompt:  document.getElementById('scPrompt').value,
    locked:  document.getElementById('testPanel').classList.contains('is-locked'),
    btnOn:   document.getElementById('btnStartDebug').className
  });
})()"""

JS_START = """(function(){
  document.getElementById('btnStartDebug').click();
  return JSON.stringify({
    progHidden: document.getElementById('ptProg').hidden,
    pct:        document.getElementById('ptProgPct').textContent,
    txt:        document.getElementById('ptProgTxt').textContent
  });
})()"""

JS_PROGRESS = """JSON.stringify({
  pct:  document.getElementById('ptProgPct').textContent,
  txt:  document.getElementById('ptProgTxt').textContent,
  width:document.getElementById('ptProgFill').style.width
})"""

JS_ROWS = """(function(){
  function snap(tr){
    var fb = tr.querySelector('.col-feedback');
    var cell = fb ? fb.querySelector('.fb-cell') : null;
    var ai = tr.querySelector('.col-judge .jd-ai');
    var on = tr.querySelector('.col-judge .jd.is-on span');
    return {
      id:    tr.getAttribute('data-id'),
      q:     (tr.querySelector('.col-question')||{}).textContent.trim(),
      ans:   (tr.querySelector('.col-answer')||{}).textContent.trim(),
      fb:    cell ? cell.textContent.trim() : null,
      fbEmpty: cell ? cell.classList.contains('is-empty') : null,
      ai:    ai ? ai.textContent.trim() : null,
      judge: on ? on.textContent.trim() : null
    };
  }
  var out = [];
  var trs = document.querySelectorAll('#tbody tr[data-id]');
  for (var i=0;i<trs.length;i++) out.push(snap(trs[i]));
  return JSON.stringify({
    rows: out,
    statTotal: document.getElementById('statTotal').textContent,
    statAcc:   document.getElementById('statAcc').textContent,
    statRight: document.getElementById('statRight').textContent,
    statWrong: document.getElementById('statWrong').textContent,
    statFix:   document.getElementById('statFix').textContent
  });
})()"""

JS_MANUAL_FIX = """(function(){
  var tr = document.querySelector('#tbody tr[data-id="%s"]');
  if (!tr) return JSON.stringify({err:'no row'});
  var r = tr.querySelector('.jd-radio[data-judge="%s"]');
  if (!r) return JSON.stringify({err:'no radio'});
  r.click();
  var on = tr.querySelector('.col-judge .jd.is-on span');
  return JSON.stringify({
    checked: r.checked,
    judge:   on ? on.textContent.trim() : null,
    statFix: document.getElementById('statFix').textContent,
    statAcc: document.getElementById('statAcc').textContent
  });
})()"""

JS_SAVE_RESULT = """(function(){
  document.getElementById('btnSaveResult').click();
  return 'clicked';
})()"""

JS_ERRORS = "JSON.stringify(window.__pdErrors || [])"


# ==================================================================== 库
def db_rows(sql, args=()):
    with sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute(sql, args)]


def db_scalar(sql, args=()):
    with sqlite3.connect(DB_PATH) as c:
        return c.execute(sql, args).fetchone()[0]


def db_count(table):
    return db_scalar("SELECT COUNT(*) FROM %s" % table)


def mask_key(v):
    """报告里只回显密钥的遮掩形态（全文会落 _ui_pt_check.txt，别把明文写进去）。"""
    k = str(v or "").replace("Bearer ", "")
    if len(k) <= 8:
        return "•" * max(len(k), 4)
    return k[:3] + "•" * 8 + k[-4:]


def api(base, method, path, body=None, timeout=30):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    req = urllib.request.Request(base.rstrip("/") + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
    try:
        return json.loads(raw)
    except ValueError:
        return raw


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


def main():
    if not os.path.isdir(os.path.join(HERE, "static")):
        w("跳过：还没跑 build_static.py")
        return 0
    if not os.path.exists(EDGE):
        w("跳过：未找到 Edge（%s）" % EDGE)
        return 0

    stamp = str(int(time.time()))[-6:]
    srv_name = "UI校验供应商-" + stamp
    mdl_name = "ui-check-model-" + stamp
    plan_name = "UI校验调试方案-" + stamp
    scheme_key = "sk-ui-check-" + stamp
    sys_prompt = "你是 UI 校验用的助手，只回答被问到的问题。"

    base = "http://127.0.0.1:%d" % APP_PORT

    w("=" * 78)
    w("PromptDeck · 「提示词测试」页端到端校验（真实 Edge + CDP）")
    w("库文件：%s" % DB_PATH)
    w("应用：%s（本脚本自起）  判定 mock：%s" % (base, MOCK_BASE))
    w("=" * 78)

    srv0, mdl0, plan0 = db_count("srv"), db_count("mdl"), db_count("tst_plan")
    rec0, dtl0 = db_count("tst_rec"), db_count("tst_rec_dtl")

    mock = start_mock()
    app_log = open(os.path.join(HERE, "_ui_pt_django.log"), "w", encoding="utf-8")
    env = dict(os.environ)
    env["PD_JUDGE_BASE_URL"] = MOCK_BASE          # 判定地址指到本机 mock
    env["PD_JUDGE_MODEL"] = JUDGE_MODEL
    app = subprocess.Popen([sys.executable, "manage.py", "runserver",
                            "127.0.0.1:%d" % APP_PORT, "--noreload"],
                           cwd=HERE, env=env, stdout=app_log, stderr=subprocess.STDOUT)

    tmp = tempfile.mkdtemp(prefix="pd_ui_pt")
    edge = None
    plan_id = srv_id = rec_id = None
    try:
        chk("自起的应用实例已就绪", wait_http("%s/api/health" % base), base)

        # ------------------------------------------------------------ 造一条临时方案
        w("")
        w("■ [1] 方案下拉的真源 = 后端 /api/tp")
        res = api(base, "POST", "/api/tp", {
            "name": plan_name, "server": srv_name, "base": MOCK_BASE,
            "model": mdl_name, "modelType": "文本生成",
            "sys": sys_prompt, "user": ["占位用户提示词"], "multi": False,
            "apiKey": scheme_key,
        })
        plan_id = res.get("id") if isinstance(res, dict) else None
        chk("POST /api/tp 造出临时方案", bool(plan_id) and res.get("ok"), "%s" % res)
        row = db_rows("SELECT id, srv_id FROM tst_plan WHERE id=?", (plan_id,))
        srv_id = row[0]["srv_id"] if row else None
        chk("方案已落 tst_plan（带外键 srv_id）", bool(srv_id), "srv_id=%s" % srv_id)

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

        # ------------------------------------------------------------ 打开页面
        page = "%s/prompt_test.html" % base
        c.go(page, 5)
        w("    页面：%s" % page)

        # 注入三道题：Q1 有标准答案且与 mock 回答一致 / Q2 不一致 / Q3 没有标准答案
        q1, q2, q3 = "问题一：一句话说明用途", "问题二：翻译一句话", "问题三：还没有标准答案"
        ans1 = "模型回答：" + q1
        ans2 = "完全不相干的标准答案"
        tests = [
            {"id": 1, "q": q1, "ans": ans1, "fb": "", "ai": "", "judge": ""},
            {"id": 2, "q": q2, "ans": ans2, "fb": "", "ai": "", "judge": ""},
            {"id": 3, "q": q3, "ans": "", "fb": "", "ai": "", "judge": ""},
        ]
        c.ev_json(JS_INJECT_TESTS % json.dumps(tests, ensure_ascii=False), "注入测试题")
        c.go(page, 5)                                  # 重载，让页面读到注入的题

        got = None
        for _ in range(40):
            got = c.ev_json(JS_WAIT_SCHEMES, "等方案下拉")
            if got.get("n"):
                break
            time.sleep(0.5)
        chk("左栏方案下拉已由 /api/tp 渲染出选项", bool(got and got.get("n")),
            "选项数=%s 空态=%r" % (got.get("n") if got else None,
                                   got.get("empty") if got else None))

        pick = c.ev_json(JS_PICK_SCHEME % json.dumps(plan_name), "选中方案")
        chk("能在下拉里选中刚建的方案", pick.get("picked") == plan_name, "%s" % pick.get("picked"))
        chk("左栏回显模型 = 方案里的模型", pick.get("model", "").startswith(mdl_name),
            "%s" % pick.get("model"))
        chk("左栏回显提示词含系统提示词", sys_prompt in (pick.get("prompt") or ""),
            "%s" % (pick.get("prompt") or "")[:60].replace("\n", " "))
        chk("选好方案后右栏解锁（不再置灰）", pick.get("locked") is False,
            "is-locked=%s" % pick.get("locked"))

        # ------------------------------------------------------------ 开始调试
        w("")
        w("■ [2][3] 开始调试：逐题真发 /api/chat + /api/judge")
        MOCK_REQS.clear()
        st = c.ev_json(JS_START, "点开始调试")
        chk("点「开始调试」后进度条出现（仍在跑）", st.get("progHidden") is False,
            "%s / %s" % (st.get("txt"), st.get("pct")))

        done = None
        for _ in range(160):
            done = c.ev_json(JS_PROGRESS, "等调试完成")
            if "100%" in (done.get("pct") or ""):
                break
            time.sleep(0.5)
        chk("进度条走到 100%", "100%" in (done.get("pct") or ""), "%s" % done.get("txt"))
        chk("进度条文案显示 3/3", "3/3" in (done.get("txt") or ""), "%s" % done.get("txt"))

        snap = c.ev_json(JS_ROWS, "读结果行")
        rows = {r["id"]: r for r in snap["rows"]}
        chk("列表 3 行都在", len(snap["rows"]) == 3, "行数=%d" % len(snap["rows"]))

        chat_reqs = [r for r in MOCK_REQS if "答案一致性判定器" not in r["system"]]
        judge_reqs = [r for r in MOCK_REQS if "答案一致性判定器" in r["system"]]
        chk("每道题都真发了一次模型调用（3 次 /api/chat）", len(chat_reqs) == 3,
            "实测 %d 次：%s" % (len(chat_reqs), [r["path"] for r in chat_reqs]))
        chk("模型调用打到 {base_url}/chat/completions", all(
            r["path"] == "/v1/chat/completions" for r in chat_reqs),
            "%s" % sorted(set(r["path"] for r in chat_reqs)))
        chk("模型调用带的是方案的 API_key", all(
            r["auth"] == "Bearer " + scheme_key for r in chat_reqs),
            "%s" % sorted(set(r["auth"] for r in chat_reqs)))
        chk("模型调用带的是方案的模型名", all(r["model"] == mdl_name for r in chat_reqs),
            "%s" % sorted(set(r["model"] for r in chat_reqs)))
        chk("模型调用是 stream=false 的同步请求", all(r["stream"] is False for r in chat_reqs))
        chk("模型调用带上了方案的系统提示词 + 题目当用户提示词",
            all(len(r["system"]) > 0 and r["user"] for r in chat_reqs)
            and all(r["system"] == sys_prompt for r in chat_reqs)
            and sorted(r["user"] for r in chat_reqs) == sorted([q1, q2, q3]),
            "system=%r" % (chat_reqs[0]["system"] if chat_reqs else None))
        chk("每道题一条消息（system + user），没有多带上下文",
            all(r["n_messages"] == 2 for r in chat_reqs),
            "%s" % sorted(set(r["n_messages"] for r in chat_reqs)))

        chk("模型答案列显示模型返回的原文",
            rows["1"]["fb"] == "模型回答：" + q1 and rows["2"]["fb"] == "模型回答：" + q2,
            "1=%r / 2=%r" % (rows["1"]["fb"], rows["2"]["fb"]))
        chk("模型答案列不再是「暂无测试反馈」占位",
            not any(r["fbEmpty"] for r in snap["rows"]),
            "%s" % [(r["id"], r["fbEmpty"]) for r in snap["rows"]])

        chk("有标准答案的两道题各发了一次判定请求（没标准答案的不发）",
            len(judge_reqs) == 2, "实测 %d 次" % len(judge_reqs))
        chk("判定请求里带上了标准答案与模型答案",
            all("【标准答案】" in r["user"] and "【模型答案】" in r["user"] for r in judge_reqs))
        chk("判定请求用的是判定模型（deepseek-v4-pro）",
            all(r["model"] == JUDGE_MODEL for r in judge_reqs),
            "%s" % sorted(set(r["model"] for r in judge_reqs)))
        chk("判定请求带的是库里的判定密钥（不是方案密钥）",
            all(r["auth"] and r["auth"] != "Bearer " + scheme_key for r in judge_reqs),
            "%s" % sorted(set(mask_key(r["auth"]) for r in judge_reqs)))

        chk("判断列：答案一致的判「对」", "对" in (rows["1"]["ai"] or ""), "%s" % rows["1"]["ai"])
        chk("判断列：答案不一致的判「错」", "错" in (rows["2"]["ai"] or ""), "%s" % rows["2"]["ai"])
        chk("判断列：没有标准答案的留「未判定」", "未判定" in (rows["3"]["ai"] or ""),
            "%s" % rows["3"]["ai"])

        chk("统计：总数量 = 3", snap["statTotal"] == "3", snap["statTotal"])
        chk("统计：AI 正确 1 / 错误 1", snap["statRight"] == "1" and snap["statWrong"] == "1",
            "right=%s wrong=%s" % (snap["statRight"], snap["statWrong"]))
        chk("统计：准确率 = 1/3 -> 33%", snap["statAcc"] == "33%", snap["statAcc"])

        # ------------------------------------------------------------ 人工改判
        w("")
        w("■ [5] 人工改判一道题")
        fix = c.ev_json(JS_MANUAL_FIX % ("2", "right"), "人工改判")
        chk("改判后该行单选落在「对」上", fix.get("checked") is True and fix.get("judge") == "对",
            "%s" % fix)
        chk("统计：人工矫正错误数量 +1", fix.get("statFix") == "1", "statFix=%s" % fix.get("statFix"))
        chk("统计：准确率随人工改判变成 67%", fix.get("statAcc") == "67%", fix.get("statAcc"))

        # ------------------------------------------------------------ 保存结果
        w("")
        w("■ [6] 保存结果 -> tst_rec + tst_rec_dtl")
        c.ev(JS_SAVE_RESULT)
        time.sleep(3)

        recs = db_rows("SELECT * FROM tst_rec WHERE plan_id=? ORDER BY id", (plan_id,))
        chk("tst_rec 写入 1 条记录", len(recs) == 1, "实测 %d 条" % len(recs))
        if recs:
            rec = recs[0]
            rec_id = rec["id"]
            chk("tst_rec.plan_id 指向本次调试的方案", rec["plan_id"] == plan_id,
                "plan_id=%s" % rec["plan_id"])
            chk("tst_rec.q_cnt = 3", rec["q_cnt"] == 3, "q_cnt=%s" % rec["q_cnt"])
            chk("tst_rec.err_cnt = 1（AI 判错 1 道）", rec["err_cnt"] == 1,
                "err_cnt=%s" % rec["err_cnt"])
            chk("tst_rec.fix_cnt = 1（人工矫正 1 道）", rec["fix_cnt"] == 1,
                "fix_cnt=%s" % rec["fix_cnt"])
            chk("tst_rec.acc_rate = 66.67（与页面 67% 同口径）",
                abs(float(rec["acc_rate"]) - 66.67) < 0.01, "acc_rate=%s" % rec["acc_rate"])
            chk("tst_rec.api_key_id 指向该方案自己的那把密钥",
                db_scalar("SELECT COUNT(*) FROM tst_api_key WHERE id=? AND plan_id=?",
                          (rec["api_key_id"], plan_id)) == 1,
                "api_key_id=%s" % rec["api_key_id"])
            chk("tst_rec.test_time 已写入", bool(rec["test_time"]), "%s" % rec["test_time"])

        dtls = db_rows("SELECT * FROM tst_rec_dtl WHERE rec_id=? ORDER BY id", (rec_id,))
        chk("tst_rec_dtl 写入 3 条逐题明细", len(dtls) == 3, "实测 %d 条" % len(dtls))
        if len(dtls) == 3:
            d1, d2, d3 = dtls
            chk("明细 1：问题 / 标准答案 / 模型输出 逐项对上",
                d1["qst"] == q1 and d1["std_ans"] == ans1 and d1["mdl_out"] == "模型回答：" + q1,
                "%s | %s | %s" % (d1["qst"], d1["std_ans"], d1["mdl_out"]))
            chk("明细 1：AI 判定 right / 未人工改判",
                d1["ai_res"] == "right" and d1["is_man_fix"] == 0,
                "%s / %s" % (d1["ai_res"], d1["is_man_fix"]))
            chk("明细 2：AI 判定 wrong 且被人工改判（is_man_fix=1）",
                d2["ai_res"] == "wrong" and d2["is_man_fix"] == 1,
                "%s / %s" % (d2["ai_res"], d2["is_man_fix"]))
            chk("明细 3：没有标准答案 -> 判定为空、未改判",
                d3["std_ans"] == "" and (d3["ai_res"] in (None, "")) and d3["is_man_fix"] == 0,
                "std=%r ai=%r fix=%s" % (d3["std_ans"], d3["ai_res"], d3["is_man_fix"]))
            chk("明细 3：模型输出仍然落了库", d3["mdl_out"] == "模型回答：" + q3,
                "%s" % d3["mdl_out"])

        lst = api(base, "GET", "/api/rec")
        chk("GET /api/rec 能读回这条记录", isinstance(lst, list) and any(
            r.get("id") == rec_id for r in lst), "条数=%s" % (len(lst) if isinstance(lst, list) else -1))

        # ------------------------------------------------------------ JS 异常
        w("")
        w("■ [7] JS 异常")
        errs = c.ev(JS_ERRORS)
        try:
            errs = json.loads(errs) if isinstance(errs, str) else []
        except ValueError:
            errs = [str(errs)]
        exc = [e for e in events if e.get("method") == "Runtime.exceptionThrown"]
        w("    页面 error：%s；CDP 捕获异常：%d 条" % (errs, len(exc)))
        chk("无页面 JS 异常", not errs, errs)

    finally:
        # ------------------------------------------------------------ 收尾
        try:
            if rec_id:
                api(base, "DELETE", "/api/rec", {"ids": [rec_id]})
            elif plan_id:
                for r in db_rows("SELECT id FROM tst_rec WHERE plan_id=?", (plan_id,)):
                    api(base, "DELETE", "/api/rec", {"ids": [r["id"]]})
        except Exception:
            pass
        try:
            if plan_id:
                api(base, "DELETE", "/api/tp", {"ids": [plan_id]})
        except Exception:
            pass
        try:
            if srv_id:
                api(base, "DELETE", "/api/srv", {"ids": [srv_id]})
        except Exception:
            pass

        try:
            if edge:
                edge.terminate()
        except Exception:
            pass
        try:
            app.terminate()
        except Exception:
            pass
        try:
            app_log.close()
        except Exception:
            pass
        try:
            mock.shutdown()
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)

    w("")
    w("■ [8] 收尾：库回到基线")
    chk("srv 回到基线", db_count("srv") == srv0, "%d -> %d" % (srv0, db_count("srv")))
    chk("mdl 回到基线", db_count("mdl") == mdl0, "%d -> %d" % (mdl0, db_count("mdl")))
    chk("tst_plan 回到基线", db_count("tst_plan") == plan0, "%d -> %d" % (plan0, db_count("tst_plan")))
    chk("tst_rec 回到基线", db_count("tst_rec") == rec0, "%d -> %d" % (rec0, db_count("tst_rec")))
    chk("tst_rec_dtl 回到基线", db_count("tst_rec_dtl") == dtl0,
        "%d -> %d" % (dtl0, db_count("tst_rec_dtl")))
    chk("判定密钥仍在库里（本轮没动它）",
        db_scalar("SELECT COUNT(*) FROM tst_api_key WHERE key_name=? AND plan_id IS NULL",
                  ("AI判定（DeepSeek）",)) == 1)

    w("")
    w("=" * 78)
    w("结果：%s" % ("全部通过" if not FAILS else "失败项 -> " + "; ".join(FAILS)))
    open(OUT, "w", encoding="utf-8").write(buf.getvalue())
    print(buf.getvalue())
    return 0 if not FAILS else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
