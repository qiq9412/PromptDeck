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
    dbs: document.querySelectorAll('#dbTree .db-node').length,
    db: dbn ? dbn.textContent : '',
    kind: dbk ? dbk.textContent : '',
    empty: !!empty,
    /* 空态的**文案**（14-18 用来断言「提示用户去连接/导入」而不是挂着模板表） */
    emptyText: empty ? (empty.textContent || '') : '',
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

# ---------------------------------------------------------------- 单库全选框
# 14-18 用户点名：库头前加一个全选框，可全选**单个库**里的所有表。
# 这里把它的行为整条钉住 —— 尤其那两条容易回归的：
#   ① 点它**不能顺手把库折叠掉**（它就在 .db-node-head 里，不拦住就会误触）；
#   ② 三态（全选 / 半选 / 空）必须跟着**表行勾选**实时变，且半选时再点是「补成全选」而不是「取消」。
JS_DB_ALL_STATE = """(function(){
  var nodes = document.querySelectorAll('#dbTree .db-node');
  var out = [];
  for (var i = 0; i < nodes.length; i++){
    var n = nodes[i], box = n.querySelector('[data-db-all]');
    var items = n.querySelectorAll('.tbl-item'), on = 0;
    for (var j = 0; j < items.length; j++){
      if (items[j].querySelector('.tbl-check').checked) on++;
    }
    out.push({ name: (n.querySelector('.db-name')||{}).textContent,
               collapsed: n.classList.contains('is-collapsed'),
               box: !!box, checked: box ? box.checked : null,
               half: box ? box.indeterminate : null,
               tables: items.length, on: on });
  }
  var tb = document.getElementById('checkAllTables');
  return JSON.stringify({ nodes: out, total: tb ? { checked: tb.checked, half: tb.indeterminate } : null });
})()"""

JS_DB_ALL_CLICK = """(function(ix){
  var nodes = document.querySelectorAll('#dbTree .db-node');
  var box = nodes[ix] && nodes[ix].querySelector('[data-db-all]');
  if (!box) return 'no-box';
  box.click();
  return 'clicked';
})(%s)"""

JS_DB_ALL_RESET = """(function(){
  for (var i = 0; i < dbs.length; i++){
    for (var j = 0; j < dbs[i].tables.length; j++){ dbs[i].tables[j].checked = false; }
  }
  renderTree();
  return 'reset';
})()"""

JS_ERR_ONLY = "JSON.stringify({ errors: (window.__pdErrors || []).slice() })"

# 把一个 .sqlite 文件当成「用户导入的库」挂到页面上（14-18 之后页面不再自动挂自带库了，
# 所以每段需要表可用的用例都得自己先导入一次）。
# 走的是页面自己的路径：选「SQLite」→ 选文件 → 确定 ⇒ POST /api/schema 上传 → addDb。
# ⚠️ 本片段只负责「打开选文件弹窗」；真正挂文件靠 DOM.setFileInputFiles（浏览器不给本机路径）。
JS_OPEN_SQLITE_FLOW = """(function(){
  var it = document.querySelector('#comboDbType .combo-item[data-value="SQLite"]');
  if (!it) return 'no-type-item';
  it.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
  var f = document.getElementById('fileMask');
  return (f && f.classList.contains('show')) ? 'opened' : 'no-mask';
})()"""

# 第 ix 个库里的表名清单（拿第 0 个 / 第 1 个来点，避免写死表名 —— 库结构会漂）
JS_DB_TABLES = """(function(ix){
  var n = document.querySelectorAll('#dbTree .db-node')[ix];
  if (!n) return JSON.stringify({ tables: [] });
  var out = [];
  n.querySelectorAll('.tbl-item').forEach(function(li){ out.push(li.getAttribute('data-table')); });
  return JSON.stringify({ tables: out });
})(%s)"""

# 「链接数据库调试」那份元数据（导入时就该登记好；没有它按钮会一直灰着）
JS_DBLINK_META = """(function(){
  var out = null;
  for (var i = 0; i < dbs.length; i++){
    if (dbs[i].kind === 'SQLite'){ out = dbs[i]; break; }
  }
  var m = null;
  try{ m = JSON.parse(sessionStorage.getItem('pd_db_link_meta') || 'null'); }catch(e){}
    return JSON.stringify({ dbs: dbs.length,
    conn: (typeof PD_CONN !== 'undefined') ? Object.keys(PD_CONN).length : -1,
    meta: m ? { kind: m.kind, hasFile: !!m.file, tables: (m.tables||[]).length } : null });
})()"""

# 存档恢复后的左栏状态（10-09：连接后自动存档 → 刷新后应自动恢复）
JS_LIB_STATE = """(function(){
  var nodes = document.querySelectorAll('#dbTree .db-node'), out = [];
  for (var i = 0; i < nodes.length; i++){
    var nm = nodes[i].querySelector('.db-name');
    out.push({ name: nm ? nm.textContent.trim() : '',
               tables: nodes[i].querySelectorAll('.tbl-item').length });
  }
  return JSON.stringify({
    dbs: dbs.length,
    nodes: out,
    connCount: (typeof PD_CONN !== 'undefined') ? Object.keys(PD_CONN).length : -1,
    emptyText: (function(){
      var e = document.querySelector('#dbTree .ts-empty');
      return e ? e.textContent.replace(/\\s+/g, ' ').trim() : '';
    })()
  });
})()"""

# 恢复出来的「连接登记」——「链接数据库调试 / 页面内调试」全靠它拿元数据
JS_LIB_CONN = """(function(){
  var r = (typeof pdFindConn === 'function') ? pdFindConn({ name: %s, kind: 'SQLite' }) : null;
  return JSON.stringify(r ? { mode: r.mode, hasFile: !!r.file } : null);
})()"""


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


def lib_ids():
    """tst_db_conn 里现有行的 id 集合。

    本页现在**会写库**（连接成功后自动存档），所以收尾要自清理，
    不能整表清空（用户自己攒的连接记录必须留着）。
    表还没就位时返回空集 —— 页面本身也容错，不必让校验挂掉。
    """
    try:
        with sqlite3.connect(DB_PATH) as conn:
            return {r[0] for r in conn.execute("SELECT id FROM tst_db_conn")}
    except sqlite3.Error:
        return set()


# ---------------------------------------------------------------- 存档隔离
# ⚠️ 10-09 起本页会把连上的库**存档**并在下次打开时自动恢复。
#    本脚本 [1] 要断言「左栏起始为空」，后续又按位置挑表 ⇒
#    库里若有别人的存档，恢复出来的库会插到左栏前面，把断言全带偏。
#    ⇒ 跑前把存档快照 + 清空，跑完**只补缺地**放回去（不冲掉期间新写的连接）。
def lib_snapshot():
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(tst_db_conn)")]
            rows = [tuple(r) for r in conn.execute("SELECT * FROM tst_db_conn")]
        return (cols, rows) if cols else None
    except sqlite3.Error:
        return None


def lib_clear():
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("DELETE FROM tst_db_conn")
            conn.commit()
    except sqlite3.Error:
        pass


def lib_restore(snap):
    """只补缺地放回跑前那份存档（测试期间用户新存的连接不能被冲掉）。"""
    if not snap:
        return 0
    cols, rows = snap
    n = 0
    try:
        with sqlite3.connect(DB_PATH) as conn:
            have = {(r[0], r[1]) for r in
                    conn.execute("SELECT conn_name, conn_kind FROM tst_db_conn")}
            ph = ",".join("?" * len(cols))
            for row in rows:
                d = dict(zip(cols, row))
                if (d.get("conn_name"), d.get("conn_kind")) not in have:
                    conn.execute("INSERT INTO tst_db_conn (%s) VALUES (%s)"
                                 % (",".join(cols), ph), row)
                    n += 1
            conn.commit()
    except sqlite3.Error:
        pass
    return n


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
    tables = ("srv", "mdl", "tst_plan", "tst_plan_prompt", "tst_api_key",
              "tst_rec", "tst_rec_dtl", "tst_db_conn")
    # ⚠️ 存档隔离：先把 tst_db_conn 快照 + 清空，让「左栏起始为空」这个前提成立
    #    （否则别人存过的库会被自动恢复进左栏，[1] 与后续按位置挑表全会偏）。
    #    跑完在 [9] 之后原样补回去。
    lib_snap = lib_snapshot()
    lib_clear()
    before = {t: db_count(t) for t in tables}
    lib_before_ids = lib_ids()
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
        w("■ [1] 打开页面 → 左栏起始为空（14-18：不再自动挂应用自带库）")
        w("    页面：%s" % page_url)
        c.go(page_url, 5)

        # 先断言「空」这件事本身 —— 这是本轮需求的核心。
        # ⚠️ 10-09 起页面会恢复**已保存的连接**，所以「起始为空」只在
        #    「没有任何存档」时成立 ⇒ 这里**现场取**存档条数，不假设库是空的
        #    （硬约束：基线一律现场取；写死的断言会被别的脚本留下的存档打翻）。
        try:
            _arch0 = (json.loads(urllib.request.urlopen(
                "http://127.0.0.1:%d/api/db-conn" % APP_PORT, timeout=10)
                .read().decode("utf-8")).get("list") or [])
        except Exception:
            _arch0 = []
        t0 = c.ev_json(JS_TREE, "tree_empty")
        if not _arch0:
            chk("★ 左栏起始为空：一个库都没连（当前无存档）", (t0.get("n") or 0) == 0, t0.get("n"))
        else:
            chk("★ 有存档时左栏**只**恢复存档里的库，一个多余的都不挂",
                (t0.get("n") or 0) == len(_arch0),
                "dbs=%s archives=%s" % (t0.get("n"), len(_arch0)))
        chk("★ 起始不含应用自带库那些模板表（srv / mdl / tst_* / django_migrations）",
            not any(x in json.dumps(t0, ensure_ascii=False)
                    for x in ("srv", "mdl", "tst_plan", "tst_rec", "django_migrations")),
            t0.get("items"))
        chk("★ 起始提示是「还没有连接数据库」而不是模板表清单",
            "还没有连接数据库" in (t0.get("emptyText") or ""), t0.get("emptyText"))
        chk("没勾选任何表 ⇒ 「提取表结构」是灰的", t0.get("extractDisabled") is True)

        # 然后由「用户导入」把库接进来 —— 走页面自己的选文件流程
        chk("导入：选「SQLite」弹出选文件窗口", c.ev(JS_OPEN_SQLITE_FLOW) == "opened")
        c.send("DOM.enable")
        doc = c.send("DOM.getDocument", {})
        node = c.send("DOM.querySelector",
                      {"nodeId": doc["root"]["nodeId"], "selector": "#fileInput"})["nodeId"]
        c.send("DOM.setFileInputFiles", {"files": [DB_PATH], "nodeId": node})
        time.sleep(1)
        if not c.ev(JS_PICKED_FILE):
            c.ev("(function(){document.getElementById('fileInput')"
                 ".dispatchEvent(new Event('change',{bubbles:true}));return 1;})()")
            time.sleep(0.5)
        chk("导入：文件已挂到 #fileInput", bool(c.ev(JS_PICKED_FILE)), c.ev(JS_PICKED_FILE))
        c.ev_json(JS_CLICK % "fileConfirm", "import_confirm")

        tree = wait_js(c, JS_TREE, "tree", lambda d: d.get("n", 0) > 0, seconds=25)
        chk("★ 导入后左栏渲染出 %d 张真实表（库里共 %d 张）" % (len(real), len(real)),
            tree.get("n") == len(real), "实际 %s 张" % tree.get("n"))
        got_names = [t["name"] for t in tree.get("items") or []]
        chk("导入后的表清单 = 该库里真实表清单", got_names == sorted(real.keys()),
            "页面=%s" % got_names)
        chk("导入的库名来自上传的文件名",
            tree.get("db") == os.path.basename(DB_PATH) and tree.get("kind") == "SQLite",
            "%s / %s" % (tree.get("db"), tree.get("kind")))
        rows_ok = []
        for it in tree.get("items") or []:
            want = real.get(it["name"], (None,))[0]
            got = it["rows"].replace(",", "").replace(" 行", "").strip()
            if got != str(want):
                rows_ok.append("%s(%s!=%s)" % (it["name"], got, want))
        chk("每张表的行数 = 库里真实行数", not rows_ok, "%s" % rows_ok)
        chk("导入后仍没勾选任何表 ⇒ 「提取表结构」是灰的",
            tree.get("extractDisabled") is True)
        chk("导入的连接已登记（链接数据库调试要用：kind / 文件绝对路径 / 表名）",
            c.ev(JS_DBLINK_META) is not None, c.ev(JS_DBLINK_META))

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

        # ============================================================ [8.5]
        w("")
        w("■ [8.5] 单库全选框（14-18 用户点名）：全选 / 半选 / 不误触折叠")
        # ⚠️ 本段跑在前面用例之后，页面上**已经有勾选残留、也可能已经连了第二个库**
        #   （实测起始就有 3 张已勾 + `pd_verify_other.sqlite`）。
        #   所以先归零 + 只留第1 个库展开，全选框的初始态才有意义 —— 基线一律现场取。
        c.ev(JS_DB_ALL_RESET)
        # 只连一个库：把本段不额外连库，改为断开第二个（移除节点，不写库、无副作用）
        c.ev("(function(){while(dbs.length>1){dbs.pop();}renderTree();return dbs.length;})()")
        time.sleep(0.4)

        d0 = c.ev_json(JS_DB_ALL_STATE, "db_all_init")
        n0 = d0["nodes"][0]
        w("    第 1 个库：%s（%s 张表）" % (n0["name"], n0["tables"]))
        chk("库头前有全选框", n0["box"] is True, n0)
        chk("★ 初始为未勾选、未半选（已先归零）",
            n0["checked"] is False and n0["half"] is False and n0["on"] == 0, n0)
        chk("本段只剩一个库（避免多库干扰计数）", len(d0["nodes"]) == 1, len(d0["nodes"]))

        # 点一下 = 全选该库
        c.ev(JS_DB_ALL_CLICK % 0)
        d1 = c.ev_json(JS_DB_ALL_STATE, "db_all_on")
        m1 = d1["nodes"][0]
        chk("★ 点一下 = 该库全部表勾上", m1["on"] == m1["tables"] and m1["tables"] > 0,
            "on=%s/%s" % (m1["on"], m1["tables"]))
        chk("库级框变实勾", m1["checked"] is True and m1["half"] is False, m1)
        chk("★ 点全选框不会误触折叠（最易回归的一条）", m1["collapsed"] is False, m1)

        # 再点 = 取消
        c.ev(JS_DB_ALL_CLICK % 0)
        m2 = c.ev_json(JS_DB_ALL_STATE, "db_all_off")["nodes"][0]
        chk("再点 = 取消全选", m2["on"] == 0, "on=%s" % m2["on"])
        chk("库级框回到未勾选", m2["checked"] is False and m2["half"] is False, m2)

        # 只勾一张表 => 半选（库级框与总全选框都要跟着半选）
        first = c.ev_json(JS_DB_TABLES % 0, "db_all_first")["tables"][0]
        c.ev(JS_PICK_DB_IX % (0, first))
        time.sleep(0.4)
        d3 = c.ev_json(JS_DB_ALL_STATE, "db_all_half")
        m3 = d3["nodes"][0]
        chk("★ 只勾一张表 => 库级框半选", m3["half"] is True and m3["checked"] is False
            and m3["on"] == 1, m3)
        chk("总全选框也跟着半选", (d3["total"] or {}).get("half") is True, d3["total"])

        # 半选时再点 = 补成全选（不是取消）
        c.ev(JS_DB_ALL_CLICK % 0)
        m4 = c.ev_json(JS_DB_ALL_STATE, "db_all_full")["nodes"][0]
        chk("★ 半选状态下再点是「补成全选」而不是取消",
            m4["on"] == m4["tables"] and m4["checked"] is True,
            "on=%s/%s" % (m4["on"], m4["tables"]))

        # 全选后的勾选集合能直接喂给提取链路（selection 里就是该库全部表、同库聚合）
        sel_all = c.ev_json(JS_SELECTION, "selection_all")
        n_tables = m4["tables"]
        n_sel = len(sel_all.get("on") or [])
        dbs_in = len(set(x.split("|")[0] for x in (sel_all.get("on") or [])))
        chk("★ 全选后 selection() 里是该库全部表、且同库聚合",
            n_sel == n_tables and dbs_in == 1,
            "sel=%s tables=%s dbs=%s" % (n_sel, n_tables, dbs_in))

        # 总全选取消 => 各库级框同步清空
        c.ev("document.getElementById('checkAllTables').click()")
        time.sleep(0.4)
        d5 = c.ev_json(JS_DB_ALL_STATE, "db_all_total_off")
        chk("表名栏总全选取消 => 库级框同步清空",
            all(n["on"] == 0 and n["checked"] is False and n["half"] is False
                for n in d5["nodes"]), d5["nodes"])

        # 折叠着也能全选（表仍在 DOM 里，不受折叠影响）
        # ⚠️ 先确保是「展开」态再点头部折叠 —— 上一段结束时展开/折叠态不确定，
        #   直接点头部可能变成「展开」，断言就会假失败（踩过一次）。
        cur = c.ev_json(JS_DB_ALL_STATE, "db_all_pre_collapse")["nodes"][0]
        if not cur["collapsed"]:
            c.ev("document.querySelector('#dbTree .db-node .db-node-head').click()")
            time.sleep(0.3)
        c.ev(JS_DB_ALL_CLICK % 0)
        m6 = c.ev_json(JS_DB_ALL_STATE, "db_all_collapsed")["nodes"][0]
        chk("库折叠着也能全选", m6["collapsed"] is True and m6["on"] == m6["tables"], m6)

        # 点库名 = 只折叠，勾选态不受影响
        c.ev("document.querySelector('#dbTree .db-node .db-node-head').click()")
        time.sleep(0.3)
        b4 = c.ev_json(JS_DB_ALL_STATE, "db_all_before_name")["nodes"][0]
        c.ev("document.querySelector('#dbTree .db-node .db-name').click()")
        time.sleep(0.3)
        af = c.ev_json(JS_DB_ALL_STATE, "db_all_after_name")["nodes"][0]
        chk("点库名 = 只折叠、勾选态不变", af["collapsed"] is True and af["on"] == b4["on"],
            "before on=%s after on=%s" % (b4["on"], af["on"]))
        c.ev("document.querySelector('#dbTree .db-node .db-node-head').click()")   # 复原展开

        # 归零 + 再全选，确认不是「只能全选一次」
        c.ev(JS_DB_ALL_RESET)
        c.ev(JS_DB_ALL_CLICK % 0)
        m7 = c.ev_json(JS_DB_ALL_STATE, "db_all_again")["nodes"][0]
        chk("归零后还能重新全选（不是一次性状态）",
            m7["on"] == m7["tables"] and m7["checked"] is True,
            "on=%s/%s" % (m7["on"], m7["tables"]))
        chk("单库全选流程无 JS 异常", not (c.ev_json(JS_ERR_ONLY, "err_all").get("errors") or []))

        # ---------------------------------------- [8.7] 已连接数据库存档（10-09 用户点名）
        w("")
        w("■ [8.7] 已连接数据库存档：连上就存 → 刷新后左栏自己回来（含表结构快照）")

        def _ts_api(method, path, body=None):
            req = urllib.request.Request(
                "http://127.0.0.1:%d%s" % (APP_PORT, path),
                data=(json.dumps(body).encode("utf-8") if body is not None else None),
                method=method)
            if body is not None:
                req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))

        _lst = (_ts_api("GET", "/api/db-conn").get("list") or [])
        w("    存档条数：%d" % len(_lst))
        chk("★ 导入后页面**自动**存了档（没点任何「保存」按钮）", len(_lst) >= 1, len(_lst))
        _lib = _lst[0] if _lst else {}
        chk("★ 存档里有库名 / 类型 / 表结构快照",
            bool(_lib.get("name")) and _lib.get("kind") == "SQLite"
            and len(((_lib.get("schema") or {}).get("tables")) or []) > 0,
            {k: _lib.get(k) for k in ("name", "kind")})
        chk("★ SQLite 存档带上了服务端落盘路径（file）",
            bool(_lib.get("file")), _lib.get("file"))
        _dump = json.dumps(_lib, ensure_ascii=False).lower()
        chk("★ 存档内容里没有密码字样（pwd / password 都不该出现）",
            "pwd" not in _dump and "password" not in _dump)

        # —— 刷新页面：存档的全部意义就在这一步之后 ——
        c.go(page_url, 5)
        stl = c.ev_json(JS_LIB_STATE, "lib_restore")
        w("    刷新后左栏：dbs=%s  nodes=%s" % (stl.get("dbs"), stl.get("nodes")))
        chk("★ 刷新后左栏**自己恢复**出存档里的库（不用重新导入）",
            (stl.get("dbs") or 0) >= 1, stl.get("dbs"))
        _names = [n.get("name") for n in (stl.get("nodes") or [])]
        chk("★ 恢复出来的库名与存档一致", _lib.get("name") in _names, _names)
        chk("★ 恢复出来的库带着表（表数与快照一致 ⇒ 能直接勾选 / 提取，不用重连）",
            any((n.get("tables") or 0) > 0 for n in (stl.get("nodes") or [])),
            stl.get("nodes"))
        chk("★ 连接登记（PD_CONN）也恢复了 ⇒「链接数据库调试 / 页面内调试」拿得到元数据",
            (stl.get("connCount") or 0) >= 1, stl.get("connCount"))
        _cn = json.loads(c.ev(JS_LIB_CONN % json.dumps(_lib.get("name") or "")))
        chk("★ 恢复出来的连接信息里带着服务端文件路径（file）",
            bool(_cn) and _cn.get("hasFile") is True, _cn)
        chk("★ 恢复后左栏不再显示「还没有连接数据库」",
            "还没有连接数据库" not in (stl.get("emptyText") or ""), stl.get("emptyText"))
        chk("恢复流程无 JS 异常", not (c.ev_json(JS_ERR_ONLY, "err_lib").get("errors") or []))

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
    w("■ [9] 收尾：自清理后库表计数回到基线")
    w("    （本页 10-09 起会写 tst_db_conn —— 连接后自动存档；"
      "收尾按「跑前记 id、跑完差集删」清掉本次造的行，用户自己的连接记录不动）")
    try:
        with sqlite3.connect(DB_PATH) as conn:
            _now = [r[0] for r in conn.execute("SELECT id FROM tst_db_conn")]
            _mine = [i for i in _now if i not in lib_before_ids]
            for i in _mine:
                conn.execute("DELETE FROM tst_db_conn WHERE id = ?", (i,))
            conn.commit()
        w("    本次自清 %d 行存档" % len(_mine))
    except sqlite3.Error as exc:
        w("    （tst_db_conn 自清跳过：%s）" % exc)
    after = {t: db_count(t) for t in tables}
    for t in tables:
        chk("%s 回到基线" % t, after[t] == before[t], "%d -> %d" % (before[t], after[t]))

    # 最后把跑前那份存档放回去（只补缺，测试期间用户新存的连接不受影响）
    _back = lib_restore(lib_snap)
    if lib_snap:
        w("    已放回跑前的存档 %d 行" % _back)

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
