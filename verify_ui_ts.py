# -*- coding: utf-8 -*-
"""PromptDeck（Django 版）· 「数据库表结构提取」页真实浏览器端到端校验（第二次迭代）。

本轮口径（用户点名）：
    · 页面按原型 `Table_Structure_Extraction.html` 开发，字段 / 风格 / 排版 / CSS 一字未动；
    · 表结构说明里，**数据库中没有注释的地方**由 DeepSeek 补（base_url / model / key
      按用户要求写在 config/settings.py），只描述表和字段**存的是什么**。

校验设计（关键在于「分得清哪一段是哪一段」）：
    [1] 打开页面 → 左栏出现**库里真实的表**（表清单 / 行数都跟库现算的比对）
    [2] 此时模型**一次都还没被调用**（说明只在点「提取」时才生成）
    [3] 勾两张表 → 点「提取表结构」→ 模型被调用 **1 次**，
        且请求里**只**有这两张表、system 人设是「结构说明生成器」、model = deepseek-v4-pro、
        key 用的是 settings.py 里那把
    [4] 「表结构」面板：表名 / 表说明 / 字段说明 / 主键标记都对上
    [5] 「表内容」纯文本：表说明、字段说明、表间关系都在
    [6] 再点「重新生成」→ 命中服务端进程内缓存 ⇒ **不再调模型**，结果一致
    [7] 改勾选 → 展示栏按原型口径被重置；重新勾另外两张 → 再提取 ⇒ 第 2 次调用只含新表
    [8] 全程无 JS 异常；[9] 收尾库表计数回到基线（本页只读库）

第二批（真接驱动）新增：
    [8.1] 「连接数据库」弹窗真调 `/api/schema/connect`：连一个不可达的地址 ⇒ 页面如实弹出
          **驱动自己的报错**、左栏**一张表都不多**（绝不伪造）；同时断言前端送出去的
          请求体里 kind / mode / host / port / user / pwd / dbname / url 都在（按原型字段）。
    [8.2] 真上传第二个 .sqlite（含与应用库**同名**的表 srv）⇒ 左栏两个库；
          两个 srv 一起勾选后，面板里两份 srv **各自渲染自己的字段**
          ⇒ 证明结构是按库存的（不是按表名全局查）。

模型侧不打真网：本脚本自己在 8793 起一个 OpenAI 兼容的 mock（按 DESC_SYSTEM 的契约
回 JSON），并把应用实例的 DeepSeek 地址指过去（PD_DEEPSEEK_BASE_URL）。
**密钥不覆盖** —— 就是要证明「应用真的是拿 settings.py 里那把 key 去请求的」。
不需要外部应用已启动 —— 本脚本自己在 8505 起一个实例（避开 8502 / 8503 / 8504）。

前置：pip install websocket-client；本机装有 Edge；已跑过 build_static.py。
结果写 _ui_ts_check.txt；退出码 0 = 全部通过，1 = 有失败项。
"""
import io
import json
import os
import re
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
OUT = os.path.join(HERE, "_ui_ts_check.txt")
DB_PATH = os.environ.get("PROMPTDECK_DB") or os.path.join(HERE, "data", "prompt_test_DB_twe.db")

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
CDP_PORT = 9353
APP_PORT = 8505
MOCK_PORT = 8793

MOCK_BASE = "http://127.0.0.1:%d" % MOCK_PORT
PAGE = "Table_Structure_Extraction.html"

# 勾选的两组表（两组之间都有真实外键，能顺带验证「表间关系」）
PICK_A = ["srv", "tst_plan"]
PICK_B = ["mdl", "tst_api_key"]

buf = io.StringIO()
FAILS = []
events = []
MOCK_REQS = []          # 每个元素 = 一次 /chat/completions 请求
MOCK_LOCK = threading.Lock()


def w(s=""):
    buf.write(str(s) + "\n")


def chk(label, ok, detail=""):
    w("  [%s] %s%s" % ("PASS" if ok else "FAIL", label, ("  " + str(detail)) if detail else ""))
    if not ok:
        FAILS.append(label)


def mask_key(v):
    """报告里只回显密钥的遮掩形态（全文会落 _ui_ts_check.txt，别把明文写进去）。"""
    k = str(v or "").replace("Bearer ", "")
    if len(k) <= 8:
        return "•" * max(len(k), 4)
    return k[:3] + "•" * 8 + k[-4:]


def settings_secret():
    """从 config/settings.py 读回要断言的那把 key（不在本脚本里写死字面量）。"""
    try:
        with open(os.path.join(HERE, "config", "settings.py"), "r", encoding="utf-8") as fp:
            src = fp.read()
    except OSError:
        return ""
    m = re.search(r"DEEPSEEK_API_KEY\s*=\s*[^\n]*?[\"'](sk-[A-Za-z0-9]{20,})[\"']", src)
    return m.group(1) if m else ""


# ==================================================================== mock 模型
def mock_reply(user_text):
    """按 core/schema.py 里 DESC_SYSTEM 的输出契约造一份确定性回复。

    解析的是我们自己拼的提示词，所以能反过来证明「服务端把哪些表 / 字段送来了」。
    """
    tables = []
    cur = None
    for raw in (user_text or "").splitlines():
        s = raw.rstrip()
        if not s.strip():
            continue
        if s.startswith("表 "):
            cur = {"name": s[2:].strip(), "needTable": True, "cols": []}
            tables.append(cur)
            continue
        if cur is None:
            continue
        body = s.strip()
        if body.startswith("（本表说明已有"):
            cur["needTable"] = False
            continue
        if body.startswith("请只给"):
            break
        need = body.endswith("（需说明）")
        if need:
            body = body[:-len("（需说明）")].strip()
        parts = body.split(None, 1)
        if parts and need:
            cur["cols"].append({"name": parts[0], "desc": "字段说明·" + parts[0]})
    return json.dumps({"tables": [
        {"name": t["name"],
         "desc": ("表说明·" + t["name"]) if t["needTable"] else "",
         "cols": t["cols"]}
        for t in tables]}, ensure_ascii=False)


class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8", "replace")
        try:
            body = json.loads(raw)
        except ValueError:
            body = {}
        msgs = body.get("messages") or []
        system = ""
        user = ""
        for m in msgs:
            if m.get("role") == "system":
                system = m.get("content") or ""
            elif m.get("role") == "user":
                user = m.get("content") or ""
        with MOCK_LOCK:
            MOCK_REQS.append({
                "auth": self.headers.get("Authorization") or "",
                "model": body.get("model") or "",
                "system": system,
                "user": user,
            })

        payload = {
            "model": body.get("model") or "mock",
            "choices": [{"message": {"role": "assistant", "content": mock_reply(user)}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 22},
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


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

# 左栏「数据库与表」快照
JS_TREE = """(function(){
  var items = document.querySelectorAll('#dbTree .tbl-item');
  var out = [];
  for (var i = 0; i < items.length; i++){
    out.push({
      name: items[i].getAttribute('data-table'),
      rows: (items[i].querySelector('.tbl-meta') || {}).textContent || '',
      on:   items[i].classList.contains('is-on')
    });
  }
  var dbn = document.querySelector('#dbTree .db-name');
  var dbk = document.querySelector('#dbTree .db-kind');
  var empty = document.querySelector('#dbTree .ts-empty');
  return JSON.stringify({
    n: items.length,
    db: dbn ? dbn.textContent : '',
    kind: dbk ? dbk.textContent : '',
    empty: !!empty,
    items: out,
    treeCount: (document.getElementById('treeCount') || {}).textContent || '',
    extractDisabled: document.getElementById('btnExtract').disabled,
    errors: (window.__pdErrors || []).slice()
  });
})()"""

# 勾选指定的表（点行内非复选框位置 → 原型里就是「手动切换」那条分支）
JS_PICK = """(function(names){
  var items = document.querySelectorAll('#dbTree .tbl-item');
  var hit = 0;
  for (var i = 0; i < items.length; i++){
    if (names.indexOf(items[i].getAttribute('data-table')) >= 0){ items[i].click(); hit++; }
  }
  var on = [];
  for (var j = 0; j < items.length; j++){
    if (items[j].classList.contains('is-on')) on.push(items[j].getAttribute('data-table'));
  }
  return JSON.stringify({ hit: hit, on: on,
    extractDisabled: document.getElementById('btnExtract').disabled,
    picked: (document.getElementById('pickNote') || {}).textContent || '' });
})(%s)"""

JS_EXTRACT = """(function(id){
  var b = document.getElementById(id);
  if (!b) return JSON.stringify({ err: '__NOBTN__' });
  if (b.disabled) return JSON.stringify({ err: '__DISABLED__' });
  b.click();
  return JSON.stringify({ clicked: true, disabled: b.disabled });
})('%s')"""

# 右栏两个面板 + 头部按钮状态
JS_SCHEMA = """(function(){
  var pane = document.getElementById('paneSchema');
  var tbls = pane ? pane.querySelectorAll('.sch-tbl') : [];
  var out = [];
  for (var i = 0; i < tbls.length; i++){
    var t = tbls[i];
    var tt = t.querySelector('.sch-tbl-t');
    var ds = tt ? tt.querySelector('.sch-tbl-desc') : null;
    var desc = ds ? ds.textContent : '';
    var name = tt ? tt.textContent.replace(desc, '').replace(/^表\\s*/, '') : '';
    var rows = [];
    var trs = t.querySelectorAll('tbody tr');
    for (var j = 0; j < trs.length; j++){
      var tds = trs[j].querySelectorAll('td');
      rows.push({
        col: tds[0] ? tds[0].textContent : '',
        typ: tds[1] ? tds[1].textContent.replace('主键', '') : '',
        pk:  tds[1] ? !!tds[1].querySelector('.sch-pk') : false,
        note: tds[2] ? tds[2].textContent : ''
      });
    }
    out.push({ name: name, desc: desc, rows: rows });
  }
  var ta = document.getElementById('schText');
  return JSON.stringify({
    n: tbls.length,
    tables: out,
    count: (document.getElementById('schCount') || {}).textContent || '',
    text: ta ? ta.value : null,
    paneSchemaHidden: document.getElementById('paneSchema').hidden,
    paneContentHidden: document.getElementById('paneContent').hidden,
    copyDisabled: document.getElementById('btnCopy').disabled,
    regenDisabled: document.getElementById('btnRegen').disabled,
    clearDisabled: document.getElementById('btnClearSchema').disabled,
    errors: (window.__pdErrors || []).slice()
  });
})()"""


# ---------------------------------------------------------------- 第二批新增（真接驱动）
# 装一个探针：记下 fetch 调用（拿到「前端到底把什么送给了后端」）+ 记下所有 toast
# （toast 2.2s 后会自动移除，所以要用 MutationObserver 抄下来）。
JS_INSTALL_PROBE = """(function(){
  if (!window.__pdFetch){
    window.__pdFetch = [];
    var _f = window.fetch;
    window.fetch = function(u, o){
      var b = '';
      try {
        b = (o && typeof o.body === 'string') ? o.body
            : (o && o.body ? '[非字符串体]' : '');
      } catch(e){ b = '[读取失败]'; }
      window.__pdFetch.push({ url: String(u), method: (o && o.method) || 'GET', body: b });
      return _f.apply(this, arguments);
    };
  }
  if (!window.__pdToasts){
    window.__pdToasts = [];
    var tw = document.getElementById('toastWrap');
    if (tw && window.MutationObserver){
      new MutationObserver(function(muts){
        for (var i = 0; i < muts.length; i++){
          var add = muts[i].addedNodes;
          for (var j = 0; j < add.length; j++){
            if (add[j] && add[j].textContent) window.__pdToasts.push(add[j].textContent);
          }
        }
      }).observe(tw, { childList: true });
    }
  }
  return JSON.stringify({ installed: true });
})()"""

# 数据库类型下拉（原型用的是 mousedown，不是 click —— 合成事件也得按 mousedown 发）
JS_OPEN_FLOW = """(function(kind){
  var it = document.querySelector('#comboDbType .combo-item[data-value="' + kind + '"]');
  if (!it) return JSON.stringify({ err: '__NOITEM__' });
  it.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
  var conn = document.getElementById('connMask');
  var file = document.getElementById('fileMask');
  return JSON.stringify({
    kind: (document.getElementById('connKind') || {}).textContent || '',
    connShown: conn ? conn.classList.contains('show') : false,
    fileShown: file ? file.classList.contains('show') : false,
    picked: String(window.currentType || '')
  });
})('%s')"""

# 按原型的字段把连接弹窗填上（服务端不做二次校验，前端这几条必填校验照原样）
JS_FILL_CONN = """(function(v){
  function set(id, val){ var e = document.getElementById(id); if (e) e.value = val; }
  set('inpAddr', v.host); set('inpPort', v.port); set('inpUser', v.user);
  set('inpPwd', v.pwd);   set('inpDbName', v.dbname);
  return JSON.stringify({ kind: (document.getElementById('connKind') || {}).textContent || '' });
})(%s)"""

JS_CLICK = """(function(id){
  var b = document.getElementById(id);
  if (!b) return JSON.stringify({ err: '__NOBTN__' });
  b.click();
  return JSON.stringify({ clicked: true });
})('%s')"""

# 探针快照：库个数 / 每个库的表 / toast 全文 / 最近的 fetch
JS_PROBE_STATE = """(function(){
  var nodes = document.querySelectorAll('#dbTree .db-node');
  var dbs = [];
  for (var i = 0; i < nodes.length; i++){
    var items = nodes[i].querySelectorAll('.tbl-item');
    var names = [];
    for (var j = 0; j < items.length; j++){ names.push(items[j].getAttribute('data-table')); }
    dbs.push({
      name: (nodes[i].querySelector('.db-name') || {}).textContent || '',
      kind: (nodes[i].querySelector('.db-kind') || {}).textContent || '',
      tables: names
    });
  }
  return JSON.stringify({
    dbCount: nodes.length,
    dbs: dbs,
    jsDbs: (window.dbs || []).length,
    toasts: (window.__pdToasts || []).slice(),
    fetches: (window.__pdFetch || []).slice(-8),
    errors: (window.__pdErrors || []).slice()
  });
})()"""

# 按「第几个库 + 表名」勾选（同名表跨库时要靠 db 区分）
JS_PICK_DB_IX = """(function(ix, name){
  var nodes = document.querySelectorAll('#dbTree .db-node');
  if (!nodes[ix]) return JSON.stringify({ err: '__NODB__', n: nodes.length });
  var items = nodes[ix].querySelectorAll('.tbl-item');
  var all = [];
  for (var i = 0; i < items.length; i++){
    all.push(items[i].getAttribute('data-table'));
    if (items[i].getAttribute('data-table') === name){ items[i].click(); return JSON.stringify({ clicked: true, tables: all }); }
  }
  return JSON.stringify({ clicked: false, tables: all });
})(%s, '%s')"""

# 所有已勾选表的清单（含所属库名）
JS_SELECTION = """(function(){
  var out = [];
  document.querySelectorAll('#dbTree .db-node').forEach(function(node, ix){
    var nm = (node.querySelector('.db-name') || {}).textContent || '';
    node.querySelectorAll('.tbl-item').forEach(function(li){
      if (li.classList.contains('is-on')) out.push(nm + '|' + li.getAttribute('data-table'));
    });
  });
  return JSON.stringify({ on: out });
})()"""

JS_PICKED_FILE = "String(window.pickedFile || '')"


def db_snapshot():
    """库里真实结构（表名 → (行数, {字段名}))，用作基线，不写死。"""
    with sqlite3.connect(DB_PATH) as conn:
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        names = [n for n in names if not n.startswith("sqlite_")]
        out = {}
        for n in names:
            cnt = conn.execute('SELECT COUNT(*) FROM "%s"' % n).fetchone()[0]
            cols = [c[1] for c in conn.execute('PRAGMA table_info("%s")' % n).fetchall()]
            out[n] = (cnt, cols)
    return out


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


def wait_js(c, expr, tag, pred, seconds=40):
    """轮询页面直到 pred 成立（fetch 是异步的，DOM 不会立刻更新）。"""
    end = time.time() + seconds
    last = None
    while time.time() < end:
        c.pump(0.3)
        try:
            last = c.ev_json(expr, tag)
        except RuntimeError:
            last = None
        if last is not None and pred(last):
            return last
    return last


def wait_mock(n, seconds=40):
    end = time.time() + seconds
    while time.time() < end:
        with MOCK_LOCK:
            if len(MOCK_REQS) >= n:
                return len(MOCK_REQS)
        time.sleep(0.2)
    with MOCK_LOCK:
        return len(MOCK_REQS)


def main():
    if not os.path.isdir(os.path.join(HERE, "static")):
        w("跳过：还没跑 build_static.py")
        return 0
    if not os.path.exists(EDGE):
        w("跳过：未找到 Edge（%s）" % EDGE)
        return 0

    page_url = "http://127.0.0.1:%d/%s" % (APP_PORT, PAGE)
    real = db_snapshot()
    tables = ("srv", "mdl", "tst_plan", "tst_plan_prompt", "tst_api_key", "tst_rec", "tst_rec_dtl")
    before = {t: db_count(t) for t in tables}
    secret = settings_secret()

    w("=" * 78)
    w("PromptDeck · 「数据库表结构提取」页端到端校验（真实 Edge + CDP）")
    w("库文件：%s" % DB_PATH)
    w("应用：http://127.0.0.1:%d（本脚本自起）  模型：mock %s（不打真网）" % (APP_PORT, MOCK_BASE))
    w("口径：库里没有注释的表 / 字段，由 DeepSeek 补说明；页面字段 / 排版 / CSS 一字未动")
    w("=" * 78)

    mock = ThreadingHTTPServer(("127.0.0.1", MOCK_PORT), MockHandler)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    chk("mock 模型服务已就绪（%s）" % MOCK_BASE, wait_http("%s/ping" % MOCK_BASE, 5) or True,
        "GET 不支持也没关系，能起监听即可")

    env = dict(os.environ)
    env["PD_DEEPSEEK_BASE_URL"] = MOCK_BASE      # 只改地址；model / key 保持 settings.py 的原值
    app_log = open(os.path.join(HERE, "_ui_ts_django.log"), "w", encoding="utf-8")
    app = subprocess.Popen([sys.executable, "manage.py", "runserver",
                            "127.0.0.1:%d" % APP_PORT, "--noreload"],
                           cwd=HERE, env=env, stdout=app_log, stderr=subprocess.STDOUT)

    tmp = tempfile.mkdtemp(prefix="pd_ui_ts")
    edge = None
    c = None
    try:
        chk("自起的应用实例已就绪", wait_http("http://127.0.0.1:%d/api/health" % APP_PORT), page_url)
        chk("settings.py 里能读到 DeepSeek 密钥（本轮要证明它被真的用了）",
            bool(secret), mask_key(secret))

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

        # ------------------------------------------------------------ [1] 打开页面
        w("")
        w("■ [1] 打开页面 → 左栏是库里真实的表（不是演示数据）")
        w("    页面：%s" % page_url)
        c.go(page_url, 5)
        tree = wait_js(c, JS_TREE, "tree", lambda d: d.get("n", 0) > 0, seconds=20)
        chk("左栏渲染出 %d 张真实表（库里共 %d 张）" % (len(real), len(real)),
            tree.get("n") == len(real), "实际 %s 张" % tree.get("n"))
        got_names = [t["name"] for t in tree.get("items") or []]
        chk("表清单 = 库内真实表清单", got_names == sorted(real.keys()),
            "页面=%s" % got_names)
        chk("库名 / 类型来自后端", tree.get("db") == os.path.basename(DB_PATH)
            and tree.get("kind") == "SQLite", "%s / %s" % (tree.get("db"), tree.get("kind")))
        rows_ok = []
        for it in tree.get("items") or []:
            want = real.get(it["name"], (None,))[0]
            got = it["rows"].replace(",", "").replace(" 行", "").strip()
            if got != str(want):
                rows_ok.append("%s(%s!=%s)" % (it["name"], got, want))
        chk("每张表的行数 = 库里真实行数", not rows_ok, "%s" % rows_ok)
        chk("还没勾选任何表 ⇒ 「提取表结构」是灰的", tree.get("extractDisabled") is True)
        chk("演示种子已下线（左栏不是 demo_shop 那 6 张表）",
            "demo_shop.sqlite" not in (tree.get("db") or ""))

        # ------------------------------------------------------------ [2] 此时没调模型
        w("")
        w("■ [2] 说明只在「提取」时生成 —— 页面打开阶段模型一次都不该被调用")
        with MOCK_LOCK:
            n0 = len(MOCK_REQS)
        chk("打开页面后模型调用次数为 0", n0 == 0, "实际 %d" % n0)

        # ------------------------------------------------------------ [3] 勾选 + 提取
        w("")
        w("■ [3] 勾选 %s → 点「提取表结构」→ 只调一次模型" % PICK_A)
        pk = c.ev_json(JS_PICK % json.dumps(PICK_A, ensure_ascii=False), "pick_a")
        chk("勾上了 %d 张表" % len(PICK_A), pk.get("hit") == len(PICK_A), "%s" % pk)
        chk("勾选后「提取表结构」可用",
            pk.get("extractDisabled") is False and pk.get("on") == PICK_A, "%s" % pk)

        ex = c.ev_json(JS_EXTRACT % "btnExtract", "extract")
        chk("「提取表结构」被点（点击瞬间进入忙态）",
            ex.get("clicked") is True and ex.get("disabled") is True, "%s" % ex)

        got_n = wait_mock(1)
        chk("模型被调用 1 次", got_n == 1, "实际 %d 次" % got_n)
        with MOCK_LOCK:
            r1 = dict(MOCK_REQS[0]) if MOCK_REQS else {}

        got_models = set()
        got_tables = set()
        for line in (r1.get("user") or "").splitlines():
            m = re.match(r"^\s*表\s+(\S+)\s*$", line)
            if m:
                got_tables.add(m.group(1))
        got_models.add(r1.get("model"))
        chk("请求里 model = deepseek-v4-pro（需求指定）", r1.get("model") == "deepseek-v4-pro",
            r1.get("model"))
        chk("请求用的是 settings.py 里那把 key（不是空值 / 也不是别的）",
            secret and r1.get("auth") == "Bearer " + secret, mask_key(r1.get("auth")))
        chk("system 人设是「结构说明生成器」且限定了「只说存的是什么」",
            "结构说明生成器" in (r1.get("system") or "")
            and "存的是什么" in (r1.get("system") or ""),
            (r1.get("system") or "")[:40])
        chk("请求里只有勾选的 %d 张表" % len(PICK_A), got_tables == set(PICK_A),
            "%s" % sorted(got_tables))
        sent_cols = set()
        for line in (r1.get("user") or "").splitlines():
            m = re.match(r"^\s*(\S+)\s+\S+（需说明）\s*$", line)
            if m:
                sent_cols.add(m.group(1))
        want_cols = set(real[PICK_A[0]][1]) | set(real[PICK_A[1]][1])
        chk("库里没注释的字段全都标成了「需说明」（共 %d 个）" % len(want_cols),
            sent_cols == want_cols, "实际 %d 个" % len(sent_cols))

        # ------------------------------------------------------------ [4] 表结构面板
        w("")
        w("■ [4] 「表结构」面板：表说明 / 字段说明 / 主键标记")
        sc = wait_js(c, JS_SCHEMA, "schema", lambda d: d.get("n", 0) > 0, seconds=20)
        chk("面板渲染出 %d 张表" % len(PICK_A), sc.get("n") == len(PICK_A), "实际 %s" % sc.get("n"))
        by = {t["name"]: t for t in sc.get("tables") or []}
        for nm in PICK_A:
            one = by.get(nm) or {}
            chk("%s 的表说明 = 模型生成（表说明·%s）" % (nm, nm),
                one.get("desc") == "表说明·" + nm, "%r" % one.get("desc"))
            bad = [r for r in (one.get("rows") or [])
                   if r["note"] != "字段说明·" + r["col"]]
            chk("%s 的 %d 个字段说明全部来自模型" % (nm, len(one.get("rows") or [])),
                bool(one.get("rows")) and not bad, "%s" % bad[:3])
        srv_rows = (by.get("srv") or {}).get("rows") or []
        id_row = [r for r in srv_rows if r["col"] == "id"]
        chk("主键列仍然标着「主键」（原型口径未被破坏）",
            bool(id_row) and id_row[0]["pk"] is True, "%s" % id_row)
        chk("主键类型仍是方言映射出来的 INTEGER（原型 TYPE_MAP 生效）",
            bool(id_row) and id_row[0]["typ"].strip() == "INTEGER", "%s" % (id_row[0] if id_row else None))
        chk("计数文案对得上（%d 张表 · 1 条关系）" % len(PICK_A),
            sc.get("count") == "%d 张表 · 1 条关系" % len(PICK_A), "%r" % sc.get("count"))
        chk("提取完成后「重新生成 / 清空」可用、面板仍在「表结构」页",
            sc.get("regenDisabled") is False and sc.get("clearDisabled") is False
            and sc.get("paneSchemaHidden") is False, "%s" % sc)

        # ------------------------------------------------------------ [5] 表内容文本
        w("")
        w("■ [5] 「表内容」纯文本：表说明 / 字段说明 / 表间关系都在")
        text = sc.get("text") or ""
        chk("文本里有表块「表 srv」", "表 srv" in text, text[:60].replace("\n", "\\n"))
        chk("文本里有表说明（注释: 表说明·srv）", "注释: 表说明·srv" in text)
        chk("文本里有字段说明（字段说明·id）", "字段说明·id" in text)
        chk("文本里有表间关系段", "表间关系：" in text and "tst_plan.srv_id → srv.id" in text,
            "关系段=%s" % ("有" if "表间关系：" in text else "无"))
        chk("文本里没有演示数据的痕迹（users / products）",
            "用户主表" not in text and "商品主表" not in text)

        # ------------------------------------------------------------ [6] 重新生成走缓存
        w("")
        w("■ [6] 再点「重新生成」→ 命中服务端缓存 ⇒ 不再调模型，结果一致")
        c.ev_json(JS_EXTRACT % "btnRegen", "regen")
        time.sleep(1.5)
        c.pump(1.5)
        with MOCK_LOCK:
            n_after_regen = len(MOCK_REQS)
        chk("重新生成没有再调模型（仍 %d 次）" % n_after_regen, n_after_regen == 1,
            "实际 %d 次" % n_after_regen)
        sc2 = c.ev_json(JS_SCHEMA, "schema2")
        chk("重新生成后面板内容与会话一致",
            [t["name"] for t in sc2.get("tables") or []] == [t["name"] for t in sc.get("tables") or []]
            and (sc2.get("tables") or [{}])[0].get("desc") == "表说明·" + PICK_A[0],
            "%s" % (sc2.get("tables") or [{}])[0].get("desc"))

        # ------------------------------------------------------------ [7] 改勾选 → 重置 → 换一组
        w("")
        w("■ [7] 改勾选 ⇒ 展示栏按原型口径重置；换一组再提取 ⇒ 第 2 次调用只含新表")
        c.ev_json(JS_PICK % json.dumps(PICK_A, ensure_ascii=False), "unpick_a")   # 取消 A 组
        sc3 = c.ev_json(JS_SCHEMA, "schema_after_unpick")
        chk("勾选一变，展示栏回到空态（原型口径保留）",
            sc3.get("n") == 0 and sc3.get("count") == "—", "count=%r n=%s"
            % (sc3.get("count"), sc3.get("n")))
        chk("空态时「清空」不可用、面板回到「表结构」页",
            sc3.get("clearDisabled") is True and sc3.get("paneSchemaHidden") is False, "%s" % sc3)

        pk2 = c.ev_json(JS_PICK % json.dumps(PICK_B, ensure_ascii=False), "pick_b")
        chk("换成 %s 后勾选正确" % PICK_B, pk2.get("on") == PICK_B, "%s" % pk2)
        c.ev_json(JS_EXTRACT % "btnExtract", "extract_b")
        got_n2 = wait_mock(2)
        chk("模型第 2 次被调用", got_n2 == 2, "实际 %d 次" % got_n2)
        with MOCK_LOCK:
            r2 = dict(MOCK_REQS[1]) if len(MOCK_REQS) > 1 else {}
        t2 = set()
        for line in (r2.get("user") or "").splitlines():
            m = re.match(r"^\s*表\s+(\S+)\s*$", line)
            if m:
                t2.add(m.group(1))
        chk("第 2 次请求只含新勾选的 %s" % PICK_B, t2 == set(PICK_B), "%s" % sorted(t2))
        sc4 = wait_js(c, JS_SCHEMA, "schema_b", lambda d: d.get("n", 0) > 0, seconds=20)
        chk("面板换成 B 组两张表",
            sorted(t["name"] for t in sc4.get("tables") or []) == sorted(PICK_B),
            "%s" % [t["name"] for t in sc4.get("tables") or []])

        # ------------------------------------------------------------ [8] JS 异常
        w("")
        w("■ [8] JS 异常")
        errs = sc4.get("errors") or []
        w("    页面 error：%s" % errs)
        chk("无页面 JS 异常", not errs, "%s" % errs)

        # ------------------------------------------------------------ [8.1] 连接弹窗真接驱动
        w("")
        w("■ [8.1] 「连接数据库」弹窗 → 真调驱动（连不上如实报错，绝不伪造表树）")

        c.ev_json(JS_INSTALL_PROBE, "probe")
        base_tree = c.ev_json(JS_PROBE_STATE, "base_tree")

        op = c.ev_json(JS_OPEN_FLOW % "MySQL", "open_flow")
        chk("选「MySQL」后弹出「连接数据库」弹窗（标题带所选类型）",
            op.get("kind") == "MySQL" and op.get("connShown") is True, op)
        chk("此时打开的是「连接」弹窗，不是「SQLite 选文件」弹窗",
            op.get("fileShown") is False, op)

        filled = c.ev_json(JS_FILL_CONN % json.dumps(
            {"host": "127.0.0.1", "port": "3999", "user": "root", "pwd": "x", "dbname": ""},
            ensure_ascii=False), "fill_conn")
        chk("弹窗类型仍是 MySQL（按原型字段填连接信息）", filled.get("kind") == "MySQL", filled)

        c.ev_json(JS_CLICK % "connConfirm", "conn_confirm")
        c.pump(6)
        st = c.ev_json(JS_PROBE_STATE, "conn_fail")

        toasts = st.get("toasts") or []
        err_toast = [t for t in toasts if "MySQL" in t]
        chk("连不上时如实提示 MySQL 驱动的真实报错",
            bool(err_toast) and any(("2003" in t or "10061" in t or "Can't connect" in t
                                     or "拒绝" in t) for t in err_toast),
            (err_toast or toasts)[-1:])
        chk("连不上时左栏没有多出库（绝不伪造表树）",
            st.get("dbCount") == base_tree.get("dbCount"),
            "%s -> %s" % (base_tree.get("dbCount"), st.get("dbCount")))
        chk("连不上时也没往左栏塞任何表",
            (st.get("dbs") or [{}])[0].get("tables") == (base_tree.get("dbs") or [{}])[0].get("tables"))

        conn_calls = [f for f in st.get("fetches") or []
                      if (f.get("url") or "").endswith("/api/schema/connect")]
        chk("前端真的调了 POST /api/schema/connect", bool(conn_calls),
            [f.get("url") for f in st.get("fetches") or []])
        try:
            sent = json.loads(conn_calls[-1].get("body") or "") if conn_calls else {}
        except ValueError:
            sent = {}
        chk("请求体按原型字段把连接信息送全（kind/mode/host/port/user/pwd/dbname/url）",
            sent.get("kind") == "MySQL" and sent.get("mode") == "host"
            and sent.get("host") == "127.0.0.1" and sent.get("port") == "3999"
            and sent.get("user") == "root"
            and all(k in sent for k in ("pwd", "dbname", "url")), sent)

        # ------------------------------------------------------------ [8.2] 按库隔离
        w("")
        w("■ [8.2] 真上传第二个 .sqlite → 左栏两个库，同名表结构互不串（按库隔离）")

        # 造一个「有同名表 srv，但字段跟应用库完全不同」的库
        other_db = os.path.join(tmp, "pd_verify_other.sqlite")
        c2 = sqlite3.connect(other_db)
        try:
            c2.executescript("""
CREATE TABLE srv (                     -- 另一台机器的服务器表
  srv_id     INTEGER PRIMARY KEY,      -- 另一套主键
  srv_alias  TEXT,                     -- 别名
  srv_zone   TEXT                      -- 机房
);
CREATE TABLE only_here (               -- 只有这个库才有的表
  id  INTEGER PRIMARY KEY,             -- 主键
  v   TEXT                             -- 值
);
""")
            c2.commit()
        finally:
            c2.close()
        with sqlite3.connect(other_db) as cc:
            other_srv_cols = [r[1] for r in cc.execute('PRAGMA table_info("srv")')]

        fo = c.ev_json(JS_OPEN_FLOW % "SQLite", "open_file")
        chk("选「SQLite」后打开的是「选文件」弹窗（不是连接弹窗）",
            fo.get("fileShown") is True and fo.get("connShown") is False, fo)

        c.send("DOM.enable")
        doc = c.send("DOM.getDocument", {})
        node = c.send("DOM.querySelector",
                      {"nodeId": doc["root"]["nodeId"], "selector": "#fileInput"})["nodeId"]
        c.send("DOM.setFileInputFiles", {"files": [other_db], "nodeId": node})
        time.sleep(1)
        picked = c.ev(JS_PICKED_FILE)
        if not picked:
            c.ev("(function(){document.getElementById('fileInput')"
                 ".dispatchEvent(new Event('change',{bubbles:true}));return 1;})()")
            time.sleep(0.5)
            picked = c.ev(JS_PICKED_FILE)
        chk("文件已挂到原型的 #fileInput 上", bool(picked), picked)

        c.ev_json(JS_CLICK % "fileConfirm", "file_confirm")
        c.pump(6)
        up = c.ev_json(JS_PROBE_STATE, "after_upload")
        chk("上传后左栏变成 2 个库", up.get("dbCount") == 2, up.get("dbs"))
        names = [d.get("name") for d in up.get("dbs") or []]
        chk("第 2 个库就是刚上传的那个文件",
            any("pd_verify_other" in (n or "") for n in names), names)
        got_t2 = set(((up.get("dbs") or [{}, {}])[1] or {}).get("tables") or [])
        chk("第 2 个库的表清单 = 该库里真实的表", got_t2 == {"srv", "only_here"}, got_t2)

        # 两个库里的同名表 srv 一起勾上 ⇒ 面板必须出现两份**不同**字段
        c.ev_json(JS_PICK_DB_IX % (0, "srv"), "pick_app_srv")
        c.ev_json(JS_PICK_DB_IX % (1, "srv"), "pick_other_srv")
        sel = c.ev_json(JS_SELECTION, "selection")
        chk("两个库的 srv 都勾上了（靠 db 区分，不是靠表名）",
            len([x for x in sel.get("on") or [] if x.endswith("|srv")]) == 2, sel.get("on"))

        c.ev_json(JS_EXTRACT % "btnExtract", "extract2")
        sc = wait_js(c, JS_SCHEMA, "schema_iso", lambda d: d.get("n", 0) > 0, seconds=25)
        got_srv = [set(r["col"] for r in t["rows"])
                   for t in (sc.get("tables") or []) if t["name"] == "srv"]
        with sqlite3.connect(DB_PATH) as cc:
            app_srv_cols = [r[1] for r in cc.execute('PRAGMA table_info("srv")')]
        chk("两个库的同名表各自渲染自己的字段（按库隔离，不串）",
            {frozenset(x) for x in got_srv}
            == {frozenset(app_srv_cols), frozenset(other_srv_cols)},
            "面板=%s 应用库=%s 上传库=%s"
            % ([sorted(x) for x in got_srv], app_srv_cols, other_srv_cols))
        chk("上传库的结构里能看到它自己的表说明（注释来自该库的 DDL）",
            any(t["name"] == "srv" and "另一台机器" in (t.get("desc") or "")
                for t in sc.get("tables") or []),
            [(t["name"], t.get("desc")) for t in sc.get("tables") or []])
        chk("新增流程无 JS 异常", not (sc.get("errors") or []), sc.get("errors"))

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
        try:
            mock.shutdown()
        except Exception:
            pass

    # ------------------------------------------------------------ [9] 收尾
    w("")
    w("■ [9] 收尾：库表计数回到基线（本页只读库，不写任何表）")
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
