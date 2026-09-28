# -*- coding: utf-8 -*-
"""从工作区根目录的原型页生成 Starlette 静态页面（static/）。

产出：
    static/foundation1_models.html ← foundation1_models.html 原型 + fetch 数据层（本次重点）
    static/model_add.html          ← model_add.html          原型 + fetch 数据层（本次重点）
    static/list_server.html        ← list_server.html        原型 + fetch 数据层
    static/new_server.html         ← new_server.html         原型 + fetch 数据层
    static/prompt.html             ← prompt.html             原型 + fetch 数据层（本次重点）

硬约束（用户要求，逐条落实）：
  1. 字段 / 风格 / 排版 / CSS 零改动 -> <style> 段与 <body> 结构逐字节保留；
  2. 控件只补交互、不改外观         -> 只改「点了按钮之后数据去哪儿」，不碰 DOM 结构与 class；
  3. 必填校验原样保留               -> checkRequired / checkUrl / REQ_FIELDS 一行不动，
                                        且服务端不做二次校验。

接线方式：页面内数据源由 mock 数组换成 fetch("/api/*")，因此不需要任何 iframe / 组件桥。

「原型是唯一来源」的边界（本轮起）：
    版式与行内行为（rowHtml、操作列、修改弹窗、外链口径、费用展示格式）一律写进原型本身，
    本脚本只负责「数据从哪来、改完往哪存」这一层接线，因此 patch 数量越少越好。
    新增 UI 请改原型，不要在这里堆 patch。
"""
import os
import re
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.dirname(BASE_DIR)                 # 原型所在目录（工作区根）
OUT_DIR = os.path.join(BASE_DIR, "static")

errors = []


def apply(text, old, new, label, want=1):
    """按字面替换，命中次数不符即记错（防止原型改动后静默失配）。"""
    n = text.count(old)
    if n != want:
        errors.append("[%s] 期望命中 %d 次，实际 %d 次" % (label, want, n))
        return text
    return text.replace(old, new)


def apply_re(text, pattern, repl, label, want=1):
    text, n = pattern.subn(repl, text)
    if n != want:
        errors.append("[%s] 正则命中 %d 次（期望 %d）" % (label, n, want))
    return text


# =====================================================================
# 模型列表 foundation1_models.html
# ---------------------------------------------------------------------
# 说明：rowHtml / 外链兜底 / 操作列与修改弹窗，都已经写进原型本身
# （原型是设计与行为的唯一来源），这里只接线「数据从哪儿来、改完往哪儿存」。
# =====================================================================
F_DEL_HEAD_OLD = r'''document.getElementById("delConfirm").addEventListener("click", function(){
  var n = selectedIds().length;
  models = models.filter(function(m){ return !selected[m.id]; });
'''

F_DEL_HEAD_NEW = r'''document.getElementById("delConfirm").addEventListener("click", function(){
  var ids = selectedIds();
  var n   = ids.length;
  models = models.filter(function(m){ return !selected[m.id]; });
'''

F_DEL_TAIL_OLD = r'''  render();
  toast("已删除 " + n + " 个模型", "ok");
});
'''

F_DEL_TAIL_NEW = r'''  render();
  toast("已删除 " + n + " 个模型", "ok");
  apiMdlDelete(ids);                       /* 数据层接线：落库 */
});
'''

# 操作列「修改」：保存时把这一行的改动落库（PUT /api/mdl），成功后回读列表
F_EDIT_TAIL_OLD = r'''  closeEdit();
  computeView();
  render();
  toast("已保存修改", "ok");
});
'''

F_EDIT_TAIL_NEW = r'''  closeEdit();
  computeView();
  render();
  toast("已保存修改", "ok");
  apiMdlUpdate(m);                         /* 数据层接线：落库 */
});
'''

F_COMBO_OLD = r'''initCombo("comboPlatform", "fPlatform", uniqueOf("platform"));
initCombo("comboType",     "fType",     uniqueOf("type"));
'''

F_COMBO_NEW = r'''/* 下拉选项改为取后端数据（srv.plat_name / mdl.mdl_type），随数据一起在 boot() 里初始化 */
'''

F_BOOT_OLD = r'''/* ================= 启动 ================= */
render();
'''

F_BOOT_NEW = r'''/* ================= 启动：数据来自后端 /api/mdl ================= */
function apiMdlDelete(ids){
  return fetch("/api/mdl", {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ids: ids })
  });
}

/* 修改一条模型：PUT 落库；成功后回读列表，让跨表联出来的平台名 / 平台主页同步刷新。
   回读后不重新 initCombo（下拉只允许初始化一次，重复初始化会挂两套监听）。 */
function apiMdlUpdate(m){
  return fetch("/api/mdl", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      id:        m.id,
      plat_name: m.platform,
      mdl_name:  m.model,
      mdl_type:  m.type,
      in_price:  m.inPrice,
      out_price: m.outPrice,
      unit:      m.unit,
      url:       m.url
    })
  }).then(function(r){ return r.json(); }).then(function(res){
    if (!res || !res.ok){ toast("保存修改失败：" + ((res && res.error) || "未知错误"), "warn"); return; }
    fetch("/api/mdl").then(function(r){ return r.json(); }).then(function(rows){
      models = rows;
      computeView();
      render();
    });
  }).catch(function(){
    toast("保存修改失败，请确认服务已启动", "warn");
  });
}

function boot(){
  fetch("/api/mdl").then(function(r){ return r.json(); }).then(function(rows){
    models   = rows;
    view     = models.slice();
    selected = {};
    initCombo("comboPlatform", "fPlatform", uniqueOf("platform"));
    initCombo("comboType",     "fType",     uniqueOf("type"));
    render();
  }).catch(function(){
    toast("加载模型列表失败，请确认服务已启动", "warn");
  });
}

boot();
'''


# =====================================================================
# 新增模型 model_add.html
# =====================================================================
A_SAVE_OLD = r'''  if (!checkUrl($modelUrl)){
    toast("网址格式不正确，请以 http:// 或 https:// 开头", "warn");
    $modelUrl.focus();
    return;
  }
  toast("模型已保存成功", "ok");
});
'''

A_SAVE_NEW = r'''  if (!checkUrl($modelUrl)){
    toast("网址格式不正确，请以 http:// 或 https:// 开头", "warn");
    $modelUrl.focus();
    return;
  }
  toast("模型已保存成功", "ok");

  /* 数据层接线：落库后返回「模型列表」（平台若是新的，后端会自动补一条 srv） */
  fetch("/api/mdl", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      plat_name: document.getElementById("platform").value.trim(),
      mdl_name:  document.getElementById("modelName").value.trim(),
      mdl_type:  document.getElementById("modelType").value.trim(),
      in_price:  document.getElementById("inPrice").value.trim(),
      out_price: document.getElementById("outPrice").value.trim(),
      unit:      document.getElementById("unit").value.trim(),
      url:       $modelUrl.value.trim()
    })
  }).then(function(){
    setTimeout(function(){ window.location.href = "foundation1_models.html"; }, 700);
  });
});
'''


# =====================================================================
# 服务器列表 list_server.html
# ---------------------------------------------------------------------
# 说明：rowHtml（外链口径 + 操作列）与修改弹窗都已写进原型，这里只接线数据层。
# =====================================================================
L_DEL_HEAD_OLD = r'''document.getElementById("delConfirm").addEventListener("click", function(){
  var n = selectedIds().length;
  servers = servers.filter(function(s){ return !selected[s.id]; });
'''

L_DEL_HEAD_NEW = r'''document.getElementById("delConfirm").addEventListener("click", function(){
  var ids = selectedIds();
  var n   = ids.length;
  servers = servers.filter(function(s){ return !selected[s.id]; });
'''

L_DEL_TAIL_OLD = r'''  render();
  toast("已删除 " + n + " 台服务器", "ok");
});
'''

L_DEL_TAIL_NEW = r'''  render();
  toast("已删除 " + n + " 台服务器", "ok");
  apiSrvDelete(ids);                       /* 数据层接线：落库 */
});
'''

# 操作列「修改」：保存时把这一行的改动落库（PUT /api/srv），成功后回读列表
L_EDIT_TAIL_OLD = r'''  render();
  toast("已保存修改", "ok");
});
'''

L_EDIT_TAIL_NEW = r'''  render();
  toast("已保存修改", "ok");
  apiSrvUpdate(s);                         /* 数据层接线：落库 */
});
'''

L_BOOT_OLD = r'''/* ================= 启动 ================= */
render();
'''

L_BOOT_NEW = r'''/* ================= 启动：数据来自后端 /api/srv ================= */
function apiSrvDelete(ids){
  return fetch("/api/srv", {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ids: ids })
  });
}

/* 修改一台服务器：PUT 落库；成功后回读列表，让关联模型的平台名 / 主页链接同步刷新。 */
function apiSrvUpdate(s){
  return fetch("/api/srv", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      id:        s.id,
      plat_name: s.platform,
      comp_name: s.company,
      base_url:  s.api,
      home_url:  s.cUrl
    })
  }).then(function(r){ return r.json(); }).then(function(res){
    if (!res || !res.ok){ toast("保存修改失败：" + ((res && res.error) || "未知错误"), "warn"); return; }
    fetch("/api/srv").then(function(r){ return r.json(); }).then(function(rows){
      servers = rows;
      var kwP = $qPlatform.value.trim().toLowerCase();
      var kwC = $qCompany.value.trim().toLowerCase();
      view = servers.filter(function(x){
        var okP = !kwP || x.platform.toLowerCase().indexOf(kwP) > -1;
        var okC = !kwC || x.company.toLowerCase().indexOf(kwC) > -1;
        return okP && okC;
      });
      render();
    });
  }).catch(function(){
    toast("保存修改失败，请确认服务已启动", "warn");
  });
}

function boot(){
  fetch("/api/srv").then(function(r){ return r.json(); }).then(function(rows){
    servers  = rows;
    view     = servers.slice();
    selected = {};
    render();
  }).catch(function(){
    toast("加载服务器列表失败，请确认服务已启动", "warn");
  });
}

boot();
'''


# =====================================================================
# 新增服务器 new_server.html
# =====================================================================
N_SAVE_OLD = r'''  toast("服务器已保存，正在返回列表…", "ok");
  setTimeout(function(){ window.location.href = "list_server.html"; }, 700);
'''

N_SAVE_NEW = r'''  /* 数据层接线：先落库，再返回列表 */
  fetch("/api/srv", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      plat_name: $platform.value.trim(),
      comp_name: document.getElementById("company").value.trim(),
      base_url:  $baseUrl.value.trim(),
      home_url:  $homeUrl.value.trim()
    })
  }).then(function(){
    toast("服务器已保存，正在返回列表…", "ok");
    setTimeout(function(){ window.location.href = "list_server.html"; }, 700);
  });
'''


# =====================================================================
# 提示词构造 prompt.html
# ---------------------------------------------------------------------
# 本页 HTML 与 CSS 一个字都不动（连三个下拉里的 mock 选项也留在 HTML 里，
# 只在页面运行时整体换成后端数据），因此「<script> 之前」相对原型逐字节一致。
# 脚本只有两处改动：
#   (1) 三个下拉的初始化推迟到选项注入之后（原型选项写死，本可立即初始化）；
#   (2) 末尾追加数据层：从 /api/srv、/api/mdl 取数生成选项，再初始化下拉。
# 接口不可用时保留原型自带 mock 选项，页面按原型照常可用。
# =====================================================================
PB_COMBO_OLD_1 = r'''/* 服务器：选中后 Base_url 跟随变更，并让类型 / 模型下拉只保留该服务器下的选项 */
var comboServer = initCombo("comboServer", function(it){'''

PB_COMBO_NEW_1 = r'''/* 三个下拉的选项由后端数据注入后再初始化（HTML 未改动，只换数据来源） */
var comboServer, comboModelType, comboModel;

function initCombos(){

/* 服务器：选中后 Base_url 跟随变更，并让类型 / 模型下拉只保留该服务器下的选项 */
comboServer = initCombo("comboServer", function(it){'''

PB_COMBO_OLD_2 = r'''/* 模型类型：选项随服务器联动，选中后模型下拉只保留该类型 */
var comboModelType = initCombo("comboModelType", function(it){'''

PB_COMBO_NEW_2 = r'''/* 模型类型：选项随服务器联动，选中后模型下拉只保留该类型 */
comboModelType = initCombo("comboModelType", function(it){'''

PB_COMBO_OLD_3 = r'''/* 模型：选中后显示对应的输入 / 输出 token 费用标签 */
var comboModel = initCombo("comboModel", function(it){'''

PB_COMBO_NEW_3 = r'''/* 模型：选中后显示对应的输入 / 输出 token 费用标签 */
comboModel = initCombo("comboModel", function(it){'''

PB_COMBO_OLD_4 = r'''}, modelKeep);
'''

PB_COMBO_NEW_4 = r'''}, modelKeep);

}
'''

PB_BOOT_OLD = "  }, 2200);\n}\n"

PB_DATA_LAYER = r'''/* ================= 数据层：下拉选项来自后端 ================= */
/* 口径：只把「下拉里的 mock 选项」这份数据换成库里的数据，页面 HTML 与 CSS 一字未动；
   接口不可用时保留原型自带的选项，页面仍按原型可用。 */
var TYPE_ORDER = ["文本生成", "多模态", "推理模型", "向量嵌入"];

function pdEsc(s){
  return String(s === null || s === undefined ? "" : s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/* 计费单价写进 data-in / data-out 时按库里的原文（原样保留小数位数，不四舍五入） */
function priceAttr(v){
  if (v === null || v === undefined) return "";
  return String(v).trim();
}

/* 按原型 combo-item 的属性顺序生成选项节点 */
function comboItemHtml(attrs, text){
  var s = "";
  for (var i = 0; i < attrs.length; i++){
    s += " " + attrs[i][0] + "=" + JSON.stringify(pdEsc(attrs[i][1]));
  }
  return "<div class=\"combo-item\"" + s + " role=\"option\">" + pdEsc(text) + "</div>";
}

function setMenu(menuId, html){
  if (html) document.getElementById(menuId).innerHTML = html;
}

/* 服务器下拉：平台 / 官方名 -> data-value，Base_url -> data-base（取自 srv 表） */
function fillServerMenu(servers){
  setMenu("menuServer", servers.map(function(s){
    return comboItemHtml([["data-value", s.platform], ["data-base", s.api]], s.platform);
  }).join(""));
}

/* 模型类型下拉：取自 mdl 表的去重类型；原型原有的 4 类按原型顺序排在前面 */
function fillTypeMenu(models){
  var seen = {}, types = [];
  models.forEach(function(m){
    if (m.type && !seen[m.type]){ seen[m.type] = 1; types.push(m.type); }
  });
  types.sort(function(a, b){
    var ia = TYPE_ORDER.indexOf(a), ib = TYPE_ORDER.indexOf(b);
    return (ia < 0 ? TYPE_ORDER.length : ia) - (ib < 0 ? TYPE_ORDER.length : ib);
  });
  setMenu("menuModelType", types.map(function(t){
    return comboItemHtml([["data-value", t], ["data-type", t]], t);
  }).join(""));
}

/* 模型下拉：模型名 + 归属服务器 + 类型 + 输入/输出计费 + 单位（取自 mdl 表） */
function fillModelMenu(models){
  setMenu("menuModel", models.map(function(m){
    return comboItemHtml([
      ["data-value",  m.model],
      ["data-type",   m.type],
      ["data-server", m.platform],
      ["data-in",     priceAttr(m.inPrice)],
      ["data-out",    priceAttr(m.outPrice)],
      ["data-unit",   m.unit]
    ], m.model);
  }).join(""));
}

function boot(){
  Promise.all([
    fetch("/api/srv").then(function(r){ return r.json(); }),
    fetch("/api/mdl").then(function(r){ return r.json(); })
  ]).then(function(res){
    var servers = res[0] || [], models = res[1] || [];
    fillServerMenu(servers);
    fillModelMenu(models);
    fillTypeMenu(models);
    initCombos();
  }).catch(function(){
    initCombos();
    toast("加载服务器 / 模型失败，请确认服务已启动", "warn");
  });
}

boot();
'''

PB_BOOT_NEW = "  }, 2200);\n}\n\n" + PB_DATA_LAYER + "\n"


# ---------------------------------------------------------------------
# 调试按钮：由「纯前端模拟」改成「调用真实模型」
# ---------------------------------------------------------------------
# 原型这段是本地造的假输出（"（模拟输出 · xx）…"）与按字符数估算的 token。
# 现在改为把请求发到后端 /api/chat，由后端转发给真实模型，再把模型返回的
# 文本与真实 usage 回填到原样的输出框与三项指标里。
# 注意：上面那段必填校验（sysPrompt / apiKey 的 is-invalid 分支）一行未动。
PB_RUN_HEAD_OLD = r'''/* ================= 调试 ================= */
function estTokens(s){ return Math.ceil((s || "").length / 2); }
function fmtTokens(n){ return (n || 0).toLocaleString("en-US"); }
'''

PB_RUN_HEAD_NEW = r'''/* ================= 调试 ================= */
function estTokens(s){ return Math.ceil((s || "").length / 2); }
function fmtTokens(n){ return (n || 0).toLocaleString("en-US"); }

/* 数据层接线：调试改为调用后端 /api/chat（由后端转发给真实模型）。
   这里只负责「发请求 / 回填结果」，不碰任何 DOM 结构、class 与样式。 */
var pdRunning = false;

/* 多轮时按「第 N 轮」依次展示每一轮的模型回答（后端是按轮次依次调用的）；
   单轮时直接就是回答原文。tail 用于把失败信息接在已拿到的回答后面。 */
function pdRenderAnswers(answers, tail){
  if (!answers || !answers.length) return tail || "";
  if (answers.length === 1) return answers[0] + (tail ? "\n\n" + tail : "");
  var parts = [];
  for (var i = 0; i < answers.length; i++){
    parts.push("【第 " + (i + 1) + " 轮】\n" + answers[i]);
  }
  var s = parts.join("\n\n");
  return tail ? s + "\n\n" + tail : s;
}

/* 按原型口径回填三项指标：tokens 用模型返回的真实 usage（多轮为各轮之和），
   费用 = tokens/1000 × 库内单价；金额不做四舍五入（fmtNum 只消掉浮点误差尾巴）。 */
function pdFillMetrics(inTokens, outTokens){
  document.getElementById("uIn").textContent    = fmtTokens(inTokens) + " tokens";
  document.getElementById("uOut").textContent   = fmtTokens(outTokens) + " tokens";
  document.getElementById("uTotal").textContent = fmtTokens(inTokens + outTokens) + " tokens";
  document.getElementById("mIn").textContent    = "¥" + fmtNum(inTokens  / 1000 * currentModel.inPrice);
  document.getElementById("mOut").textContent   = "¥" + fmtNum(outTokens / 1000 * currentModel.outPrice);
  document.getElementById("mTotal").textContent = "¥" + fmtNum((inTokens  / 1000 * currentModel.inPrice)
                                                            + (outTokens / 1000 * currentModel.outPrice));
}
'''

PB_RUN_OLD = r'''  var inTokens = estTokens(sys) + estTokens(userAll);
  var outText =
    "（模拟输出 · " + currentModel.name + "）\n\n" +
    "已按服务器 " + currentServer.name + "（" + currentServer.base + "）发起一次调试请求。\n" +
    "会话轮次：" + (1 + rounds) + " 轮    " +
    "系统提示词 " + sys.length + " 字符 ｜ 用户提示词 " + user.length + " 字符\n\n" +
    "这里是模型的示例回复内容，用于演示调试结果的展示效果。接入真实服务后，此区域将显示模型的实际返回文本。";
  var outTokens = estTokens(outText);

  var inCost  = inTokens  / 1000 * currentModel.inPrice;
  var outCost = outTokens / 1000 * currentModel.outPrice;

  $modelOut.value = outText;
  document.getElementById("uIn").textContent    = fmtTokens(inTokens) + " tokens";
  document.getElementById("uOut").textContent   = fmtTokens(outTokens) + " tokens";
  document.getElementById("uTotal").textContent = fmtTokens(inTokens + outTokens) + " tokens";
  document.getElementById("mIn").textContent    = "¥" + fmtNum(inCost);
  document.getElementById("mOut").textContent   = "¥" + fmtNum(outCost);
  document.getElementById("mTotal").textContent = "¥" + fmtNum(inCost + outCost);

  toast("调试完成", "ok");
'''

PB_RUN_NEW = r'''  /* 数据层接线：发起一次真实调用（后端转发给模型），输出区显示模型的实际返回文本。
     点击后先把输出区置为「正在链接模型」占位，避免上一轮答案一直挂到新返回到达 */
  if (pdRunning){ toast("正在调用模型，请稍候…", "warn"); return; }

  var roundTexts = [user];
  if ($multi.checked){
    var $roundTas = $rounds.querySelectorAll("textarea");
    for (var ri = 0; ri < $roundTas.length; ri++) roundTexts.push($roundTas[ri].value);
  }

  pdRunning = true;
  $modelOut.value = "正在链接模型";
  toast("正在调用模型…", "ok");

  fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      base_url: currentServer.base,
      api_key:  $apiKey.value.trim(),
      model:    currentModel.name,
      system:   sys,
      rounds:   roundTexts
    })
  }).then(function(r){ return r.json(); }).then(function(res){
    pdRunning = false;
    if (!res || !res.ok){
      var failMsg = "（第 " + ((res && res.round) || 1) + " 轮调用失败）" + ((res && res.error) || "未知错误");
      $modelOut.value = pdRenderAnswers(res && res.answers, failMsg);
      pdFillMetrics(0, 0);
      toast("模型调用失败", "warn");
      return;
    }
    $modelOut.value = pdRenderAnswers(res.answers, "");
    pdFillMetrics(res.prompt_tokens || 0, res.completion_tokens || 0);
    toast("调试完成", "ok");
  }).catch(function(err){
    pdRunning = false;
    $modelOut.value = "（调用失败）" + err;
    pdFillMetrics(0, 0);
    toast("模型调用失败", "warn");
  });
'''


PATCHES = {
    "foundation1_models.html": [
        ("re", re.compile(r"var models = \[.*?\n\];", re.S), "var models = [];", "models-1 mock数组"),
        ("str", F_DEL_HEAD_OLD, F_DEL_HEAD_NEW, "models-2 删除取 ids"),
        ("str", F_DEL_TAIL_OLD, F_DEL_TAIL_NEW, "models-3 删除落库"),
        ("str", F_EDIT_TAIL_OLD, F_EDIT_TAIL_NEW, "models-4 修改落库"),
        ("str", F_COMBO_OLD, F_COMBO_NEW, "models-5 下拉选项改由后端提供"),
        ("str", F_BOOT_OLD, F_BOOT_NEW, "models-6 启动加载"),
    ],
    "model_add.html": [
        ("str", A_SAVE_OLD, A_SAVE_NEW, "add-1 保存落库"),
    ],
    "list_server.html": [
        ("re", re.compile(r"var servers = \[.*?\n\];", re.S), "var servers = [];", "srv-list-1 mock数组"),
        ("str", L_DEL_HEAD_OLD, L_DEL_HEAD_NEW, "srv-list-2 删除取 ids"),
        ("str", L_DEL_TAIL_OLD, L_DEL_TAIL_NEW, "srv-list-3 删除落库"),
        ("str", L_EDIT_TAIL_OLD, L_EDIT_TAIL_NEW, "srv-list-4 修改落库"),
        ("str", L_BOOT_OLD, L_BOOT_NEW, "srv-list-5 启动加载"),
    ],
    "new_server.html": [
        ("str", N_SAVE_OLD, N_SAVE_NEW, "srv-new-1 保存落库"),
    ],
    "prompt.html": [
        ("str", PB_COMBO_OLD_1, PB_COMBO_NEW_1, "prompt-1 下拉初始化延迟 + initCombos 包裹"),
        ("str", PB_COMBO_OLD_2, PB_COMBO_NEW_2, "prompt-2 模型类型下拉延迟初始化"),
        ("str", PB_COMBO_OLD_3, PB_COMBO_NEW_3, "prompt-3 模型下拉延迟初始化"),
        ("str", PB_COMBO_OLD_4, PB_COMBO_NEW_4, "prompt-4 收尾 initCombos"),
        ("str", PB_BOOT_OLD, PB_BOOT_NEW, "prompt-5 启动加载（数据来自 /api/*）"),
        ("str", PB_RUN_HEAD_OLD, PB_RUN_HEAD_NEW, "prompt-6 调试指标回填 + 并发守卫"),
        ("str", PB_RUN_OLD, PB_RUN_NEW, "prompt-7 调试改为调用真实模型"),
    ],
}

# 本期无「原样托管」页（prompt.html 已接入数据层）
VERBATIM = []


def build(name, edits):
    with open(os.path.join(SRC_DIR, name), "r", encoding="utf-8") as fp:
        text = fp.read()
    for kind, old, new, label in edits:
        if kind == "re":
            text = apply_re(text, old, new, label)
        else:
            text = apply(text, old, new, label)
    return text


def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    for name, edits in PATCHES.items():
        html = build(name, edits)
        out = os.path.join(OUT_DIR, name)
        with open(out, "w", encoding="utf-8", newline="\n") as fp:
            fp.write(html)
        print("生成 %-28s %8d bytes" % (name, os.path.getsize(out)))

    for name in VERBATIM:
        with open(os.path.join(SRC_DIR, name), "r", encoding="utf-8") as fp:
            text = fp.read()
        out = os.path.join(OUT_DIR, name)
        with open(out, "w", encoding="utf-8", newline="\n") as fp:
            fp.write(text)
        print("复制 %-28s %8d bytes（原样，无数据层改动）" % (name, os.path.getsize(out)))

    if errors:
        print("\n接线失败：")
        for e in errors:
            print("  - " + e)
        return 1

    print("\n全部接线成功。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
