# -*- coding: utf-8 -*-
"""真实浏览器端到端校验（Edge + CDP，不依赖 playwright）。

针对 Starlette 版：页面是顶层文档（不再有 iframe），直接用 CDP 操作 DOM。

覆盖链路：
    [1] 打开「模型列表」        -> 行数 = 库内 mdl 行数，下拉选项来自后端
    [2] 平台列外链口径          -> href == srv.home_url
    [3] 点「添加模型」进新增页  -> 填表（含只读多选类型）-> 保存
    [4] 自动回到列表            -> 新记录可见，行数 +1，且新平台已自动补进 srv
    [5] 再存一条
    [6] 勾选两条 -> 批量删除    -> 列表与库同步回落
    [7] 顺手验证导航：进「服务器列表」能拿到后端数据
    [8] 提示词构造：三个下拉选项 = 库内 srv / mdl 数据
    [9] 服务器联动 -> Base_url 自动填充，模型/类型下拉收窄
    [10] 类型联动 -> 模型下拉再按类型收窄
    [11] 选模型 -> 费用标签取库内计费
    [12] 必填校验原样：未填系统提示词 / API_key 点「调试」被拦下
    [13] 真实调用：base_url 指向本地 mock 模型服务 -> 点击后输出区先显示「正在链接模型」
         占位，返回后显示模型真实文本；tokens / 费用来自模型返回的真实 usage 与库内单价；
         并核对 mock 端确实收到了正确的 path / Authorization / model / messages
         （服务端未做二次校验，401 由模型服务给出）
    [14] 掩码开关 / 多轮会话加轮 / 清空用户提示词 / 复制
    [23] 两个列表页「修改」弹窗的字段与控件形态，逐项对齐「新增」页
         （期望值现场取自新增页 .form-body，不硬编码）+ 新控件行为（下拉/多选/只读/数字过滤）
    [15] 无 JS 异常

前置：
    1. 应用已在 8502 运行：python server.py --port 8502
    2. pip install websocket-client；本机装有 Edge

结果写 _ui_check.txt；结束时自动清理本次产生的「UI复现*」测试数据。
退出码 0 = 全部通过，1 = 有失败项。
"""
import http.server
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

try:
    import websocket  # websocket-client
except ImportError:
    print("跳过：未安装 websocket-client（pip install websocket-client）")
    sys.exit(0)

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "_ui_check.txt")
# 被测应用地址：默认本地开发服务；跑「创空间产物 + 子路径反代」时用 PD_APP_URL 覆盖，
# 例如 PD_APP_URL=http://127.0.0.1:8799/studio/app/
APP = os.environ.get("PD_APP_URL") or "http://127.0.0.1:8502/"
PORT = 9341
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

# ---------------------------------------------------------------- mock 模型服务
# 本机起一个 OpenAI 兼容的假模型：用来验证「浏览器 -> 后端 -> 模型 -> 回填」这条链路
# 真的在发 HTTP、真的用了带外凭据、真的把返回文本与 usage 落到了页面上。
# 好处：不依赖外网、不花用户的钱、不碰用户的 key，而且返回可精确断言。
MOCK_PORT = 8791
MOCK_KEY = "sk-mock-verify-key"
MOCK_TEXT = "你好，我是本地模拟模型的真实返回内容。"
# 推理模型场景：正式回答为空、只有 reasoning_content（Qwen3 / DeepSeek-R1 常见）
MOCK_REASON_TEXT = "（思考内容）我先想一下再回答……"
MOCK_USAGE = {"prompt_tokens": 12, "completion_tokens": 34, "total_tokens": 46}
# 测试用模型单价（写死，便于精确断言费用）：in 1.00 / out 2.00，单位 K
MOCK_IN_PRICE, MOCK_OUT_PRICE, MOCK_UNIT = 1.0, 2.0, "K"
MOCK_HITS = []


class MockModelHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8", "replace")
        try:
            body = json.loads(raw)
        except Exception:
            body = {"__raw__": raw}
        MOCK_HITS.append({
            "path": self.path,
            "auth": self.headers.get("Authorization"),
            "ctype": self.headers.get("Content-Type"),
            "body": body,
        })
        # 凭据不对就按真实模型服务的方式回 401 —— 服务端不替它做校验
        if (self.headers.get("Authorization") or "") != "Bearer " + MOCK_KEY:
            self._send(401, {"error": {"message": "Invalid API key provided."}})
            return
        if body.get("model") == "mock-reasoning-only":
            self._send(200, {
                "id": "chatcmpl-mock-r", "object": "chat.completion",
                "model": body.get("model"),
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "",
                                         "reasoning_content": MOCK_REASON_TEXT}}],
                "usage": dict(MOCK_USAGE),
            })
            return
        # 多轮时按「这是第几轮」返回可区分的文本，好断言「逐轮各产出一条回答」。
        # 单轮（只有 1 条 user 消息）仍返回 MOCK_TEXT，供既有单轮用例精确断言。
        msgs = body.get("messages") or []
        n_user = len([m for m in msgs if isinstance(m, dict) and m.get("role") == "user"])
        text = MOCK_TEXT if n_user <= 1 else (MOCK_TEXT + "·第 %d 轮" % n_user)
        self._send(200, {
            "id": "chatcmpl-mock", "object": "chat.completion",
            "model": body.get("model") or "mock",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}],
            "usage": dict(MOCK_USAGE),
        })

    def log_message(self, *args):
        pass


def start_mock():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", MOCK_PORT), MockModelHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def post_json(path, obj, timeout=30):
    """直接打后端接口（不走浏览器），用于验证错误链路。"""
    req = urllib.request.Request(
        APP.rstrip("/") + path,
        data=json.dumps(obj, ensure_ascii=False).encode("utf-8"),
        method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fmt_num(v):
    """页面 fmtNum 的等价实现：金额不做四舍五入，最多 10 位小数并去掉浮点误差尾巴。

    对应 static/*.html 里的
        n.toFixed(10).replace(/0+$/,"").replace(/\\.$/,"")
    """
    n = float(v)
    s = ("%.10f" % n).rstrip("0").rstrip(".")
    return s if s not in ("", "-") else "0"


def fmt_price(v):
    """页面 fmtPrice 的等价实现（计费单价按库内原文展示，原样保留小数位数）。"""
    if v is None:
        return "0.00"
    return str(v).strip() or "0.00"


sys.path.insert(0, HERE)

# ---------------------------------------------------------------- 核对用的数据层
# 第三次迭代换了库：应用读写的是 promptdeck/data/prompt_test_DB_twe.db 的
# srv / mdl / tst_plan / tst_plan_prompt / tst_api_key 表。
# 上一版这里写的是 `import db as appdb` —— 导的是 Starlette 版遗留的 db.py，
# 它连的是**旧库**、查的是已废弃的 tp 表。换库后不改这里，
# 「页面写的库」和「脚本核对的库」就不是同一个，断言会整片假失败。
# 所以改成走 Django 侧同一套数据层 core.store，下面再包一层适配。
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

from core import store as _store  # noqa: E402


class _DbBridge(object):
    """把 core.store 的输出适配成 verify_ui.py 一直用的那套接口形态。

    差异只有一处：store.list_srv 返回的是 ORM 对象，而脚本按下标取字段
    （r["plat_name"] / r["id"]），所以这里转成 dict。
    store.list_mdl 本来就返回 dict 列表，原样透传。
    """

    @staticmethod
    def list_srv(platform="", company=""):
        return [{"id": s.id,
                 "plat_name": s.plat_name or "",
                 "base_url": s.base_url,
                 "comp_name": s.comp_name,
                 "home_url": s.home_url}
                for s in _store.list_srv(platform, company)]

    def count_srv(self):
        return len(self.list_srv())

    @staticmethod
    def list_mdl():
        return _store.list_mdl()

    @staticmethod
    def count_mdl():
        return len(_store.list_mdl())

    @staticmethod
    def price_text(v):
        return _store.price_text(v)

    @staticmethod
    def create_srv(*a, **kw):
        return _store.create_srv(*a, **kw)

    @staticmethod
    def create_mdl(*a, **kw):
        return _store.create_mdl(*a, **kw)

    @staticmethod
    def delete_srv(ids):
        return _store.delete_srv(ids)

    @staticmethod
    def delete_mdl(ids):
        return _store.delete_mdl(ids)


appdb = _DbBridge()

buf = io.StringIO()
events = []
FAILS = []


def w(s=""):
    buf.write(str(s) + "\n")


def chk(label, ok, detail=""):
    w("  [%s] %s%s" % ("PASS" if ok else "FAIL", label, ("  " + str(detail)) if detail else ""))
    if not ok:
        FAILS.append(label)


def ev_json(cdp, expr, tag):
    """求值并要求返回 JSON 字符串后解析。

    拿到非字符串时立刻报错：本类脚本里最常见的坑就是 JS 片段结尾的 () 丢了，
    表达式退化成「未被调用的函数」，CDP 会回一个 {} 却不报错，从而静默误判。
    """
    v = cdp.ev(expr)
    if isinstance(v, str) and v.startswith("__JSERROR__"):
        # 页面侧真的抛异常了 —— 直接把原始描述抛出来，别让它淹成 JSONDecodeError
        raise RuntimeError("%s：页面执行该片段时报错 -> %s" % (tag, v))
    if not isinstance(v, str):
        raise RuntimeError("%s：期望 JS 返回 JSON 字符串，实际拿到 %s %r"
                           "（检查该片段结尾是否为 })()）" % (tag, type(v).__name__, v))
    try:
        return json.loads(v)
    except ValueError as exc:
        raise RuntimeError("%s：返回的不是合法 JSON（%s）-> %r" % (tag, exc, v[:200]))


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


JS_MDL_SNAPSHOT = """(function(){
  var trs = document.querySelectorAll('#tbody tr');
  var hit = 0, first = '';
  for (var i=0;i<trs.length;i++){
    var t = trs[i].textContent.replace(/\\s+/g,' ').trim();
    if (t.indexOf('UI复现') > -1) hit++;
    if (!first) first = t.slice(0,70);
  }
  return JSON.stringify({
    rows: trs.length, 测试行: hit, first: first,
    meta: (document.getElementById('metaTotal')||{}).textContent,
    comboP: document.querySelectorAll('#comboPlatform .combo-item').length,
    comboT: document.querySelectorAll('#comboType .combo-item').length
  });
})()"""

JS_FILL_AND_SAVE = """(function(){
  var d = document;
  if (!d.getElementById('modelForm')) return 'no form';
  d.getElementById('platform').value  = %s;
  d.getElementById('modelName').value = %s;
  var it = d.querySelector('#menuType .combo-item[data-value="对话"]');
  if (it) it.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true}));
  d.getElementById('inPrice').value  = '9.99';
  d.getElementById('outPrice').value = '19.99';
  var typed = d.getElementById('modelType').value;
  d.getElementById('btnSave').click();
  return typed;
})()"""


# ---------------------------------------------------------------- prompt.html
JS_PB_SNAPSHOT = """(function(){
  function items(id){ return Array.prototype.slice.call(document.querySelectorAll('#'+id+' .combo-item')); }
  function attr(el, k){ return el.getAttribute(k); }
  var s = items('menuServer'), m = items('menuModel'), t = items('menuModelType');
  return JSON.stringify({
    servers: s.length, models: m.length, types: t.length,
    srv0: s.length ? {v: attr(s[0],'data-value'), b: attr(s[0],'data-base')} : null,
    typeVals: t.map(function(x){ return attr(x,'data-value'); }),
    mdl0: m.length ? {v: attr(m[0],'data-value'), tp: attr(m[0],'data-type'),
                      srv: attr(m[0],'data-server'), i: attr(m[0],'data-in'),
                      o: attr(m[0],'data-out'), u: attr(m[0],'data-unit')} : null
  });
})()"""

JS_PB_PICK_SERVER = """(function(){
  var it = document.querySelector('#menuServer .combo-item[data-value=' + JSON.stringify(%s) + ']');
  if (!it) return '__NOITEM__';
  it.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true}));
  return document.getElementById('baseUrl').value;
})()"""

JS_PB_VISIBLE = """(function(menu){
  var all = document.querySelectorAll('#' + menu + ' .combo-item'), vis = [];
  for (var i=0;i<all.length;i++){
    if (all[i].style.display !== 'none') vis.push(all[i].getAttribute('data-value'));
  }
  return JSON.stringify(vis);
})"""

JS_PB_PICK_TYPE = (
    "(function(){\n"
    "  var it = document.querySelector('#menuModelType .combo-item[data-value=' + JSON.stringify(%s) + ']');\n"
    "  if (!it) return '__NOITEM__';\n"
    "  it.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true}));\n"
    "  var all = document.querySelectorAll('#menuModel .combo-item'), vis = [];\n"
    "  for (var i=0;i<all.length;i++){\n"
    "    if (all[i].style.display !== 'none') vis.push(all[i].getAttribute('data-value'));\n"
    "  }\n"
    "  return JSON.stringify(vis);\n"
    "})()"
)

JS_PB_PICK_MODEL = """(function(){
  var it = document.querySelector('#menuModel .combo-item[data-value=' + JSON.stringify(%s) + ']');
  if (!it) return JSON.stringify({err: '__NOITEM__'});
  it.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true}));
  return JSON.stringify({
    inTag:    document.getElementById('tagIn').textContent,
    outTag:   document.getElementById('tagOut').textContent,
    unitIn:   document.getElementById('tagUnitIn').textContent,
    unitOut:  document.getElementById('tagUnitOut').textContent,
    hidden:   document.getElementById('costTags').hidden,
    modelVal: document.getElementById('model').value
  });
})()"""

# 必填校验原样：先两个都空、再只填系统提示词，各点一次「调试」
JS_PB_RUN_GUARD = """(function(){
  var o = {};
  document.getElementById('sysPrompt').value = '';
  document.getElementById('apiKey').value = '';
  document.getElementById('btnRun').click();
  o.sysInvalid = document.getElementById('sysPrompt').className;
  document.getElementById('sysPrompt').value = '你是一个助手';
  document.getElementById('btnRun').click();
  o.keyInvalid = document.getElementById('apiKey').className;
  o.out = document.getElementById('modelOut').value;
  o.uIn = document.getElementById('uIn').textContent;
  o.uTotal = document.getElementById('uTotal').textContent;
  return JSON.stringify(o);
})()"""

# 真实调用：填好必填项 -> 点「调试」-> 同步读一次输出框（应为「正在链接模型」占位）
#           -> 轮询等异步回填（后端要真发一次 HTTP）
JS_PB_RUN_REAL = (
    "(function(){\n"
    "  document.getElementById('sysPrompt').value  = '你是一个小猫咪';\n"
    "  document.getElementById('apiKey').value     = %s;\n"
    "  document.getElementById('userPrompt').value = '你好';\n"
    "  document.getElementById('btnRun').click();\n"
    "  var loadingNow = document.getElementById('modelOut').value;\n"
    "  function snap(){\n"
    "    return {\n"
    "      out:    document.getElementById('modelOut').value,\n"
    "      uIn:    document.getElementById('uIn').textContent,\n"
    "      uOut:   document.getElementById('uOut').textContent,\n"
    "      uTotal: document.getElementById('uTotal').textContent,\n"
    "      mIn:    document.getElementById('mIn').textContent,\n"
    "      mOut:   document.getElementById('mOut').textContent,\n"
    "      mTotal: document.getElementById('mTotal').textContent\n"
    "    };\n"
    "  }\n"
    "  return new Promise(function(resolve){\n"
    "    var t0 = Date.now();\n"
    "    (function poll(){\n"
    "      var s = snap();\n"
    "      var settled = s.uTotal !== '0 tokens' || s.out.indexOf('链接模型失败') === 0 || s.out.indexOf('（调用失败）') === 0;\n"
    "      if (settled || Date.now() - t0 > 25000) resolve(JSON.stringify({loadingNow: loadingNow, final: s}));\n"
    "      else setTimeout(poll, 150);\n"
    "    })();\n"
    "  });\n"
    "})()"
)

# 多轮真实调用：系统提示词 + 第 1 轮（用户提示词）+ 第 2 轮（#rounds 里的一轮）
#   预期后端按轮次依次调用，返回 answers[]（每轮一条），前端按「第 N 轮」逐条展示。
JS_PB_RUN_MULTI = (
    "(function(){\n"
    "  document.getElementById('sysPrompt').value  = '你是一个小猫咪';\n"
    "  document.getElementById('apiKey').value     = %s;\n"
    "  document.getElementById('userPrompt').value = '第一问';\n"
    "  var cb = document.getElementById('multiTurn');\n"
    "  cb.checked = true; cb.dispatchEvent(new Event('change', {bubbles:true}));\n"
    "  var box = document.getElementById('rounds');\n"
    "  var tas = box.querySelectorAll('textarea');\n"
    "  if (!tas.length){ document.getElementById('btnAddRound').click(); tas = box.querySelectorAll('textarea'); }\n"
    "  tas[0].value = '第二问';\n"
    "  for (var i=1;i<tas.length;i++) tas[i].value = '';\n"
    "  document.getElementById('btnRun').click();\n"
    "  function snap(){\n"
    "    return {\n"
    "      out:    document.getElementById('modelOut').value,\n"
    "      uIn:    document.getElementById('uIn').textContent,\n"
    "      uOut:   document.getElementById('uOut').textContent,\n"
    "      uTotal: document.getElementById('uTotal').textContent,\n"
    "      mIn:    document.getElementById('mIn').textContent,\n"
    "      mOut:   document.getElementById('mOut').textContent,\n"
    "      mTotal: document.getElementById('mTotal').textContent\n"
    "    };\n"
    "  }\n"
    "  return new Promise(function(resolve){\n"
    "    var t0 = Date.now();\n"
    "    (function poll(){\n"
    "      var s = snap();\n"
    "      var settled = s.uTotal !== '0 tokens' || s.out.indexOf('链接模型失败') === 0 || s.out.indexOf('（调用失败）') === 0;\n"
    "      if (settled || Date.now() - t0 > 25000) resolve(JSON.stringify(s));\n"
    "      else setTimeout(poll, 150);\n"
    "    })();\n"
    "  });\n"
    "})()"
)

# 打开当前列表页中「名字包含 NAME 的那一行」的「修改」按钮，并回读弹窗带出的原值
JS_OPEN_EDIT_MODEL = """(function(){
  var trs = document.querySelectorAll('#tbody tr'), tr = null;
  for (var i=0;i<trs.length;i++){
    if (trs[i].textContent.indexOf(NAME) > -1){ tr = trs[i]; break; }
  }
  if (!tr) return JSON.stringify({err:'__NOROW__'});
  var b = tr.querySelector('td.col-op button.btn-op[data-edit]');
  if (!b) return JSON.stringify({err:'__NOBTN__'});
  b.click();
  var g = function(id){ return (document.getElementById(id)||{}).value; };
  return JSON.stringify({
    mask: document.getElementById('editMask').className,
    title: document.getElementById('editTitle').textContent,
    platform: g('ed_platform'), model: g('ed_model'), type: g('ed_type'),
    inPrice: g('ed_inPrice'), outPrice: g('ed_outPrice'),
    unit: g('ed_unit'), url: g('ed_url')
  });
})()"""

JS_OPEN_EDIT_SRV = """(function(){
  var trs = document.querySelectorAll('#tbody tr'), tr = null;
  for (var i=0;i<trs.length;i++){
    if (trs[i].textContent.indexOf(NAME) > -1){ tr = trs[i]; break; }
  }
  if (!tr) return JSON.stringify({err:'__NOROW__'});
  var b = tr.querySelector('td.col-op button.btn-op[data-edit]');
  if (!b) return JSON.stringify({err:'__NOBTN__'});
  b.click();
  var g = function(id){ return (document.getElementById(id)||{}).value; };
  return JSON.stringify({
    mask: document.getElementById('editMask').className,
    title: document.getElementById('editTitle').textContent,
    platform: g('ed_platform'), company: g('ed_company'),
    api: g('ed_api'), cUrl: g('ed_cUrl')
  });
})()"""

# 抓某个表单容器内每个 .field 的「标签 / 是否必填 / 控件形态」，用于把编辑弹窗与新增页做结构比对。
# SEL 由 Python 侧替换（新增页是 .form-body，编辑弹窗是 #editBody）。
JS_FORM_SPEC = """(function(){
  var root = document.querySelector(SEL);
  if (!root) return JSON.stringify({err:'__NOROOT__'});
  var fs = root.querySelectorAll('.field'), out = [];
  for (var i=0;i<fs.length;i++){
    var lb = fs[i].querySelector('label');
    var ip = fs[i].querySelector('input');
    var combo = fs[i].querySelector('.combo');
    out.push({
      label: lb ? lb.textContent.replace(/\\s|\\*/g,'') : '',
      req: !!(lb && lb.querySelector('.req')),
      ctrl: ip ? ip.tagName.toLowerCase() : '',
      type: ip ? (ip.getAttribute('type') || '') : '',
      inputmode: ip ? (ip.getAttribute('inputmode') || '') : '',
      readonly: !!(ip && ip.hasAttribute('readonly')),
      combo: !!combo,
      multi: !!(combo && combo.classList.contains('is-multi')),
      hint: !!fs[i].querySelector('.field-hint')
    });
  }
  return JSON.stringify(out);
})()"""

# 类型多选下拉：展开 -> 勾两项 -> 值应以「、」连接、面板保持展开、勾选框是方形 .cb
JS_EDIT_TYPE_MULTI = """(function(){
  var root = document.getElementById('edCombo_type');
  var input = document.getElementById('ed_type');
  if (!root || !input) return JSON.stringify({err:'__NOCOMBO__'});
  var before = input.value;
  root.querySelector('[data-combo-btn]').click();
  var opened = root.classList.contains('open');
  var items = root.querySelectorAll('.combo-item');
  var cbs = root.querySelectorAll('.combo-item .cb').length;
  var v1 = items.length ? items[0].getAttribute('data-value') : '';
  var v2 = items.length > 2 ? items[2].getAttribute('data-value') : '';
  /* 先清空既有勾选再点，否则点到「已选中的那一项」等于取消勾选，断言会随初始数据漂移 */
  for (var k=0;k<items.length;k++){
    items[k].classList.remove('on');
    items[k].setAttribute('aria-selected', 'false');
  }
  input.value = '';
  items[0].dispatchEvent(new MouseEvent('mousedown', {bubbles:true}));
  items[2].dispatchEvent(new MouseEvent('mousedown', {bubbles:true}));
  var stillOpen = root.classList.contains('open');
  var on = root.querySelectorAll('.combo-item.on').length;
  var val = input.value;
  /* 复位：还原成打开弹窗时的取值，避免影响后续步骤 */
  for (var i=0;i<items.length;i++){
    items[i].classList.remove('on');
    items[i].setAttribute('aria-selected', 'false');
  }
  input.value = before;
  return JSON.stringify({opened:opened, items:items.length, cb:cbs, on:on,
                         val:val, expect:(v1+'、'+v2), stillOpen:stillOpen, before:before});
})()"""

# 单位：只读 + 单选，点第二项后回填并收起
JS_EDIT_UNIT_SELECT = """(function(){
  var root = document.getElementById('edCombo_unit');
  var input = document.getElementById('ed_unit');
  if (!root || !input) return JSON.stringify({err:'__NOCOMBO__'});
  var before = input.value;
  root.querySelector('[data-combo-btn]').click();
  var opened = root.classList.contains('open');
  var items = root.querySelectorAll('.combo-item');
  var second = items.length > 1 ? items[1].getAttribute('data-value') : '';
  items[1].dispatchEvent(new MouseEvent('mousedown', {bubbles:true}));
  return JSON.stringify({readonly: input.hasAttribute('readonly'), opened:opened,
                         items:items.length, second:second, after:input.value,
                         closed: !root.classList.contains('open'), before:before});
})()"""

# 计费框：只留数字与一个小数点
JS_EDIT_NUM_FILTER = """(function(){
  var el = document.getElementById('ed_inPrice');
  if (!el) return JSON.stringify({err:'__NOINPUT__'});
  el.value = '1a2.3b4.5';
  el.dispatchEvent(new Event('input', {bubbles:true}));
  var v1 = el.value;
  el.value = '18.75';
  el.dispatchEvent(new Event('input', {bubbles:true}));
  return JSON.stringify({v1:v1, v2:el.value});
})()"""

# 平台：可编辑（非只读）+ 可展开 + 选中即回填收起
JS_EDIT_PLATFORM_COMBO = """(function(){
  var root = document.getElementById('edCombo_platform');
  var input = document.getElementById('ed_platform');
  if (!root || !input) return JSON.stringify({err:'__NOCOMBO__'});
  var before = input.value;
  root.querySelector('[data-combo-btn]').click();
  var opened = root.classList.contains('open');
  var items = root.querySelectorAll('.combo-item');
  var first = items.length ? items[0].getAttribute('data-value') : '';
  if (items.length) items[0].dispatchEvent(new MouseEvent('mousedown', {bubbles:true}));
  return JSON.stringify({readonly: input.hasAttribute('readonly'), before:before,
                         opened:opened, items:items.length, first:first,
                         after:input.value, closed: !root.classList.contains('open')});
})()"""

# 新增模型页 · 「平台 / 官方名」下拉：选项应来自服务器列表（无写死的假平台）
JS_ADD_PLATFORM_MENU = """(function(){
  var root  = document.getElementById('comboPlatform');
  var input = document.getElementById('platform');
  if (!root || !input) return JSON.stringify({err:'__NOCOMBO__'});
  root.querySelector('[data-combo-btn]').click();
  var opened = root.classList.contains('open');
  var nodes  = root.querySelectorAll('.combo-item');
  var vals = [], dots = 0;
  for (var i = 0; i < nodes.length; i++){
    vals.push(nodes[i].getAttribute('data-value'));
    if (nodes[i].querySelector('.tag-dot')) dots++;
  }
  var first = nodes.length ? nodes[0].getAttribute('data-value') : '';
  if (nodes.length) nodes[0].dispatchEvent(new MouseEvent('mousedown', {bubbles:true}));
  return JSON.stringify({opened:opened, items:nodes.length, vals:vals, dots:dots,
                         first:first, after:input.value,
                         readonly: input.hasAttribute('readonly'),
                         closed: !root.classList.contains('open')});
})()"""

# 列表页快照：操作列结构 + 命中行的关键单元格（模板块用 NAME 占位，交由 .replace 注入）
JS_OP_COL = """(function(){
  var tbl = document.querySelector('#tbody').closest('table');
  var ths = tbl.querySelectorAll('thead th');
  var lastTh = ths.length ? ths[ths.length-1].textContent.trim() : '';
  var trs = document.querySelectorAll('#tbody tr');
  var opTd = 0, opBtn = 0;
  for (var i=0;i<trs.length;i++){
    if (trs[i].querySelector('td.col-op')) opTd++;
    var b = trs[i].querySelector('td.col-op button.btn-op[data-edit]');
    if (b && b.textContent.trim() === '修改') opBtn++;
  }
  return JSON.stringify({lastTh: lastTh, cols: ths.length, rows: trs.length,
                         opTd: opTd, opBtn: opBtn});
})()"""

JS_MODEL_ROW = """(function(){
  var trs = document.querySelectorAll('#tbody tr'), tr = null;
  for (var i=0;i<trs.length;i++){
    if (trs[i].textContent.indexOf(NAME) > -1){ tr = trs[i]; break; }
  }
  if (!tr) return JSON.stringify({err:'__NOROW__'});
  var a = tr.querySelector('td.col-model a.link-model');
  var nums = tr.querySelectorAll('td.col-num .num-val');
  var pa  = tr.querySelector('td.col-platform a');
  return JSON.stringify({
    href:   a ? a.getAttribute('href') : null,
    target: a ? a.getAttribute('target') : null,
    hasTag: !!a && a.tagName === 'A',
    inTxt:  nums.length ? nums[0].textContent.trim() : '',
    outTxt: nums.length > 1 ? nums[1].textContent.trim() : '',
    platIsLink: !!pa,
    platHref: pa ? pa.getAttribute('href') : null
  });
})()"""

JS_SRV_ROW = """(function(){
  var trs = document.querySelectorAll('#tbody tr'), tr = null;
  for (var i=0;i<trs.length;i++){
    if (trs[i].textContent.indexOf(NAME) > -1){ tr = trs[i]; break; }
  }
  if (!tr) return JSON.stringify({err:'__NOROW__'});
  var a = tr.querySelector('td.col-company a');
  return JSON.stringify({
    platform: tr.querySelector('td.col-platform').textContent.trim(),
    company:  tr.querySelector('td.col-company').textContent.trim(),
    api:      tr.querySelector('td.col-api').textContent.trim(),
    companyHref: a ? a.getAttribute('href') : null
  });
})()"""

JS_PB_MISC = """(function(){
  var o = {};
  var before = document.getElementById('apiKey').type;
  document.getElementById('btnEye').click();
  o.eye = {before: before, after: document.getElementById('apiKey').type,
           cls: document.getElementById('btnEye').className,
           openHidden: document.querySelector('#btnEye .eye-open').hidden,
           offHidden:  document.querySelector('#btnEye .eye-off').hidden};

  var cb = document.getElementById('multiTurn');
  cb.checked = true; cb.dispatchEvent(new Event('change', {bubbles:true}));
  o.roundsToggle = document.querySelectorAll('#rounds .pb-round').length;
  o.roundsHidden = document.getElementById('rounds').hidden;
  document.getElementById('btnAddRound').click();
  o.roundsAdd = document.querySelectorAll('#rounds .pb-round').length;
  o.labels = Array.prototype.map.call(
    document.querySelectorAll('.pb-round .pb-round-idx'), function(e){ return e.textContent; });
  o.addBtnHidden = document.getElementById('btnAddRound').hidden;

  document.getElementById('btnClearUser').click();
  o.cleared = document.getElementById('userPrompt').value === '' &&
    Array.prototype.every.call(
      document.querySelectorAll('#roundBase textarea, #rounds textarea'),
      function(t){ return t.value === ''; });
  return JSON.stringify(o);
})()"""

JS_PB_COPY = """(function(){
  document.getElementById('btnCopySys').click();
  document.getElementById('btnCopyOut').click();
  return 'clicked';
})()"""


def main():
    if not os.path.exists(EDGE):
        w("跳过：未找到 Edge（%s）" % EDGE)
        return 0

    tmp = tempfile.mkdtemp(prefix="pd_ui_check")
    proc = subprocess.Popen([
        EDGE, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
        "--remote-debugging-port=%d" % PORT, "--user-data-dir=" + tmp,
        "--window-size=1600,1000", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    stamp = str(int(time.time()))
    plat = "UI复现平台-" + stamp
    m1 = "UI复现A-" + stamp
    m2 = "UI复现B-" + stamp
    base_rows = appdb.count_mdl()

    # [13] 用的测试服务器 / 模型：base_url 指向本机 mock 模型服务
    mock_plat = "UI复现模型平台-" + stamp
    mock_model = "UI复现模型-" + stamp
    mock_base = "http://127.0.0.1:%d/v1" % MOCK_PORT
    mock = None
    try:
        mock = start_mock()
    except Exception as exc:
        w("!! mock 模型服务启动失败（127.0.0.1:%d）：%r" % (MOCK_PORT, exc))
        FAILS.append("mock 模型服务")

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

        w("=" * 74)
        w("[1] 打开「模型列表」（库内 mdl 共 %d 条）" % base_rows)
        c.go(APP + "foundation1_models.html", wait=5)
        snap = json.loads(c.ev(JS_MDL_SNAPSHOT))
        w("    %s" % snap)
        chk("列表行数 == 库内 mdl 行数", snap["rows"] == base_rows,
            "page=%s db=%s" % (snap["rows"], base_rows))
        chk("meta-bar 计数已更新", str(snap["meta"]).endswith("%d 条记录" % base_rows), snap["meta"])
        # 下拉项数按库内真实去重数比（不写死 >1：库小的时候只有 1 种类型是正常的）
        exp_p = len({r["plat_name"] for r in appdb.list_mdl() if r["plat_name"]})
        exp_t = len({r["mdl_type"] for r in appdb.list_mdl() if r["mdl_type"]})
        chk("平台下拉项数 == 库内去重平台数", snap["comboP"] == exp_p,
            "page=%s db=%s" % (snap["comboP"], exp_p))
        chk("类型下拉项数 == 库内去重类型数", snap["comboT"] == exp_t,
            "page=%s db=%s" % (snap["comboT"], exp_t))

        w("")
        w("[2] 平台列外链口径：href 应等于该平台主页（srv.home_url）")
        hrefs = c.ev("""JSON.stringify(Array.prototype.slice.call(
            document.querySelectorAll('#tbody tr'), 0, 3).map(function(tr){
              var a = tr.querySelector('.col-platform a');
              return {t: tr.querySelector('.col-platform').textContent.trim(),
                      href: a ? a.getAttribute('href') : null};
            }))""")
        w("    %s" % hrefs)
        h0 = json.loads(hrefs)[0]
        want = [r["home_url"] for r in appdb.list_srv() if r["plat_name"] == h0["t"]]
        chk("首行平台外链 == home_url", bool(want) and h0["href"] == want[0],
            "%s vs %s" % (h0["href"], want))

        w("")
        w("[3] 点「添加模型」-> 填表并保存（平台 %s）" % plat)
        c.ev("document.getElementById('btnAddModel').click()")
        c.pump(4)
        chk("已进入新增模型页", "model_add.html" in str(c.ev("location.pathname")),
            c.ev("location.pathname"))
        typed = c.ev(JS_FILL_AND_SAVE % (json.dumps(plat), json.dumps(m1)))
        w("    多选类型回填值: %r" % typed)
        chk("只读多选类型可写入（对话）", typed == "对话", typed)
        c.pump(6)

        w("")
        w("[4] 保存后自动回到列表")
        path = str(c.ev("location.pathname"))
        chk("已回到模型列表", "foundation1_models.html" in path, path)
        snap = json.loads(c.ev(JS_MDL_SNAPSHOT))
        w("    %s" % snap)
        chk("列表出现新模型", snap["测试行"] >= 1, m1)
        chk("行数 +1", snap["rows"] == base_rows + 1, "page=%s" % snap["rows"])
        rows = [r for r in appdb.list_mdl() if r["mdl_name"] == m1]
        chk("已落库 mdl", len(rows) == 1, rows)
        if rows:
            chk("类型存为「对话」", rows[0]["mdl_type"] == "对话", rows[0]["mdl_type"])
            chk("输入计费存为 9.99", abs(float(rows[0]["in_price"]) - 9.99) < 1e-6, rows[0]["in_price"])
            chk("输出计费存为 19.99", abs(float(rows[0]["out_price"]) - 19.99) < 1e-6, rows[0]["out_price"])
            chk("单位存为默认 K", rows[0]["unit"] == "K", rows[0]["unit"])
        plats = [r for r in appdb.list_srv() if r["plat_name"] == plat]
        chk("新平台已自动补进 srv（满足外键）", len(plats) == 1, plats)

        w("")
        w("[5] 再存一条 %s" % m2)
        c.ev("document.getElementById('btnAddModel').click()")
        c.pump(4)
        c.ev(JS_FILL_AND_SAVE % (json.dumps(plat), json.dumps(m2)))
        c.pump(6)
        snap = json.loads(c.ev(JS_MDL_SNAPSHOT))
        w("    %s" % snap)
        chk("两条测试模型都在列表", snap["测试行"] == 2, snap)
        chk("行数 +2", snap["rows"] == base_rows + 2, "page=%s" % snap["rows"])

        w("")
        w("[6] 勾选两条测试记录 -> 批量删除")
        n_sel = c.ev("""(function(){
          var trs = document.querySelectorAll('#tbody tr'), n = 0;
          for (var i=0;i<trs.length;i++){
            if (trs[i].textContent.indexOf('UI复现') > -1){
              var cb = trs[i].querySelector('.row-check');
              if (cb && !cb.checked){ cb.checked = true; cb.dispatchEvent(new Event('change',{bubbles:true})); n++; }
            }
          }
          return n;
        })()""")
        chk("勾选到 2 条", n_sel == 2, n_sel)
        mask = c.ev("""(function(){
          document.getElementById('btnBatchDelete').click();
          return document.getElementById('delMask').className;
        })()""")
        chk("删除确认弹窗已打开", "show" in str(mask), mask)
        c.pump(1)
        c.ev("document.getElementById('delConfirm').click()")
        c.pump(5)

        w("")
        w("[7] 删除后核对")
        snap = json.loads(c.ev(JS_MDL_SNAPSHOT))
        w("    %s" % snap)
        chk("列表已无测试行", snap["测试行"] == 0, snap)
        chk("行数回落", snap["rows"] == base_rows, "now=%s was=%s" % (snap["rows"], base_rows))
        chk("库内已无测试模型", not [r for r in appdb.list_mdl() if r["mdl_name"].startswith("UI复现")])

        w("")
        w("[8] 导航烟测：进「服务器列表」应拿到后端数据")
        c.go(APP + "list_server.html", wait=5)
        srv_snap = c.ev("""JSON.stringify({
            rows: document.querySelectorAll('#tbody tr').length,
            meta: (document.getElementById('metaTotal')||{}).textContent,
            path: location.pathname
        })""")
        w("    %s" % srv_snap)
        d = json.loads(srv_snap)
        chk("服务器列表拿到后端数据", d["rows"] == appdb.count_srv(), "page=%s db=%s" % (d["rows"], appdb.count_srv()))

        # ================= 提示词构造 prompt.html =================
        srvs = appdb.list_srv()
        mdls = appdb.list_mdl()
        proto_order = ["文本生成", "多模态", "推理模型", "向量嵌入"]
        seen_t, exp_types = set(), []
        for r in mdls:
            if r["mdl_type"] and r["mdl_type"] not in seen_t:
                seen_t.add(r["mdl_type"])
                exp_types.append(r["mdl_type"])
        exp_types.sort(key=lambda t: proto_order.index(t) if t in proto_order else len(proto_order))

        w("")
        w("[8] 打开「提示词构造」：三个下拉的选项应完全来自库内数据")
        c.go(APP + "prompt.html", wait=5)
        snap = ev_json(c, JS_PB_SNAPSHOT, "[8] 快照")
        w("    页面: servers=%s models=%s types=%s" % (snap["servers"], snap["models"], snap["types"]))
        w("    库内: srv=%s mdl=%s 类型=%s" % (len(srvs), len(mdls), exp_types))
        chk("服务器下拉项数 == 库内 srv 行数", snap["servers"] == len(srvs),
            "page=%s db=%s" % (snap["servers"], len(srvs)))
        chk("模型下拉项数 == 库内 mdl 行数", snap["models"] == len(mdls),
            "page=%s db=%s" % (snap["models"], len(mdls)))
        chk("类型下拉 == 库内去重 mdl_type", snap["typeVals"] == exp_types,
            "%s vs %s" % (snap["typeVals"], exp_types))
        chk("首项服务器 data-value/data-base == 库内首行",
            snap["srv0"] == {"v": srvs[0]["plat_name"], "b": srvs[0]["base_url"] or ""}, snap["srv0"])
        m0 = mdls[0]
        chk("首项模型 data-* 全来自库内（类型/归属/计费/单位）",
            snap["mdl0"] == {"v": m0["mdl_name"], "tp": m0["mdl_type"] or "",
                             "srv": m0["plat_name"] or "",
                             "i": appdb.price_text(m0["in_price"]) or "",
                             "o": appdb.price_text(m0["out_price"]) or "",
                             "u": m0["unit"] or ""}, snap["mdl0"])

        # ⚠️ 不许硬编码平台名（原先写死 srvs[0] / OpenAI）：现场挑一台「库里确实挂着模型」
        #    的服务器来测联动。某台服务器在库里一个模型都没配是完全合法的状态
        #    （例如 OpenAI 可以只登记了平台、模型另配），写死就会让 exp_t 变成空列表，
        #    紧接着 exp_t[0] 直接 IndexError —— 校验脚本当场崩，而不是如实报结论。
        plat_row = next((r for r in srvs
                         if any(m["plat_name"] == r["plat_name"] for m in mdls)), None)
        if plat_row is None:
            w("")
            w("[9]-[11] 跳过：库里没有任何「挂着模型的服务器」，联动用例失去前提")
            chk("库里至少有一台挂着模型的服务器", False,
                "srv=%d mdl=%d" % (len(srvs), len(mdls)))
        else:
            plat = plat_row["plat_name"]
            w("")
            w("[9] 选服务器「%s」-> Base_url 自动填充 + 模型/类型下拉收窄" % plat)
            base = c.ev(JS_PB_PICK_SERVER % json.dumps(plat))
            chk("Base_url 自动填成该服务器的 base_url", base == (plat_row["base_url"] or ""), base)
            vis_m = ev_json(c, JS_PB_VISIBLE + "('menuModel')", "[9] 模型下拉")
            exp_m = [r["mdl_name"] for r in mdls if r["plat_name"] == plat]
            chk("模型下拉只剩该服务器下的模型", vis_m == exp_m, "%s vs %s" % (vis_m, exp_m))
            vis_t = ev_json(c, JS_PB_VISIBLE + "('menuModelType')", "[9] 类型下拉")
            exp_t = [t for t in exp_types
                     if any(r["mdl_type"] == t and r["plat_name"] == plat for r in mdls)]
            chk("类型下拉只剩该服务器下存在的类型", vis_t == exp_t, "%s vs %s" % (vis_t, exp_t))

            ttype = exp_t[0]
            w("")
            w("[10] 选模型类型「%s」-> 模型下拉再按类型收窄" % ttype)
            vis_m2 = ev_json(c, JS_PB_PICK_TYPE % json.dumps(ttype), "[10] 类型联动")
            exp_m2 = [r["mdl_name"] for r in mdls
                      if r["plat_name"] == plat and r["mdl_type"] == ttype]
            chk("模型下拉只剩该类型", vis_m2 == exp_m2, "%s vs %s" % (vis_m2, exp_m2))

            tgt = [r for r in mdls if r["plat_name"] == plat and r["mdl_type"] == ttype][0]
            w("")
            w("[11] 选模型「%s」-> 费用标签取库内计费" % tgt["mdl_name"])
            tags = ev_json(c, JS_PB_PICK_MODEL % json.dumps(tgt["mdl_name"]), "[11] 选模型")
            w("    %s" % json.dumps(tags, ensure_ascii=False))
            chk("费用标签已显示", tags.get("hidden") is False, tags)
            chk("输入计费标签 == in_price 原文（不四舍五入）",
                tags.get("inTag") == "¥" + fmt_price(appdb.price_text(tgt["in_price"])),
                tags.get("inTag"))
            chk("输出计费标签 == out_price 原文（不四舍五入）",
                tags.get("outTag") == "¥" + fmt_price(appdb.price_text(tgt["out_price"])),
                tags.get("outTag"))
            chk("单位标签 == unit", tags.get("unitIn") == "/ " + (tgt["unit"] or ""), tags.get("unitIn"))

        w("")
        w("[12] 必填校验原样（未填系统提示词 / API_key 时应被拦下）")
        guard = ev_json(c, JS_PB_RUN_GUARD, "[12] 必填校验")
        w("    sysPrompt.class=%r  apiKey.class=%r" % (guard["sysInvalid"], guard["keyInvalid"]))
        chk("系统提示词为空 -> 进告警态", "is-invalid" in str(guard["sysInvalid"]), guard["sysInvalid"])
        chk("API_key 为空 -> 进告警态", "is-invalid" in str(guard["keyInvalid"]), guard["keyInvalid"])
        chk("被拦下：模型输出仍为空", guard["out"] == "", str(guard["out"])[:60])
        chk("被拦下：指标仍为 0 tokens",
            guard["uIn"] == "0 tokens" and guard["uTotal"] == "0 tokens",
            "%s / %s" % (guard["uIn"], guard["uTotal"]))

        w("")
        w("[13] 真实调用：新建测试服务器（base_url -> 本地 mock 模型服务）与测试模型")
        appdb.create_srv(mock_plat, mock_base, "本地模拟公司", "http://127.0.0.1:%d" % MOCK_PORT)
        appdb.create_mdl(mock_plat, mock_model, "对话",
                         MOCK_IN_PRICE, MOCK_OUT_PRICE, MOCK_UNIT, "")
        w("    测试服务器 %s -> %s" % (mock_plat, mock_base))
        w("    测试模型   %s（in %s / out %s / 单位 %s）"
          % (mock_model, MOCK_IN_PRICE, MOCK_OUT_PRICE, MOCK_UNIT))

        c.go(APP + "prompt.html", wait=5)              # 重新加载，让新数据进下拉
        base2 = c.ev(JS_PB_PICK_SERVER % json.dumps(mock_plat))
        chk("Base_url 自动填成 mock 地址", base2 == mock_base, base2)
        picked = ev_json(c, JS_PB_PICK_MODEL % json.dumps(mock_model), "[13] 选测试模型")
        chk("已选中测试模型", picked.get("modelVal") == mock_model, picked.get("modelVal"))

        MOCK_HITS.clear()
        run = ev_json(c, JS_PB_RUN_REAL % json.dumps(MOCK_KEY), "[13] 真实调试")
        fin = run["final"]
        w("    点击瞬间输出区: %r" % run["loadingNow"])
        w("    输出: %r" % fin["out"][:80])
        w("    tokens: %s / %s / %s" % (fin["uIn"], fin["uOut"], fin["uTotal"]))
        w("    费用:   %s / %s / %s" % (fin["mIn"], fin["mOut"], fin["mTotal"]))

        chk("点击调试后输出区立即显示「正在链接模型」",
            run["loadingNow"] == "正在链接模型", repr(run["loadingNow"]))
        chk("输出区显示的就是模型返回的文本", fin["out"] == MOCK_TEXT, repr(fin["out"])[:120])
        chk("输入 tokens == 模型返回的 usage",
            fin["uIn"] == "%d tokens" % MOCK_USAGE["prompt_tokens"], fin["uIn"])
        chk("输出 tokens == 模型返回的 usage",
            fin["uOut"] == "%d tokens" % MOCK_USAGE["completion_tokens"], fin["uOut"])
        chk("总 tokens == usage 之和",
            fin["uTotal"] == "%d tokens" % MOCK_USAGE["total_tokens"], fin["uTotal"])
        want_in = MOCK_USAGE["prompt_tokens"] / 1000.0 * MOCK_IN_PRICE
        want_out = MOCK_USAGE["completion_tokens"] / 1000.0 * MOCK_OUT_PRICE
        chk("输入费用 == tokens/1000 × 库内 in_price",
            fin["mIn"] == "¥" + fmt_num(want_in), "%s vs ¥%s" % (fin["mIn"], fmt_num(want_in)))
        chk("输出费用 == tokens/1000 × 库内 out_price",
            fin["mOut"] == "¥" + fmt_num(want_out), "%s vs ¥%s" % (fin["mOut"], fmt_num(want_out)))
        chk("总费用 == 输入 + 输出",
            fin["mTotal"] == "¥" + fmt_num(want_in + want_out),
            "%s vs ¥%s" % (fin["mTotal"], fmt_num(want_in + want_out)))
        chk("费用不四舍五入（不是 .toFixed(4) 口径）",
            fin["mIn"] != "¥%.4f" % want_in and fin["mOut"] != "¥%.4f" % want_out,
            "%s / %s" % (fin["mIn"], fin["mOut"]))

        w("")
        w("[13b] mock 端收到的请求（证明真的发了一次 HTTP）")
        hit = MOCK_HITS[-1] if MOCK_HITS else {}
        body = hit.get("body") or {}
        msgs = body.get("messages") or []
        w("    path=%s  auth=%s" % (hit.get("path"), hit.get("auth")))
        w("    body=%s" % json.dumps(body, ensure_ascii=False)[:240])
        chk("恰好收到 1 次调用", len(MOCK_HITS) == 1, len(MOCK_HITS))
        chk("请求打在 {base_url}/chat/completions",
            hit.get("path") == "/v1/chat/completions", hit.get("path"))
        chk("Bearer API_key 已透传", hit.get("auth") == "Bearer " + MOCK_KEY, hit.get("auth"))
        chk("model 透传为所选模型", body.get("model") == mock_model, body.get("model"))
        chk("stream=false", body.get("stream") is False, body.get("stream"))
        chk("messages = 系统提示词 + 用户提示词",
            len(msgs) == 2 and msgs[0].get("role") == "system" and msgs[1].get("role") == "user",
            msgs)
        chk("系统提示词内容正确",
            bool(msgs) and msgs[0].get("content") == "你是一个小猫咪",
            msgs[0].get("content") if msgs else None)
        chk("用户提示词内容正确",
            len(msgs) > 1 and msgs[1].get("content") == "你好",
            msgs[1].get("content") if len(msgs) > 1 else None)

        w("")
        w("[13c] 错误链路：服务端不替模型服务做校验，失败如实回传")
        bad = post_json("/api/chat", {"base_url": "http://127.0.0.1:1/v1", "api_key": "x",
                                      "model": "m", "system": "s", "rounds": ["hi"]})
        w("    连不上的地址 -> %s" % json.dumps(bad, ensure_ascii=False)[:170])
        chk("连不上时 ok=false 且带可读错误",
            bad.get("ok") is False and len(str(bad.get("error") or "")) > 10, bad)
        MOCK_HITS.clear()
        wrong = post_json("/api/chat", {"base_url": mock_base, "api_key": "sk-wrong",
                                        "model": mock_model, "system": "s", "rounds": ["hi"]})
        w("    错误 key     -> %s" % json.dumps(wrong, ensure_ascii=False)[:170])
        chk("错误 key 不被服务端拦截，由模型服务回 401",
            wrong.get("ok") is False and "401" in str(wrong.get("error")), wrong)
        chk("错误 key 时后端确实把请求发到了模型服务", len(MOCK_HITS) == 1, len(MOCK_HITS))

        w("")
        w("[13d] 推理模型兜底：正式回答为空时取 reasoning_content")
        MOCK_HITS.clear()
        rz = post_json("/api/chat", {"base_url": mock_base, "api_key": MOCK_KEY,
                                     "model": "mock-reasoning-only",
                                     "system": "s", "rounds": ["hi"]})
        w("    -> %s" % json.dumps(rz, ensure_ascii=False)[:170])
        chk("content 为空时回退到 reasoning_content",
            rz.get("ok") is True and rz.get("text") == MOCK_REASON_TEXT, rz)

        w("")
        w("[14] 掩码开关 / 多轮会话 / 清空用户提示词 / 复制")
        misc = ev_json(c, JS_PB_MISC, "[14] 交互")
        w("    %s" % json.dumps(misc, ensure_ascii=False))
        chk("眼睛开关：password -> text", misc["eye"]["after"] == "text", misc["eye"])
        chk("眼睛图标切换", misc["eye"]["openHidden"] is True and misc["eye"]["offHidden"] is False)
        chk("勾选多轮会话 -> 出现 1 轮", misc["roundsToggle"] == 1, misc["roundsToggle"])
        chk("多轮区已展开", misc["roundsHidden"] is False)
        chk("「添加一轮」-> 共 2 轮", misc["roundsAdd"] == 2, misc["roundsAdd"])
        chk("轮次编号与占位正确",
            misc["labels"] == ["第 1 轮", "第 2 轮", "第 3 轮"], misc["labels"])
        chk("清空用户提示词生效", misc["cleared"] is True)
        c.ev(JS_PB_COPY)
        c.pump(1.5)
        n_toast = c.ev("document.querySelectorAll('#toastWrap .toast').length")
        chk("复制按钮有反馈（出现 toast）", isinstance(n_toast, int) and n_toast >= 1, n_toast)

        # ============ 本轮改动专项：操作列 / 修改 / 外链 / 小数位 / 格式锁定 / 多轮 ============

        w("")
        w("[15] 模型列表：末尾多一列「操作」，每行一个「修改」按钮")
        c.go(APP + "foundation1_models.html", wait=5)
        opc = ev_json(c, JS_OP_COL, "[15] 操作列")
        w("    %s" % opc)
        chk("表头最后一列是「操作」", opc["lastTh"] == "操作", opc["lastTh"])
        chk("表头 7 列（原 6 列 + 操作）", opc["cols"] == 7, opc["cols"])
        chk("每行都有操作列单元格", opc["rows"] > 0 and opc["opTd"] == opc["rows"],
            "%s/%s" % (opc["opTd"], opc["rows"]))
        chk("每行都有「修改」按钮", opc["opBtn"] == opc["rows"], "%s/%s" % (opc["opBtn"], opc["rows"]))

        w("")
        w("[16] 模型列表：点「修改」-> 弹窗带出原值 -> 改 名称/计费(4.135, 0.0005)/网址 -> 保存")
        fx_plat = "UI复现平台-" + stamp
        fx_old  = "UI复现改前-" + stamp
        fx_new  = "UI复现改后-" + stamp
        fx_url0 = "https://example.com/ui/old-" + stamp
        fx_url1 = "https://example.com/ui/new-" + stamp
        appdb.create_mdl(fx_plat, fx_old, "对话", "1.00", "2.00", "K", fx_url0)
        c.go(APP + "foundation1_models.html", wait=5)

        opened = ev_json(c, JS_OPEN_EDIT_MODEL.replace("NAME", json.dumps(fx_old)), "[16] 打开修改弹窗")
        w("    %s" % json.dumps(opened, ensure_ascii=False))
        chk("修改弹窗已打开", "show" in str(opened.get("mask")), opened.get("mask"))
        chk("弹窗标题带出该模型名", fx_old in str(opened.get("title")), opened.get("title"))
        chk("弹窗带出原值（名/类型/计费/单位/网址）",
            opened.get("model") == fx_old and opened.get("type") == "对话"
            and opened.get("inPrice") == "1.00" and opened.get("outPrice") == "2.00"
            and opened.get("unit") == "K" and opened.get("url") == fx_url0, opened)

        saved_n = ev_json(c, """(function(){
          document.getElementById('ed_model').value    = %s;
          document.getElementById('ed_inPrice').value  = %s;
          document.getElementById('ed_outPrice').value = %s;
          document.getElementById('ed_url').value      = %s;
          document.getElementById('editConfirm').click();
          return JSON.stringify({mask: document.getElementById('editMask').className});
        })()""" % (json.dumps(fx_new), json.dumps("4.135"),
                    json.dumps("0.0005"), json.dumps(fx_url1)), "[16] 保存修改")
        chk("保存后弹窗关闭", "show" not in str(saved_n.get("mask")), saved_n.get("mask"))
        c.pump(3)                                  # 等 PUT 落库 + 回读列表

        frows = [r for r in appdb.list_mdl() if r["mdl_name"] == fx_new]
        chk("修改已落库（能按新名称查到）", len(frows) == 1, len(frows))
        if frows:
            r0 = frows[0]
            chk("计费原文保留：in_price == '4.135'（未四舍五入）", r0["in_price"] == "4.135", r0["in_price"])
            chk("计费原文保留：out_price == '0.0005'（未四舍五入）", r0["out_price"] == "0.0005", r0["out_price"])
            chk("网址已更新", r0["url"] == fx_url1, r0["url"])
            chk("未改动的单位保持不变", r0["unit"] == "K", r0["unit"])
        chk("修改是 UPDATE 不是新增（旧名已不存在）",
            not [r for r in appdb.list_mdl() if r["mdl_name"] == fx_old], fx_old)

        c.pump(1.5)
        disp = ev_json(c, JS_MODEL_ROW.replace("NAME", json.dumps(fx_new)), "[16] 列表显示")
        w("    %s" % json.dumps(disp, ensure_ascii=False))
        chk("列表输入计费显示库内原文 4.135", disp.get("inTxt") == "4.135", disp.get("inTxt"))
        chk("列表输出计费显示库内原文 0.0005", disp.get("outTxt") == "0.0005", disp.get("outTxt"))

        w("")
        w("[17] 模型名称外链指向「新增模型」里填的网址（未填则降级为纯文字）")
        fx_nourl = "UI复现无网址-" + stamp
        appdb.create_mdl(fx_plat, fx_nourl, "对话", "1.00", "2.00", "K", "")
        c.go(APP + "foundation1_models.html", wait=5)
        d17a = ev_json(c, JS_MODEL_ROW.replace("NAME", json.dumps(fx_new)), "[17] 有网址的行")
        chk("模型名称是超链接", d17a.get("hasTag") is True, d17a.get("hasTag"))
        chk("href == 库内 url（不再指向不存在的 model_detail.html）",
            d17a.get("href") == fx_url1, d17a.get("href"))
        chk("不再出现 model_detail.html 这种打不开的地址",
            "model_detail" not in str(d17a.get("href")), d17a.get("href"))
        chk("外链新窗口打开", d17a.get("target") == "_blank", d17a.get("target"))
        d17b = ev_json(c, JS_MODEL_ROW.replace("NAME", json.dumps(fx_nourl)), "[17] 无网址的行")
        chk("没填网址时不给 href（不会点到空链接）", d17b.get("href") is None, d17b.get("href"))
        chk("平台列仍渲染为链接元素（既有外链口径未被动过）",
            d17a.get("platIsLink") is True, d17a.get("platIsLink"))

        w("")
        w("[18] 服务器列表：末尾「操作」列 + 「修改」落库")
        sv_old = "UI复现服务器-" + stamp
        sv_new = "UI复现服务器改后-" + stamp
        sv_api2 = "https://api.example.com/v2"
        sv_home2 = "https://www.example.com/new"
        appdb.create_srv(sv_old, "https://api.example.com/v1", "UI复现公司", "https://www.example.com")
        c.go(APP + "list_server.html", wait=5)
        sopc = ev_json(c, JS_OP_COL, "[18] 服务器操作列")
        w("    %s" % sopc)
        chk("服务器列表表头最后一列是「操作」", sopc["lastTh"] == "操作", sopc["lastTh"])
        chk("表头 5 列（原 4 列 + 操作）", sopc["cols"] == 5, sopc["cols"])
        chk("服务器每行都有「修改」按钮",
            sopc["rows"] > 0 and sopc["opBtn"] == sopc["rows"],
            "%s/%s" % (sopc["opBtn"], sopc["rows"]))

        sopened = ev_json(c, JS_OPEN_EDIT_SRV.replace("NAME", json.dumps(sv_old)), "[18] 打开服务器修改弹窗")
        w("    %s" % json.dumps(sopened, ensure_ascii=False))
        chk("服务器修改弹窗已打开", "show" in str(sopened.get("mask")), sopened.get("mask"))
        chk("弹窗带出原值（平台/公司/api/首页）",
            sopened.get("platform") == sv_old and sopened.get("company") == "UI复现公司"
            and sopened.get("api") == "https://api.example.com/v1"
            and sopened.get("cUrl") == "https://www.example.com", sopened)

        c.ev("""(function(){
          document.getElementById('ed_platform').value = %s;
          document.getElementById('ed_company').value  = %s;
          document.getElementById('ed_api').value      = %s;
          document.getElementById('ed_cUrl').value     = %s;
          document.getElementById('editConfirm').click();
          return 'ok';
        })()""" % (json.dumps(sv_new), json.dumps("UI复现公司改后"),
                    json.dumps(sv_api2), json.dumps(sv_home2)))
        c.pump(3)
        srows = [r for r in appdb.list_srv() if r["plat_name"] == sv_new]
        chk("服务器修改已落库", len(srows) == 1, len(srows))
        if srows:
            chk("base_url 已更新", srows[0]["base_url"] == sv_api2, srows[0]["base_url"])
            chk("公司名已更新", srows[0]["comp_name"] == "UI复现公司改后", srows[0]["comp_name"])
            chk("公司首页网址已更新", srows[0]["home_url"] == sv_home2, srows[0]["home_url"])
        chk("服务器修改也是 UPDATE（旧名已不存在）",
            not [r for r in appdb.list_srv() if r["plat_name"] == sv_old], sv_old)
        c.pump(1)
        sdisp = ev_json(c, JS_SRV_ROW.replace("NAME", json.dumps(sv_new)), "[18] 服务器列表显示")
        w("    %s" % json.dumps(sdisp, ensure_ascii=False))
        chk("列表显示新平台名", sdisp.get("platform") == sv_new, sdisp.get("platform"))
        chk("列表显示新 base_url", sdisp.get("api") == sv_api2, sdisp.get("api"))
        chk("公司名外链 == 新公司首页", sdisp.get("companyHref") == sv_home2, sdisp.get("companyHref"))

        w("")
        w("[19] 新增服务器：base_url 格式锁定（非网址不许保存，裸域名自动补协议头）")
        np_plat = "UI复现网址锁-" + stamp
        c.go(APP + "new_server.html", wait=5)
        blk = ev_json(c, """(function(){
          document.getElementById('platform').value = %s;
          document.getElementById('baseUrl').value  = 'not a url';
          document.getElementById('btnSave').click();
          return JSON.stringify({
            cls: document.getElementById('baseUrl').className,
            val: document.getElementById('baseUrl').value
          });
        })()""" % json.dumps(np_plat), "[19] 非法网址")
        w("    %s" % json.dumps(blk, ensure_ascii=False))
        chk("非法 base_url 进告警态", "is-invalid" in str(blk.get("cls")), blk.get("cls"))
        c.pump(2.5)
        chk("被拦下：仍停留在新增服务器页（没有跳回列表）",
            "new_server.html" in str(c.ev("location.pathname")), c.ev("location.pathname"))
        chk("被拦下：库内没有写入这条服务器",
            not [r for r in appdb.list_srv() if r["plat_name"] == np_plat], np_plat)

        fixup = ev_json(c, """(function(){
          document.getElementById('baseUrl').value = 'example.com/v1';
          document.getElementById('btnSave').click();
          return JSON.stringify({val: document.getElementById('baseUrl').value});
        })()""", "[19] 裸域名补全")
        w("    裸域名补全后 base_url = %s" % fixup.get("val"))
        chk("裸域名自动补 https://", fixup.get("val") == "https://example.com/v1", fixup.get("val"))
        c.pump(3.5)
        chk("合法网址保存成功并跳回服务器列表",
            "list_server.html" in str(c.ev("location.pathname")), c.ev("location.pathname"))
        nrows = [r for r in appdb.list_srv() if r["plat_name"] == np_plat]
        chk("库内已写入该服务器", len(nrows) == 1, len(nrows))
        if nrows:
            chk("落库的 base_url 是补全后的网址",
                nrows[0]["base_url"] == "https://example.com/v1", nrows[0]["base_url"])

        w("")
        w("[20] 提示词构造：多轮会话按轮次依次调用，逐轮各产出一条回答")
        c.go(APP + "prompt.html", wait=5)
        b20 = c.ev(JS_PB_PICK_SERVER % json.dumps(mock_plat))
        chk("已选中 mock 服务器", b20 == mock_base, b20)
        pk20 = ev_json(c, JS_PB_PICK_MODEL % json.dumps(mock_model), "[20] 选模型")
        chk("已选中 mock 模型", pk20.get("modelVal") == mock_model, pk20.get("modelVal"))

        MOCK_HITS.clear()
        multi = ev_json(c, JS_PB_RUN_MULTI % json.dumps(MOCK_KEY), "[20] 多轮真实调试")
        w("    输出: %r" % multi["out"][:140])
        w("    tokens: %s   费用: %s / %s / %s"
          % (multi["uTotal"], multi["mIn"], multi["mOut"], multi["mTotal"]))
        chk("输出里有【第 1 轮】", "【第 1 轮】" in multi["out"], multi["out"][:80])
        chk("输出里有【第 2 轮】（不再只有一次回答）", "【第 2 轮】" in multi["out"], multi["out"][:80])
        chk("两轮回答文本各一条", multi["out"].count(MOCK_TEXT) == 2, multi["out"][:160])
        chk("第 2 轮内容是模型对第 2 轮的回复",
            (MOCK_TEXT + "·第 2 轮") in multi["out"], multi["out"][:160])
        chk("tokens 为两轮之和",
            multi["uTotal"] == "%d tokens" % (MOCK_USAGE["total_tokens"] * 2), multi["uTotal"])
        chk("费用按两轮 tokens 合计",
            multi["mIn"] == "¥" + fmt_num(MOCK_USAGE["prompt_tokens"] * 2 / 1000.0 * MOCK_IN_PRICE),
            multi["mIn"])
        chk("mock 端恰好收到 2 次调用（一轮一次）", len(MOCK_HITS) == 2, len(MOCK_HITS))
        if len(MOCK_HITS) == 2:
            h1 = MOCK_HITS[0]["body"].get("messages") or []
            h2 = MOCK_HITS[1]["body"].get("messages") or []
            roles1 = [m.get("role") for m in h1]
            roles2 = [m.get("role") for m in h2]
            w("    第 1 次请求 roles=%s" % roles1)
            w("    第 2 次请求 roles=%s" % roles2)
            chk("第 1 次：system + user(第 1 轮)", roles1 == ["system", "user"], roles1)
            chk("第 2 次：带上第 1 轮的 assistant 回复（真多轮上下文）",
                roles2 == ["system", "user", "assistant", "user"], roles2)
            chk("第 1 轮提问内容正确",
                len(h1) > 1 and h1[1].get("content") == "第一问",
                h1[1].get("content") if len(h1) > 1 else None)
            chk("上一轮回答被当作上下文回传",
                len(h2) > 2 and h2[2].get("content") == MOCK_TEXT,
                h2[2].get("content") if len(h2) > 2 else None)
            chk("第 2 轮提问内容正确",
                len(h2) > 3 and h2[3].get("content") == "第二问",
                h2[3].get("content") if len(h2) > 3 else None)

        w("")
        w("[21] 提示词构造：计费单价按库内原文展示（4 位小数不被压成 2 位）")
        c.go(APP + "prompt.html", wait=5)
        c.ev(JS_PB_PICK_SERVER % json.dumps(fx_plat))
        t21 = ev_json(c, JS_PB_PICK_MODEL % json.dumps(fx_new), "[21] 选 4 位小数计费的模型")
        w("    %s" % json.dumps(t21, ensure_ascii=False))
        chk("输入计费标签 == ¥4.135（原文，不四舍五入）", t21.get("inTag") == "¥4.135", t21.get("inTag"))
        chk("输出计费标签 == ¥0.0005（原文，不四舍五入）", t21.get("outTag") == "¥0.0005", t21.get("outTag"))

        w("")
        w("[23] 修改弹窗的字段与控件形态，逐项对齐「新增」页（期望值取自新增页自身 DOM，非硬编码）")

        # 模型：新增模型页 vs 模型列表的修改弹窗
        c.go(APP + "model_add.html", wait=4)
        spec_add_m = ev_json(c, JS_FORM_SPEC.replace("SEL", json.dumps(".form-body")),
                             "[23] 新增模型页表单")
        c.go(APP + "foundation1_models.html", wait=5)
        ev_json(c, JS_OPEN_EDIT_MODEL.replace("NAME", json.dumps(fx_new)), "[23] 打开模型修改弹窗")
        spec_ed_m = ev_json(c, JS_FORM_SPEC.replace("SEL", json.dumps("#editBody")),
                            "[23] 模型修改弹窗")

        # 服务器：新增服务器页 vs 服务器列表的修改弹窗
        c.go(APP + "new_server.html", wait=4)
        spec_add_s = ev_json(c, JS_FORM_SPEC.replace("SEL", json.dumps(".form-body")),
                             "[23] 新增服务器页表单")
        c.go(APP + "list_server.html", wait=5)
        ev_json(c, JS_OPEN_EDIT_SRV.replace("NAME", json.dumps(sv_new)), "[23] 打开服务器修改弹窗")
        spec_ed_s = ev_json(c, JS_FORM_SPEC.replace("SEL", json.dumps("#editBody")),
                            "[23] 服务器修改弹窗")

        SPEC_KEYS = ("label", "req", "ctrl", "type", "inputmode", "readonly", "combo", "multi", "hint")
        for page, want, got in (("模型", spec_add_m, spec_ed_m), ("服务器", spec_add_s, spec_ed_s)):
            w("    %s · 新增页 %d 字段：%s"
              % (page, len(want), "、".join(x.get("label", "") for x in want)))
            w("    %s · 修改弹窗 %d 字段：%s"
              % (page, len(got), "、".join(x.get("label", "") for x in got)))
            chk("%s：字段个数与新增页一致" % page, len(want) == len(got),
                "新增页 %s / 弹窗 %s" % (len(want), len(got)))
            for i in range(min(len(want), len(got))):
                a, b = want[i], got[i]
                same = all(a.get(k) == b.get(k) for k in SPEC_KEYS)
                chk("%s：第 %d 项「%s」标签/必填/控件/输入类型 与新增页一致"
                    % (page, i + 1, a.get("label")), same,
                    "新增页 %s ←→ 弹窗 %s" % (a, b))

        w("")
        w("[23] 弹窗内控件的行为与新增页同款（下拉展开 / 多选勾选 / 只读锁定 / 数字过滤）")
        c.go(APP + "foundation1_models.html", wait=5)
        ev_json(c, JS_OPEN_EDIT_MODEL.replace("NAME", json.dumps(fx_new)), "[23] 重开模型弹窗")

        mt = ev_json(c, JS_EDIT_TYPE_MULTI, "[23] 模型类型多选")
        w("    %s" % json.dumps(mt, ensure_ascii=False))
        chk("类型下拉可展开", mt.get("opened") is True, mt)
        chk("每个类型项都带方形勾选框 .cb", mt.get("items") and mt.get("cb") == mt.get("items"),
            "%s/%s" % (mt.get("cb"), mt.get("items")))
        chk("勾选 2 项后取值以「、」连接", mt.get("val") == mt.get("expect"),
            "%s vs %s" % (mt.get("val"), mt.get("expect")))
        chk("多选时面板保持展开（可连续勾选）", mt.get("stillOpen") is True, mt.get("stillOpen"))

        un = ev_json(c, JS_EDIT_UNIT_SELECT, "[23] 计费单位只读单选")
        w("    %s" % json.dumps(un, ensure_ascii=False))
        chk("计费单位框只读（不可手输）", un.get("readonly") is True, un.get("readonly"))
        chk("单位下拉可展开并有选项", un.get("opened") is True and un.get("items", 0) > 0, un)
        chk("单位选中即回填", un.get("after") == un.get("second"),
            "%s vs %s" % (un.get("after"), un.get("second")))
        chk("单位单选后立即收起面板", un.get("closed") is True, un.get("closed"))

        nf = ev_json(c, JS_EDIT_NUM_FILTER, "[23] 计费框输入限制")
        w("    %s" % json.dumps(nf, ensure_ascii=False))
        chk("计费框滤掉字母与多余小数点", nf.get("v1") == "12.345", nf.get("v1"))
        chk("正常小数原样保留", nf.get("v2") == "18.75", nf.get("v2"))

        pf = ev_json(c, JS_EDIT_PLATFORM_COMBO, "[23] 模型 · 平台可编辑下拉")
        w("    %s" % json.dumps(pf, ensure_ascii=False))
        chk("平台框可编辑（非只读，允许自定义输入）", pf.get("readonly") is False, pf.get("readonly"))
        chk("平台下拉可展开且有选项", pf.get("opened") is True and pf.get("items", 0) > 0, pf)
        chk("平台选中即回填并收起面板",
            pf.get("after") == pf.get("first") and pf.get("closed") is True, pf)

        c.go(APP + "list_server.html", wait=5)
        ev_json(c, JS_OPEN_EDIT_SRV.replace("NAME", json.dumps(sv_new)), "[23] 重开服务器弹窗")
        spf = ev_json(c, JS_EDIT_PLATFORM_COMBO, "[23] 服务器 · 平台可编辑下拉")
        w("    %s" % json.dumps(spf, ensure_ascii=False))
        chk("服务器平台框可编辑（非只读）", spf.get("readonly") is False, spf.get("readonly"))
        chk("服务器平台下拉可展开且有选项",
            spf.get("opened") is True and spf.get("items", 0) > 0, spf)
        chk("服务器平台选中即回填并收起面板",
            spf.get("after") == spf.get("first") and spf.get("closed") is True, spf)

        w("")
        w("[24] 新增模型 · 「平台 / 官方名」下拉 = 服务器列表（动态同步，无写死的假平台）")
        # 先真实新增一台服务器，再打开页面 —— 用来证明下拉取的是「当前」服务器列表，而不是快照
        sync_plat = "UI复现平台同步-" + stamp
        appdb.create_srv(sync_plat, "https://api.example.com/v1", "UI复现同步公司", "https://www.example.com")
        c.go(APP + "model_add.html", wait=4)

        exp_plats, _seen = [], set()
        for r in appdb.list_srv():
            n = r["plat_name"]
            if n and n not in _seen:
                _seen.add(n)
                exp_plats.append(n)

        pm = ev_json(c, JS_ADD_PLATFORM_MENU, "[24] 新增模型页 · 平台下拉")
        w("    库内服务器（去重）：%s" % json.dumps(exp_plats, ensure_ascii=False))
        w("    下拉实际选项：%s" % json.dumps(pm.get("vals"), ensure_ascii=False))
        chk("下拉可展开", pm.get("opened") is True, pm)
        chk("选项与「服务器列表」逐项同值同序", pm.get("vals") == exp_plats,
            "%s vs %s" % (pm.get("vals"), exp_plats))
        chk("刚新增的服务器已同步进下拉", sync_plat in (pm.get("vals") or []), sync_plat)
        chk("每个选项都带彩色标记点 .tag-dot（沿用原型视觉）",
            pm.get("items") and pm.get("dots") == pm.get("items"),
            "%s/%s" % (pm.get("dots"), pm.get("items")))
        chk("框仍可编辑（支持手输自定义平台名）", pm.get("readonly") is False, pm.get("readonly"))
        chk("选中即回填对应平台名并收起面板",
            pm.get("after") == pm.get("first") and pm.get("closed") is True, pm)

        # 页面源码层面：写死的假平台必须一个都不剩
        with urllib.request.urlopen(APP + "model_add.html", timeout=20) as resp:
            add_src = resp.read().decode("utf-8", "replace")
        for ghost in ("魔搭社区", "OpenAI", "硅基流动", "OpenRouter"):
            chk("页面源码已无写死的平台项「%s」" % ghost,
                ('data-value="%s"' % ghost) not in add_src)

        w("")
        w("[22] 控制台异常")
        bad = [e for e in events if e.get("method") == "Runtime.exceptionThrown"]
        for e in bad:
            w("    EXCEPTION: %s" % str(e["params"].get("exceptionDetails", {})
                                        .get("exception", {}).get("description"))[:300])
        chk("页面无 JS 异常", not bad, "count=%d" % len(bad))

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
        if mock is not None:
            try:
                mock.shutdown()
            except Exception:
                pass
        try:
            bad_mdl = [r["id"] for r in appdb.list_mdl() if r["mdl_name"].startswith("UI复现")]
            if bad_mdl:
                appdb.delete_mdl(bad_mdl)
            bad_srv = [r["id"] for r in appdb.list_srv() if r["plat_name"].startswith("UI复现")]
            if bad_srv:
                appdb.delete_srv(bad_srv)
            if bad_mdl or bad_srv:
                w("(清理测试数据：模型 %d 行 / 平台 %d 行)" % (len(bad_mdl), len(bad_srv)))
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        shutil.rmtree(tmp, ignore_errors=True)
        open(OUT, "w", encoding="utf-8").write(buf.getvalue())
        print("written: %s" % OUT)


if __name__ == "__main__":
    sys.exit(main())
