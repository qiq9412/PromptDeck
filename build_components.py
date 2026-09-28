# -*- coding: utf-8 -*-
"""从工作区根目录的原型页生成 Streamlit 静态组件前端。

产出：
    comp/list/index.html   ← new/list_server.html 原型 + 数据层接线 + 组件桥
    comp/new/index.html    ← new_server.html  原型 + 数据层接线 + 组件桥

硬约束（用户要求，逐条落实）：
  1. 字段、风格、排版零改动  -> <style> 段与 <body> 结构逐字节保留；
  2. 控件只补交互、不改外观  -> 只改「点了按钮之后干什么」，不碰 DOM 结构与 class；
  3. 必填校验原样保留        -> checkRequired / checkUrl 一行不动，且不做服务器端校验。

相对原型仅 6 处差异（全部在 <script> 内，界面零变化）：
  [list-1] mock 数据数组清空，改由 Streamlit 组件注入
  [list-2] rowHtml 外链口径：平台名 -> 纯文字；公司名 -> home_url（本次确认的口径）
  [list-3] 批量删除确认后，除本地移除，再向后端广播 delete 事件
  [list-4] 新增 onPDArgs 回调，接收组件注入的行数据
  [new-1]  保存校验通过后，除 toast，再向后端广播 save 事件（替代本地跳转）
  [new-2]  取消按钮由 location.href 改为广播 nav 事件
另外每页在 </body> 前追加一段「组件桥」脚本（不属于原型，用于 iframe <-> Streamlit 通信）。
"""
import os
import re
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.dirname(BASE_DIR)           # 原型所在目录（工作区根）
COMP_DIR = os.path.join(BASE_DIR, "comp")

errors = []


def apply(text, old, new, label, want=1):
    n = text.count(old)
    if n != want:
        errors.append("[%s] 期望命中 %d 次，实际 %d 次" % (label, want, n))
        return text
    return text.replace(old, new)


# =====================================================================
# 组件桥（追加在 </body> 前，两个页面通用）
# =====================================================================
BRIDGE = '''
<script>
/* ===== PromptDeck · Streamlit 组件桥（本次新增，界面元素零改动） ===== */
window.PD_FRAME_HEIGHT = 880;
(function(){
  var CURRENT = "";

  function send(type, payload){
    var msg = { isStreamlitMessage: true, type: type };
    if (payload){ for (var k in payload){ msg[k] = payload[k]; } }
    window.parent.postMessage(msg, "*");
  }

  function frameHeight(){
    var h = window.PD_FRAME_HEIGHT || 880;
    try {
      var ph = window.parent.innerHeight;      /* 同源时随浏览器高度自适应 */
      if (ph && ph > 400 && ph < 3000) h = ph - 16;
    } catch (e) { /* 跨域则用兜底高度 */ }
    return h;
  }

  window.PD = {
    value: function(v){
      if (v && typeof v === "object" && !v.nonce){
        v.nonce = Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
      }
      send("streamlit:setComponentValue", { value: v, dataType: "json" });
    },
    nav: function(to){
      if (to === CURRENT) return;                                  /* 已在本页，忽略 */
      if (to === "list_server" || to === "new_server"){
        window.PD.value({ event: "nav", to: to });
        return;
      }
      if (typeof window.toast === "function"){
        window.toast("原型演示：跳转至「" + to + "」页");            /* 未接入的页面沿用原型提示 */
      }
    }
  };

  window.addEventListener("message", function(e){
    var d = e.data;
    /* 判别口径必须只按 type：父窗口 -> 组件的 render 报文【不带】 isStreamlitMessage
       （streamlit ComponentInstance 实际发出：postMessage({type:"streamlit:render",args,dfs,disabled,theme})）。
       isStreamlitMessage 只是「组件 -> 父窗口」方向才必需的字段，父窗口的 onMessageEvent 会校验它。 */
    if (!d || d.type !== "streamlit:render") return;
    var args = d.args || {};
    CURRENT = args.page || "";
    if (typeof window.onPDArgs === "function"){ window.onPDArgs(args); }
  });

  /* 站内 .html 跳转交给 Streamlit 换页；外部首页链接不受影响 */
  document.addEventListener("click", function(e){
    var a = e.target && e.target.closest ? e.target.closest("a[href]") : null;
    if (!a) return;
    var href = a.getAttribute("href") || "";
    if (!/\\.html(\\?|#|$)/i.test(href)) return;
    e.preventDefault();
    window.PD.nav(href.replace(/\\.html.*$/i, ""));
  }, true);

  send("streamlit:componentReady", { apiVersion: 1 });
  send("streamlit:setFrameHeight", { height: frameHeight() });
  window.addEventListener("resize", function(){
    send("streamlit:setFrameHeight", { height: frameHeight() });
  });
})();
</script>
'''


# =====================================================================
# list_server.html 的 4 处接线
# =====================================================================
# [list-1]
LIST_MOCK = (re.compile(r"var servers = \[.*?\n\];", re.S), "var servers = [];")

# [list-2] 外链口径：平台名纯文字，公司名 -> home_url
LIST_ROW_OLD = '''function rowHtml(s){
  var checked = selected[s.id] ? " checked" : "";
  var rowCls  = selected[s.id] ? " class=\\"is-checked\\"" : "";
  return ''
    + '<tr data-id="' + s.id + '"' + rowCls + '>'
    +   '<td class="col-check"><input type="checkbox" class="row-check"' + checked + ' data-id="' + s.id + '" /></td>'
    +   '<td class="col-platform">'
    +     '<a class="link" href="' + esc(s.pUrl) + '" target="_blank" rel="noopener noreferrer" title="访问 ' + esc(s.pUrl) + '">'
    +       esc(s.platform) + EXT_ICON
    +     '</a>'
    +   '</td>'
    +   '<td class="col-company">'
    +     '<a class="link link-company" href="' + esc(s.cUrl) + '" target="_blank" rel="noopener noreferrer" title="访问 ' + esc(s.cUrl) + '">'
    +       esc(s.company) + EXT_ICON
    +     '</a>'
    +   '</td>'
    +   '<td class="col-api"><span class="api">' + esc(s.api) + '</span></td>'
    + '</tr>';
}'''

LIST_ROW_NEW = '''function rowHtml(s){
  var checked = selected[s.id] ? " checked" : "";
  var rowCls  = selected[s.id] ? " class=\\"is-checked\\"" : "";
  /* 外链口径：仅「公司名」指向公司首页网址（home_url）；「平台 / 官方名」为纯文字 */
  var compAttr = s.cUrl
    ? ' href="' + esc(s.cUrl) + '" target="_blank" rel="noopener noreferrer" title="访问 ' + esc(s.cUrl) + '"'
    : '';
  return ''
    + '<tr data-id="' + s.id + '"' + rowCls + '>'
    +   '<td class="col-check"><input type="checkbox" class="row-check"' + checked + ' data-id="' + s.id + '" /></td>'
    +   '<td class="col-platform">' + esc(s.platform) + '</td>'
    +   '<td class="col-company">'
    +     '<a class="link link-company"' + compAttr + '>'
    +       esc(s.company) + (s.cUrl ? EXT_ICON : '')
    +     '</a>'
    +   '</td>'
    +   '<td class="col-api"><span class="api">' + esc(s.api) + '</span></td>'
    + '</tr>';
}'''

# [list-3] 批量删除：本地移除后广播 delete 事件
LIST_DEL_OLD = '''document.getElementById("delConfirm").addEventListener("click", function(){
  var n = selectedIds().length;
  servers = servers.filter(function(s){ return !selected[s.id]; });
  selected = {};'''

LIST_DEL_NEW = '''document.getElementById("delConfirm").addEventListener("click", function(){
  var ids = selectedIds();
  var n   = ids.length;
  servers = servers.filter(function(s){ return !selected[s.id]; });
  selected = {};'''

LIST_DEL_TAIL_OLD = '''  render();
  toast("已删除 " + n + " 台服务器", "ok");
});'''

LIST_DEL_TAIL_NEW = '''  render();
  toast("已删除 " + n + " 台服务器", "ok");
  PD.value({ event: "delete", ids: ids });   /* 数据层接线：落库 */
});'''

# [list-4] 注入数据 + 启动
LIST_BOOT_OLD = '''/* ================= 启动 ================= */
render();'''

LIST_BOOT_NEW = '''/* ================= 数据源：由 Streamlit 组件注入 ================= */
window.onPDArgs = function(args){
  servers = (args && args.rows) || [];
  view = servers.slice();
  selected = {};
  render();
};

/* ================= 启动 ================= */
render();'''


# =====================================================================
# new_server.html 的 2 处接线
# =====================================================================
# [new-1] 保存：校验通过后广播 save 事件
NEW_SAVE_OLD = '''  toast("服务器已保存，正在返回列表…", "ok");
  setTimeout(function(){ window.location.href = "list_server.html"; }, 700);'''

NEW_SAVE_NEW = '''  toast("服务器已保存，正在返回列表…", "ok");
  /* 数据层接线：交给后端落库后再回列表 */
  setTimeout(function(){
    PD.value({
      event: "save",
      data: {
        plat_name: $platform.value.trim(),
        comp_name: document.getElementById("company").value.trim(),
        base_url:  $baseUrl.value.trim(),
        home_url:  $homeUrl.value.trim()
      }
    });
  }, 500);'''

# [new-2] 取消：广播 nav 事件
NEW_CANCEL_OLD = '''document.getElementById("btnCancel").addEventListener("click", function(){
  window.location.href = "list_server.html";
});'''

NEW_CANCEL_NEW = '''document.getElementById("btnCancel").addEventListener("click", function(){
  PD.nav("list_server");
});'''


def build(name, edits):
    src = os.path.join(SRC_DIR, name)
    with open(src, "r", encoding="utf-8") as fp:
        text = fp.read()
    for old, new, label in edits:
        if isinstance(old, re.Pattern):
            text, n = old.subn(new, text)
            if n != 1:
                errors.append("[%s] 正则替换命中 %d 次（期望 1）" % (label, n))
        else:
            text = apply(text, old, new, label)
    if "</body>" not in text:
        errors.append("[%s] 找不到 </body>" % name)
        return text
    return text.replace("</body>", BRIDGE + "</body>", 1)


def main():
    targets = {
        "list": ("list_server.html", [
            (LIST_MOCK[0], LIST_MOCK[1], "list-1 mock数组"),
            (LIST_ROW_OLD, LIST_ROW_NEW, "list-2 外链口径"),
            (LIST_DEL_OLD, LIST_DEL_NEW, "list-3a 删除取ids"),
            (LIST_DEL_TAIL_OLD, LIST_DEL_TAIL_NEW, "list-3b 删除广播"),
            (LIST_BOOT_OLD, LIST_BOOT_NEW, "list-4 注入数据"),
        ]),
        "new": ("new_server.html", [
            (NEW_SAVE_OLD, NEW_SAVE_NEW, "new-1 保存广播"),
            (NEW_CANCEL_OLD, NEW_CANCEL_NEW, "new-2 取消广播"),
        ]),
    }

    for key, (fname, edits) in targets.items():
        html = build(fname, edits)
        out_dir = os.path.join(COMP_DIR, key)
        os.makedirs(out_dir, exist_ok=True)
        out = os.path.join(out_dir, "index.html")
        with open(out, "w", encoding="utf-8", newline="") as fp:
            fp.write(html)
        print("生成 %s  (%d bytes)" % (out, os.path.getsize(out)))

    if errors:
        print("\n接线失败：")
        for e in errors:
            print("  - " + e)
        return 1
    print("\n全部接线成功。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
