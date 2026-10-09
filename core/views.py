# -*- coding: utf-8 -*-
"""PromptDeck · 视图层（Django 版）。

两类活儿：
  1. 托管原型页 —— build_static.py 从工作区根目录的原型生成的产物（static/），
     原样吐给浏览器，字段 / 风格 / 排版 / CSS 与原型逐字节一致；
  2. 提供 /api/* 数据接口 —— 页面内用 fetch 取数 / 落库。

接口清单与上一版（Starlette）完全对齐，另外新增本次迭代的主角 /api/tp：
    GET    /api/srv     列表（?platform= &company= 模糊过滤）
    POST   /api/srv     新增        PUT 修改        DELETE 批量删除 {"ids":[...]}
                        ⚠️ base_url 查重（用户点名）：命中已有网址回 409（前端也拦一道）
    GET    /api/mdl     列表        POST 新增       PUT 修改   DELETE 批量删除
    POST   /api/chat    调试（转发给真实模型）
    GET    /api/tp      提示词方案列表
    POST   /api/tp      保存一份方案（按 id 精确覆盖 → 同名覆盖 → 新增）
    DELETE /api/tp      批量删除 {"ids":[...]}

第二次迭代新增（「提示词测试」页接线）：
    POST   /api/judge   AI 判定：标准答案 ↔ 模型答案是否语义一致（固定走 DeepSeek）
    GET    /api/rec     测试记录列表（含逐题明细）
    POST   /api/rec     保存一次测试结果 → tst_rec + tst_rec_dtl
    DELETE /api/rec     批量删除 {"ids":[...]}

第二次迭代新增（「数据库表结构提取」页接线）：
    GET    /api/schema         应用库的真实表结构
    POST   /api/schema         上传的 .sqlite 文件的真实表结构（multipart: file）
    POST   /api/schema/connect 用真实驱动连 MySQL/PG/SQL Server/Oracle/Db2 并读结构
    POST   /api/schema/desc    只给「库里没有注释」的表 / 字段调 DeepSeek 补说明

第三次迭代新增（「链接数据库调试」）：
    POST   /api/dbeaver/link   唤起 DBeaver 打开表结构提取时记录的那个连接，
                               并把最新一次模型生成的 SQL 放进新的 SQL 编辑器标签

第四次迭代新增（「页面内调试」弹窗，与上面的 DBeaver 路线**并存**）：
    POST   /api/database/execute-sql
                               服务端真连库执行**只读** SQL，把结果集回给页面。
                               密码由弹窗临时提交，只活这一次请求 ——
                               不落库、不写文件、不进会话。

第四次迭代新增（已连接数据库存档）：
    GET    /api/db-conn        列出已保存的连接（含表结构快照）
    POST   /api/db-conn        保存 / 覆盖一个已连接的库（按「库名+类型」覆盖）
    DELETE /api/db-conn        按 {"ids":[…]} 或 {"name","kind"} 删除
                               ⚠️ 本组接口**只收不含密码的连接信息**：
                                  前端交接时就不登记密码，store 落库前再白名单式过滤一次。
"""
import json
import os
import tempfile
import time

from django.conf import settings
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.views.decorators.csrf import csrf_exempt

from . import chat, dbeaver, schema, sqlrun, store

# 允许托管的原型页（白名单，顺带挡住路径穿越）
PAGES = {
    "foundation1_models.html",
    "list_server.html",
    "new_server.html",
    "model_add.html",
    "prompt.html",
    "prompt_scheme.html",
    "prompt_test.html",
    "prompt_test_results_list.html",
    "Table_Structure_Extraction.html",
}

# 根路径落到哪一页（本次迭代的焦点是提示词构造 → 提示词方案）
HOME_PAGE = "prompt.html"


# ------------------------------------------------------------------ 请求体
def payload(request):
    """取请求体：优先 JSON，兼容表单提交。取不到就给空 dict（不报错）。"""
    ctype = (request.content_type or "").lower()
    if "application/json" in ctype:
        try:
            data = json.loads(request.body.decode("utf-8") or "{}")
            return data if isinstance(data, dict) else {}
        except (ValueError, UnicodeDecodeError):
            return {}
    try:
        return {k: request.POST.get(k) for k in request.POST.keys()}
    except Exception:
        return {}


def ids_of(data):
    """从请求体取待删除 id 列表，兼容 {"ids":[...]} 与 "ids=1,2" 两种形式。"""
    ids = data.get("ids")
    if ids is None:
        return []
    if isinstance(ids, str):
        items = [x for x in ids.split(",") if x.strip()]
    elif isinstance(ids, (list, tuple)):
        items = list(ids)
    else:
        return []
    out = []
    for i in items:
        try:
            out.append(int(i))
        except (TypeError, ValueError):
            continue
    return out


def to_str(v):
    return ("" if v is None else str(v)).strip()


# ------------------------------------------------------------------ 页面托管
def home(request):
    return redirect("/" + HOME_PAGE)


def page(request, name):
    """吐出 static/ 下的原型产物（白名单内的页面名）。"""
    if name not in PAGES:
        raise Http404("页面不存在：%s" % name)
    path = os.path.join(settings.STATIC_PAGES_DIR, name)
    if not os.path.isfile(path):
        raise Http404("页面尚未生成，请先运行 build_static.py：%s" % name)
    with open(path, "rb") as fp:
        body = fp.read()
    return HttpResponse(body, content_type="text/html; charset=utf-8")


def health(request):
    return HttpResponse("ok", content_type="text/plain; charset=utf-8")


# ------------------------------------------------------------------ 服务器表
@csrf_exempt
def srv_api(request):
    if request.method == "GET":
        rows = store.list_srv(
            request.GET.get("platform", ""),
            request.GET.get("company", ""),
        )
        return JsonResponse([{
            "id": r.id,
            "platform": r.plat_name or "",
            "company": r.comp_name or "",
            "api": r.base_url or "",
            "cUrl": r.home_url or "",
        } for r in rows], safe=False)

    if request.method == "POST":
        d = payload(request)
        dup = store.find_srv_by_url(to_str(d.get("base_url")))
        if dup is not None:
            return JsonResponse({
                "ok": False, "error": "已存在服务器API网址",
                "dupId": dup.id, "dupUrl": dup.base_url or "",
            }, status=409)
        new_id = store.create_srv(
            to_str(d.get("plat_name")),
            to_str(d.get("base_url")),
            to_str(d.get("comp_name")),
            to_str(d.get("home_url")),
        )
        return JsonResponse({"ok": True, "id": new_id}, status=201)

    if request.method in ("PUT", "PATCH"):
        d = payload(request)
        srv_id = d.get("id")
        if srv_id in (None, ""):
            return JsonResponse({"ok": False, "error": "缺少 id"}, status=400)
        dup = store.find_srv_by_url(to_str(d.get("base_url")), exclude_id=srv_id)
        if dup is not None:
            return JsonResponse({
                "ok": False, "error": "已存在服务器API网址",
                "dupId": dup.id, "dupUrl": dup.base_url or "",
            }, status=409)
        n = store.update_srv(
            int(srv_id),
            to_str(d.get("plat_name")),
            to_str(d.get("base_url")),
            to_str(d.get("comp_name")),
            to_str(d.get("home_url")),
        )
        return JsonResponse({"ok": True, "updated": n})

    if request.method == "DELETE":
        d = payload(request)
        return JsonResponse({"ok": True, "deleted": store.delete_srv(ids_of(d))})

    return JsonResponse({"ok": False, "error": "method not allowed"}, status=405)


# ------------------------------------------------------------------ 模型表
@csrf_exempt
def mdl_api(request):
    if request.method == "GET":
        rows = store.list_mdl()
        return JsonResponse([{
            "id": r["id"],
            "srvId": r["srv_id"],
            "platform": r["plat_name"] or "",
            "cUrl": r["home_url"] or "",
            "model": r["mdl_name"] or "",
            "type": r["mdl_type"] or "",
            "inPrice": store.price_text(r["in_price"]) or "",
            "outPrice": store.price_text(r["out_price"]) or "",
            "unit": r["unit"] or "",
            "url": r["url"] or "",
        } for r in rows], safe=False)

    if request.method == "POST":
        d = payload(request)
        new_id = store.create_mdl(
            to_str(d.get("plat_name")),
            to_str(d.get("mdl_name")),
            to_str(d.get("mdl_type")),
            to_str(d.get("in_price")),
            to_str(d.get("out_price")),
            to_str(d.get("unit")),
            to_str(d.get("url")),
        )
        return JsonResponse({"ok": True, "id": new_id}, status=201)

    if request.method in ("PUT", "PATCH"):
        d = payload(request)
        mdl_id = d.get("id")
        if mdl_id in (None, ""):
            return JsonResponse({"ok": False, "error": "缺少 id"}, status=400)
        n = store.update_mdl(
            int(mdl_id),
            to_str(d.get("plat_name")),
            to_str(d.get("mdl_name")),
            to_str(d.get("mdl_type")),
            to_str(d.get("in_price")),
            to_str(d.get("out_price")),
            to_str(d.get("unit")),
            to_str(d.get("url")),
        )
        return JsonResponse({"ok": True, "updated": n})

    if request.method == "DELETE":
        d = payload(request)
        return JsonResponse({"ok": True, "deleted": store.delete_mdl(ids_of(d))})

    return JsonResponse({"ok": False, "error": "method not allowed"}, status=405)


# ------------------------------------------------------------------ 调试
@csrf_exempt
def chat_api(request):
    """POST /api/chat —— 把调试请求转发给真实模型。

    请求体：{"base_url", "api_key", "model", "system", "rounds":[...]}
    响应体：{"ok":true,"text","answers":[...],"prompt_tokens","completion_tokens","model"}
            {"ok":false,"error","answers":[...已拿到的],"round":失败的轮次}
    """
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "method not allowed"}, status=405)

    d = payload(request)
    api_key = to_str(d.get("api_key"))
    base_url = to_str(d.get("base_url"))
    model = to_str(d.get("model"))
    system = "" if d.get("system") is None else str(d.get("system"))

    rounds = d.get("rounds")
    if isinstance(rounds, str):
        rounds = [rounds]
    elif not isinstance(rounds, (list, tuple)):
        rounds = []

    texts = []
    for one in rounds:
        t = ("" if one is None else str(one)).strip()
        if t:
            texts.append(t)
    if not texts:
        texts = [""]

    answers, ptok, ctok, error, fail_round = chat.chat_rounds(
        base_url, api_key, model, system, texts
    )

    if error:
        return JsonResponse({
            "ok": False,
            "error": error,
            "answers": answers,
            "round": fail_round,
        })

    return JsonResponse({
        "ok": True,
        "text": "\n\n".join(answers),
        "answers": answers,
        "model": model,
        "prompt_tokens": ptok,
        "completion_tokens": ctok,
    })


# ------------------------------------------------------------------ 提示词方案表（本次新增）
@csrf_exempt
def tp_api(request):
    """GET 列表 / POST 保存 / DELETE 批量删除。

    tp 表是「提示词方案」的唯一真源：
      · prompt.html 点「保存」→ POST 到这里 → 成功后再跳 prompt_scheme.html；
      · prompt_scheme.html 打开就 GET 列表，删除走 DELETE。
    """
    if request.method == "GET":
        return JsonResponse(store.list_tp(), safe=False)

    if request.method == "POST":
        d = payload(request)
        name = to_str(d.get("name"))
        # 原型里「方案命名」为空时前端已 toast 拦住；这里只兜住空名字的脏请求，
        # 不复制前端的必填校验，也不返回字段级提示。
        if not name:
            return JsonResponse({"ok": False, "error": "缺少方案命名"}, status=400)
        new_id, kind = store.save_tp(d)
        return JsonResponse({"ok": True, "id": new_id, "result": kind},
                            status=201 if kind == "created" else 200)

    if request.method == "DELETE":
        d = payload(request)
        return JsonResponse({"ok": True, "deleted": store.delete_tp(ids_of(d))})

    return JsonResponse({"ok": False, "error": "method not allowed"}, status=405)


# ------------------------------------------------------------------ AI 判定（第二次迭代新增）
@csrf_exempt
def judge_api(request):
    """POST /api/judge —— 判定「标准答案」与「模型答案」是否语义一致。

    请求体：{"ans": 标准答案, "out": 模型答案}
    响应体：{"ok":true,  "ai":"right|wrong|", "reason": 判定模型原文, "model": 判定模型名}
            {"ok":false, "ai":"", "error": ...}

    判定模型固定走 DeepSeek（core.chat.JUDGE_BASE_URL / JUDGE_MODEL），
    密钥从 tst_api_key 表里现读（plan_id 为 NULL、key_name='AI判定（DeepSeek）'）——
    按需求不写进源码。
    """
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "method not allowed"}, status=405)

    d = payload(request)
    std = "" if d.get("ans") is None else str(d.get("ans"))
    out = "" if d.get("out") is None else str(d.get("out"))

    # 任一侧为空 ⇒ 按原型口径「不给结论」（判定列留空、等人工判定），
    # 这一步不需要密钥，放在取密钥之前，免得没配密钥时连「未判定」都报错。
    if not std.strip() or not out.strip():
        return JsonResponse({"ok": True, "ai": "", "reason": "", "model": chat.JUDGE_MODEL})

    key = store.judge_key()
    if not key:
        return JsonResponse({
            "ok": False, "ai": "",
            "error": "未在 tst_api_key 表里配置 AI 判定密钥（先跑 setup_judge_key.py --key …）",
        })

    ai, raw, err = chat.judge_pair(key, std, out)
    if err:
        return JsonResponse({"ok": False, "ai": "", "error": err})

    return JsonResponse({
        "ok": True,
        "ai": ai,                       # "" = 有一边为空 / 判定模型没说清 ⇒ 未判定
        "reason": raw,
        "model": chat.JUDGE_MODEL,
    })


# ------------------------------------------------------------------ 测试记录（tst_rec / tst_rec_dtl）
@csrf_exempt
def rec_api(request):
    """GET 列表 / POST 保存一次测试结果 / DELETE 批量删除。

    对应原型「提示词测试」页的「保存结果」：
      · POST 落 tst_rec（一条汇总）+ tst_rec_dtl（逐题明细）；
      · 四个汇总数字由服务端按逐题明细现算，与页面 updateStat() 同口径。
    """
    if request.method == "GET":
        return JsonResponse(store.list_rec(), safe=False)

    if request.method == "POST":
        d = payload(request)
        plan_id = d.get("planId")
        if plan_id in (None, ""):
            return JsonResponse({"ok": False, "error": "缺少 planId"}, status=400)
        try:
            plan_id = int(plan_id)
        except (TypeError, ValueError):
            return JsonResponse({"ok": False, "error": "planId 不是数字"}, status=400)

        details = d.get("details")
        if not isinstance(details, (list, tuple)):
            details = []

        rec_id, summary = store.save_rec(plan_id, details)
        return JsonResponse({"ok": True, "id": rec_id, "summary": summary}, status=201)

    if request.method == "DELETE":
        d = payload(request)
        return JsonResponse({"ok": True, "deleted": store.delete_rec(ids_of(d))})

    return JsonResponse({"ok": False, "error": "method not allowed"}, status=405)


# ------------------------------------------------------------------ 数据库表结构（第二次迭代新增）
def _prune_uploads(up_dir, keep=20):
    """清理 data/uploads/ 里太老的副本，只留最近 `keep` 个。

    这些副本是**故意保留**的（表结构页上传的 .sqlite 要留给 DBeaver 打开），
    所以会一直堆积 —— 每上传一次多一个文件。不清理的话目录会无限变大。
    按修改时间倒序保留最近的；删不掉就跳过（不因为清理失败影响主流程）。
    """
    try:
        items = []
        for name in os.listdir(up_dir):
            p = os.path.join(up_dir, name)
            if os.path.isfile(p):
                items.append((os.path.getmtime(p), p))
        items.sort(reverse=True)
        for _, p in items[keep:]:
            try:
                os.remove(p)
            except OSError:
                pass
    except OSError:
        pass


@csrf_exempt
def schema_api(request):
    """GET  → 应用库的真实表结构（core/schema.default_db_path()）
    POST → 上传的 .sqlite 文件的真实表结构（multipart，字段名 file）

    两者回同一个形状：{ok, name, kind, path, tables, relations}。
    库里**本来就有注释**的表 / 字段把注释直接带回来（`descFrom: "db"`），
    没有注释的留空 —— 由页面在「提取表结构」时再走 /api/schema/desc 补。
    这里只读结构，不写任何库，也不做任何业务校验。

    `path` = 该库的**绝对路径**（上传的落盘副本）。这是「链接数据库调试」唤起
    DBeaver 打开 SQLite 时唯一需要的定位信息 —— 浏览器拿不到本机路径，只能由服务端回传。
    """
    if request.method == "GET":
        db_path = schema.default_db_path()
        res = schema.read_sqlite(db_path)
        if res.get("ok"):
            res["path"] = os.path.abspath(db_path)
        return JsonResponse(res, status=200 if res.get("ok") else 400)

    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "method not allowed"}, status=405)

    up = request.FILES.get("file")
    if up is None:
        return JsonResponse({"ok": False, "error": "没有收到文件（字段名应为 file）"},
                            status=400)

    # 浏览器只给文件内容、不给本机路径 ⇒ 落一份到 data/uploads/ 再读结构。
    # ⚠️ 刻意**保留**这份副本（原来读完即删）：「链接数据库调试」要在 DBeaver 里打开
    #    这个文件，删了就没有可指的路径了。名字加时间戳，避免同名互相覆盖。
    up_dir = os.path.abspath(os.path.join(str(getattr(settings, "BASE_DIR", ".")),
                                          "data", "uploads"))
    try:
        os.makedirs(up_dir, exist_ok=True)
        _prune_uploads(up_dir)
        base = os.path.basename(up.name or "") or "upload.sqlite"
        stamp = time.strftime("%Y%m%d_%H%M%S")
        target = os.path.join(up_dir, "%s_%s" % (stamp, base))
        with open(target, "wb") as fp:
            for chunk in up.chunks():
                fp.write(chunk)
    except Exception as exc:                       # 超大文件 / 落盘失败等
        return JsonResponse({"ok": False, "error": "保存上传文件失败：%s" % exc}, status=400)

    res = schema.read_sqlite(target, name=os.path.basename(up.name or "") or "未命名.sqlite")
    if res.get("ok"):
        res["path"] = os.path.abspath(target)
    return JsonResponse(res, status=200 if res.get("ok") else 400)


@csrf_exempt
def dbeaver_link_api(request):
    """POST /api/dbeaver/link —— 「链接数据库调试」：唤起 DBeaver 并送 SQL 进去。

    请求体（JSON）：{"meta": {kind, name, mode, host, port, dbname, user, file, tables},
                     "sql": "模型生成的 SQL 原文"}
    响应体：{"ok":true, exe, sqlFile, target, note}
             {"ok":false, "error": 缺什么 / 路径不对 / 启动失败}

    ⚠️ 请求体里**永远不该有密码**：表结构页交接时就不登记它，这里也不接收 ——
       认证交给 DBeaver 自己已保存的凭据，不经我们的手。
    """
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "method not allowed"}, status=405)

    d = payload(request)
    meta = d.get("meta")
    if not isinstance(meta, dict):
        meta = {}
    # tables 只用来写进脚本头部的注释，认「数组或逗号分隔串」两种写法。
    # ⚠️ 10-09 起前端交来的元素**可能是对象**（页面内调试弹窗要字段结构，
    #    于是 tables 升级成 [{name,rows,desc,cols}]）—— 这里只取 name，
    #    不能直接 to_str(x)，否则脚本注释里会写出一串 Python dict 原文。
    tables = meta.get("tables")
    if isinstance(tables, str):
        tables = [x.strip() for x in tables.split(",") if x.strip()]
    elif isinstance(tables, (list, tuple)):
        out = []
        for x in tables:
            if isinstance(x, dict):
                n = to_str(x.get("name"))
            else:
                n = to_str(x)
            if n:
                out.append(n)
        tables = out
    else:
        tables = []

    clean = {
        "kind":   to_str(meta.get("kind")),
        "name":   to_str(meta.get("name")),
        "mode":   to_str(meta.get("mode")) or "host",
        "host":   to_str(meta.get("host")),
        "port":   to_str(meta.get("port")),
        "dbname": to_str(meta.get("dbname")),
        "user":   to_str(meta.get("user")),
        "file":   to_str(meta.get("file")),
        "tables": tables,
    }

    res = dbeaver.launch(clean, to_str(d.get("sql")))
    return JsonResponse(res, status=200)


@csrf_exempt
def sql_execute_api(request):
    """POST /api/database/execute-sql —— 「页面内调试」：服务端代跑只读 SQL。

    请求体（JSON）：
        {"kind": "MySQL"|"PostgreSQL"|"SQL Server"|"Oracle"|"Db2 for LUW"|"SQLite",
         "meta": {…表结构页交接来的连接信息…},
         "pwd": "弹窗里临时输入的密码",
         "sql": "要执行的 SQL 原文"}

    响应体：{"ok":true,  columns, rows, elapsed, rowCount, truncated}
             {"ok":false, error:"…"}

    三条口径（与本文件其余接口的差异，都是刻意为之）：
      1. **密码只活这一次请求**：`pwd` 从请求体取出后直接进 `sqlrun`，函数返回即丢。
         **不写 tst_api_key、不写 session、不写任何文件** —— 与 `/api/dbeaver/link`
         「认证交给 DBeaver 已存凭据」是同一道安全地基，两条路线都不落密码。
      2. **只放行只读语句**：SQL 原文进 `sqlrun.check_readonly`，写/管理语句一律拒。
         这里**刻意做服务端二次校验**，与项目「不做二次校验」的通例不同：
         其余接口校验错了只是页面报错，这里漏一条就是数据损坏。
      3. **驱动真实报错原样透出**：不加工、不兜底成「执行失败」，让页面能显示
         `no such table: xxx` 这种用户看得懂、能自己改的信息。
    """
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "method not allowed"}, status=405)

    d = payload(request)
    meta = d.get("meta")
    if not isinstance(meta, dict):
        meta = {}

    kind = to_str(d.get("kind") or meta.get("kind"))
    sql = to_str(d.get("sql"))
    pwd = to_str(d.get("pwd"))          # 弹窗临时输入的密码

    # 连接信息与「表结构提取」页交接的形状保持一致；meta 里**从来没有密码**
    cfg = dict(meta)
    cfg["kind"] = kind
    cfg["pwd"] = pwd                    # 只在这里挂上，函数返回后随 cfg 一起消失
    cfg.pop("tables", None)             # 表清单对执行没用，去掉省得白传一遍

    res = sqlrun.run_sql(kind, cfg, sql)

    # 双保险：即便 run_sql 内部漏了清理，这里也不让密码残留在任何长期引用里
    cfg["pwd"] = ""
    d["pwd"] = ""

    return JsonResponse(res, status=200)


@csrf_exempt
def db_conn_api(request):
    """已连接数据库存档的三个动词（第四次迭代新增）。

    GET    → {"ok":true, "list":[…]}
    POST   → {"ok":true, "id":N, "action":"created"|"updated"}
    DELETE → {"ok":true, "deleted":N}

    口径：
      · 保存键 = (库名, 类型)，同一个库重复导入 / 重连只更新那一条；
      · **密码不进这张表**：请求体里就算带了 pwd 也会被 store 白名单式剔除；
      · 表没就位（线上老库）时 GET 回 `needSetup:true` 的空列表，不报 500。
    """
    if request.method == "GET":
        return JsonResponse(store.list_db_conn(), status=200)

    d = payload(request)

    if request.method == "POST":
        # meta 里才是连接信息本体；也接受把字段平铺在顶层（两种写法都认）
        meta = d.get("meta")
        if not isinstance(meta, dict):
            meta = d
        entry = {
            "name": to_str(meta.get("name")),
            "kind": to_str(meta.get("kind")),
            "mode": to_str(meta.get("mode")),
            "host": to_str(meta.get("host")),
            "port": to_str(meta.get("port")),
            "dbname": to_str(meta.get("dbname")),
            "user": to_str(meta.get("user")),
            "file": to_str(meta.get("file")),
            "schema": d.get("schema") if isinstance(d.get("schema"), dict) else meta.get("schema"),
            "checked": d.get("checked"),
        }
        new_id, action = store.save_db_conn(entry)
        if new_id is None:
            # needSetup / 缺库名类型 —— 都按「未成功」回，但不 500
            return JsonResponse({"ok": False, "error": action}, status=200)
        return JsonResponse({"ok": True, "id": new_id, "action": action}, status=200)

    if request.method == "DELETE":
        ids = d.get("ids")
        if isinstance(ids, (list, tuple)) and ids:
            n = store.delete_db_conn(ids)
            return JsonResponse({"ok": True, "deleted": n}, status=200)
        # 也支持按「库名 + 类型」删（页面「移除该库」时手上只有这两个）
        n = store.delete_db_conn_by_key(d.get("name"), d.get("kind"))
        if not to_str(d.get("name")) or not to_str(d.get("kind")):
            return JsonResponse({"ok": False, "error": "缺少 ids 或 name/kind"}, status=200)
        return JsonResponse({"ok": True, "deleted": n}, status=200)

    return JsonResponse({"ok": False, "error": "method not allowed"}, status=405)


@csrf_exempt
def schema_connect_api(request):
    """POST /api/schema/connect —— 用**真实驱动**连上用户的数据库并读回结构。

    请求体（JSON，字段名与原型连接弹窗一一对应）：
        {"kind": "MySQL", "mode": "host"|"url",
         "host", "port", "user", "pwd", "dbname",     # mode=host
         "url"}                                        # mode=url（形如 mysql://u:p@h:3306/db）
    响应体：{"ok":true, "name", "kind", "tables", "relations"}
           {"ok":false, "error": 驱动给的原文}

    **服务端不做任何二次校验**（用户 2026-09-30 点名）：不查必填、不查端口格式，
    前端送什么就拿着去连；连不上（改地址 / 没权限 / 没装驱动）就把驱动的真实报错原样回给页面。
    整个过程只读结构，不写任何库。
    """
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "method not allowed"}, status=405)

    d = payload(request)
    res = schema.read_db(to_str(d.get("kind")), d)
    # 连不上不是「请求格式错」，是业务结果 ⇒ 统一 200，由页面按 ok=false 提示真实原因
    return JsonResponse(res, status=200)


@csrf_exempt
def schema_desc_api(request):
    """POST /api/schema/desc —— 给「库里没有注释」的表 / 字段补说明。

    请求体：{"tables": [{"name","needDesc","need":[...],"cols":[[字段, 类型], ...]}]}
            （页面只把**缺注释**的表送过来，字段里 need 那几个才需要写说明）
    响应体：{"ok":true, "tables":[{"name","desc","cols":[{"name","desc"}]}], "model": …}
            {"ok":false, "error": …}

    说明生成走 config/settings.py 里的 DeepSeek 三项配置；
    模型不可用时这里返回 ok=false，页面只是「说明为空」，提取本身照常可用。
    """
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "method not allowed"}, status=405)

    d = payload(request)
    rows = d.get("tables")
    if not isinstance(rows, (list, tuple)) or not rows:
        return JsonResponse({"ok": False, "error": "没有需要生成说明的表"}, status=400)

    # 页面送来的是它自己勾选的那几张「缺注释」的表，这里拼成内部用的形状即可。
    need = []
    for one in rows:
        if not isinstance(one, dict):
            continue
        name = to_str(one.get("name"))
        if not name:
            continue
        cols = []
        for c in one.get("cols") or []:
            if isinstance(c, (list, tuple)) and c:
                cols.append([to_str(c[0]), to_str(c[1]) if len(c) > 1 else ""])
        need.append({
            "name": name,
            "needDesc": bool(one.get("needDesc")),
            "need": [to_str(x) for x in (one.get("need") or []) if to_str(x)],
            "cols": cols,
        })
    if not need:
        return JsonResponse({"ok": False, "error": "没有需要生成说明的表"}, status=400)

    res = schema.describe_need(need, db_tag=to_str(d.get("db")))
    return JsonResponse(res, status=200 if res.get("ok") else 502)
