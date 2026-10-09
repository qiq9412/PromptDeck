# -*- coding: utf-8 -*-
"""PromptDeck（Django 版）· 跨页编辑流 + 构造页草稿 —— 真实浏览器端到端（Edge + CDP）。

覆盖（10-09 需求「编辑 / 修改 → 跳构造页回填 → 跳走再回来内容不清空」）：

  [1] 「提示词方案」页点该行「编辑」→ 自动跳 prompt.html，并把方案内容回填到对应控件
      （服务器 / 模型类型 / 模型 / Base_url / 系统提示词 / 用户提示词 / API_key）。
      ⚠️ 同时断言**没有 JS 异常**：修复前这里 pbPickCombo(comboServer,…) 会抛
         TypeError（下拉还没初始化）、整段回填中断 —— 页面是全空的。
  [2] 回填后进入「草稿会话」：「清空草稿」按钮出现、sessionStorage 里有草稿。
  [3] 在构造页改内容 → 跳到别的页面（侧栏链接）→ 再回来，内容**不清空**（含改过的部分），
      且 pdEditId 仍在 —— 保证再保存是「覆盖原方案」而不是「新增一份」。
  [4] 保存 → 服务端按 id 覆盖（方案行数不变、内容已更新）+ 草稿自动清除。
  [5] 「提示词测试」页点「修改」→ 同一条链路（跳转 + 回填 + 开草稿）。
  [6] 点「清空草稿」→ 刷新后回到空白构造页、草稿没了、按钮隐藏。
  [7] 直接打开构造页不受影响：无草稿、按钮隐藏、表单空白。
  [7b] 「表结构提取」页点「复制」→ 弹窗确认 → 跳构造页，表结构（前缀句 + 表结构）
       **整段覆盖**进系统提示词，并开草稿（跳走再回来内容仍在）。选「取消」则什么都不发生。
       （10-09 第二件事，用户点名。）
  [8] 全程无 JS 异常。
  [9] 收尾：tst_plan 回到跑之前的基线。

前置：应用已在 8502 运行；`pip install websocket-client`；本机装有 Edge。
结果写 _ui_edit_check.txt；退出码 0 = 全部通过，1 = 有失败项。
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
OUT = os.path.join(HERE, "_ui_edit_check.txt")
DB_PATH = os.environ.get("PROMPTDECK_DB") or os.path.join(HERE, "data", "prompt_test_DB_twe.db")
APP = os.environ.get("PD_APP_URL") or "http://127.0.0.1:8502/"
PORT = 9351
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

# ⚠️ 本机有 http_proxy 且没有 no_proxy ⇒ urllib 会把 127.0.0.1 也送去代理、拿到 502。
#    显式用空 ProxyHandler 绕开（CDP 那条 websocket 也要传 http_proxy_host=None）。
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

HOOK = """window.__pdErrors = [];
window.addEventListener('error', function(e){ window.__pdErrors.push(String(e.message)); });
window.addEventListener('unhandledrejection', function(e){
  window.__pdErrors.push('unhandledrejection: ' + String(e.reason));
});
"""

buf = io.StringIO()
FAILS = []
events = []


def w(s=""):
    buf.write(str(s) + "\n")


def chk(label, ok, detail=""):
    w("  [%s] %s%s" % ("PASS" if ok else "FAIL", label, ("  " + str(detail)) if detail else ""))
    if not ok:
        FAILS.append(label)


def api(method, path, body=None):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    req = urllib.request.Request(APP.rstrip("/") + path, data=data, headers=headers, method=method)
    with OPENER.open(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def db_rows(table):
    with sqlite3.connect(DB_PATH) as c:
        return sorted(r[0] for r in c.execute("SELECT id FROM %s" % table).fetchall())


# ---------------------------------------------------------------- 存档隔离
# ⚠️ 10-09 起「表结构提取」页会把连上的库**存档**到 tst_db_conn，并在下次打开时
#    自动恢复。本脚本的 [7b] 走的是「左栏起始为空 → 导入我这一张库 → 选第一张表」，
#    一旦别人的存档在库里，选的就会是**别人库里的表**，整条链路集体假失败（踩过）。
#    ⇒ 跑前把存档整表快照下来并清空，收尾**只补缺地**放回去（不删测试期间新写的行）。
def conn_archive_snapshot():
    """tst_db_conn 全量快照 (cols, rows)；表还没就位则 None。"""
    try:
        with sqlite3.connect(DB_PATH) as c:
            cols = [r[1] for r in c.execute("PRAGMA table_info(tst_db_conn)")]
            rows = [tuple(r) for r in c.execute("SELECT * FROM tst_db_conn")]
        return (cols, rows) if cols else None
    except sqlite3.Error:
        return None


def conn_archive_clear():
    try:
        with sqlite3.connect(DB_PATH) as c:
            c.execute("DELETE FROM tst_db_conn")
            c.commit()
    except sqlite3.Error:
        pass


def conn_archive_restore(snap):
    """把跑前那份存档放回去。**只补缺的行** —— 测试期间用户新存的连接不能被冲掉。"""
    if not snap:
        return 0
    cols, rows = snap
    n = 0
    try:
        with sqlite3.connect(DB_PATH) as c:
            have = {(r[0], r[1]) for r in
                    c.execute("SELECT conn_name, conn_kind FROM tst_db_conn")}
            ph = ",".join("?" * len(cols))
            for row in rows:
                d = dict(zip(cols, row))
                if (d.get("conn_name"), d.get("conn_kind")) not in have:
                    c.execute("INSERT INTO tst_db_conn (%s) VALUES (%s)"
                              % (",".join(cols), ph), row)
                    n += 1
            c.commit()
    except sqlite3.Error:
        pass
    return n


def db_plan(plan_id):
    """方案主体 + 系统提示词（seq_no=0）。"""
    with sqlite3.connect(DB_PATH) as c:
        row = c.execute("SELECT id, plan_name, base_url FROM tst_plan WHERE id=?",
                        (plan_id,)).fetchone()
        sys_txt = c.execute("SELECT prompt_content FROM tst_plan_prompt"
                            " WHERE plan_id=? AND seq_no=0", (plan_id,)).fetchone()
    return {"id": row[0] if row else None,
            "name": row[1] if row else None,
            "base": row[2] if row else None,
            "sys": (sys_txt[0] if sys_txt else None)}


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
            d = json.loads(raw)
            if d.get("id") == mid:
                if "error" in d:
                    raise RuntimeError("%s -> %s" % (method, d["error"]))
                return d.get("result", {})
            if "method" in d:
                events.append(d)
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
                d = json.loads(raw)
            except Exception:
                continue
            if "method" in d:
                events.append(d)
        self.ws.settimeout(30)

    def go(self, url, wait=4):
        self.send("Page.navigate", {"url": url})
        self.pump(wait)

    def ev(self, expr):
        r = self.send("Runtime.evaluate",
                      {"expression": expr, "returnByValue": True, "awaitPromise": True})
        if "exceptionDetails" in r:
            return "__JSERROR__: " + str(
                r["exceptionDetails"].get("exception", {}).get("description"))[:300]
        return r.get("result", {}).get("value")


# ---------------------------------------------------------------- JS 片段
READ_STATE = """(function(){
  function val(sel){ var el = document.querySelector(sel); return el ? el.value : null; }
  var b = document.getElementById('btnClearDraft');
  var draft = null;
  try{ draft = JSON.parse(sessionStorage.getItem('pd_prompt_draft') || 'null'); }catch(e){ draft = 'ERR'; }
  return JSON.stringify({
    href: location.href,
    server: val('#comboServer input'),
    modelType: val('#comboModelType input'),
    model: val('#comboModel input'),
    base: val('#baseUrl'),
    sys: val('#sysPrompt'),
    user: val('#userPrompt'),
    apiKey: val('#apiKey'),
    pdEditId: (typeof pdEditId === 'undefined' ? 'NO_VAR' : pdEditId),
    pdEditName: (typeof pdEditName === 'undefined' ? 'NO_VAR' : pdEditName),
    btnHidden: b ? !!b.hidden : 'NO_BTN',
    btnVisible: b ? !!(b.offsetParent !== null) : 'NO_BTN',
    draft: draft === null ? null : {
      editId: draft.editId, editName: draft.editName, server: draft.server,
      model: draft.model, sys: draft.sys, apiKey: draft.apiKey
    },
    errors: (window.__pdErrors || []).slice(0, 6)
  });
})()"""

CLICK_TEXT = """(function(){
  var rows = document.querySelectorAll('#tbody tr');
  for (var i=0;i<rows.length;i++){
    if ((rows[i].textContent||'').indexOf(%s) > -1){
      var b = rows[i].querySelector('[data-edit]');
      if (b){ b.click(); return 'clicked'; }
      return 'no-button';
    }
  }
  return 'row-not-found';
})()"""


# ---------------------------------------------------------------- 表结构页 → 构造页
# 10-09 用户点名：表结构页点「复制」后弹窗确认 → 跳「提示词构造」页、表结构自动填进系统提示词。
PREFIX = "你是一个专业的sql语句工程师，现有表结构如下，根据用户问题返回sql语句"
# 16:32 用户点名追加的输出约束（逐字原话）：模型只给 SQL、别带解释 / 围栏，
# 这样「链接数据库调试」把它送进 DBeaver 后能直接跑。
PREFIX_TAIL = "不要有其他解释，仅返回sql语句"
# 整段前缀（含两句 + 各自的空行）
PREFIX_FULL = PREFIX + "\n\n" + PREFIX_TAIL + "\n\n"

TS_PICK_FIRST = """(function(){
  var it = document.querySelectorAll('#dbTree .tbl-item');
  if (!it.length) return JSON.stringify({n: 0});
  it[0].click();
  return JSON.stringify({n: it.length, name: it[0].getAttribute('data-table')});
})()"""

# 直接调原型的 renderSchema() 生成表内容 —— 绕开「提取表结构」那一步。
# ⚠️ 那一步在应用里会真调 DeepSeek 给「库里没注释」的表 / 字段补说明（最长 120s），
#    本节验的是「复制 → 确认 → 跳转填系统提示词」这条链路，不该被外部模型拖成活体。
TS_RENDER = """(function(){
  renderSchema();
  return JSON.stringify({extracted: extracted,
    text: (document.getElementById('schText') || {}).value || ''});
})()"""

TS_COPY = """(function(){
  var b = document.getElementById('btnCopy');
  if (!b) return JSON.stringify({err: '__NOBTN__'});
  b.click();
  var m = document.getElementById('copyMask');
  return JSON.stringify({
    shown: m ? m.classList.contains('show') : 'NO_MASK',
    toast: String((document.getElementById('toastWrap')||{}).textContent||'')
  });
})()"""

TS_STATE = """(function(){
  var m = document.getElementById('copyMask');
  var held = null;
  try{ held = JSON.parse(sessionStorage.getItem('pd_schema_handoff') || 'null'); }catch(e){ held = 'ERR'; }
  return JSON.stringify({
    href: location.href,
    shown: m ? m.classList.contains('show') : 'NO_MASK',
    held: (held && typeof held === 'object')
            ? {len: String(held.text||'').length, head: String(held.text||'').slice(0, 46)}
            : held
  });
})()"""

HANDOFF_LEFT = ("(function(){try{return sessionStorage.getItem('pd_schema_handoff')===null;}"
                "catch(e){return 'ERR';}})()")


# ---------------------------------------------------------------- 链接数据库调试
# 10-09 用户点名：模型输出区新增「链接数据库调试」按钮。
# 本节钉三件事：
#   ① 前置条件不满足时**置灰**（aria-disabled=true）且 title 说明缺什么；
#   ② 生成 SQL 之后自动变可点（不必刷新）；
#   ③ 点它真调后端 —— 但**绝不能真把 DBeaver 拉起来**，所以先把 PD_DBG_API 换成探针。
DBG_STATE = """(function(){
  var b = document.getElementById('btnDbgLink');
  if (!b) return JSON.stringify({err: '__NOBTN__'});
  var meta = null;
  try{ meta = JSON.parse(sessionStorage.getItem('pd_db_link_meta') || 'null'); }catch(e){}
  return JSON.stringify({
    off: b.getAttribute('aria-disabled'),
    title: b.title || '',
    label: (b.textContent || '').replace(/\\s+/g, ' ').trim(),
    out: (document.getElementById('modelOut') || {}).value || '',
    meta: meta ? {kind: meta.kind, name: meta.name, mode: meta.mode,
                  hasFile: !!meta.file, tables: (meta.tables || []).length,
                  hasPwd: ('pwd' in meta) || ('password' in meta)} : null
  });
})()"""

# 把真接口换成探针：记录「有没有被调、传了什么」，不去启动任何 GUI 进程
DBG_PROBE = """(function(){
  window.__pdDbgCalls = [];
  window.PD_DBG_API = function(meta, sql){
    window.__pdDbgCalls.push({kind: meta.kind, name: meta.name, sqlLen: (sql||'').length});
  };
  return 'patched';
})()"""

DBG_CALLS = "(function(){return JSON.stringify(window.__pdDbgCalls || []);})()"

DBG_SET_OUT = """(function(t){
  var el = document.getElementById('modelOut');
  el.value = t;
  el.classList.remove('is-error');
  pdDbgSync();
  return 'set';
})(%s)"""

DBG_SET_ERR = """(function(){
  var el = document.getElementById('modelOut');
  el.value = '链接模型失败\\n\\n原因：模拟失败';
  el.classList.add('is-error');
  pdDbgSync();
  return 'set-err';
})()"""


# ---------------------------------------------------------------- 页面内调试弹窗
# 10-09 用户点名：模型输出区再增一个「页面内调试」按钮，开三栏弹窗。
# 本节钉六件事：
#   ① 按钮存在、前置条件与「链接数据库调试」同口径（同一份 SQL / 同一份元数据）；
#   ② 弹窗能打开，三栏骨架齐（表结构 / SQL / 结果），Esc 与遮罩能关；
#   ③ 点「执行」才发请求 —— **打开时不该自动发**（服务器型要先填密码）；
#   ④ PDX_RUN_API 收到的参数形状对（kind / meta / pwd / sql），且**密码不在 meta 里**；
#   ⑤ 请求结束后密码输入框被清空（不留存）；
#   ⑥ 真调后端时（不替换探针）能拿到真结果集并渲染成表格。
PDX_STATE = """(function(){
  var b = document.getElementById('btnDbgRun');
  if (!b) return JSON.stringify({err: '__NOBTN__'});
  var m = document.getElementById('dbgRunMask');
  var tree = document.getElementById('pdxTree');
  return JSON.stringify({
    off: b.getAttribute('aria-disabled'),
    label: (b.textContent || '').replace(/\\s+/g, ' ').trim(),
    open: m ? m.classList.contains('show') : null,
    tables: tree ? tree.querySelectorAll('.pdx-tb').length : -1,
    cols: tree ? tree.querySelectorAll('.pdx-col').length : -1,
    colText: (function(){
      var a = [], n = tree ? tree.querySelectorAll('.pdx-cn') : [];
      for (var i = 0; i < n.length; i++) a.push(n[i].textContent);
      return a;
    })(),
    tbText: (function(){
      var a = [], n = tree ? tree.querySelectorAll('.pdx-tb-h span:first-of-type') : [];
      for (var i = 0; i < n.length; i++) a.push(n[i].textContent);
      return a;
    })(),
    fkText: (function(){
      var a = [], n = tree ? tree.querySelectorAll('.pdx-fk') : [];
      for (var i = 0; i < n.length; i++) a.push(n[i].textContent.replace(/\\s+/g,' ').trim());
      return a;
    })(),
    pkCount: tree ? tree.querySelectorAll('.pdx-pk').length : -1,
    sql: (document.getElementById('pdxSql') || {}).textContent || '',
    pwdVisible: (function(){
      var r = document.getElementById('pdxPwdRow');
      return r ? r.style.display !== 'none' : null;
    })(),
    resRows: document.querySelectorAll('#pdxResult table.pdx-tab tbody tr').length,
    resHead: (function(){
      var th = document.querySelectorAll('#pdxResult table.pdx-tab thead th');
      var a = []; for (var i = 0; i < th.length; i++) a.push(th[i].textContent);
      return a;
    })(),
    status: (document.getElementById('pdxStatus') || {}).textContent || '',
    alert: (document.querySelector('#pdxResult .pdx-alert') || {}).textContent || ''
  });
})()"""

# 把 PDX_RUN_API 换成探针：只记录参数，不发请求
PDX_PROBE = """(function(){
  window.__pdPdxCalls = [];
  window.PDX_RUN_API = function(req, onOk, onErr, onDone){
    window.__pdPdxCalls.push({
      kind: req.kind,
      hasMeta: !!(req.meta && req.meta.kind),
      metaHasPwd: !!(req.meta && (('pwd' in req.meta) || ('password' in req.meta))),
      pwdLen: (req.pwd || '').length,
      sqlLen: (req.sql || '').length
    });
    onOk({ok:true, columns:[{key:'c0',title:'plan_name'}],
           rows:[{c0:'E2E 方案'}], elapsed:7, rowCount:1, truncated:false});
    if (onDone) onDone();
  };
  return 'patched';
})()"""

PDX_CALLS = "(function(){return JSON.stringify(window.__pdPdxCalls || []);})()"

# 恢复页面自带的真接线：探针是给 window.PDX_RUN_API 赋了值，而页面里的**裸函数声明**
# 在全局是不可删除的（delete 不掉）⇒ 只能重新加载页面，拿回真正的接线。
PDX_RELOAD = "prompt.html"

DBG_CLICK = "(function(){document.getElementById('btnDbgLink').click();return 'clicked';})()"


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    if not os.path.exists(EDGE):
        w("跳过：未找到 Edge（%s）" % EDGE)
        print("跳过：未找到 Edge")
        return 0

    stamp = str(int(time.time()))
    name = "UI复现编辑流-" + stamp
    plan_id = [None]
    cleaned = [False]

    base_plans = db_rows("tst_plan")
    # ⚠️ 存档隔离：本脚本前提是「左栏起始为空」（[7b] 选的是全局第一张表）。
    #    先快照 + 清空 tst_db_conn，收尾原样放回 —— 否则别人存过的连接会被
    #    自动恢复进左栏，选中它库里的表，整条链路集体假失败。
    conn_snap = conn_archive_snapshot()
    conn_archive_clear()
    base_conns = db_rows("tst_db_conn") if conn_snap is not None else None
    servers = api("GET", "/api/srv")
    models = api("GET", "/api/mdl")
    srv = servers[0]
    mdl = next((m for m in models if m.get("platform") == srv.get("platform")), models[0])
    SYS0 = "系统提示词-原值-" + stamp
    USER0 = "用户提示词-原值-" + stamp
    KEY0 = "sk-ui-edit-" + stamp

    res = api("POST", "/api/tp", {
        "name": name, "server": srv.get("platform"), "base": srv.get("api"),
        "model": mdl.get("model"), "modelType": mdl.get("type"),
        "sys": SYS0, "user": [USER0], "apiKey": KEY0,
    })
    plan_id[0] = res.get("id")
    # ⚠️ 「保存是覆盖不是新增」要跟**建完测试方案之后**的快照比 ——
    #    拿 base_plans（还没建方案时的空快照）比，必然多出刚建的那一条（踩过）。
    mid_plans = db_rows("tst_plan")

    tmp = tempfile.mkdtemp(prefix="pd_ui_edit")
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
                with OPENER.open("http://127.0.0.1:%d/json/version" % PORT, timeout=3) as r:
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
        c.send("Page.addScriptToEvaluateOnNewDocument", {"source": HOOK})

        w("=" * 76)
        w("PromptDeck（Django 版）· 跨页编辑流 + 构造页草稿（真实浏览器 Edge/CDP）")
        w("库文件：%s（tst_plan 基线 %d 条）" % (DB_PATH, len(base_plans)))
        w("方案：%s（id=%s / %s / %s）" % (name, plan_id[0], srv.get("platform"), mdl.get("model")))
        w("=" * 76)

        # ============================================================ [1][2]
        w("")
        w("■ [1][2] 「提示词方案」页点「编辑」→ 跳构造页回填 + 开草稿")
        c.go(APP + "prompt_scheme.html", wait=4)
        chk("方案库页列出该方案", c.ev(
            "(function(){var r=document.querySelectorAll('#tbody tr');return r.length;})()") >= 1)
        chk("点中该行「编辑」", c.ev(CLICK_TEXT % json.dumps(name)) == "clicked")
        c.pump(5)

        st = json.loads(c.ev(READ_STATE))
        chk("已跳到 prompt.html", "prompt.html" in (st.get("href") or ""), st.get("href"))
        chk("服务器 / 模型类型 / 模型 三项回填正确",
            st.get("server") == srv.get("platform") and st.get("modelType") == mdl.get("type")
            and st.get("model") == mdl.get("model"),
            "server=%r type=%r model=%r" % (st.get("server"), st.get("modelType"), st.get("model")))
        chk("Base_url 回填正确", st.get("base") == srv.get("api"), st.get("base"))
        chk("系统提示词 / 用户提示词 / API_key 回填正确",
            st.get("sys") == SYS0 and st.get("user") == USER0 and st.get("apiKey") == KEY0,
            "sys=%r user=%r key=%r" % (st.get("sys"), st.get("user"), st.get("apiKey")))
        chk("pdEditId 记住了要覆盖的方案", st.get("pdEditId") == plan_id[0], st.get("pdEditId"))
        chk("页面加载期无 JS 异常（修复前这里 pbPickCombo 抛 TypeError）",
            not st.get("errors"), st.get("errors"))
        chk("一次性的回填入口已消费（地址栏变成 ?pdEditOff=1）",
            "pdEditOff=1" in (st.get("href") or ""), st.get("href"))
        chk("「清空草稿」按钮出现且可见",
            st.get("btnHidden") is False and st.get("btnVisible") is True,
            "hidden=%s visible=%s" % (st.get("btnHidden"), st.get("btnVisible")))
        chk("草稿已落 sessionStorage，且带上了 editId / editName（否则再保存会变成新增）",
            isinstance(st.get("draft"), dict) and st["draft"].get("editId") == plan_id[0]
            and st["draft"].get("editName") == name,
            st.get("draft"))

        # ============================================================ [3]
        w("")
        w("■ [3] 在构造页改内容 → 跳去别的页面 → 回来：内容不清空")
        c.ev("""(function(){
          var s = document.getElementById('sysPrompt');
          s.value = s.value + '-已改';
          s.dispatchEvent(new Event('input', { bubbles:true }));
          return s.value;
        })()""")
        c.pump(0.6)   # 等 150ms 的合并写盘
        chk("侧栏点去「服务器列表」", c.ev(
            "(function(){var a=document.querySelector('a[href=\"list_server.html\"]');"
            "if(a){a.click();return 'clicked';}return 'no-link';})()") == "clicked")
        c.pump(4)
        chk("确实到了别的页面", "list_server.html" in (c.ev("location.href") or ""),
            c.ev("location.href"))

        chk("从别的页面点回「提示词构造」", c.ev(
            "(function(){var a=document.querySelector('a[href=\"prompt.html\"]');"
            "if(a){a.click();return 'clicked';}return 'no-link';})()") == "clicked")
        c.pump(5)
        st2 = json.loads(c.ev(READ_STATE))
        chk("回到 construct 页（无任何 query 参数）",
            (st2.get("href") or "").rstrip("/").endswith("prompt.html"), st2.get("href"))
        chk("★ 跳走再回来内容没清空：服务器 / 模型仍选中",
            st2.get("server") == srv.get("platform") and st2.get("model") == mdl.get("model"),
            "server=%r model=%r" % (st2.get("server"), st2.get("model")))
        chk("★ 改过的系统提示词也还在",
            st2.get("sys") == SYS0 + "-已改", st2.get("sys"))
        chk("用户提示词 / API_key 仍在",
            st2.get("user") == USER0 and st2.get("apiKey") == KEY0,
            "user=%r key=%r" % (st2.get("user"), st2.get("apiKey")))
        chk("pdEditId 仍指向原方案（再保存是覆盖、不是新增）",
            st2.get("pdEditId") == plan_id[0], st2.get("pdEditId"))
        chk("恢复过程无 JS 异常", not st2.get("errors"), st2.get("errors"))

        # ============================================================ [4]
        w("")
        w("■ [4] 保存 → 服务端按 id 覆盖 + 草稿自动清除")
        c.ev("document.getElementById('btnSave').click()")
        c.pump(1.2)
        chk("保存弹窗按编辑流带出原方案名",
            c.ev("(function(){var e=document.getElementById('saveName');"
                 "return e ? e.value : 'NO_INPUT';})()") == name)
        c.ev("document.getElementById('saveConfirm').click()")
        c.pump(6)

        chk("保存后方案行数不变（是覆盖、不是新增）", db_rows("tst_plan") == mid_plans,
            "建完后=%s 保存后=%s" % (mid_plans, db_rows("tst_plan")))
        row = db_plan(plan_id[0])
        chk("库里该方案的系统提示词已更新为改后的值", row.get("sys") == SYS0 + "-已改", row)
        after = c.ev("(function(){try{return sessionStorage.getItem('pd_prompt_draft')===null;}"
                     "catch(e){return 'ERR';}})()")
        chk("草稿已被自动清除", after is True, after)

        # ============================================================ [5]
        w("")
        w("■ [5] 「提示词测试」页点「修改」→ 同一条链路")
        c.go(APP + "prompt_test.html", wait=5)
        chk("测试页左栏选中该方案", c.ev(
            "(function(){var it=document.querySelectorAll('#comboScheme .combo-item');"
            "for(var i=0;i<it.length;i++){if(it[i].getAttribute('data-value')===%s){"
            "it[i].dispatchEvent(new MouseEvent('mousedown',{bubbles:true,cancelable:true}));"
            "return 'picked';}}return 'not-found';})()" % json.dumps(name)) == "picked")
        c.pump(1.2)
        chk("点「修改」", c.ev(
            "(function(){var b=document.getElementById('btnEditScheme');"
            "if(!b)return 'no-btn';b.click();return 'clicked';})()") == "clicked")
        c.pump(5)
        st3 = json.loads(c.ev(READ_STATE))
        chk("测试页「修改」也跳到 prompt.html 且回填正确",
            "prompt.html" in (st3.get("href") or "")
            and st3.get("server") == srv.get("platform")
            and st3.get("sys") == SYS0 + "-已改",
            "href=%s server=%r sys=%r" % (st3.get("href"), st3.get("server"), st3.get("sys")))
        chk("测试页「修改」也开了草稿会话 + 按钮可见",
            isinstance(st3.get("draft"), dict) and st3.get("btnVisible") is True,
            "draft=%s visible=%s" % (bool(st3.get("draft")), st3.get("btnVisible")))
        chk("测试页「修改」链路无 JS 异常", not st3.get("errors"), st3.get("errors"))

        # ============================================================ [6]
        w("")
        w("■ [6] 点「清空草稿」→ 刷新后回到空白构造页")
        c.ev("document.getElementById('btnClearDraft').click()")
        c.pump(5)
        st4 = json.loads(c.ev(READ_STATE))
        chk("清空后表单是空白的（下拉 / 文案 / 密钥都空）",
            not st4.get("server") and not st4.get("model") and not st4.get("sys")
            and not st4.get("user") and not st4.get("apiKey"),
            "server=%r model=%r sys=%r user=%r key=%r" % (
                st4.get("server"), st4.get("model"), st4.get("sys"),
                st4.get("user"), st4.get("apiKey")))
        chk("清空后草稿没了 + 按钮隐藏",
            st4.get("draft") is None and st4.get("btnHidden") is True,
            "draft=%s hidden=%s" % (st4.get("draft"), st4.get("btnHidden")))
        chk("清空后 pdEditId 归零（不再指向原方案）",
            st4.get("pdEditId") in (None, 0, "null"), st4.get("pdEditId"))
        chk("清空链路无 JS 异常", not st4.get("errors"), st4.get("errors"))

        # ============================================================ [7]
        w("")
        w("■ [7] 直接打开构造页：不受影响（无草稿、按钮隐藏）")
        c.go(APP + "prompt.html", wait=5)
        st5 = json.loads(c.ev(READ_STATE))
        chk("直接打开时按钮隐藏、没有草稿、表单空白",
            st5.get("btnHidden") is True and st5.get("draft") is None
            and not st5.get("server") and not st5.get("sys"),
            "hidden=%s draft=%s server=%r" % (st5.get("btnHidden"), st5.get("draft"),
                                              st5.get("server")))
        chk("直接打开无 JS 异常", not st5.get("errors"), st5.get("errors"))

        # ============================================================ [7b]
        w("")
        w("■ [7b] 「表结构提取」页点「复制」→ 弹窗确认 → 跳构造页填系统提示词（10-09 用户点名）")
        c.go(APP + "Table_Structure_Extraction.html", wait=5)
        # 14-18 起页面**不再自动挂应用自带库** ⇒ 本段要先「导入」一个库才有表可勾。
        # 走页面自己的路径：选「SQLite」→ DOM.setFileInputFiles 挂应用库 → 确定。
        chk("导入前左栏为空（14-18：不再自动挂自带库）",
            json.loads(c.ev(TS_PICK_FIRST)).get("n") == 0)
        chk("导入：选「SQLite」弹出选文件窗口",
            c.ev("(function(){var it=document.querySelector("
                 "'#comboDbType .combo-item[data-value=\"SQLite\"]');"
                 "if(!it)return 'no-item';"
                 "it.dispatchEvent(new MouseEvent('mousedown',{bubbles:true,cancelable:true}));"
                 "var f=document.getElementById('fileMask');"
                 "return (f&&f.classList.contains('show'))?'opened':'no-mask';})()") == "opened")
        c.send("DOM.enable")
        doc = c.send("DOM.getDocument", {})
        node = c.send("DOM.querySelector",
                      {"nodeId": doc["root"]["nodeId"], "selector": "#fileInput"})["nodeId"]
        c.send("DOM.setFileInputFiles", {"files": [DB_PATH], "nodeId": node})
        c.pump(1.0)
        if not c.ev("String(window.pickedFile||'')"):
            c.ev("document.getElementById('fileInput')"
                 ".dispatchEvent(new Event('change',{bubbles:true}))")
            c.pump(0.5)
        chk("导入：文件已挂到 #fileInput", bool(c.ev("String(window.pickedFile||'')")))
        c.ev("document.getElementById('fileConfirm').click()")
        c.pump(6)

        # ① 没提取过就点复制：仍按原型口径拦下（这一步不该弹窗）
        mk0 = json.loads(c.ev(TS_COPY))
        chk("没提取过就点「复制」：按原型口径拦下、不弹窗",
            "请先点「提取表结构」生成表结构" in str(mk0.get("toast")) and mk0.get("shown") is False, mk0)

        # ② 勾第一张表并生成表内容（直接调 renderSchema，绕开会真调 DeepSeek 的补注释那一步）
        tree = {"n": 0}
        for _ in range(16):
            tree = json.loads(c.ev(TS_PICK_FIRST))
            if tree.get("n"):
                break
            c.pump(1.0)
        tbl = tree.get("name")
        w("    左栏表数：%s，选了第一张：%s" % (tree.get("n"), tbl))
        chk("左栏已列出真实库的表", bool(tree.get("n")), tree)
        ren = json.loads(c.ev(TS_RENDER))
        w("    生成的表内容首行：%r" % str(ren.get("text", "")).splitlines()[:1])
        chk("已生成表内容（extracted=true）且文本里有该表名",
            ren.get("extracted") is True and tbl and tbl in str(ren.get("text")), ren.get("extracted"))

        # ③ 点复制 → 弹窗；选「取消」：关掉弹窗、仍停在表结构页、没有留下待填内容
        mk1 = json.loads(c.ev(TS_COPY))
        chk("点「复制」后弹出确认框", mk1.get("shown") is True, mk1)
        c.pump(0.4)
        c.ev("document.getElementById('copyCancel').click()")
        c.pump(0.5)
        mk1b = json.loads(c.ev(TS_STATE))
        chk("选「取消」：弹窗关闭 + 仍停在表结构页 + 没留下待填内容",
            mk1b.get("shown") is False
            and "Table_Structure_Extraction.html" in (mk1b.get("href") or "")
            and mk1b.get("held") is None, mk1b)

        # ④ 再点复制 → 确认 → 跳构造页
        json.loads(c.ev(TS_COPY))
        c.pump(0.4)
        c.ev("document.getElementById('copyConfirm').click()")
        c.pump(6)
        st6 = json.loads(c.ev(READ_STATE))
        w("    跳到：%s" % st6.get("href"))
        chk("已跳到 prompt.html 且带上 ?pdSchema=1 入口",
            "prompt.html" in (st6.get("href") or ""), st6.get("href"))
        sys6 = str(st6.get("sys") or "")
        chk("服务端确实拿表结构填了系统提示词（不是空）", bool(sys6.strip()), len(sys6))
        chk("前缀句在**最开头**（用户逐字给的原话）", sys6.startswith(PREFIX), sys6[:60])
        # 16:32 用户点名追加的输出约束：必须在第一句之后、且与表结构之间还空一行
        chk("★ 第二句「不要有其他解释，仅返回sql语句」紧跟第一句（逐字原话）",
            sys6.startswith(PREFIX + "\n\n" + PREFIX_TAIL),
            repr(sys6[:len(PREFIX) + len(PREFIX_TAIL) + 6]))
        chk("★ 两句前缀与表结构之间各空一行，表结构里有刚选的那张表",
            sys6.startswith(PREFIX_FULL) and tbl in sys6, repr(sys6[:130]))
        chk("前缀句只出现一次（不是拼了两遍）", sys6.count(PREFIX) == 1, sys6.count(PREFIX))
        chk("★ 输出约束也只出现一次", sys6.count(PREFIX_TAIL) == 1, sys6.count(PREFIX_TAIL))

        # ---------- 交接载荷的结构（页面内调试弹窗左栏的数据源） ----------
        # pd_schema_handoff 在认领后会被清掉，所以这里验**同一个 session 标签页**里
        # 仍然留在的 pd_db_link_meta —— 它才是弹窗左栏的真正来源。
        meta7 = json.loads(c.ev("""(function(){
            var m = null;
            try{ m = JSON.parse(sessionStorage.getItem('pd_db_link_meta') || 'null'); }catch(e){}
            if (!m) return JSON.stringify({err:'__NOKEY__'});
            var t0 = (m.tables || [])[0] || {};
            return JSON.stringify({
              kind: m.kind, nTables: (m.tables || []).length,
              firstName: t0.name,
              firstCols: (t0.cols || []).length,
              firstColSample: (t0.cols || [])[0] || null,
              hasRows: (t0.rows != null),
              hasDesc: !!(t0.desc),
              nRels: (m.relations || []).length,
              relSample: (m.relations || [])[0] || null,
              allNamed: (m.tables || []).every(function(x){
                return typeof x === 'string' || !!(x && x.name); })
            });
        })()"""))
        chk("★ 交接后 pd_db_link_meta 仍在（弹窗左栏靠它，不靠会被清掉的 pd_schema_handoff）",
            "__NOKEY__" not in meta7.get("err", ""), meta7)
        chk("★ tables 已升级成结构化表清单（不再是纯表名数组）",
            meta7.get("nTables", 0) >= 1 and meta7.get("firstCols", 0) >= 1
            and meta7.get("allNamed") is True, meta7)
        chk("★ 每张表都带得到字段名/类型/说明四元组",
            isinstance(meta7.get("firstColSample"), list)
            and len(meta7["firstColSample"]) >= 3
            and str(meta7["firstColSample"][0]).strip() != "", meta7.get("firstColSample"))
        # ⚠️ 外键**不许假设库里一定有**（硬约束 5：基线一律现场取）。
        # 有就逐条验形状，没有就报现场情况、不判失败。
        if meta7.get("nRels"):
            rs = meta7.get("relSample") or {}
            chk("★ relations 走顶层且**带从表名**（弹窗靠它把外键挂到对应表下）",
                isinstance(rs, dict) and bool(rs.get("fromTable")), rs)
        else:
            chk("★ relations 字段存在（本次该库无外键，跳过逐条验形）",
                "relations" in c.ev("(function(){return sessionStorage.getItem('pd_db_link_meta')||'';})()"),
                "本次 nRels=0（该库本来就没有外键）")
        chk("★ 交接载荷里仍没有密码（安全口径没因为加字段而破）",
            "pwd" not in c.ev("(function(){return sessionStorage.getItem('pd_db_link_meta')||'';})()")
            and "password" not in c.ev("(function(){return sessionStorage.getItem('pd_db_link_meta')||'';})()"))
        # 这句约束的意义：模型只吐 SQL，粘进 DBeaver 能直接跑 ⇒ 表结构里不能夹解释性文字
        chk("★ 前缀里没有 Markdown 围栏 / 多余说明（否则 DBeaver 里跑不了）",
            "```" not in sys6[:len(PREFIX_FULL) + 4], repr(sys6[:len(PREFIX_FULL) + 4]))
        chk("一次性入口已消费：暂存清掉、地址栏变成 ?pdEditOff=1",
            c.ev(HANDOFF_LEFT) is True and "pdEditOff=1" in (st6.get("href") or ""),
            "left=%s href=%s" % (c.ev(HANDOFF_LEFT), st6.get("href")))
        chk("这条链路也开了草稿会话（按钮可见 + 草稿里的 sys 就是它）",
            st6.get("btnVisible") is True and isinstance(st6.get("draft"), dict)
            and str((st6.get("draft") or {}).get("sys") or "").startswith(PREFIX),
            "visible=%s draft.sys.head=%r" % (st6.get("btnVisible"),
                                              str((st6.get("draft") or {}).get("sys") or "")[:40]))

        # ⑤ 跳走再回来：内容仍在（与编辑流同一套草稿机制）
        c.ev("(function(){var a=document.querySelector('a[href=\"list_server.html\"]');"
             "if(a){a.click();return 'clicked';}return 'no-link';})()")
        c.pump(4)
        c.ev("(function(){var a=document.querySelector('a[href=\"prompt.html\"]');"
             "if(a){a.click();return 'clicked';}return 'no-link';})()")
        c.pump(5)
        st7 = json.loads(c.ev(READ_STATE))
        chk("★ 跳走再回来：表结构还在系统提示词里",
            str(st7.get("sys") or "").startswith(PREFIX), str(st7.get("sys") or "")[:60])
        chk("★ 跳走再回来：那句输出约束也在（草稿恢复的是整段）",
            PREFIX_TAIL in str(st7.get("sys") or ""), str(st7.get("sys") or "")[:90])
        chk("这条链路无 JS 异常", not st7.get("errors"), st7.get("errors"))

        # ============================================================ [7c]
        w("")
        w("■ [7c] 「链接数据库调试」按钮：前置条件 → 灰 / 可点（10-09 用户点名）")
        chk("先把 PD_DBG_API 换成探针（避免本用例真把 DBeaver 拉起来）",
            c.ev(DBG_PROBE) == "patched")

        d0 = json.loads(c.ev(DBG_STATE))
        chk("按钮文案就是「链接数据库调试」",
            "链接数据库调试" in str(d0.get("label") or ""), d0.get("label"))
        chk("表结构交接时数据库元数据也带过来了（kind / 表名齐）",
            isinstance(d0.get("meta"), dict) and d0["meta"].get("kind") == "SQLite"
            and d0["meta"].get("tables", 0) >= 1, d0.get("meta"))
        chk("★ 元数据里**没有密码**（安全口径）",
            isinstance(d0.get("meta"), dict) and d0["meta"].get("hasPwd") is False, d0.get("meta"))

        # ① 还没生成 SQL → 置灰，且 title 说清缺什么
        d1 = json.loads(c.ev(DBG_STATE))
        chk("没生成 SQL 时按钮置灰",
            d1.get("off") == "true", "off=%s title=%r" % (d1.get("off"), d1.get("title")))
        chk("置灰时 title 点明缺「请先生成 SQL」",
            "请先生成 SQL" in str(d1.get("title") or ""), d1.get("title"))
        chk("置灰时点它**不会**调后端", json.loads(c.ev(DBG_CALLS)) == [],
            json.loads(c.ev(DBG_CALLS)))

        # ② 生成 SQL → 自动变可点（不用刷新）
        sql = "SELECT plan_name, base_url FROM tst_plan WHERE plan_name LIKE '%E2E%';"
        c.ev(DBG_SET_OUT % json.dumps(sql))
        d2 = json.loads(c.ev(DBG_STATE))
        chk("★ 输出框一有 SQL，按钮自动变为可点",
            d2.get("off") == "false", "off=%s title=%r" % (d2.get("off"), d2.get("title")))
        chk("可点时 title 说的是「打开该数据库并粘贴这条 SQL」",
            "粘贴这条 SQL" in str(d2.get("title") or ""), d2.get("title"))

        # ③ 点它 → 真调到 PD_DBG_API，且带的是当前这条 SQL
        c.ev(DBG_CLICK)
        c.pump(0.5)
        calls = json.loads(c.ev(DBG_CALLS))
        chk("★ 点按钮调了一次后端接线，且参数带的是最新那条 SQL",
            len(calls) == 1 and calls[0].get("sqlLen") == len(sql)
            and calls[0].get("kind") == "SQLite", calls)
        chk("★ 在**生成 SQL 之前**点过灰按钮，一次都不该调后端（调用次数口径）",
            len(calls) == 1, calls)

        # ④ 输出变成失败态 → 立刻退回置灰（失败文案不是可调试的 SQL）
        c.ev(DBG_SET_ERR)
        d3 = json.loads(c.ev(DBG_STATE))
        chk("模型失败态下按钮退回置灰（不拿错误文案去调试）",
            d3.get("off") == "true", "off=%s" % d3.get("off"))

        # ⑤ 换个没有元数据的场景：清掉 pd_db_link_meta 后应提示缺连接信息
        c.ev("(function(){try{sessionStorage.removeItem('pd_db_link_meta');}catch(e){};"
             "document.getElementById('modelOut').value='SELECT 1;';pdDbgSync();return 1;})()")
        d4 = json.loads(c.ev(DBG_STATE))
        chk("没有数据库元数据时置灰，并提示去表结构提取页复制",
            d4.get("off") == "true"
            and "数据库表结构提取" in str(d4.get("title") or ""), d4.get("title"))

        # ⑥ 真调后端那条路也验一次（用 SQLite 指向一个**不存在**的文件 —— 不该起任何进程）
        res_d = api("POST", "/api/dbeaver/link", {
            "meta": {"kind": "SQLite", "name": "x", "file": "/no/such/file.db"},
            "sql": "SELECT 1;",
        })
        chk("后端对不存在的 SQLite 文件回明确提示（ok=false，不抛 500）",
            isinstance(res_d, dict) and res_d.get("ok") is False
            and "路径无效" in str(res_d.get("error") or ""), res_d)

        # ============================================================ [7d]
        w("")
        w("■ [7d] 「页面内调试」弹窗：三栏骨架 + 调用时机 + 密码不留存（10-09 用户点名）")
        # 先把 ⑤ 破坏掉的元数据放回去，并恢复一条正常 SQL。
        # ⚠️ 这里的形状**必须与表结构页 pdDbLinkMeta 现在的真实产物一致**：
        #    tables 是 [{name,rows,desc,cols:[[名,类型,说明,真实类型]]}]，
        #    外键走顶层 relations 且**带 fromTable**。照着真实结构写，
        #    才能测出「弹窗左栏渲染不出字段」这类问题。
        c.ev("""(function(){
            var meta = {kind:'SQLite', name:'prompt_test_DB_twe.db', mode:'file',
                        file:'/no/such/file.db',
                        tables:[
                          {name:'tst_plan', rows:1, desc:'提示词方案表',
                           cols:[['plan_name','TEXT','方案名称','TEXT'],
                                 ['base_url','TEXT','接口地址','TEXT'],
                                 ['id','pk','','']]},
                          {name:'tst_rec', rows:2, desc:'测试记录表',
                           cols:[['rec_id','pk','主键','INTEGER'],
                                 ['score','INT','得分','INTEGER'],
                                 ['plan_id','INT','关联方案','INTEGER']]}
                        ],
                        relations:[
                          {fromTable:'tst_rec', column:'plan_id', refTable:'tst_plan',
                           refColumn:'id', label:'外键关联', db:'prompt_test_DB_twe.db'}
                        ]};
            sessionStorage.setItem('pd_db_link_meta', JSON.stringify(meta));
            var el = document.getElementById('modelOut');
            el.value = "SELECT plan_name FROM tst_plan WHERE plan_name LIKE '%E2E%';";
            el.classList.remove('is-error');
            pdDbgSync();
            return 1;
        })()""")

        chk("换成探针（挡掉真请求，避免本节打后端）", c.ev(PDX_PROBE) == "patched")

        p0 = json.loads(c.ev(PDX_STATE))
        chk("★ 按钮文案是「页面内调试」（与「链接数据库调试」并存，两个都在）",
            p0.get("label") == "页面内调试" and "__NOBTN__" not in p0.get("err", ""), p0.get("label"))
        chk("与另一条路线**同口径**：同一份 SQL 决定置灰与否",
            p0.get("off") == "false", "off=%s" % p0.get("off"))
        chk("★ 打开前弹窗是关的、结果区没有表格",
            p0.get("open") is False and p0.get("resRows") == 0, p0)

        # ② 点它开弹窗：此刻**不该**发任何请求（服务器型要先填密码）
        c.ev("document.getElementById('btnDbgRun').click()")
        c.pump(0.4)
        p1 = json.loads(c.ev(PDX_STATE))
        chk("★ 点按钮弹窗打开", p1.get("open") is True, p1.get("open"))
        chk("★ 打开时**不自动发请求**（等用户填密码 / 点执行）",
            json.loads(c.ev(PDX_CALLS)) == [], json.loads(c.ev(PDX_CALLS)))
        chk("左栏渲染出 2 张表的表结构",
            p1.get("tables") == 2 and p1.get("cols") == 6,
            "tables=%s cols=%s" % (p1.get("tables"), p1.get("cols")))
        chk("★ 左栏列出的表名就是交接过来的那两张",
            p1.get("tbText") == ["tst_plan", "tst_rec"], p1.get("tbText"))
        chk("★ 左栏渲染出**真实字段名**（不是空壳 —— 形状对不上时这里会空）",
            p1.get("colText") == ["plan_name", "base_url", "id",
                                  "rec_id", "score", "plan_id"], p1.get("colText"))
        chk("★ 主键被标出来",
            p1.get("pkCount") == 2, p1.get("pkCount"))
        chk("★ 外键挂在对应表下（顶层 relations 带 fromTable 才挂得上）",
            len(p1.get("fkText") or []) == 1
            and "plan_id" in p1["fkText"][0]
            and "tst_plan" in p1["fkText"][0], p1.get("fkText"))
        chk("右上展示的就是模型那条 SQL",
            "SELECT plan_name FROM tst_plan" in str(p1.get("sql") or ""), p1.get("sql"))
        chk("★ SQLite 不需要密码 ⇒ 密码栏隐藏",
            p1.get("pwdVisible") is False, p1.get("pwdVisible"))

        # ③ 点「执行」才发请求，参数形状对
        c.ev("document.getElementById('pdxRefresh').click()")
        c.pump(0.5)
        pc = json.loads(c.ev(PDX_CALLS))
        chk("★ 点「执行」才调一次接线，且参数齐（kind/meta/sql）",
            len(pc) == 1 and pc[0].get("kind") == "SQLite"
            and pc[0].get("hasMeta") is True and pc[0].get("sqlLen") > 0, pc)
        chk("★ meta 里**不含密码**（密码只能走顶层 pwd 字段）",
            len(pc) == 1 and pc[0].get("metaHasPwd") is False, pc)
        p2 = json.loads(c.ev(PDX_STATE))
        chk("结果表格渲染出来了（表头 + 数据行）",
            p2.get("resHead") == ["plan_name"] and p2.get("resRows") == 1,
            "head=%s rows=%s" % (p2.get("resHead"), p2.get("resRows")))
        chk("状态行显示成功与耗时", "查询成功" in str(p2.get("status") or ""), p2.get("status"))

        # ④ Esc 关闭
        c.ev("document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}))")
        c.pump(0.3)
        chk("★ Esc 能关弹窗", json.loads(c.ev(PDX_STATE)).get("open") is False)

        # ⑤ 点遮罩关闭（点弹窗体内不关）
        c.ev("document.getElementById('btnDbgRun').click()")
        c.pump(0.3)
        c.ev("(function(){var m=document.getElementById('dbgRunMask');"
             "m.dispatchEvent(new MouseEvent('click',{bubbles:true}));return 1;})()")
        c.pump(0.3)
        chk("★ 点遮罩空白处能关弹窗",
            json.loads(c.ev(PDX_STATE)).get("open") is False)

        # ⑥ 服务器型：密码栏出现、密码随请求发出、**发完即清空**
        c.ev("""(function(){
            var meta = {kind:'MySQL', name:'demo', mode:'host',
                        host:'127.0.0.1', port:3306, user:'u', dbname:'d',
                        tables:[{name:'t1', rows:1, desc:'', cols:[['id','pk','','']]}],
                        relations:[]}
            sessionStorage.setItem('pd_db_link_meta', JSON.stringify(meta));
            pdDbgSync(); return 1;
        })()""")
        c.ev("document.getElementById('btnDbgRun').click()")
        c.pump(0.4)
        p3 = json.loads(c.ev(PDX_STATE))
        chk("★ 服务器型数据库显示密码栏", p3.get("pwdVisible") is True, p3.get("pwdVisible"))
        c.ev("(function(){document.getElementById('pdxPwd').value='PdE2e_Probe1';return 1;})()")
        c.ev("document.getElementById('pdxRefresh').click()")
        c.pump(0.5)
        pc2 = json.loads(c.ev(PDX_CALLS))
        chk("★ 密码随这一次请求发出（只走顶层 pwd）",
            len(pc2) == 2 and pc2[1].get("pwdLen") == len("PdE2e_Probe1")
            and pc2[1].get("metaHasPwd") is False, pc2)
        chk("★ 请求结束后密码输入框已清空（不留存）",
            c.ev("(function(){return document.getElementById('pdxPwd').value;})()") == "",
            c.ev("(function(){return document.getElementById('pdxPwd').value;})()"))
        chk("★ 密码没被写进 sessionStorage",
            "PdE2e_Probe1" not in c.ev("(function(){return sessionStorage.getItem('pd_db_link_meta')||'';})()"))

        # ⑦ **重载页面**拿回真接线（探针给 window.PDX_RUN_API 赋的值删不掉），
        #    再真打一次后端：SQLite 指向不存在的文件 ⇒ 该给友好报错，不该 500 / 白屏。
        c.go(APP + PDX_RELOAD, wait=5)
        chk("★ 重载后接线是页面自带的真实现（不是探针）",
            c.ev("(function(){"
                 "var s='';try{s=PDX_RUN_API.toString();}catch(e){s='__ERR__';}"
                 "return (s.indexOf('prototype')>=0 || s.indexOf('__pdPdxCalls')>=0)"
                 " ? 'probe' : 'real';})()") == "real")
        c.ev("""(function(){
            var meta = {kind:'SQLite', name:'nope', mode:'file', file:'/no/such/file.db',
                        tables:[{name:'t1', rows:1, desc:'', cols:[['id','pk','','']]}],
                        relations:[]}
            sessionStorage.setItem('pd_db_link_meta', JSON.stringify(meta));
            var el = document.getElementById('modelOut');
            el.value = 'SELECT * FROM t1;';
            el.classList.remove('is-error');
            pdDbgSync();
            return 1;
        })()""")
        c.ev("document.getElementById('btnDbgRun').click()")
        c.pump(0.4)
        c.ev("document.getElementById('pdxRefresh').click()")
        c.pump(1.5)
        p4 = json.loads(c.ev(PDX_STATE))
        chk("★ 真调后端：失败时错误条展示服务端原话（不白屏、不吞错）",
            "不存在" in str(p4.get("alert") or "")
            or "路径无效" in str(p4.get("alert") or "")
            or "打开 SQLite" in str(p4.get("alert") or ""), p4.get("alert"))
        chk("失败时状态行标红为 err",
            "失败" in str(p4.get("status") or ""), p4.get("status"))
        chk("★ 后端对写语句返回 ok=false（前端不崩、只是显示错误）",
            (lambda r: r.get("ok") is False)(
                api("POST", "/api/database/execute-sql",
                    {"kind": "SQLite", "meta": {"file": "/no/such/file.db"},
                     "sql": "DROP TABLE tst_plan"})))
        # 收尾：关掉弹窗，别影响后面 [8]
        c.ev("(function(){var m=document.getElementById('dbgRunMask');"
             "m.classList.remove('show');return 1;})()")

        chk("[7c] 全程无 JS 异常", not json.loads(c.ev(READ_STATE)).get("errors"))

        # ============================================================ [8]
        w("")
        w("■ [8] 全程 JS 异常汇总")
        exc = []
        for e in events:
            if e.get("method") == "Runtime.exceptionThrown":
                d = e.get("params", {}).get("exceptionDetails", {})
                exc.append(str(d.get("exception", {}).get("description"))[:160].replace("\n", " "))
            elif e.get("method") == "Log.entryAdded":
                p = e["params"]["entry"]
                # 忽略 favicon 之类静态资源 404（与本链路无关）
                if p.get("level") == "error" and "404" not in str(p.get("text")):
                    exc.append("LOG: " + str(p.get("text"))[:160])
        chk("不仅页面内钩子、CDP 侧也没抓到 JS 异常", not exc, exc[:3])

        # ============================================================ [9]
        w("")
        w("■ [9] 收尾（先自清理再断言回基线 —— 断言写在 finally 的清理之前会永远看到待删那条）")
        api("DELETE", "/api/tp", {"ids": [plan_id[0]]})
        cleaned[0] = True
        chk("tst_plan 回到跑之前的基线", db_rows("tst_plan") == base_plans,
            "base=%s now=%s" % (base_plans, db_rows("tst_plan")))

        # tst_db_conn：跑前已清空并快照 ⇒ 这里断言「跑完仍是空的」，
        # 真正的「把用户原来的存档放回去」在下面的 finally 里（异常也会执行）。
        if base_conns is not None:
            chk("★ tst_db_conn 跑完仍为空（页面自动存的那些由收尾统一放回原状）",
                db_rows("tst_db_conn") == [], db_rows("tst_db_conn"))
    finally:
        try:
            proc.terminate()
        except Exception:
            pass
        if plan_id[0] and not cleaned[0]:
            try:
                api("DELETE", "/api/tp", {"ids": [plan_id[0]]})
            except Exception as ex:
                w("!! 清理方案失败：%s" % ex)
        # 不管中间成败，跑前那份存档都要放回去（只补缺，不删期间新写的行）
        _back = conn_archive_restore(conn_snap)
        if conn_snap:
            w("（已放回跑前的存档 %d 行）" % _back)

    w("")
    w("=" * 76)
    w("结果：%s（失败 %d 项）" % ("全部通过" if not FAILS else "有失败", len(FAILS)))
    for f in FAILS:
        w("  - " + f)
    w("=" * 76)

    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write(buf.getvalue())
    print(buf.getvalue())
    print("报告已写入 %s" % OUT)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
