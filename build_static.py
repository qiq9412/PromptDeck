# -*- coding: utf-8 -*-
"""从工作区根目录的原型页生成 Django 托管的静态页面（static/）。

产出：
    static/foundation1_models.html ← foundation1_models.html 原型 + fetch 数据层
    static/model_add.html          ← model_add.html          原型 + fetch 数据层
    static/list_server.html        ← list_server.html        原型 + fetch 数据层
    static/new_server.html         ← new_server.html         原型 + fetch 数据层
    static/prompt.html             ← prompt.html             原型 + fetch 数据层
    static/prompt_scheme.html      ← prompt_scheme.html      原型 + fetch 数据层
    static/prompt_test.html        ← prompt_test.html        原型 + fetch 数据层（第二次迭代接线）
    static/Table_Structure_Extraction.html ← 同名原型        原型 + fetch 数据层（第二次迭代接线）
    另有一页原样托管（VERBATIM）：prompt_test_results_list

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

# ---------------------------------------------------------------------
# 「平台 / 官方名」下拉：选项改由服务器列表（srv）提供
# ---------------------------------------------------------------------
# 原型里这个下拉曾写死 4 个假平台（硅基流动 / OpenRouter / 魔搭社区 / OpenAI），
# 会出现「服务器列表里没有、下拉里却有」的错位。现在原型把 <div class="combo-menu">
# 清空，选项在页面运行时由 /api/srv 注入 —— 与「模型列表」页 initCombo(..., options)
# 的做法一致（空容器 + 运行期生成，颜色同样按 DOTS 轮转）。
# 注意顺序：本页 initCombo 会在初始化时固定选项集合，所以必须「先注入、再 initCombo」。
A_COMBO_OLD = r'''initCombo("comboPlatform");   /* 可编辑 + 可输入自定义 */
'''

A_COMBO_NEW = r'''/* 平台 / 官方名：下拉选项来自服务器列表（/api/srv），与「服务器列表」页保持一致。
   HTML 里不再写死平台项，避免出现「列表里没有、下拉里却有」的假选项。
   本页 initCombo 在初始化时会固定选项集合，所以必须先注入选项、再初始化下拉。 */
var PD_DOTS = ["d-purple", "d-mint", "d-orange", "d-gold"];

function pdEsc(s){
  return String(s === null || s === undefined ? "" : s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/* 用服务器列表里的「平台 / 官方名」生成选项：顺序与服务器列表一致，同名只留一条 */
function fillPlatformMenu(servers){
  var seen = {}, names = [];
  for (var i = 0; i < servers.length; i++){
    var n = servers[i] && servers[i].platform;
    if (n && !seen[n]){ seen[n] = 1; names.push(n); }
  }
  var html = "";
  for (var j = 0; j < names.length; j++){
    html += '<div class="combo-item" data-value="' + pdEsc(names[j]) + '" role="option">'
         +  '<i class="tag-dot ' + PD_DOTS[j % PD_DOTS.length] + '"></i>' + pdEsc(names[j]) + '</div>';
  }
  document.getElementById("menuPlatform").innerHTML = html;
}

function boot(){
  fetch("/api/srv").then(function(r){ return r.json(); }).then(function(rows){
    fillPlatformMenu(rows || []);
    initCombo("comboPlatform");   /* 可编辑 + 可输入自定义 */
  }).catch(function(){
    /* 接口不可用时保留空下拉（会显示「无匹配选项，可直接使用当前输入」），仍可手输平台名 */
    initCombo("comboPlatform");
    toast("加载服务器列表失败，请确认服务已启动", "warn");
  });
}
boot();
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

/* ===== 链接模型 / 多轮输出（09-29 新增） =====
   原型页没有后端，用一次定时器模拟「链接模型 -> 返回结果」的往返；口径与应用侧一致：
     · 链接中   输出框显示「正在链接模型」，同一次调试未返回前不接受重复点击；
     · 多轮会话 按「【第 N 轮】」逐轮标注每一轮的模型回答；
     · 链接失败 输出框整体转红字（.is-error）并写明失败原因，三项费用指标清零。
   演示失败分支：地址栏加 ?pdFail=401（可选 401/403/404/429/500/timeout），
   或在控制台执行 PD_FORCE_FAIL = "自定义原因"。不加参数即链接成功。 */
var pbLinking = false;
var pbRunSeq  = 0;
var PB_FAIL_MAP = {
  "401": "HTTP 401 Unauthorized —— API_key 无效或已过期",
  "403": "HTTP 403 Forbidden —— 该 API_key 无权访问此模型",
  "404": "HTTP 404 Not Found —— Base_url 或模型名称不存在",
  "429": "HTTP 429 Too Many Requests —— 请求频率或额度已超限",
  "500": "HTTP 500 Internal Server Error —— 服务商侧异常，请稍后重试",
  "timeout": "请求超时 —— 30s 内未收到服务器响应，请检查 Base_url 与网络"
};
function pbFailReason(){
  if (typeof window.PD_FORCE_FAIL === "string" && window.PD_FORCE_FAIL) return window.PD_FORCE_FAIL;
  var m = /[?&]pdFail=([^&#]+)/.exec(window.location.search || "");
  if (!m) return "";
  var k = decodeURIComponent(m[1]);
  return PB_FAIL_MAP[k] || ("请求失败 —— " + k);
}
/* 模拟单轮回答（接入真实服务后，这段由后端返回的真实文本取代） */
function pbAnswer(round){
  var ctx = round > 1 ? "，并带入前 " + (round - 1) + " 轮上下文" : "";
  return "模拟输出 · " + currentModel.name + "\n" +
    "已按服务器 " + currentServer.name + "（" + currentServer.base + "）提交第 " + round + " 轮请求" + ctx + "。\n" +
    "这里是第 " + round + " 轮的模型示例回复内容，用于演示调试结果的展示效果；接入真实服务后，此区域显示模型的实际返回文本。";
}
/* 多轮时按「第 N 轮」逐轮标注；单轮时直接就是回答原文（口径与应用侧一致） */
function pbRenderRounds(answers){
  if (!answers || !answers.length) return "";
  if (answers.length === 1) return answers[0];
  var parts = [];
  for (var i = 0; i < answers.length; i++){
    parts.push("【第 " + (i + 1) + " 轮】\n" + answers[i]);
  }
  return parts.join("\n\n");
}
/* 链接失败的文案：已拿到的回答接在前面，失败原因写在后面 */
function pbFailText(reason, answers){
  var tail = "链接模型失败\n\n原因：" + reason;
  return (answers && answers.length) ? (pbRenderRounds(answers) + "\n\n" + tail) : tail;
}
/* 原型按字符数估算 token；接入真实服务后改用后端返回的真实 usage */
function pbFillMetrics(inTokens, outTokens){
  document.getElementById("uIn").textContent    = fmtTokens(inTokens) + " tokens";
  document.getElementById("uOut").textContent   = fmtTokens(outTokens) + " tokens";
  document.getElementById("uTotal").textContent = fmtTokens(inTokens + outTokens) + " tokens";
  document.getElementById("mIn").textContent    = "¥" + fmtNum(inTokens  / 1000 * currentModel.inPrice);
  document.getElementById("mOut").textContent   = "¥" + fmtNum(outTokens / 1000 * currentModel.outPrice);
  document.getElementById("mTotal").textContent = "¥" + fmtNum(inTokens  / 1000 * currentModel.inPrice
                                                            + outTokens / 1000 * currentModel.outPrice);
}
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

PB_RUN_OLD = r'''  /* 本次调试的轮次数：第 1 轮 + 多轮会话时追加的每一轮 */
  var roundCount = 1;
  if ($multi.checked) roundCount += tas.length;

  /* 链接守卫：同一次调试未返回前拦住重复点击（按钮外观与文案均不改） */
  if (pbLinking){ toast("正在调用模型，请稍候…", "warn"); return; }

  var myRun = ++pbRunSeq;
  pbLinking = true;
  $modelOut.classList.remove("is-error");
  $modelOut.value = "正在链接模型";            /* 链接中的占位文案 */

  /* 原型无真实后端：用一次定时器模拟「链接 -> 返回」的往返 */
  setTimeout(function(){
    if (myRun !== pbRunSeq) return;            /* 已被新的一次调试取代，丢弃这次回填 */
    pbLinking = false;

    var reason = pbFailReason();
    if (reason){                               /* 链接失败：红字 + 原因 + 指标清零 */
      $modelOut.value = pbFailText(reason, null);
      $modelOut.classList.add("is-error");
      pbFillMetrics(0, 0);
      toast("模型调用失败", "warn");
      return;
    }

    var answers = [];
    for (var k = 0; k < roundCount; k++) answers.push(pbAnswer(k + 1));
    $modelOut.value = pbRenderRounds(answers);
    pbFillMetrics(estTokens(sys) + estTokens(userAll), estTokens($modelOut.value));

    toast("调试完成", "ok");
  }, 900);
'''

PB_RUN_NEW = r'''  /* 数据层接线：发起一次真实调用（后端转发给模型），输出区显示模型的实际返回文本。
     点击后先把输出区置为「正在链接模型」占位，避免上一轮答案一直挂到新返回到达；
     链接失败时输出框整体转红字（.is-error）并写明失败原因，三项费用指标清零。 */
  if (pdRunning){ toast("正在调用模型，请稍候…", "warn"); return; }

  var roundTexts = [user];
  if ($multi.checked){
    var $roundTas = $rounds.querySelectorAll("textarea");
    for (var ri = 0; ri < $roundTas.length; ri++) roundTexts.push($roundTas[ri].value);
  }

  pdRunning = true;
  $modelOut.classList.remove("is-error");
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
      /* 已拿到的轮次照常展示，失败信息接在后面（与原型 pbFailText 同口径） */
      var failMsg = "链接模型失败\n\n原因：（第 " + ((res && res.round) || 1) + " 轮调用失败）"
                  + ((res && res.error) || "未知错误");
      $modelOut.value = pdRenderAnswers(res && res.answers, failMsg);
      $modelOut.classList.add("is-error");
      pdFillMetrics(0, 0);
      toast("模型调用失败", "warn");
      return;
    }
    $modelOut.value = pdRenderAnswers(res.answers, "");
    $modelOut.classList.remove("is-error");
    pdFillMetrics(res.prompt_tokens || 0, res.completion_tokens || 0);
    toast("调试完成", "ok");
  }).catch(function(err){
    pdRunning = false;
    $modelOut.value = "链接模型失败\n\n原因：" + err;
    $modelOut.classList.add("is-error");
    pdFillMetrics(0, 0);
    toast("模型调用失败", "warn");
  });
'''


# =====================================================================
# 「保存」按钮：由「只写本地存储」改成「落服务端 tp 表 + 跳提示词方案列表页」
# ---------------------------------------------------------------------
# 原型口径一行不动：先弹「保存提示词方案」弹窗（系统提示词必填、方案命名必填
# 都由前端拦），确认后按「编辑流 id 覆盖 → 同名覆盖 → 新增」定位目标。
# 本次只把落点从 localStorage 换成 tp 表，成功后跳回 prompt_scheme.html。
# =====================================================================
PB_SAVE_OLD = r'''  if (!pdWriteSchemes(list)){
    toast("保存失败：当前环境无法写入本地存储", "warn");
    return;
  }
  closeSaveModal();
  if (targetId && hit > -1){
    toast("已覆盖提示词方案「" + name + "」，相关测试结果已同步", "ok");
  } else {
    toast(hit > -1 ? "已覆盖同名方案「" + name + "」"
                   : "已保存提示词方案「" + name + "」，可到「提示词测试」页选用", "ok");
  }
  pdClearEditEntry();      /* 覆盖完成：这次编辑流结束，把入口参数摘掉 */
});
'''

PB_SAVE_NEW = r'''  /* 本地存储镜像：真源是服务端 tp 表，这里仍写一份到本地，让「提示词测试」页
     左栏下拉继续可用（下一轮把它也接上就可以删掉这一段）。 */
  pdWriteSchemes(list);
  closeSaveModal();
  toast("正在保存提示词方案「" + name + "」…", "ok");

  /* ★ 第二次迭代：把这一份方案 POST 到服务端 tp 表，成功后再跳「提示词方案」列表页。
     覆盖口径（编辑流按 id 覆盖 / 其余同名覆盖）由服务端复用原型逻辑判定，因此
     非编辑流刻意不带 id —— 本地算出来的 mx+1 只是本地镜像的下标，不是库里的主键。 */
  var postBody = {};
  for (var pk in item){ postBody[pk] = item[pk]; }
  if (!targetId){ delete postBody.id; }

  fetch("/api/tp", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(postBody)
  }).then(function(r){ return r.json(); }).then(function(res){
    if (!res || !res.ok) throw new Error((res && res.error) || "服务端未返回 ok");

    /* 用服务端返回的主键回写本地镜像，避免两处 id 对不上 */
    if (res.id){
      var arr = pdReadSchemes();
      for (var z = 0; z < arr.length; z++){
        if (arr[z] && String(arr[z].name) === name) arr[z].id = res.id;
      }
      pdWriteSchemes(arr);
    }
    pdClearEditEntry();      /* 覆盖完成：这次编辑流结束，把入口参数摘掉 */

    var okMsg = (res.result === "updated")
      ? "已覆盖提示词方案「" + name + "」，相关测试结果已同步"
      : (res.result === "overwritten"
          ? "已覆盖同名方案「" + name + "」"
          : "已保存提示词方案「" + name + "」，可到「提示词测试」页选用");
    toast(okMsg, "ok");
    setTimeout(function(){ window.location.href = "prompt_scheme.html"; }, 600);
  }).catch(function(err){
    toast("保存失败：" + err, "warn");
  });
});
'''


# =====================================================================
# 提示词方案列表 prompt_scheme.html
# ---------------------------------------------------------------------
# 第二次迭代（Django 版）口径变化：方案库的真源从浏览器本地存储挪到服务端 tp 表
#   · 列表  GET    /api/tp
#   · 删除  DELETE /api/tp
#   · 保存  POST   /api/tp（在 prompt.html 点「保存」时发起）
# 本地存储降级成「镜像」，只为让本轮尚未接后端的页面（提示词测试页的左栏下拉）
# 还能读到同一批方案 —— 下一轮把它也接上即可删掉镜像那几行。
# 页面 HTML 与 CSS 一个字不动，脚本只改「数据从哪儿来、删完往哪儿存」。
# =====================================================================
SCH_SCHEMES_OLD = r'''var schemes = (function(){
  var raw = null;
  try{ raw = window.localStorage.getItem(PD_SCHEME_KEY); }catch (e){ raw = null; }
  if (raw === null){
    try{ window.localStorage.setItem(PD_SCHEME_KEY, JSON.stringify(SEED_SCHEMES)); }catch (e){}
  }
  pdUpgradeSchemeApiKeys();   /* 旧数据补 apiKey（键已存在时不播种，这里兜住） */
  var stored = pdReadSchemes();
  var src = (stored !== null) ? stored : SEED_SCHEMES;
  var out = [];
  for (var i = 0; i < src.length; i++){
    var s = pdNormScheme(src[i]);
    if (!s.id) s.id = i + 1;
    out.push(s);
  }
  return out;
})();
'''

SCH_SCHEMES_NEW = r'''/* 数据源：方案库的真源在服务端三张表（tst_plan / tst_plan_prompt / tst_api_key），
   列表由 pdBoot() 拉取后填入。库里没有方案时，页面按原型渲染那 1 行空态占位。 */
var schemes = [];
'''

# 注：原型里「/* 启动 */ + render();」出现两次（1388 与 1519 行各一处，
# 原型自带的重复，不属本次改动范围），所以锚点往前多带两行把它定位到第一处。
SCH_BOOT_OLD = r'''  if (d){ pdGotoEdit(Number(d.getAttribute("data-edit"))); return; }
});

/* ================= 启动 ================= */
render();
'''

SCH_BOOT_NEW = r'''  if (d){ pdGotoEdit(Number(d.getAttribute("data-edit"))); return; }
});

/* ================= 启动：数据来自后端 /api/tp（第二次迭代） =================
   页面 HTML 与 CSS 一个字不动，只换数据来源。本地存储再写一份镜像，
   供本轮尚未接入后端的页面（提示词测试页的左栏下拉）读取。 */
function apiTpList(){
  return fetch("/api/tp").then(function(r){ return r.json(); });
}
function apiTpDelete(ids){
  return fetch("/api/tp", {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ids: ids })
  });
}

function pdBoot(){
  apiTpList().then(function(list){
    list = (list && typeof list.length === "number") ? list : [];
    var out = [];
    for (var i = 0; i < list.length; i++){
      var s = pdNormScheme(list[i]);
      if (!s.id) s.id = i + 1;
      out.push(s);
    }
    schemes = out;
    selected = {};
    view = schemes.slice();
    pdWriteSchemes(schemes);        /* 本地存储镜像（下一轮接入后即可删掉） */
    refreshTypeOptions();
    applyFilter();
  }).catch(function(){
    render();
    toast("加载提示词方案失败，请确认服务已启动", "warn");
  });
}
pdBoot();
'''

SCH_DEL_OLD = r'''  closeModal();
  pdPersist();
  refreshTypeOptions();
  applyFilter();
  toast("已删除 " + ids.length + " 个提示词方案", "ok");
});
'''

SCH_DEL_NEW = r'''  closeModal();
  pdPersist();
  refreshTypeOptions();
  applyFilter();
  toast("已删除 " + ids.length + " 个提示词方案", "ok");
  /* 数据层接线：同步删掉服务端 tp 表里的这几条，删完回读一次（以库为准） */
  apiTpDelete(ids).then(function(){ pdBoot(); })
                  .catch(function(){ toast("删除未同步到服务端，请确认服务已启动", "warn"); });
});
'''


# =====================================================================
# 提示词测试 prompt_test.html（第二次迭代起接线）
# ---------------------------------------------------------------------
# 本页 HTML 与 CSS 一个字不动，只换三处「数据从哪儿来、改完往哪儿存」：
#   1. 左栏方案下拉：本地存储 → GET /api/tp（方案库的真源在服务端三张表）
#   2. 「开始调试」：原型态的 pdMockAnswer / pdAutoJudge 模拟 →
#      逐题调 /api/chat（用选定方案的 模型 + API_key + 系统提示词，题目当用户提示词）
#      → 模型回答写「测试反馈答案」列 → 再调 /api/judge 得到「判断」列的 AI 结论
#   3. 「保存结果」：原本只写本地存储 → 同时 POST /api/rec（tst_rec + tst_rec_dtl）
# 原型里那套模拟函数（pdMockAnswer / pdAutoJudge / pdHash…）**保留不删** ——
# 原型页没有后端，打开原型本身仍按原样演示；这与 prompt.html 的 pbAnswer 同款处理。
# =====================================================================
PT_SCHEMES_OLD = r'''/* 重载方案列表（打开页面 / 另一个标签页保存过 / 存储变化时） */
function loadSchemes(){
  var keep = currentScheme ? String(currentScheme.name) : "";
  schemes = pdReadSchemes();
  currentScheme = null;
  for (var i = 0; i < schemes.length; i++){
    if (String(schemes[i].name) === keep){ currentScheme = schemes[i]; break; }
  }
  buildSchemeMenu();
  fillSchemeView();
}
'''

PT_SCHEMES_NEW = r'''/* 重载方案列表（打开页面 / 另一个标签页保存过 / 存储变化时）。
   第二次迭代：方案库的真源在服务端三张表（tst_plan / tst_plan_prompt / tst_api_key），
   这里改成 GET /api/tp 拉取。字段名与原型的方案对象一字不差，所以下游
   buildSchemeMenu / fillSchemeView / pickScheme 全部零改动。
   返回 Promise —— 启动段据此等方案到位，再跑「重新测试」与 render()。 */
function pdNormScheme(s){
  return {
    id:        s.id,
    name:      String(s.name      || ""),
    server:    String(s.server    || ""),
    base:      String(s.base      || ""),
    model:     String(s.model     || ""),
    modelType: String(s.modelType || ""),
    sys:       String(s.sys       || ""),
    user:      (s.user && typeof s.user.length === "number") ? s.user : [],
    multi:     !!s.multi,
    apiKey:    String(s.apiKey || "")
  };
}

function loadSchemes(){
  var keep = currentScheme ? String(currentScheme.name) : "";
  return fetch("/api/tp").then(function(r){ return r.json(); }).then(function(list){
    list = (list && typeof list.length === "number") ? list : [];
    var out = [];
    for (var i = 0; i < list.length; i++){
      var s = pdNormScheme(list[i]);
      if (!s.name) continue;
      if (!s.id) s.id = i + 1;
      out.push(s);
    }
    schemes = out;
    currentScheme = null;
    for (var j = 0; j < schemes.length; j++){
      if (String(schemes[j].name) === keep){ currentScheme = schemes[j]; break; }
    }
    buildSchemeMenu();
    fillSchemeView();
  }).catch(function(){
    toast("加载提示词方案失败，请确认服务已启动", "warn");
  });
}
'''

PT_BOOT_OLD = r'''/* ================= 启动 ================= */
loadSchemes();
'''

PT_BOOT_NEW = r'''/* ================= 启动：方案列表来自后端 /api/tp =================
   方案是异步拉回来的，所以原型的启动收尾（「重新测试」入口 + render）挪进 then 里。 */
loadSchemes().then(function(){
'''

PT_TAIL_OLD = r'''render();
if (!pdStoreOK){
  toast("当前环境无法持久化（localStorage 不可用），刷新后新增内容会丢失", "warn");
}
'''

PT_TAIL_NEW = r'''  render();
  if (!pdStoreOK){
    toast("当前环境无法持久化（localStorage 不可用），刷新后新增内容会丢失", "warn");
  }
});
'''

PT_RUN_OLD = r'''$btnDebug.addEventListener("click", function(){
  if (debugging) return;
  if (!currentScheme){ toast("请先在左侧选择提示词方案", "warn"); return; }
  if (!tests.length){ toast("请先导入或添加测试问题", "warn"); return; }

  debugging = true;
  debugged  = true;                 /* 跑过一次后统计栏才开始出「准确率」 */
  $prog.hidden = false;
  pdResetProgress();
  refreshView();

  var total = tests.length, i = 0, seed = pdHash(currentScheme.name);
  var hitRight = 0, hitWrong = 0;

  function step(){
    if (i >= total){
      debugging = false;
      pdSetProgress(total, total);
      pdSave(tests);
      refreshView();
      toast("调试完成：共 " + total + " 题，AI 判对 " + hitRight + " 题、判错 " + hitWrong
            + " 题，可在列表里人工改判", "ok");
      return;
    }
    var t   = tests[i];
    var fb  = pdMockAnswer(t.q, t.ans, seed + i);
    var ai  = pdAutoJudge(t.ans, fb);
    var old = t.ai;
    /* 人工改判过的行（判定与上一轮 AI 结论不同）保留人工结论，其余跟随本轮 AI 判定 */
    var manual = !!(old && t.judge && t.judge !== old);
    t.fb   = fb;
    t.ai   = ai;
    if (!manual) t.judge = ai;
    if (ai === "right") hitRight++;
    else if (ai === "wrong") hitWrong++;

    i++;
    pdSetProgress(i, total);
    pdSave(tests);
    pdPatchRow(t);
    setTimeout(step, 160);
  }
  step();
});
'''

PT_RUN_NEW = r'''/* 数据层接线（第二次迭代）：一道题 = 两次调用
     ① /api/chat —— 用选定方案的「模型 + API_key + 系统提示词」把题目当用户提示词发出去，
        返回的文本就是「测试反馈答案」列（= 模型答案列）要显示的内容；
     ② /api/judge —— 拿「标准答案 ↔ 模型答案」走固定判定模型（DeepSeek）做语义一致性判定，
        结论写进「判断」列。判定模型调不动时该题只显示模型答案、判定留空（未判定）。 */
function pdJudgeQuestion(t, out){
  return fetch("/api/judge", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ans: String(t.ans == null ? "" : t.ans), out: out })
  }).then(function(r){ return r.json(); }).then(function(res){
    return { fb: out, ai: (res && res.ok && res.ai) ? res.ai : "", failed: false };
  }).catch(function(){
    return { fb: out, ai: "", failed: false };
  });
}

function pdRunQuestion(t){
  return fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      base_url: currentScheme.base,
      api_key:  currentScheme.apiKey,
      model:    currentScheme.model,
      system:   currentScheme.sys,
      rounds:   [String(t.q == null ? "" : t.q)]
    })
  }).then(function(r){ return r.json(); }).then(function(res){
    if (!res || !res.ok){
      return { fb: "（调用失败）" + ((res && res.error) || "未知错误"), ai: "", failed: true };
    }
    var out = (res.answers && res.answers.length) ? res.answers[0] : (res.text || "");
    return pdJudgeQuestion(t, out);
  }).catch(function(err){
    return { fb: "（调用失败）" + err, ai: "", failed: true };
  });
}

$btnDebug.addEventListener("click", function(){
  if (debugging) return;
  if (!currentScheme){ toast("请先在左侧选择提示词方案", "warn"); return; }
  if (!tests.length){ toast("请先导入或添加测试问题", "warn"); return; }

  debugging = true;
  debugged  = true;                 /* 跑过一次后统计栏才开始出「准确率」 */
  $prog.hidden = false;
  pdResetProgress();
  refreshView();

  var total = tests.length, i = 0;
  var hitRight = 0, hitWrong = 0, hitFail = 0;

  function step(){
    if (i >= total){
      debugging = false;
      pdSetProgress(total, total);
      pdSave(tests);
      refreshView();
      toast("调试完成：共 " + total + " 题，AI 判对 " + hitRight + " 题、判错 " + hitWrong
            + " 题" + (hitFail ? "，" + hitFail + " 题调用失败" : "") + "，可在列表里人工改判", "ok");
      return;
    }
    var t = tests[i];
    pdRunQuestion(t).then(function(r){
      var old = t.ai;
      /* 人工改判过的行（判定与上一轮 AI 结论不同）保留人工结论，其余跟随本轮 AI 判定 */
      var manual = !!(old && t.judge && t.judge !== old);
      t.fb = r.fb;
      t.ai = r.ai;
      if (!manual) t.judge = r.ai;
      if (r.ai === "right") hitRight++;
      else if (r.ai === "wrong") hitWrong++;
      if (r.failed) hitFail++;

      i++;
      pdSetProgress(i, total);
      pdSave(tests);
      pdPatchRow(t);
      step();
    });
  }
  step();
});
'''

PT_SAVE_OLD = r'''  var ok = false;
  try{ window.localStorage.setItem(PD_RESULT_KEY, JSON.stringify(list)); ok = true; }catch (e){}
  if (!ok){ toast("保存失败：当前环境无法写入本地存储", "warn"); return; }

  toast((hit >= 0 ? "已更新" : "已保存")
        + "到「测试结果列表」：" + name + "（共 " + problems.length + " 题）", "ok");
}
'''

PT_SAVE_NEW = r'''  var ok = false;
  try{ window.localStorage.setItem(PD_RESULT_KEY, JSON.stringify(list)); ok = true; }catch (e){}
  if (!ok){ toast("保存失败：当前环境无法写入本地存储", "warn"); return; }

  toast((hit >= 0 ? "已更新" : "已保存")
        + "到「测试结果列表」：" + name + "（共 " + problems.length + " 题）", "ok");

  /* 数据层接线（第二次迭代）：同一次保存再落一份到数据库 ——
     tst_rec 一条汇总（针对哪个方案 / 用哪把 key / 题数 / 准确率 / 错题数 / 矫正数）
     + tst_rec_dtl 逐题明细（问题 / 标准答案 / 模型输出 / AI 判定 / 是否人工改判）。
     本地那份仍然写，「测试结果列表」页继续可用；落库结果由服务端按明细现算。
     失败只补一条提示，成功不额外打扰（页面原有的成功提示已经给过了）。 */
  fetch("/api/rec", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      planId:  currentScheme.id,
      details: problems.map(function(p){
        return { q: p.q, ans: p.ans, out: p.out, ai: p.ai, judge: p.judge };
      })
    })
  }).then(function(r){ return r.json(); }).then(function(res){
    if (!res || !res.ok){
      toast("保存到数据库失败：" + ((res && res.error) || "未知错误"), "warn");
    }
  }).catch(function(){
    toast("保存到数据库失败，请确认服务已启动", "warn");
  });
}
'''


# =====================================================================
# 数据库表结构提取 Table_Structure_Extraction.html（第二次迭代接线）
# ---------------------------------------------------------------------
# 页面本体（字段 / 风格 / 排版 / CSS / 控件外观 / 必填校验）一字未动，只换数据来源：
#   · SCHEMA / RELATIONS 两份演示数据下线，改由 /api/schema（应用库）/ POST /api/schema
#     （上传 .sqlite）/ POST /api/schema/connect（真连 MySQL·PG·SQL Server·Oracle·Db2）灌入；
#   · 「提取表结构 / 重新生成」前先向 /api/schema/desc 要「库里没有注释」的表 /
#     字段说明（服务端调 DeepSeek 生成），补完再按原型原有口径渲染；
#   · 结构**按库**挂（db.map / db.rels），一次连多个库时同名表不会串；
#   · 服务端不做二次校验：连不上就把驱动的真实报错原样提示，不伪造表树。
# =====================================================================
TS_SCHEMA_OLD = re.compile(
    r"/\* 原型演示用的表结构：字段名 / 逻辑类型 / 字段描述 \*/\n"
    r"var SCHEMA = \[.*?\n\];", re.S)

TS_SCHEMA_NEW = '''/* 二次迭代：原型的演示表结构已下线，改由后端 /api/schema 灌入真实库结构。
   字段说明分两路（见 core/schema.py）：库里写了注释的直接用，没有的调 DeepSeek 补。 */
var SCHEMA = [];'''

TS_REL_OLD = re.compile(
    r"/\* 表间关系：\[从表, 从字段, 主表, 主字段, 关系说明\] \*/\n"
    r"var RELATIONS = \[.*?\n\];", re.S)

TS_REL_NEW = '''/* 表间关系：[从表, 从字段, 主表, 主字段, 关系说明]
   二次迭代：改由后端提供（取自真实外键），原型演示数据已下线。
   第二批：外键按「每个已连接的库」分别保存（见 pdFillDb / collectRels），
   这里的全局 RELATIONS 只作兜底。 */
var RELATIONS = [];

/* ============ 真实库结构（二次迭代 · 第二批新增的数据层） ============
   后端返回的统一形状（/api/schema、POST /api/schema、/api/schema/connect 都一样）：
     { ok, name, kind,
       tables:    [{ name, rows, desc, descFrom, cols:[[字段名, 类型关键字, 字段说明, 真实类型]] }],
       relations: [[从表, 从字段, 主表, 主字段, 关系说明]] }
   · cols[j][1] === "pk" 表示主键 —— 原型据此标「主键」，并用 TYPE_MAP 把它映射成
     该方言下的真实类型（MySQL→BIGINT / Oracle→NUMBER …）；
   · 其余列给库里声明的真实类型原文，原型 colType() 查不到就原样返回
     （所以 VARCHAR(255) / DECIMAL(10,2) 这类会透传，不会被改写）。
   结构**按库**挂在 db.map / db.rels 上（支持一次连多个库，两个库有同名表也不会串），
   于是原型的 schemaOf / collectRels / buildSchemaHtml / buildText 照常工作。 */
function pdFillDb(db, sch){
  db.schema = sch || { tables: [], relations: [] };
  db.map = {};
  db.rels = db.schema.relations || [];
  var i;
  for (i = 0; i < (db.schema.tables || []).length; i++){
    db.map[db.schema.tables[i].name] = db.schema.tables[i];
  }
  db.tables = mockTables(db.kind, db.name, db.schema.tables || []);
}

/* 「提取表结构」前，把本次勾选到的表里「库里没有注释」的部分交给服务端补说明
   （服务端调 DeepSeek）。哪张表 / 哪个字段进 payload，由 needDesc / need 决定，
   所以库里本来就有注释的一律不重发、不覆盖。 */
function pdFetchDesc(sel){
  var payload = [], names = [], i, j, k;
  for (i = 0; i < sel.length; i++){
    var db = sel[i].db, sch = schemaOf(sel[i].t.name, db);
    var need = [], cols = [], missing = false;
    for (j = 0; j < sch.cols.length; j++){
      cols.push([sch.cols[j][0], colType(db.kind, sch.cols[j][1])]);
      if (!String(sch.cols[j][2] || "").length){ need.push(sch.cols[j][0]); missing = true; }
    }
    var noTableDesc = !String(sch.desc || "").length;
    if (noTableDesc) missing = true;
    if (missing){
      payload.push({ name: sch.name, needDesc: noTableDesc, need: need, cols: cols });
    }
    for (k = 0; k < names.length; k++){ if (names[k] === db.name) break; }
    if (k === names.length) names.push(db.name);
  }
  if (!payload.length) return Promise.resolve({ ok: true, tables: [], skipped: true });
  return fetch("/api/schema/desc", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ db: names.join(" + "), tables: payload })
  }).then(function(r){ return r.json(); }).then(function(d){
    if (!d || !d.ok) return null;
    pdMergeDesc(d.tables || [], sel);
    return d;
  }, function(){ return null; });
}

/* 把服务端生成的说明并回**对应那个库**的结构 —— **只填空的**，库里已有的注释一律不覆盖。 */
function pdMergeDesc(list, sel){
  var by = {}, i, j;
  for (i = 0; i < list.length; i++){ by[list[i].name] = list[i]; }
  for (i = 0; i < sel.length; i++){
    var db = sel[i].db, nm = sel[i].t.name;
    var tgt = (db.map && db.map[nm]) || null, src = by[nm];
    if (!tgt || !src) continue;
    if (!String(tgt.desc || "").length && src.desc) tgt.desc = src.desc;
    var cm = {};
    for (j = 0; j < (src.cols || []).length; j++){ cm[src.cols[j].name] = src.cols[j].desc; }
    for (j = 0; j < tgt.cols.length; j++){
      var one = cm[tgt.cols[j][0]];
      if (one && !String(tgt.cols[j][2] || "").length) tgt.cols[j][2] = one;
    }
  }
}

/* 真连一个数据库（二次迭代 · 第二批）：把连接弹窗里的信息交给后端，
   由后端用真实驱动连上去读结构（MySQL / PostgreSQL / SQL Server / Oracle / Db2）。
   服务端**不做二次校验** —— 连不上就把驱动的真实报错原样提示出来，不伪造表树。 */
function pdConnectDb(kind, cfg, name){
  toast("正在连接 " + kind + " 数据库…", "");
  return fetch("/api/schema/connect", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(cfg)
  }).then(function(r){ return r.json(); }).then(function(d){
    if (!d || !d.ok){ toast((d && d.error) || ("连接 " + kind + " 失败"), "warn"); return null; }
    var db = addDb(name, kind, true, d);
    toast("已连接数据库：" + name + "（" + ((d.tables || []).length) + " 张表）", "ok");
    return db;
  }, function(){
    toast("连接失败，请确认服务已启动", "warn");
    return null;
  });
}'''

TS_MOCK_OLD = '''function mockTables(kind, name){
  var s = kind + "|" + name, seed = 0, i;
  for (i = 0; i < s.length; i++){ seed = (seed * 31 + s.charCodeAt(i)) % 99991; }
  var out = [];
  for (i = 0; i < SCHEMA.length; i++){
    out.push({ name: SCHEMA[i].name, rows: ((seed * (i + 7) + i * 613) % 9800) + 96, checked: false });
  }
  return out;
}'''

TS_MOCK_NEW = '''/* 二次迭代（第二批）：表清单来自后端灌进来的「本库结构」，行数用库里真实行数 */
function mockTables(kind, name, sch){
  var out = [], i;
  for (i = 0; i < (sch || []).length; i++){
    out.push({ name: sch[i].name, rows: sch[i].rows || 0, checked: false });
  }
  return out;
}'''

# ⚠️ addDb 必须**原地替换**（不能像别处那样在别处新增一个同名函数）：
#    原型里 addDb 在 mockTables 之后，函数声明是「后写的赢」，
#    新增的那个会被原版直接盖掉 —— 实测表现为左栏 0 张表（踩过一次）。
TS_ADDB_OLD = '''function addDb(name, kind, silent){
  dbs.push({ id: nextDbId++, name: name, kind: kind, collapsed: false, tables: mockTables(kind, name) });
  renderTree();
  if (!silent) toast("已连接数据库：" + name, "ok");
}'''

TS_ADDB_NEW = '''/* 二次迭代（第二批）：按库保存结构（db.map / db.rels）——
   支持一次连多个库；两个库有同名表也不会互相串数据。 */
function addDb(name, kind, silent, sch){
  var db = { id: nextDbId++, name: name, kind: kind, collapsed: false,
             tables: [], map: {}, rels: [] };
  dbs.push(db);
  pdFillDb(db, sch);
  renderTree();
  if (!silent) toast("已连接数据库：" + name, "ok");
  return db;
}'''

TS_BOOT_OLD = '''/* ================= 启动 ================= */
initCombo("comboDbType", function(v){ currentType = v; openConnectFlow(v); });

/* 演示用种子：默认已连接一个 SQLite 数据库，页面开箱即可体验「勾选 → 提取」
   （不需要的话删掉这一行即可，页面回到「先连接数据库」的初始态） */
addDb("demo_shop.sqlite", "SQLite", true);

resetSchema();'''

TS_BOOT_NEW = '''/* ================= 启动 ================= */
initCombo("comboDbType", function(v){ currentType = v; openConnectFlow(v); });

/* 二次迭代（第二批）：打开页面先连上后端自己的应用库（SQLite，真实结构），
   其余 5 种类型由用户从「连接数据库」弹窗**真连**（见 pdConnectDb）。
   「演示种子」换成「真实结构」，字段 / 排版 / CSS 一字未动。
   说明文字不在这一步生成 —— 点「提取表结构」时才调 DeepSeek 补，且只补勾选到的表。 */
function pdBoot(){
  fetch("/api/schema").then(function(r){ return r.json(); }).then(function(d){
    if (!d || !d.ok){ toast((d && d.error) || "读取库结构失败", "warn"); resetSchema(); return; }
    addDb(d.name, d.kind, true, d);
    resetSchema();
  }, function(){
    toast("读取库结构失败，请确认服务已启动", "warn");
    resetSchema();
  });
}
pdBoot();'''

TS_EXTRACT_OLD = '''$btnExtract.addEventListener("click", function(){
  var sel = selection();
  if (!sel.length){ toast("请先勾选要提取的表", "warn"); return; }
  renderSchema();
  toast("已提取 " + sel.length + " 张表的表结构", "ok");
});

/* 重新生成：按当前勾选重跑一次（勾选没变时「提取表结构」按钮是灰的，这里可以强制重出） */
$btnRegen.addEventListener("click", function(){
  var sel = selection();
  if (!sel.length){ toast("请先勾选要提取的表", "warn"); return; }
  renderSchema();
  toast("已重新生成 " + sel.length + " 张表的表结构", "ok");
});'''

TS_EXTRACT_NEW = '''/* 二次迭代：提取前先让服务端把「库里没有注释」的表 / 字段说明补上（调 DeepSeek），
   补完再按原型原有口径渲染 —— 字段 / 排版 / CSS 一字未动。
   模型不可用时只是说明为空，提取本身照常出结果。 */
function pdRunExtract(word){
  var sel = selection();
  if (!sel.length){ toast("请先勾选要提取的表", "warn"); return; }
  $btnExtract.disabled = true;
  $btnRegen.disabled   = true;
  toast("正在生成表结构说明…", "");
  pdFetchDesc(sel).then(function(d){
    renderSchema();
    syncSchBtns();
    toast(d ? (word + " " + sel.length + " 张表的表结构")
            : (word + " " + sel.length + " 张表（说明未生成，模型不可用）"),
          d ? "ok" : "warn");
  });
}

$btnExtract.addEventListener("click", function(){ pdRunExtract("已提取"); });

/* 重新生成：按当前勾选重跑一次（勾选没变时「提取表结构」按钮是灰的，这里可以强制重出） */
$btnRegen.addEventListener("click", function(){ pdRunExtract("已重新生成"); });'''

TS_FILE_OLD = '''$fileConfirm.addEventListener("click", function(){
  if (!pickedFile){ toast("请先选择 SQLite 数据库文件", "warn"); return; }
  hideMask($fileMask);
  addDb(pickedFile, "SQLite");
});'''

TS_FILE_NEW = '''$fileConfirm.addEventListener("click", function(){
  if (!pickedFile){ toast("请先选择 SQLite 数据库文件", "warn"); return; }
  var f = $fileInput.files && $fileInput.files[0];
  if (!f){ toast("请先选择 SQLite 数据库文件", "warn"); return; }
  /* 二次迭代：浏览器拿不到本机路径，改为把选中的 .sqlite 交给服务端临时打开、
     读出真实表结构（含外键）后回传；说明仍在「提取表结构」时由 DeepSeek 补。 */
  var fd = new FormData();
  fd.append("file", f);
  hideMask($fileMask);
  toast("正在读取数据库结构…", "");
  fetch("/api/schema", { method: "POST", body: fd })
    .then(function(r){ return r.json(); })
    .then(function(d){
      if (!d || !d.ok){ toast((d && d.error) || "读取数据库结构失败", "warn"); return; }
      addDb(d.name || f.name, d.kind || "SQLite", false, d);
    }, function(){ toast("读取数据库结构失败，请确认服务已启动", "warn"); });
});'''

TS_CONN_OLD = '''  var name = deriveName(kind, $inpAddr.value, $inpDbName.value);
  hideMask($connMask);
  addDb(name, kind);
});'''

TS_CONN_NEW = '''  var name = deriveName(kind, $inpAddr.value, $inpDbName.value);
  hideMask($connMask);
  /* 二次迭代（第二批）：**真接驱动** —— 把弹窗里的连接信息交给后端，由后端用真实驱动
     （MySQL = pymysql / PostgreSQL = psycopg2 / SQL Server = pymssql /
       Oracle = oracledb / Db2 for LUW = ibm_db）连上去读真实表结构。
     服务端**不做二次校验**：不查必填、不查端口格式，连不上就把驱动的真实报错原样提示。
     URL 模式下端口 / 用户名 / 密码 / 库名都在连接串里，所以把 url 一起送过去。 */
  pdConnectDb(kind, {
    kind: kind, mode: connMode,
    host: $inpAddr.value, port: $inpPort.value,
    user: $inpUser.value, pwd: $inpPwd.value, dbname: $inpDbName.value,
    url: $inpAddr.value
  }, name);
});'''


TS_SCHEMAOF_OLD = '''function schemaOf(name){
  for (var i = 0; i < SCHEMA.length; i++){ if (SCHEMA[i].name === name) return SCHEMA[i]; }
  return { name:name, desc:"", cols:[] };
}'''

TS_SCHEMAOF_NEW = '''/* 二次迭代（第二批）：结构按库取（db.map）—— 一次连多个库时，同名表不会串；
   没给 db（或该库里没有这张表）时回落到全局 SCHEMA。 */
function schemaOf(name, db){
  if (db && db.map && db.map[name]) return db.map[name];
  for (var i = 0; i < SCHEMA.length; i++){ if (SCHEMA[i].name === name) return SCHEMA[i]; }
  return { name:name, desc:"", cols:[] };
}'''

TS_RELS_OLD = '''    for (k = 0; k < RELATIONS.length; k++){
      var r = RELATIONS[k];
      if (g.names[r[0]] && g.names[r[2]]) out.push({ db: g.db, r: r });
    }'''

TS_RELS_NEW = '''    var rels = (g.db && g.db.rels) || RELATIONS;   /* 二次迭代（第二批）：外键按库取 */
    for (k = 0; k < rels.length; k++){
      var r = rels[k];
      if (g.names[r[0]] && g.names[r[2]]) out.push({ db: g.db, r: r });
    }'''

# buildSchemaHtml / buildText 里同一行取结构的地方（两处，want=2）
TS_SITEOF_OLD = "    var db = sel[i].db, tbl = sel[i].t, sch = schemaOf(tbl.name);"
TS_SITEOF_NEW = "    var db = sel[i].db, tbl = sel[i].t, sch = schemaOf(tbl.name, db);"


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
        ("str", A_COMBO_OLD, A_COMBO_NEW, "add-2 平台下拉选项改由服务器列表提供"),
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
        ("str", PB_SAVE_OLD, PB_SAVE_NEW, "prompt-8 保存落 tp 表 + 跳提示词方案列表页"),
    ],
    "prompt_scheme.html": [
        ("str", SCH_SCHEMES_OLD, SCH_SCHEMES_NEW, "scheme-1 方案列表改由后端提供"),
        ("str", SCH_BOOT_OLD, SCH_BOOT_NEW, "scheme-2 启动从 /api/tp 拉列表"),
        ("str", SCH_DEL_OLD, SCH_DEL_NEW, "scheme-3 删除同步到 tp 表"),
    ],
    # 提示词测试：第二次迭代起接线（HTML / CSS 一字未动，只换数据层）
    "prompt_test.html": [
        ("str", PT_SCHEMES_OLD, PT_SCHEMES_NEW, "ptest-1 方案下拉改由 /api/tp 提供"),
        ("str", PT_BOOT_OLD, PT_BOOT_NEW, "ptest-2 启动等方案到位"),
        ("str", PT_TAIL_OLD, PT_TAIL_NEW, "ptest-3 启动收尾收进 then"),
        ("str", PT_RUN_OLD, PT_RUN_NEW, "ptest-4 开始调试改调真实模型 + AI 判定"),
        ("str", PT_SAVE_OLD, PT_SAVE_NEW, "ptest-5 保存结果落 tst_rec / tst_rec_dtl"),
    ],
    # 数据库表结构提取：第二次迭代起接线（HTML / CSS 一字未动，只换数据层）
    "Table_Structure_Extraction.html": [
        ("re", TS_SCHEMA_OLD, TS_SCHEMA_NEW, "ts-1 演示表结构下线，改由 /api/schema 灌入"),
        ("re", TS_REL_OLD, TS_REL_NEW, "ts-2 演示关系下线 + 真实结构数据层（按库）"),
        ("str", TS_MOCK_OLD, TS_MOCK_NEW, "ts-3 表清单按本库结构生成"),
        ("str", TS_ADDB_OLD, TS_ADDB_NEW, "ts-3b addDb 原地改：按库保存结构"),
        ("str", TS_BOOT_OLD, TS_BOOT_NEW, "ts-4 启动改连真实库"),
        ("str", TS_EXTRACT_OLD, TS_EXTRACT_NEW, "ts-5 提取/重新生成先补说明再渲染"),
        ("str", TS_FILE_OLD, TS_FILE_NEW, "ts-6 选文件改成真上传读结构"),
        ("str", TS_CONN_OLD, TS_CONN_NEW, "ts-7 主机 / URL 连接改真接驱动"),
        ("str", TS_SCHEMAOF_OLD, TS_SCHEMAOF_NEW, "ts-8 schemaOf 按库取结构"),
        ("str", TS_RELS_OLD, TS_RELS_NEW, "ts-9 collectRels 外键按库取"),
        ("str", TS_SITEOF_OLD, TS_SITEOF_NEW, "ts-10 两处取结构处传上 db", 2),
    ],
}

# 「原样托管」页：本轮不接后端，原样复制到 static/（侧栏跳转能正常打开）
VERBATIM = [
    "prompt_test_results_list.html",
]


def build(name, edits):
    """按 edits 顺序给一页原型接线。

    edit 是 4 元组 `(kind, old, new, label)`，或 5 元组多给一个 `want`（期望命中次数，
    默认 1）。同一段字面在原型里出现多次、且**都要改**时用 5 元组（例如
    `buildSchemaHtml` 与 `buildText` 里取结构的那一行完全一样）。
    """
    with open(os.path.join(SRC_DIR, name), "r", encoding="utf-8") as fp:
        text = fp.read()
    for edit in edits:
        kind, old, new, label = edit[0], edit[1], edit[2], edit[3]
        want = edit[4] if len(edit) > 4 else 1
        if kind == "re":
            text = apply_re(text, old, new, label, want=want)
        else:
            text = apply(text, old, new, label, want=want)
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
