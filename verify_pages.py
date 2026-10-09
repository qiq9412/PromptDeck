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
        并确认服务端没有对应校验（第二次迭代后看 core/views.py + store.py + chat.py）。

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
NODE = r"C:\Users\Lenovo\.workbuddy\binaries\node\versions\22.22.2-6\node.exe"

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


def settings_secrets():
    """从 config/settings.py 里读回明文密钥（sk-…），用来断言「它没进任何产物页」。

    刻意**不写死字面量** —— 否则这个校验脚本自己就成了泄漏点
    （verify_django.py 那条「密钥没写死在任何 .py 里」的扫描会把它抓出来）。
    """
    try:
        src = read(os.path.join(HERE, "config", "settings.py"))
    except OSError:
        return []
    return sorted(set(re.findall(r"sk-[A-Za-z0-9]{20,}", src)))


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
                      # 平台 / 官方名下拉：HTML 里为空容器，选项由服务器列表（/api/srv）注入
                      '<div class="combo-menu" id="menuPlatform" data-combo-menu role="listbox"></div>',
                      'fetch("/api/srv")', 'function fillPlatformMenu(', 'initCombo("comboPlatform")',
                      'REQ_FIELDS', 'function checkUrl(el)', 'function bindNumber(el)'],
        "must_not": ["PD.value", "isStreamlitMessage", "streamlit:",
                     # 原型里那批写死的假平台项必须已经消失（三个都是「服务器列表里没有的」）
                     'data-value="硅基流动"', 'data-value="魔搭社区"', 'data-value="OpenAI"',
                     'data-value="OpenRouter"'],
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
                      'checkUrl($baseUrl)',
                      # 10-09 新增：base_url 查重（用户点名）
                      #   前端：pdUrlTaken 改读真实库（原型读的是演示清单）+ 命中就停在本页
                      #   后端：POST /api/srv 命中回 409，页面照样给同一句提示
                      'function pdNormUrl(', 'function pdUrlTaken(',
                      'function pdSaveServer(', 'r.status === 409',
                      '已存在服务器API网址'],
        "must_not": ["PD.value", "isStreamlitMessage", "streamlit:",
                     # 演示用的网址清单必须已经下线（应用侧读 /api/srv）
                     '"https://api.openai.com/v1"', '"https://api.deepseek.com"'],
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
                      'function pdRenderAnswers(',
                      # 09-29 新增：链接中占位 / 链接失败红字 + 原因 + 指标清零（原型侧同口径）
                      '.pb-textarea.is-error',
                      '$modelOut.value = "正在链接模型";',
                      'classList.remove("is-error")', 'classList.add("is-error")',
                      '链接模型失败', "pdFillMetrics(0, 0)",
                      # 10-09 新增：跨页编辑流（方案页「编辑」/ 测试页「修改」→ 跳本页回填）
                      #   —— 三个下拉是 initCombos() 里**异步**初始化的，回填必须等它就绪，
                      #   否则 pbPickCombo(comboServer,…) 抛 TypeError、整段回填中断（页面全空）。
                      'function pdCombosReadyNow(', 'function pdWhenCombosReady(',
                      'pdWhenCombosReady(function(){',
                      # 10-09 新增：构造页草稿（sessionStorage，仅当前标签页）
                      'var PD_DRAFT_KEY = "pd_prompt_draft";',
                      'function pdDraftStart(', 'function pdDraftSchedule(', 'function pdDraftSave(',
                      'function pdDraftDrop(', 'function pdDraftRestore(', 'function pdFillForm(',
                      'function pdIsDraftField(', 'function pdComboVal(',
                      'window.sessionStorage.setItem(PD_DRAFT_KEY',
                      'window.addEventListener("pagehide", pdDraftSave)',
                      # 「清空草稿」按钮：只在编辑流出现，按钮自带 display ⇒ 必须有 [hidden] 规则压住
                      'id="btnClearDraft"', 'document.getElementById("btnClearDraft")',
                      '.pb-cleardraft[hidden]{display:none;}',
                      # 保存成功 → 内容已落库，草稿作废
                      'pdDraftDrop();',
                      # 10-09 新增：从「表结构提取」页「复制 → 确认」跳进来（?pdSchema=1）
                      #   —— 把「前缀句 + 表结构」整段覆盖进系统提示词，认领后清暂存 + 开草稿
                      'var PD_SCHEMA_KEY = "pd_schema_handoff";',
                      'params.get("pdSchema")',
                      '$sysPrompt.value = String(sent.text);',
                      'pd_schema_handoff',
                      '表结构已填入「系统提示词」',
                      # 10-09 新增：模型输出区的「链接数据库调试」按钮（用户点名）
                      #   —— 三个前置条件（表结构交接过 / 已生成 SQL / 连接元数据齐）
                      #      由 pdDbgMissing() 判，缺项置灰 + title 说明缺什么；
                      #      真调用由 PD_DBG_API 注入（原型态只弹一句演示提示、不发请求）。
                      'id="btnDbgLink"', '.pb-dbg[aria-disabled="true"]',
                      'var PD_DBLINK_KEY = "pd_db_link_meta";',
                      'function pdDbgMeta(', 'function pdDbgSql(',
                      'function pdDbgMissing(', 'function pdDbgSync(',
                      'function PD_DBG_API(', 'fetch("/api/dbeaver/link"',
                      '请先生成 SQL', '未检测到数据库连接信息',
                      # 10-09 新增：「页面内调试」弹窗（用户点名，与上面那条并存）
                      #   —— 三栏骨架 + 只读执行 + 密码弹窗内临时输入不留存
                      'id="btnDbgRun"', 'id="dbgRunMask"', 'id="pdxTree"',
                      'id="pdxSql"', 'id="pdxResult"', 'id="pdxPwd"',
                      'function pdxOpen(', 'function pdxExecute(',
                      'function pdxRenderResult(', 'function pdxWipePwd(',
                      'function PDX_RUN_API(', 'fetch("/api/database/execute-sql"',
                      '本次执行临时使用，不保存', 'try { pdxRenderResult(res); } finally { pdxWipePwd(); }'],
        "must_count": {
            # ① 定义 1 处；② initCombos() **里面**的就绪信号 1 处。合计恰好 2 处。
            #    原型里那句顶层的（在 initCombos 之外）必须被接线吃掉 —— 若它活到产物里，
            #    pdWhenCombosReady 一看「已就绪」就立刻回填，等于白等（踩过一遍）。
            "pdCombosReadyNow()": 2,
        },
        "must_not": ["PD.value", "isStreamlitMessage", "streamlit:",
                     # 原型里那段本地造的假输出必须已经消失
                     "（模拟输出", "estTokens(outText)",
                     # 费用不再四舍五入（金额用 fmtNum；单价属性不再走 num2 两位小数）
                     "toFixed(4)", "num2(",
                     # 就绪信号不许留在 initCombos() 之外（原型那句的原文，接线后会消失）
                     "pdCombosReadyNow();          /* 原型：三个下拉已同步初始化完成",
                     # ⚠️ 演示分支不许活到产物里：产物必须走 PD_DBG_API 真调后端，
                     #    否则点了按钮只会弹一句「原型演示」。
                     '原型演示：将唤起 DBeaver',
                     # ⚠️ 弹窗里那几个函数是**裸声明**（pdxXxx），调用处却写成 $pdxXxx
                     #    的话，运行时 ReferenceError 会静默打断整条渲染链 ——
                     #    e2e 才发现过一次，这里在静态层就挡住。
                     '$pdxSetStatus(', '$pdxRenderResult(', '$pdxExecute(',
                     '$pdxOpen(', '$pdxWipePwd('],
    },
    # 提示词方案列表：第二次迭代起，方案库的真源是服务端 tp 表
    "prompt_scheme.html": {
        "verify_snippets": [],
        "must_have": [
            # 列表 / 删除接后端
            'fetch("/api/tp")', 'apiTpDelete(ids)', 'function pdBoot()', 'pdBoot();',
            # 编辑入口（10-09 纳入断言）：点「编辑」写整份快照 + 带 query 跳构造页
            'var PD_EDIT_SNAP_KEY = "pd_edit_snapshot";', 'function pdGotoEdit(',
            '"&pdEntry=" + encodeURIComponent("prompt.html")',
            'var d = e.target.closest ? e.target.closest("[data-edit]") : null;',
            # 原型的示例方案改为由迁移 0002_seed_tp 灌库，页面不再自己播种
            "var schemes = [];"],
        "must_not": ["PD.value", "isStreamlitMessage", "streamlit:",
                     # 首屏播种本地存储的那段 IIFE 必须已经消失（改由后端提供数据）
                     "window.localStorage.setItem(PD_SCHEME_KEY, JSON.stringify(SEED_SCHEMES))"],
    },
    # 提示词测试：第二次迭代起接线（HTML / CSS 一字未动，只换数据层）
    "prompt_test.html": {
        "verify_snippets": [],
        "anchors": [
            # 必填校验内联在「添加测试问题」的确定按钮里（问题不能为空）
            ('document.getElementById("addConfirm").addEventListener("click", function(){',
             "  var q = $addQuestion.value.trim();\n",
             '添加测试问题必填校验'),
        ],
        "must_have": [
            # 左栏方案下拉的真源改成服务端方案库
            'fetch("/api/tp")', 'function pdNormScheme(',
            # 「开始调试」= 逐题调真实模型 + 逐题 AI 判定
            'fetch("/api/chat"', 'fetch("/api/judge"',
            'function pdRunQuestion(', 'function pdJudgeQuestion(',
            # 「保存结果」同时落 tst_rec / tst_rec_dtl
            'fetch("/api/rec"', 'planId:  currentScheme.id',
            # 「修改」入口（10-09 纳入断言）：与方案库页「编辑」同口径
            'window.sessionStorage.setItem("pd_edit_snapshot", JSON.stringify({',
            'location.href = "prompt.html?pdSchemeId=" + encodeURIComponent(String(s.id))'
            ' + "&pdEntry=prompt.html";'],
        "must_not": ["PD.value", "isStreamlitMessage", "streamlit:",
                     # 原型态的「假输出 + 本地比对」两处调用点必须已经不在了
                     "pdMockAnswer(t.q, t.ans, seed + i)",
                     "var ai  = pdAutoJudge(t.ans, fb);",
                     "setTimeout(step, 160);",
                     # 方案不再从本地存储读
                     "schemes = pdReadSchemes();"],
    },
    # 数据库表结构提取：第二次迭代起接线（HTML / CSS 一字未动，只换数据层）
    "Table_Structure_Extraction.html": {
        "verify_snippets": [],
        "anchors": [
            # 连接弹窗的必填 / 端口校验内联在「连接」按钮里，整段原样保留
            ('  if (connMode === "url"){', "\n  }", 'URL 模式服务器地址必填'),
            ('  if (connMode === "host"){', "\n  }", '端口号必填 / 1–65535 范围校验'),
        ],
        "must_have": [
            # 演示数据下线，改由后端灌真实库结构
            "var SCHEMA = [];", "var RELATIONS = [];",
            # 第二批：结构按库挂（一次连多个库，同名表不串）
            "function pdFillDb(", "db.map", "db.rels",
            "function schemaOf(name, db){", "schemaOf(tbl.name, db)",
            "function mockTables(kind, name, sch){",
            "function pdMergeDesc(",
            # 第二批：主机 / URL 连接真接驱动
            "function pdConnectDb(", 'fetch("/api/schema/connect"',
            # 打开页面**不再自动挂应用自带库**（14-18 用户点名）：左栏起始为空，
            # 后续流程全部由用户导入 / 连接的库驱动。
            # ⚠️ 必须钉住「启动时不调 GET 那个 schema 接口」—— 只断言没有 addDb 是不够的，
            #    那句自动挂库正是靠一次 fetch 触发的。
            "resetSchema();",
            # 第二批：主机 / URL 连接真接驱动
            "function pdConnectDb(", 'fetch("/api/schema/connect"',
            # 打开页面不再连应用库；说明仍由 DeepSeek 在「提取表结构」时补
            "function pdFetchDesc(", 'fetch("/api/schema/desc"', "function pdRunExtract(",
            # SQLite 选文件改成真上传
            'method: "POST", body: fd',
            # 10-09 新增：复制后弹窗确认 → 跳「提示词构造」页填系统提示词（用户点名）
            #   · 复制到剪贴板的老能力保留，弹窗只是「是否同时送过去」；
            #   · 内容走 sessionStorage 的 pd_schema_handoff，构造页按 ?pdSchema=1 认领；
            #   · 前缀句是用户逐字给的原话 —— 断言里带上它，防止被顺手改写。
            # 16:32 用户点名在其后追加一句输出约束「不要有其他解释，仅返回sql语句」
            #   （起因：模型爱在 SQL 前后带自然语言说明 / ```sql 围栏，粘进 DBeaver 跑不了）。
            #   ⚠️ 断言写成**两段拼接**的形状，与原型一致；哪一段都不许被润色。
            'var PD_SCHEMA_KEY    = "pd_schema_handoff";',
            'var PD_SCHEMA_PREFIX = "你是一个专业的sql语句工程师，现有表结构如下，根据用户问题返回sql语句\\n\\n"',
            '+ "不要有其他解释，仅返回sql语句\\n\\n";',
            '不要有其他解释，仅返回sql语句',
            "function pdSendSchemaToPrompt(",
            'window.location.href = "prompt.html?pdSchema=1";',
            'id="copyMask"', "$copyConfirm.addEventListener(",
            "showMask($copyMask);",
            # 10-09 新增：「链接数据库调试」需要的数据库元数据随同交接一起带走
            #   （连接方式 / 主机 / 端口 / 库名 / 用户名，SQLite 则是文件绝对路径 + 表名）。
            #   ⚠️ 密码**不登记**：交接体里只应该有 user，不该有 pwd / password。
            'var PD_DBLINK_KEY    = "pd_db_link_meta";',
            "function pdRememberConn(", "function pdFindConn(", "function pdDbLinkMeta(",
            'window.sessionStorage.setItem(PD_DBLINK_KEY',
            "pdRememberConn(name, kind, {",
            # SQLite 上传后由后端回传绝对路径（浏览器拿不到本机路径）⇒ 必须补写进登记项
            "pdRememberConn(d.name || f.name",
            # 14-18 新增：库头前的单库全选框（可全选单个库里所有表）
            #   · 三态（全选 / 半选 / 空）**从表行实时算**，不单独存 ⇒ 表行怎么变它都跟着对
            "function syncDbAllChecks(",
            'class="db-all-check" data-db-all="',
            'data-db-all]',          # 点击拦截：必须在折叠 / 展开之前拦，否则点它会顺手折叠库
            "t.closest(\"[data-db-all]\")",
        ],
        "must_not": ["PD.value", "isStreamlitMessage", "streamlit:",
                     # 演示种子与演示表结构必须已经下线
                     "demo_shop.sqlite", 'desc:"用户主表"', '["orders","user_id","users","id"',
                     # 第二批：「其余类型未实现」的占位提示 + 单库全局变量必须已经下线
                     "本机暂只支持 SQLite 直连", "pdApplySchema",
                     # ⚠️ 只认「单库全局变量」那一句的原文，别用裸 PD_DB ——
                     #    本轮新增的 PD_DBLINK_KEY / PD_CONN 里都含这四个字母（踩过一次）
                     "var PD_DB ",
                     # 14-18：启动时自动挂应用自带库的那段（含 pdBoot 与那次 GET schema）必须下线
                     "function pdBoot(", "pdBoot();",
                     # ⚠️ 新增同名函数会被原型里「后写的那个」盖掉 ⇒ 必须原地替换
                     #    （踩过：addDb 出现两份，原版生效 ⇒ 左栏 0 张表）
                     "function addDb(name, kind, silent){"],
    },
}

# 「原样托管」页：本轮不接后端，原样复制到 static/（侧栏跳转能正常打开）
VERBATIM = [
    "prompt_test_results_list.html",
]

# 服务端「不做二次校验」的断言要扫的文件（第二次迭代：Starlette 的 server.py
# 换成了 Django 的 core/views.py + core/store.py + core/chat.py；
# 本轮把 core/schema.py 也纳入 —— 它是表结构提取的唯一后端实现）
SERVER_FILES = [
    os.path.join(HERE, "core", "views.py"),
    os.path.join(HERE, "core", "store.py"),
    os.path.join(HERE, "core", "chat.py"),
    os.path.join(HERE, "core", "schema.py"),
    # 第二批：真接驱动的实现（MySQL/PG/SQL Server/Oracle/Db2）也纳入
    # 「服务端不做二次校验」的扫描范围
    os.path.join(HERE, "core", "dbx.py"),
]


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
        # 「必须恰好出现 N 次」—— 光有 must_have（存在性）挡不住「多留了一份」这类回归：
        # 原型里那句顶层的就绪信号若没被接线吃掉，产物会变成 3 处（踩过）。
        for s, n in spec.get("must_count", {}).items():
            actual = gen.count(s)
            chk("%s 恰好出现 %d 次（实际 %d）" % (s, n, actual), actual == n)

        # 10-09：「链接数据库调试」的元数据交接**不许带密码**。
        #   逐个 pdRememberConn(...) 调用点检查（连接登记是唯一入口）。
        for page, fname in (("Table_Structure_Extraction.html", "pdRememberConn("),):
            if page not in CHECKS:
                continue
            body = read(os.path.join(OUT_DIR, page))
            hits = []
            pos = 0
            while True:
                i = body.find(fname, pos)
                if i < 0:
                    break
                j = body.find("});", i)
                seg = body[i:(j + 3 if j > 0 else i + 300)]
                if "pwd" in seg or "password" in seg:
                    hits.append(seg[:80])
                pos = i + len(fname)
            chk("%s 登记连接信息时不带密码" % page, not hits, "命中 %s" % hits)

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
    w("产物页不得含明文密钥（第二次迭代：说明生成只在服务端，页面一个 sk- 都不该有）")
    secrets = settings_secrets()
    chk("能从 config/settings.py 读到 DeepSeek 密钥（供本项比对）", bool(secrets),
        "%d 把" % len(secrets))
    hit = []
    for name in list(CHECKS) + list(VERBATIM):
        body = read(os.path.join(OUT_DIR, name))
        for s in secrets:
            if s in body:
                hit.append(name)
    chk("所有产物页都搜不到明文密钥", not hit, "命中 %s" % sorted(set(hit)))

    w("")
    w("=" * 78)
    w("服务端校验口径（第二次迭代：Starlette 的 server.py 已换成 Django 的 core/*.py）：")
    srv = "\n".join(read(p) for p in SERVER_FILES)
    chk("服务端无必填校验（无 required/checkRequired）",
        "required" not in srv and "checkRequired" not in srv)
    chk("服务端无网址格式校验", "URL_RE" not in srv and "is-invalid" not in srv)
    # 10-09：用户点名的唯一例外 —— base_url 查重（前端提示 + 后端兜底）。
    #   它拦的是「这条网址已经存在」，不是「必填 / 格式」，所以上面两条仍然成立。
    chk("服务端有 base_url 查重（用户点名的例外）",
        "def find_srv_by_url(" in srv and "已存在服务器API网址" in srv)
    w("  说明：store.create_srv / create_mdl / save_tp 直接落库，"
      "仅做 空串->NULL 的类型归一。")
    w("        /api/chat 也只做「组装 messages -> 转发 -> 归一返回」，不复现前端的必填校验。")
    w("        例外（10-09 用户点名）：base_url 不可重复 ⇒ POST /api/srv 回 409、"
      "PUT 排除自己那条；")
    w("        前端 new_server.html 保存前也拦一道，两边提示同一句话。")

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
