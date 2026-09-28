# -*- coding: utf-8 -*-
"""原型零改动回归校验：比对 static/ 产物 与 工作区根目录的原型。

校验口径（对应三条用户约束）：
  1. 字段 / 风格 / 排版 / CSS 零改动
     -> <style> 段、<body> 开头至 <script> 之前、</script> 之后至文件尾，三处逐字节一致；
  2. 控件只补交互、不改外观
     -> <script> 之外的 DOM 结构零差异；<script> 内的改动逐行打印，便于人工核对；
  3. 必填校验原样保留
     -> 把校验相关函数/代码块整体抽出，要求原型与产物逐字节相同；
        锚点可按页配置（prompt.html 的必填校验是内联在「调试」按钮里的）；
        并确认服务端没有对应校验（server.py 不含 required 检查）。

另做两件事：
  - 用 node --check 验证脚本语法；
  - 检查是否残留上一版（Streamlit 组件桥）的东西或 mock 数据。

结果写 _check.txt；退出码 0 = 全部通过。
"""
import difflib
import io
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.dirname(HERE)
OUT_DIR = os.path.join(HERE, "static")
NODE = r"C:\Users\Lenovo\.workbuddy\binaries\node\versions\22.22.2-3\node.exe"

buf = io.StringIO()
FAILS = []


def w(s=""):
    buf.write(str(s) + "\n")


def chk(label, ok, detail=""):
    w("  [%s] %s%s" % ("PASS" if ok else "FAIL", label, ("  " + str(detail)) if detail else ""))
    if not ok:
        FAILS.append(label)


def read(p):
    with open(p, "r", encoding="utf-8") as fp:
        return fp.read()


def split_script(html):
    """拆成 前置 / 脚本 / 后置；页面只有一个 <script> 块。"""
    i = html.find("<script>")
    j = html.find("</script>")
    if i < 0 or j < 0:
        return html, None, ""
    return html[:i], html[i + len("<script>"):j], html[j + len("</script>"):]


# 每页要求「原型与产物逐字节一致」的校验相关片段（从原型里抓，避免手抄）
def grab(text, start, end, label):
    i = text.find(start)
    if i < 0:
        FAILS.append("原型里找不到锚点 %s" % label)
        return None
    j = text.find(end, i)
    if j < 0:
        FAILS.append("原型里找不到锚点结束 %s" % label)
        return None
    return text[i:j + len(end)]


DEFAULT_ANCHORS = [
    ('REQ_FIELDS', "\n}", '必填字段清单'),
    ('function checkUrl(el)', "\n}", '网址校验 checkUrl'),
    ('function bindNumber(el)', "\n}", '计费框数字限制 bindNumber'),
]

CHECKS = {
    "foundation1_models.html": {
        "verify_snippets": [],
        "must_have": ['var models = [];', 'fetch("/api/mdl")', 'apiMdlDelete(ids);',
                      'apiMdlUpdate(m);',
                      'initCombo("comboPlatform", "fPlatform", uniqueOf("platform"));',
                      # 修改弹窗：字段与控件形态对齐「新增模型」页
                      'function initEditCombo(', 'kind:"multi"', 'kind:"select"',
                      'bindEditNumber('],
        "must_not": ["PD.value", "isStreamlitMessage", "pUrl", "id:1,", "streamlit:",
                     # model 名称不再指向不存在的 model_detail.html，改为新增模型时填的网址
                     "model_detail.html",
                     # 旧的「一行一个纯文本框」渲染器已由 EDIT_FIELDS + 控件分派取代
                     "editFieldEl("],
    },
    "model_add.html": {
        "verify_snippets": [],
        "must_have": ['fetch("/api/mdl", {', 'plat_name: document.getElementById("platform")',
                      'REQ_FIELDS', 'function checkUrl(el)', 'function bindNumber(el)'],
        "must_not": ["PD.value", "isStreamlitMessage", "streamlit:"],
    },
    "list_server.html": {
        "verify_snippets": [],
        "must_have": ['var servers = [];', 'fetch("/api/srv")', 'apiSrvDelete(ids);',
                      'apiSrvUpdate(s);',
                      # 修改弹窗：字段与控件形态对齐「新增服务器」页（含该页原本没有的可编辑下拉）
                      'function initEditCombo(', 'kind:"combo"', 'class="combo"',
                      'inputmode="url"'],
        "must_not": ["PD.value", "isStreamlitMessage", "pUrl", "id:1,", "streamlit:",
                     # 旧的「一行一个纯文本框」渲染器已由 EDIT_FIELDS + 控件分派取代
                     "editFieldEl("],
    },
    "new_server.html": {
        "verify_snippets": [],
        "must_have": ['fetch("/api/srv", {', 'plat_name: $platform.value.trim()',
                      # base_url 的格式锁定（必填校验之外的网址格式校验）
                      'checkUrl($baseUrl)'],
        "must_not": ["PD.value", "isStreamlitMessage", "streamlit:"],
    },
    # 提示词构造：HTML / CSS 一字未动，三个下拉的选项在运行时由后端数据注入
    "prompt.html": {
        "verify_snippets": [],
        "anchors": [
            # 必填校验内联在「调试」按钮里：系统提示词 + API_key
            ('document.getElementById("btnRun").addEventListener("click", function(){',
             "  var sys  = $sysPrompt.value;\n",
             '调试按钮必填校验（系统提示词 / API_key）'),
        ],
        "must_have": ['fetch("/api/srv")', 'fetch("/api/mdl")',
                      'fillServerMenu(servers);', 'fillModelMenu(models);', 'fillTypeMenu(models);',
                      'function initCombos()', 'initCombos();', 'var comboServer, comboModelType, comboModel;',
                      # 调试已接入真实模型，多轮按轮次依次调用
                      'fetch("/api/chat"', 'function pdFillMetrics(', 'pdRunning',
                      'function pdRenderAnswers('],
        "must_not": ["PD.value", "isStreamlitMessage", "streamlit:",
                     # 原型里那段本地造的假输出必须已经消失
                     "（模拟输出", "estTokens(outText)",
                     # 费用不再四舍五入（金额用 fmtNum；单价属性不再走 num2 两位小数）
                     "toFixed(4)", "num2("],
    },
}

# 本期无「原样托管」页（prompt.html 已接入数据层）
VERBATIM = []


def js_syntax_ok(code, tag):
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fp:
        fp.write(code)
        path = fp.name
    try:
        r = subprocess.run([NODE, "--check", path], capture_output=True, text=True)
        if r.returncode != 0:
            FAILS.append("%s JS 语法" % tag)
            w("      %s" % (r.stderr or r.stdout).strip()[:300])
        return r.returncode == 0
    except FileNotFoundError:
        return None
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def main():
    w("=" * 78)
    w("PromptDeck · 原型零改动回归校验（static/ vs 原型）")
    w("=" * 78)

    for name, spec in CHECKS.items():
        src = read(os.path.join(SRC_DIR, name))
        gen = read(os.path.join(OUT_DIR, name))
        pre_a, js_a, post_a = split_script(src)
        pre_b, js_b, post_b = split_script(gen)

        w("")
        w("■ %s" % name)
        w("  1) 字段/风格/排版/CSS 零改动")
        chk("<style> 段与 body 结构（<script> 之前）逐字节一致", pre_a == pre_b,
            "len %d vs %d" % (len(pre_a), len(pre_b)))
        chk("</script> 之后（文件尾）逐字节一致", post_a == post_b)

        w("  2) 控件只补交互：<script> 内的改动")
        diff = [ln for ln in difflib.unified_diff(js_a.splitlines(), js_b.splitlines(),
                                                  lineterm="", n=0)
                if ln[:1] in "+-" and not ln.startswith(("+++", "---"))]
        w("     改动行数：%d（新增 %d / 删除 %d）"
          % (len(diff), sum(1 for d in diff if d[0] == "+"), sum(1 for d in diff if d[0] == "-")))
        for d in diff:
            w("       %s" % d)
        chk("改动行数非零（确有接线）", len(diff) > 0)

        w("  3) 必填校验原样保留")
        hit_anchor = 0
        for anchor, end, label in spec.get("anchors", DEFAULT_ANCHORS):
            if anchor not in src:
                continue
            hit_anchor += 1
            a = grab(src, anchor, end, label)
            b = grab(gen, anchor, end, label)
            chk("%s 逐字节一致" % label, a is not None and a == b)
        if not hit_anchor:
            w("      本页原型不含必填/格式校验（校验在 model_add.html，见该项）")

        w("  4) 接线标记")
        for s in spec["must_have"]:
            chk("含 %s" % s, s in gen)
        for s in spec["must_not"]:
            chk("无残留 %s" % s, s not in gen)

        w("  5) JS 语法 node --check")
        ok = js_syntax_ok(js_b, name)
        chk("语法通过", ok is not False, "未找到 node，跳过" if ok is None else "")

    for name in VERBATIM:
        src = read(os.path.join(SRC_DIR, name))
        gen = read(os.path.join(OUT_DIR, name))
        w("")
        w("■ %s（本期原样托管）" % name)
        chk("逐字节一致", src == gen)

    w("")
    w("=" * 78)
    w("服务端校验口径：")
    srv = read(os.path.join(HERE, "server.py"))
    chk("server.py 无必填校验（无 required/checkRequired）",
        "required" not in srv and "checkRequired" not in srv)
    chk("server.py 无网址格式校验", "URL_RE" not in srv and "is-invalid" not in srv)
    w("  说明：db.create_srv / db.create_mdl 直接落库，仅做 空串->NULL、字符串->数值 的类型归一。")
    w("        /api/chat 也只做「组装 messages -> 转发 -> 归一返回」，不复现前端的必填校验。")

    w("")
    w("=" * 78)
    w("结果：%s" % ("全部通过" if not FAILS else "失败项 -> " + "; ".join(FAILS)))

    open(os.path.join(HERE, "_check.txt"), "w", encoding="utf-8").write(buf.getvalue())
    print(buf.getvalue())
    return 0 if not FAILS else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
