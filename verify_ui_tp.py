# -*- coding: utf-8 -*-
"""PromptDeck（Django 版）· 真实浏览器端到端校验（Edge + CDP，不依赖 playwright）。

只盯本次迭代新做的那条链路，页面是顶层文档，直接用 CDP 操作 DOM：

    [1] 清空浏览器本地存储后打开「提示词方案」页
        -> 列表行数 == 库内 tst_plan 行数（证明数据真来自 /api/tp，不再是本地存储）
    [2] 打开「提示词构造」页 -> 三个下拉的选项来自库内 srv / mdl
    [3] 选服务器 -> 选模型类型 -> 选模型 -> Base_url 自动填充
    [4] 填系统提示词 / API_key / 用户提示词 -> 点「保存」-> 原型弹窗按原样弹出
        （回显里 API_key 只给遮掩形态，明文不出现在页面上）
    [5] 填「方案命名」-> 点「保存方案」-> 真发 POST /api/tp
        -> 自动跳到「提示词方案」列表页，新方案出现在第一屏
    [6] 落库核对：接口条数 == 库内 tst_plan 行数，且该方案在三张表
        （tst_plan / tst_plan_prompt / tst_api_key）里的字段逐项对得上
    [7] 多轮会话：勾「多轮会话调试」自动带出 1 轮 + 点「+ 添加一轮」再追加 1 轮
        -> 3 轮用户提示词一起保存 -> 逐轮落 tst_plan_prompt
        （seq_no=0 系统 / 1..N 用户），并按 user 条数还原成 multi=true
    [8] 列表页点该行「删除」-> 二次确认 -> 行消失，库内也同步删掉
        （此时库里另有一行多轮方案，用来证明删的是指定那一行、不误伤别人）
    [9] 提示词列：文案最多显示前 25 个字，超过补省略号（第二次迭代口径）
        —— 用接口造 4 条临时方案（系统 35 字 / 恰 25 字 / 7 字 / 系统为空看用户轮兜底），
           打开列表页逐条量 .col-prompt 的文案；量完即删，库回到基线
    [10] 全程无 JS 异常
    [11] 收尾：srv / mdl / tst_plan 回到跑之前的基线
        （save_tp 那套「按名字查不到 srv / mdl 就补建」的兜底行为会留下行，
         脚本自己在 finally 里收掉，保证基础数据不被校验污染）

前置：
    1. 应用已在 8502 运行：python manage.py runserver 127.0.0.1:8502
    2. pip install websocket-client；本机装有 Edge

结果写 _ui_tp_check.txt；结束时自动清理本次产生的「UI复现方案*」测试数据。
退出码 0 = 全部通过，1 = 有失败项。
"""
import io
import json
import os
import shutil
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
OUT = os.path.join(HERE, "_ui_tp_check.txt")
DB_PATH = os.environ.get("PROMPTDECK_DB") or os.path.join(HERE, "data", "prompt_test_DB_twe.db")
APP = os.environ.get("PD_APP_URL") or "http://127.0.0.1:8502/"
PORT = 9349
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

buf = io.StringIO()
FAILS = []
events = []


def w(s=""):
    buf.write(str(s) + "\n")


def chk(label, ok, detail=""):
    w("  [%s] %s%s" % ("PASS" if ok else "FAIL", label, ("  " + str(detail)) if detail else ""))
    if not ok:
        FAILS.append(label)


# ---------------------------------------------------------------- 库（新库三张表）
def db_count():
    """方案总数 = tst_plan 的行数（提示词与密钥挂在子表，不重复计数）。"""
    with sqlite3.connect(DB_PATH) as c:
        return c.execute("SELECT COUNT(*) FROM tst_plan").fetchone()[0]


def db_ids(table):
    """取一张表当前的全部主键（用来做「跑之前 / 跑之后」的差集清理）。"""
    with sqlite3.connect(DB_PATH) as c:
        return set(r[0] for r in c.execute("SELECT id FROM %s" % table))


def db_cleanup_extra(table, before):
    """把本次跑期间新长出来的行删掉，返回删掉的主键。

    保存方案时 store.save_tp 有「按名字查不到 srv / mdl 就补建一条」的兜底设计
    （保证 tst_plan 的外键有落点）。本脚本在页面上选的组合可能是「A 平台的
    某模型」（下拉是全库模型），保存后就会多出一条挂在 A 平台下的同名校型。
    那是产品行为、不是 bug，但**校验脚本自己得把现场收干净**，
    否则每跑一次基础数据就多一条，后面的基线全都漂。"""
    extra = sorted(db_ids(table) - before)
    if not extra:
        return []
    with sqlite3.connect(DB_PATH) as c:
        c.execute("DELETE FROM %s WHERE id IN (%s)"
                  % (table, ",".join("?" * len(extra))), extra)
        c.commit()
    return extra


def db_plan(plan_id):
    """把一个方案的完整落库形态从新库三张表拼出来（断言以库为准，不看页面）。

    既带回 tst_plan 的原生列，也把 srv / mdl / 子表拼成对照字段，
    方便跟「原型方案对象」逐项对得上。
    """
    with sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        p = c.execute("SELECT * FROM tst_plan WHERE id = ?", (plan_id,)).fetchone()
        if not p:
            return None
        out = dict(p)
        s = c.execute("SELECT plat_name FROM srv WHERE id = ?", (p["srv_id"],)).fetchone()
        m = c.execute("SELECT mdl_name, mdl_type FROM mdl WHERE id = ?", (p["mdl_id"],)).fetchone()
        out["srv_name"] = s["plat_name"] if s else None
        out["mdl_name"] = m["mdl_name"] if m else None
        out["mdl_type"] = m["mdl_type"] if m else None

        rows = c.execute("SELECT seq_no, prompt_role, prompt_content FROM tst_plan_prompt"
                         " WHERE plan_id = ? ORDER BY seq_no", (plan_id,)).fetchall()
        out["sys_prompt"] = ""
        out["user_prompt"] = []
        for r in rows:
            role = (r["prompt_role"] or "").lower()
            if role == "system":
                out["sys_prompt"] = r["prompt_content"]
            elif role == "user":
                out["user_prompt"].append(r["prompt_content"])

        k = c.execute("SELECT api_key FROM tst_api_key WHERE plan_id = ? AND is_active = 1"
                      " ORDER BY id LIMIT 1", (plan_id,)).fetchone()
        out["api_key"] = k["api_key"] if k else None

        out["_prompt_rows"] = len(rows)
        out["_key_rows"] = c.execute("SELECT COUNT(*) FROM tst_api_key WHERE plan_id = ?",
                                     (plan_id,)).fetchone()[0]
        return out


def api(method, path, body=None):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    req = urllib.request.Request(APP.rstrip("/") + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------- CDP
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


# ---------------------------------------------------------------- JS 片段
JS_SCHEME_SNAPSHOT = """(function(){
  var trs = document.querySelectorAll('#tbody tr');
  var names = [];
  for (var i=0;i<trs.length;i++){
    var td = trs[i].querySelector('.col-scheme');
    if (td) names.push(td.textContent.trim());
  }
  var stored = null;
  try{ stored = JSON.parse(localStorage.getItem('pd_prompt_schemes') || 'null'); }catch(e){}
  return JSON.stringify({
    rows: trs.length,
    /* dataRows = 真正的数据行（有 .col-scheme 的那些）。
       库为空时原型会渲染 1 行 colspan=8 的空态占位（.empty-cell），
       那行没有 .col-scheme，所以 rows=1 而 dataRows=0 —— 断言一律用 dataRows。 */
    dataRows: names.length,
    names: names,
    meta: (document.getElementById('metaTotal')||{}).textContent,
    foot: (document.getElementById('footCount')||{}).textContent,
    mirror: (stored && typeof stored.length === 'number') ? stored.length : null
  });
})()"""

JS_PICK_SERVER_AND_MODEL = """(function(){
  function pick(menuId, value, idx){
    var items = document.querySelectorAll('#' + menuId + ' .combo-item');
    if (!items.length) return null;
    var el = null;
    if (value){
      for (var i=0;i<items.length;i++){
        if (items[i].getAttribute('data-value') === value){ el = items[i]; break; }
      }
    }
    if (!el) el = items[idx || 0];
    el.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true}));
    return el.getAttribute('data-value');
  }
  return JSON.stringify({
    nSrv:   document.querySelectorAll('#menuServer .combo-item').length,
    nType:  document.querySelectorAll('#menuModelType .combo-item').length,
    nModel: document.querySelectorAll('#menuModel .combo-item').length,
    srv:    pick('menuServer', '', 0)
  });
})()"""

JS_PICK_TYPE_MODEL = """(function(){
  function pick(menuId){
    var items = document.querySelectorAll('#' + menuId + ' .combo-item');
    if (!items.length) return null;
    items[0].dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true}));
    return items[0].getAttribute('data-value');
  }
  return JSON.stringify({
    nType:  document.querySelectorAll('#menuModelType .combo-item').length,
    nModel: document.querySelectorAll('#menuModel .combo-item').length,
    type:   pick('menuModelType')
  });
})()"""

JS_PICK_MODEL_ONLY = """(function(){
  var items = document.querySelectorAll('#menuModel .combo-item');
  if (!items.length) return JSON.stringify({model:null});
  items[0].dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true}));
  return JSON.stringify({model: items[0].getAttribute('data-value')});
})()"""

JS_FILL_FORM = """(function(){
  var d = document;
  d.getElementById('sysPrompt').value  = %s;
  d.getElementById('userPrompt').value = %s;
  d.getElementById('apiKey').value     = %s;
  d.getElementById('btnSave').click();
  var mask = d.getElementById('saveMask');
  return JSON.stringify({
    open:      mask ? mask.classList.contains('show') : false,
    svServer:  (d.getElementById('svServer')||{}).textContent,
    svBase:    (d.getElementById('svBase')||{}).textContent,
    svModel:   (d.getElementById('svModel')||{}).textContent,
    svApiKey:  (d.getElementById('svApiKey')||{}).textContent,
    svSys:     (d.getElementById('svSys')||{}).textContent,
    svUser:    (d.getElementById('svUser')||{}).textContent,
    baseInput: d.getElementById('baseUrl').value
  });
})()"""

JS_CONFIRM_SAVE = """(function(){
  document.getElementById('saveName').value = %s;
  document.getElementById('saveConfirm').click();
  return 'clicked';
})()"""

# ---- 多轮会话（第 1 轮 + 追加轮）：勾选 / 加轮 / 填轮 / 保存 -----------------
JS_MULTI_ON = """(function(){
  var d = document;
  var m = d.getElementById('multiTurn');
  m.checked = true;
  m.dispatchEvent(new Event('change', {bubbles:true}));
  return JSON.stringify({
    extra:     d.querySelectorAll('#rounds .pb-round textarea').length,
    hidden:    d.getElementById('rounds').hidden,
    addHidden: d.getElementById('btnAddRound').hidden
  });
})()"""

JS_MULTI_ADD_ROUND = """(function(){
  document.getElementById('btnAddRound').click();
  return document.querySelectorAll('#rounds .pb-round textarea').length;
})()"""

# 5 个占位：系统提示词 / 第 1 轮 / 追加第 2 轮 / 追加第 3 轮 / API_key
JS_MULTI_FILL = """(function(){
  var d = document;
  d.getElementById('sysPrompt').value  = %s;
  d.getElementById('userPrompt').value = %s;
  var tas = d.querySelectorAll('#rounds .pb-round textarea');
  if (tas[0]) tas[0].value = %s;
  if (tas[1]) tas[1].value = %s;
  d.getElementById('apiKey').value = %s;
  d.getElementById('btnSave').click();
  var mask = d.getElementById('saveMask');
  /* 直接问原型自己的收集函数：证明弹窗里的多轮内容与页面一致（不是脚本自己拼的） */
  var got = null;
  try{ got = collectUserPrompts(); }catch(e){ got = '__ERR__' + e; }
  return JSON.stringify({
    open:      mask ? mask.classList.contains('show') : false,
    svUser:    (d.getElementById('svUser')||{}).textContent,
    collected: got
  });
})()"""

JS_JS_ERRORS = """JSON.stringify((function(){
  var out = [];
  for (var i=0;i<window.__pdErrors.length;i++) out.push(window.__pdErrors[i]);
  return out;
})())"""

# 量「提示词」列的缩略文案：按 id 逐行取 .col-prompt 里的 .prompt-thumb，
# 返回 { lbl:标签文案, body:去掉「系统：」/「用户：」标签后的正文 }
JS_PROMPT_THUMBS = """(function(){
  var ids = %s;
  function cell(id){
    var tr = document.querySelector('#tbody tr[data-id="' + id + '"]');
    if (!tr) return null;
    var d = tr.querySelector('.col-prompt .prompt-thumb');
    if (!d) return null;
    var full = d.textContent;
    var lbl  = (d.querySelector('.lbl') || {}).textContent || '';
    return { lbl: lbl, body: full.slice(lbl.length) };
  }
  var out = [];
  for (var i = 0; i < ids.length; i++) out.push(cell(ids[i]));
  return JSON.stringify(out);
})()"""

HOOK = """window.__pdErrors = [];
window.addEventListener('error', function(e){ window.__pdErrors.push(String(e.message)); });
window.addEventListener('unhandledrejection', function(e){
  window.__pdErrors.push('unhandledrejection: ' + String(e.reason));
});
"""


def main():
    if not os.path.exists(EDGE):
        w("跳过：未找到 Edge（%s）" % EDGE)
        return 0

    stamp = str(int(time.time()))
    scheme_name = "UI复现方案-" + stamp
    multi_name = "UI复现多轮方案-" + stamp
    base_n = db_count()
    srv0 = db_ids("srv")          # 基础数据基线：跑完用来收掉 save_tp 兜底补建的行
    mdl0 = db_ids("mdl")
    new_id = [None]
    multi_id = [None]

    tmp = tempfile.mkdtemp(prefix="pd_ui_tp")
    proc = subprocess.Popen([
        EDGE, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
        "--remote-debugging-port=%d" % PORT, "--user-data-dir=" + tmp,
        "--window-size=1600,1000", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        ws_browser = None
        for _ in range(60):
            time.sleep(0.5)
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d/json/version" % PORT, timeout=3) as r:
                    ws_browser = json.loads(r.read().decode())["webSocketDebuggerUrl"]
                break
            except Exception:
                pass
        if not ws_browser:
            w("!! Edge devtools 未就绪（%d）" % PORT)
            FAILS.append("Edge devtools")
            return 1

        c = CDP(ws_browser)
        tid = c.send("Target.createTarget", {"url": "about:blank"}, session=False)["targetId"]
        c.sid = c.send("Target.attachToTarget", {"targetId": tid, "flatten": True},
                       session=False)["sessionId"]
        c.send("Runtime.enable")
        c.send("Page.enable")
        c.send("Log.enable")
        # 每个新文档都装上 JS 异常钩子，切页之后依然有效
        c.send("Page.addScriptToEvaluateOnNewDocument", {"source": HOOK})

        w("=" * 74)
        w("PromptDeck（Django 版）· 保存 → tst_plan 三张表 → 跳方案列表页（真实浏览器）")
        w("库文件：%s（tst_plan 基线 %d 条）" % (DB_PATH, base_n))
        w("=" * 74)

        # ---------------------------------------------------------- [1]
        w("")
        w("[1] 清掉浏览器本地存储，再打开「提示词方案」页（证数据来自 /api/tp）")
        c.go(APP, wait=3)
        c.ev("localStorage.clear()")
        c.go(APP + "prompt_scheme.html", wait=4)
        snap = json.loads(c.ev(JS_SCHEME_SNAPSHOT))
        w("    页面：数据行 %d / 表格行 %d / meta=%s；本地镜像=%s"
          % (snap["dataRows"], snap["rows"], snap["meta"], snap["mirror"]))
        chk("列表数据行数 == 库内 tst_plan 行数", snap["dataRows"] == base_n,
            "page=%s db=%s" % (snap["dataRows"], base_n))
        chk("表格行数与库一致（空库时 = 原型自带那 1 行空态占位）",
            snap["rows"] == (base_n or 1), "表格行=%s 库=%s" % (snap["rows"], base_n))
        chk("meta-bar / 表尾计数与行数一致",
            str(snap["meta"]).endswith("%d 条记录" % base_n)
            and str(snap["foot"]).endswith("%d 条记录" % base_n),
            "%s / %s" % (snap["meta"], snap["foot"]))
        chk("页面已把库里的方案回写成本地镜像（供测试页复用）",
            snap["mirror"] == base_n, "镜像=%s" % snap["mirror"])
        # 示例方案这条改成「现场取基线」：库里确实有才断言页面上能看到它。
        # 空库（方案被删光）是合法状态，不能拿「库里必有 5 条迁移示例」当硬假设。
        with sqlite3.connect(DB_PATH) as conn:
            seeded = conn.execute("SELECT COUNT(*) FROM tst_plan WHERE plan_name = ?",
                                  ("电商客服回复方案",)).fetchone()[0]
        if seeded:
            chk("列表里能看到库内已有的示例方案", "电商客服回复方案" in snap["names"])
        else:
            w("    （库里没有「电商客服回复方案」这条示例 —— 空库属合法状态，跳过该断言）")

        # ---------------------------------------------------------- [2]
        w("")
        w("[2] 打开「提示词构造」页，三个下拉的选项应来自库内 srv / mdl")
        c.go(APP + "prompt.html", wait=4)
        pick1 = json.loads(c.ev(JS_PICK_SERVER_AND_MODEL))
        w("    下拉项数：服务器 %s / 类型 %s / 模型 %s" % (pick1["nSrv"], pick1["nType"], pick1["nModel"]))
        srv_n = len(api("GET", "/api/srv"))
        mdl_n = len(api("GET", "/api/mdl"))
        chk("服务器下拉项数 == 库内 srv 条数", pick1["nSrv"] == srv_n,
            "page=%s db=%s" % (pick1["nSrv"], srv_n))
        chk("模型下拉项数 == 库内 mdl 条数", pick1["nModel"] == mdl_n,
            "page=%s db=%s" % (pick1["nModel"], mdl_n))
        chk("已选中服务器", bool(pick1["srv"]), pick1["srv"])

        # ---------------------------------------------------------- [3]
        w("")
        w("[3] 服务器 → 模型类型 → 模型 逐级联动（联动逻辑本身未改动）")
        c.pump(1)
        pick2 = json.loads(c.ev(JS_PICK_TYPE_MODEL))
        c.pump(1)
        pick3 = json.loads(c.ev(JS_PICK_MODEL_ONLY))
        c.pump(1)
        base_url = c.ev("document.getElementById('baseUrl').value")
        w("    类型=%s 模型=%s Base_url=%s" % (pick2["type"], pick3["model"], base_url))
        chk("已选中模型类型", bool(pick2["type"]), pick2["type"])
        chk("已选中模型", bool(pick3["model"]), pick3["model"])
        chk("Base_url 跟随服务器自动填充", bool(base_url) and base_url.startswith("http"),
            base_url)

        # ---------------------------------------------------------- [4]
        w("")
        w("[4] 填提示词 → 点「保存」→ 原型的保存弹窗按原样弹出")
        sys_text = "你是 UI 自动化校验助手，只回一句话。"
        user_text = "请回一句：收到。"
        api_key = "sk-ui-check-0123456789abcdef"
        form = json.loads(c.ev(JS_FILL_FORM % (json.dumps(sys_text), json.dumps(user_text),
                                               json.dumps(api_key))))
        c.pump(1)
        w("    弹窗回显：server=%s base=%s model=%s" % (form["svServer"], form["svBase"], form["svModel"]))
        chk("保存弹窗已弹出（.mask.show）", form["open"] is True)
        chk("弹窗回显服务器 / 请求地址 / 模型", form["svServer"] == pick1["srv"]
            and form["svBase"] == base_url and form["svModel"] == pick3["model"],
            "%s / %s / %s" % (form["svServer"], form["svBase"], form["svModel"]))
        chk("弹窗回显系统提示词与用户提示词",
            form["svSys"] == sys_text and form["svUser"] == user_text)
        chk("API_key 只给遮掩形态（明文中段不外露）",
            "·" in form["svApiKey"] and api_key[3:20] not in form["svApiKey"]
            and form["svApiKey"].endswith("cdef"), form["svApiKey"])

        # ---------------------------------------------------------- [5]
        w("")
        w("[5] 填「方案命名」→ 点「保存方案」→ POST /api/tp → 自动跳列表页")
        c.ev(JS_CONFIRM_SAVE % json.dumps(scheme_name))
        for _ in range(20):
            c.pump(0.5)
            path = str(c.ev("location.pathname"))
            if path.endswith("prompt_scheme.html"):
                break
        c.pump(2)
        path = str(c.ev("location.pathname"))
        w("    当前地址：%s" % path)
        chk("保存后已跳转到提示词方案列表页", path.endswith("/prompt_scheme.html"), path)

        snap2 = json.loads(c.ev(JS_SCHEME_SNAPSHOT))
        w("    列表：数据行 %d / meta=%s" % (snap2["dataRows"], snap2["meta"]))
        chk("跳转后条数 = 基线 + 1", snap2["dataRows"] == base_n + 1,
            "page=%s 期望=%s" % (snap2["dataRows"], base_n + 1))
        chk("新保存的方案出现在列表里", scheme_name in snap2["names"])
        chk("本地镜像同步为库内条数", snap2["mirror"] == base_n + 1, "镜像=%s" % snap2["mirror"])

        # ---------------------------------------------------------- [6]
        w("")
        w("[6] 落库核对（断言以库为准，不看页面）")
        listing = api("GET", "/api/tp")
        chk("接口条数 == 库内 tst_plan 行数", len(listing) == db_count(),
            "接口 %d / 库 %d" % (len(listing), db_count()))
        chk("库内 tst_plan 行数 = 基线 + 1", db_count() == base_n + 1, "库 %d" % db_count())
        hit = [r for r in listing if r.get("name") == scheme_name]
        chk("接口里能找到新方案", len(hit) == 1)
        if hit:
            new_id[0] = hit[0]["id"]
            row = db_plan(new_id[0])
            w("    库内：%s" % {k: row[k] for k in ("id", "plan_name", "srv_name",
                                                   "base_url", "mdl_name", "api_key")})
            chk("主表 tst_plan 字段逐项核对",
                row["plan_name"] == scheme_name and row["srv_name"] == pick1["srv"]
                and row["base_url"] == base_url and row["mdl_name"] == pick3["model"],
                row["plan_name"])
            chk("提示词 / 密钥两张子表也落对了（1 条系统 + 1 条用户 + 1 把密钥）",
                row["sys_prompt"] == sys_text and row["user_prompt"] == [user_text]
                and row["api_key"] == api_key
                and row["_prompt_rows"] == 2 and row["_key_rows"] == 1,
                "prompt=%s key=%s" % (row["_prompt_rows"], row["_key_rows"]))

        # ---------------------------------------------------------- [7]
        w("")
        w("[7] 多轮会话：第 1 轮 + 追加 2 轮 → 逐轮落 tst_plan_prompt")
        c.go(APP + "prompt.html", wait=4)
        json.loads(c.ev(JS_PICK_SERVER_AND_MODEL))
        c.pump(1)
        json.loads(c.ev(JS_PICK_TYPE_MODEL))
        c.pump(1)
        m_model = json.loads(c.ev(JS_PICK_MODEL_ONLY))
        c.pump(1)
        m_url = c.ev("document.getElementById('baseUrl').value")

        mult = json.loads(c.ev(JS_MULTI_ON))
        c.pump(1)
        w("    勾选多轮会话：追加轮次 %s 个 / 多轮区展开=%s / 加轮按钮可见=%s"
          % (mult["extra"], not mult["hidden"], not mult["addHidden"]))
        chk("勾选多轮会话自动带出 1 轮", mult["extra"] == 1, mult)
        chk("多轮区展开且「+ 添加一轮」可用",
            mult["hidden"] is False and mult["addHidden"] is False)

        n_extra = c.ev(JS_MULTI_ADD_ROUND)
        c.pump(1)
        w("    点「+ 添加一轮」后追加轮次：%s 个（连第 1 轮共 %s 轮）" % (n_extra, n_extra + 1))
        chk("点「+ 添加一轮」后追加轮次变 2 个", n_extra == 2, n_extra)

        m_sys = "你是多轮校对助手，只回一句话。"
        r1_text = "第 1 轮：把这句话改得更正式。"
        r2_text = "第 2 轮：再压缩到 10 字以内。"
        r3_text = "第 3 轮：最后加上一个感叹号。"
        m_key = "sk-ui-multi-0123456789abcdef"
        mform = json.loads(c.ev(JS_MULTI_FILL % (json.dumps(m_sys), json.dumps(r1_text),
                                                 json.dumps(r2_text), json.dumps(r3_text),
                                                 json.dumps(m_key))))
        c.pump(1)
        w("    弹窗用户提示词回显：%s" % str(mform["svUser"])[:70].replace("\n", " | "))
        chk("多轮保存弹窗已弹出", mform["open"] is True)
        chk("原型 collectUserPrompts() 收集到 3 轮且顺序正确",
            mform["collected"] == [r1_text, r2_text, r3_text], mform["collected"])
        chk("弹窗把 3 轮逐轮回显（含「下一轮」分隔）",
            all(t in str(mform["svUser"]) for t in (r1_text, r2_text, r3_text)), mform["svUser"])

        c.ev(JS_CONFIRM_SAVE % json.dumps(multi_name))
        for _ in range(20):
            c.pump(0.5)
            if str(c.ev("location.pathname")).endswith("prompt_scheme.html"):
                break
        c.pump(2)
        m_path = str(c.ev("location.pathname"))
        w("    当前地址：%s" % m_path)
        chk("多轮方案保存后同样跳到提示词方案列表页",
            m_path.endswith("/prompt_scheme.html"), m_path)

        m_listing = api("GET", "/api/tp")
        m_hit = [r for r in m_listing if r.get("name") == multi_name]
        chk("多轮方案已落库并出现在 /api/tp", len(m_hit) == 1)
        if m_hit:
            multi_id[0] = m_hit[0]["id"]
            mrow = db_plan(multi_id[0])
            with sqlite3.connect(DB_PATH) as conn:
                seq = conn.execute("SELECT seq_no, prompt_role FROM tst_plan_prompt"
                                   " WHERE plan_id = ? ORDER BY seq_no",
                                   (multi_id[0],)).fetchall()
            w("    tst_plan_prompt：%s" % seq)
            w("    库内：srv=%s base=%s mdl=%s api_key=%s"
              % (mrow["srv_name"], mrow["base_url"], mrow["mdl_name"], mrow["api_key"]))
            chk("系统提示词 seq_no=0 / 用户提示词 seq_no=1..N",
                seq == [(0, "system"), (1, "user"), (2, "user"), (3, "user")], seq)
            chk("多轮用户提示词逐轮落库，顺序与页面一致",
                mrow["user_prompt"] == [r1_text, r2_text, r3_text] and mrow["sys_prompt"] == m_sys,
                mrow["user_prompt"])
            chk("提示词明细行数 = 1 条系统 + 3 条用户", mrow["_prompt_rows"] == 4,
                mrow["_prompt_rows"])
            chk("主表字段仍逐项对得上",
                mrow["srv_name"] == m_hit[0]["server"] and mrow["base_url"] == m_url
                and mrow["mdl_name"] == m_model["model"])
            chk("多轮方案按 user 条数还原成 multi=true（不单独落库）",
                m_hit[0].get("multi") is True, m_hit[0].get("multi"))
            chk("密钥仍是一把", mrow["_key_rows"] == 1, mrow["_key_rows"])

        # ---------------------------------------------------------- [8]
        w("")
        w("[8] 列表页点该行「删除」→ 二次确认 → 行与库内同步删掉")
        # 此刻库里还有 [7] 那个多轮方案，所以基线是 base_n + 1（不是 base_n）
        pre_del = db_count()
        c.ev("""(function(){
          var trs = document.querySelectorAll('#tbody tr');
          for (var i=0;i<trs.length;i++){
            var td = trs[i].querySelector('.col-scheme');
            if (td && td.textContent.trim() === %s){
              trs[i].querySelector('[data-del]').click();
              return 'clicked';
            }
          }
          return 'not found';
        })()""" % json.dumps(scheme_name))
        c.pump(1)
        chk("删除确认弹窗已弹出", c.ev(
            "document.getElementById('delMask').classList.contains('show')") is True)
        c.ev("document.getElementById('delConfirm').click()")
        for _ in range(12):
            c.pump(0.5)
            if db_count() == pre_del - 1:
                break
        c.pump(1)
        snap3 = json.loads(c.ev(JS_SCHEME_SNAPSHOT))
        w("    列表：数据行 %d / 表格行 %d；库内 tst_plan：%d 条"
          % (snap3["dataRows"], snap3["rows"], db_count()))
        chk("列表条数回落 1 行（删的是这一行）", snap3["dataRows"] == pre_del - 1,
            "page=%s" % snap3["dataRows"])
        chk("新方案已从列表消失", scheme_name not in snap3["names"])
        chk("库内 tst_plan 行数回落（删除真落库）", db_count() == pre_del - 1,
            "库 %d" % db_count())
        chk("库内已无该行", db_plan(new_id[0]) is None if new_id[0] else True)
        if new_id[0]:
            # ⚠️ 这里别用 c 当变量名：外层的 c 是 CDP 连接对象，会被覆盖
            with sqlite3.connect(DB_PATH) as conn:
                left = (conn.execute("SELECT COUNT(*) FROM tst_plan_prompt WHERE plan_id = ?",
                                     (new_id[0],)).fetchone()[0]
                        + conn.execute("SELECT COUNT(*) FROM tst_api_key WHERE plan_id = ?",
                                       (new_id[0],)).fetchone()[0])
            chk("删除后两张子表无残留（级联生效）", left == 0, "残留 %s 行" % left)
            chk("另一行（多轮方案）未受牵连，仍留在库里",
                db_plan(multi_id[0]) is not None if multi_id[0] else True)

        # 多轮方案走同一套删除链路清掉，顺手核对它的子表也级联干净
        if multi_id[0]:
            api("DELETE", "/api/tp", {"ids": [multi_id[0]]})
            for _ in range(12):
                c.pump(0.5)
                if db_count() == base_n:
                    break
            with sqlite3.connect(DB_PATH) as conn:
                mleft = (conn.execute("SELECT COUNT(*) FROM tst_plan_prompt WHERE plan_id = ?",
                                      (multi_id[0],)).fetchone()[0]
                         + conn.execute("SELECT COUNT(*) FROM tst_api_key WHERE plan_id = ?",
                                        (multi_id[0],)).fetchone()[0])
            chk("多轮方案删除后主表回到基线、子表无残留",
                db_count() == base_n and mleft == 0, "库 %d / 残留 %d" % (db_count(), mleft))
            multi_id[0] = None

        # ---------------------------------------------------------- [9]
        w("")
        w("[9] 提示词列：文案最多显示前 25 个字，超过补省略号（第二次迭代口径）")
        # 素材：拿库里已有的一组「服务器 / 模型」落库，避免 save_tp 的兜底补建。
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            pair = conn.execute("SELECT s.plat_name, s.base_url, m.mdl_name, m.mdl_type"
                                " FROM mdl m JOIN srv s ON s.id = m.srv_id"
                                " ORDER BY m.id LIMIT 1").fetchone()
        if not pair:
            w("    （库里没有任何模型，跳过该用例）")
        else:
            w("    落库用的服务器 / 模型：%s / %s" % (pair["plat_name"], pair["mdl_name"]))
            long_sys   = "你是电商客服助手，请用礼貌简洁的中文回复用户的售后问题并给出处理步骤。"
            eq25_sys   = long_sys[:25]          # 恰好 25 字：不该加省略号
            short_sys  = "你是客服助手。"
            long_user  = "这是一句很长的用户提示词，用来验证系统提示词为空时列表列同样只显示前二十五个字。"
            short_user = "只回一句：收到。"
            chk("用例素材长度符合预期（长 %d / 恰 %d / 短 %d）"
                % (len(long_sys), len(eq25_sys), len(short_sys)),
                len(long_sys) > 25 and len(eq25_sys) == 25 and len(short_sys) < 25)

            # (说明, 系统提示词, 用户提示词, 期望标签, 期望列内文案)
            cases = [
                ("系统 35 字", long_sys,  short_user, "系统：", long_sys[:25] + "…"),
                ("系统恰 25 字", eq25_sys, short_user, "系统：", eq25_sys),
                ("系统 7 字",   short_sys, short_user, "系统：", short_sys),
                ("系统为空",    "",        long_user,  "用户：", long_user[:25] + "…"),
            ]
            tmp_ids = []
            try:
                for i, (tag, csys, cuser, _lbl, _exp) in enumerate(cases):
                    res = api("POST", "/api/tp", {
                        "name":   "UI截断%d-%s" % (i + 1, stamp),
                        "sys":    csys,
                        "user":   [cuser],
                        "base":   pair["base_url"] or "",
                        "server": pair["plat_name"],
                        "model":  pair["mdl_name"],
                        "modelType": pair["mdl_type"] or "",
                    })
                    tmp_ids.append(res["id"])
                chk("4 条临时方案已按接口落库", db_count() == base_n + 4, "库 %d" % db_count())

                c.go(APP + "prompt_scheme.html", wait=4)
                cells = json.loads(c.ev(JS_PROMPT_THUMBS % json.dumps(tmp_ids)))
                for (tag, csys, cuser, lbl, exp), cell in zip(cases, cells):
                    src = csys or cuser
                    if not cell:
                        chk("%s：列表里能找到这一行并量到「提示词」列" % tag, False, "行为空")
                        continue
                    w("    %s：源 %d 字 -> 列内「%s」" % (tag, len(src), cell["body"]))
                    chk("%s：标签仍为「%s」" % (tag, lbl), cell["lbl"] == lbl, cell["lbl"])
                    chk("%s：列内文案 = 前 25 字%s"
                        % (tag, " + 省略号" if len(src) > 25 else "（原样，不加省略号）"),
                        cell["body"] == exp, cell["body"])
                    if len(src) > 25:
                        chk("%s：截断后以「…」结尾且总长 = 26" % tag,
                            cell["body"].endswith("…") and len(cell["body"]) == 26,
                            len(cell["body"]))
                    else:
                        chk("%s：未超 25 字，文案里没有省略号" % tag, "…" not in cell["body"])
            finally:
                if tmp_ids:
                    api("DELETE", "/api/tp", {"ids": tmp_ids})
            chk("4 条临时方案已清掉，库回到基线", db_count() == base_n, "库 %d" % db_count())

        # ---------------------------------------------------------- [10]
        w("")
        w("[10] JS 异常")
        page_errs = c.ev(JS_JS_ERRORS)
        try:
            page_errs = json.loads(page_errs) if isinstance(page_errs, str) else []
        except ValueError:
            page_errs = [str(page_errs)]
        exc = [e for e in events if e.get("method") == "Runtime.exceptionThrown"]
        w("    页面 error：%s；CDP 捕获异常：%d 条" % (page_errs, len(exc)))
        chk("无页面 JS 异常", not page_errs, page_errs)

    finally:
        for one in (new_id[0], multi_id[0]):
            if not one:
                continue
            try:
                api("DELETE", "/api/tp", {"ids": [one]})
            except Exception:
                pass
        # 收现场：测试方案已删，再收掉本次因 save_tp 兜底而补建的模型 / 供应商
        try:
            extra_mdl = db_cleanup_extra("mdl", mdl0)
            extra_srv = db_cleanup_extra("srv", srv0)
            if extra_mdl or extra_srv:
                w("    （已清理本次补建的基础数据：mdl %s / srv %s）" % (extra_mdl, extra_srv))
        except Exception as e:
            w("    （清理本次补建的基础数据失败：%s）" % e)
        try:
            proc.terminate()
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)

    # ---------------------------------------------------------------- [11]
    w("")
    w("[11] 收尾：基础数据未被本次校验污染")
    chk("srv 回到跑之前的基线", db_ids("srv") == srv0, "现在 %d 条" % len(db_ids("srv")))
    chk("mdl 回到跑之前的基线", db_ids("mdl") == mdl0, "现在 %d 条" % len(db_ids("mdl")))
    chk("tst_plan 回到跑之前的基线", db_count() == base_n, "库 %d 条" % db_count())

    w("")
    w("=" * 74)
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
