# -*- coding: utf-8 -*-
"""PromptDeck · Starlette 版入口。

架构：
    Starlette 负责「路由 + 静态页面托管 + 数据落库」；
    原型页以静态文件原样托管（字段 / 风格 / 排版 / CSS 零改动），
    页面内只把数据源由 mock 数组换成 fetch("/api/*")，
    因此不需要 iframe 内嵌，也不需要组件桥。

运行：
    python server.py                     # 默认 http://127.0.0.1:8502/
    python server.py --port 8501
    python -m uvicorn server:app --port 8502

数据：
    SQLite，库名 prompt_test_DB（data/prompt_test_DB.db），表 srv / mdl，见 db.py。
    启动时自动建表（幂等）；表为空时灌入原型表格里的演示数据。

接口：
    GET    /api/srv          服务器列表（可选 ?platform= &company= 模糊过滤）
    POST   /api/srv          新增服务器
    PUT    /api/srv          修改服务器（按 id 更新）
    DELETE /api/srv          批量删除服务器 {"ids":[...]}（mdl 由外键级联清理）
    GET    /api/mdl          模型列表（联 srv 取平台名与平台主页网址）
    POST   /api/mdl          新增模型（平台不存在时自动补一条 srv，满足外键）
    PUT    /api/mdl          修改模型（按 id 更新）
    DELETE /api/mdl          批量删除模型 {"ids":[...]}
    POST   /api/chat         调试：转发给真实模型（多轮时按轮次依次调用）
    GET    /api/health       健康检查

校验口径（按用户约束）：
    必填 / 网址格式校验只保留在原型前端，服务端不做二次校验；
    服务端只做「空串 -> NULL」的类型归一。
    计费单价按用户输入的原文存 TEXT（不转 float），前端显示时不再四舍五入。
"""
import argparse
import contextlib
import json
import os
import sys
import urllib.error
import urllib.request

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

import db

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

# 根路径落到哪一页（本期焦点是模型模块）
HOME_PAGE = "foundation1_models.html"


# ------------------------------------------------------------------ 序列化
def srv_public(row):
    """srv 行 -> 服务器列表页的行对象。"""
    return {
        "id": row["id"],
        "platform": row["plat_name"] or "",
        "company": row["comp_name"] or "",
        "api": row["base_url"] or "",
        "cUrl": row["home_url"] or "",
    }


def mdl_public(row):
    """mdl 行 -> 模型列表页的行对象（platform / cUrl 来自关联的 srv）。

    in_price / out_price 直接给「用户输入原文」（库里就是 TEXT），前端不再四舍五入，
    小数位数原样保留；留空（NULL）给空串，由前端决定展示成 0.00 还是留白。
    """
    return {
        "id": row["id"],
        "srvId": row["srv_id"],
        "platform": row["plat_name"] or "",
        "cUrl": row["home_url"] or "",
        "model": row["mdl_name"] or "",
        "type": row["mdl_type"] or "",
        "inPrice": db.price_text(row["in_price"]) or "",
        "outPrice": db.price_text(row["out_price"]) or "",
        "unit": row["unit"] or "",
        "url": row["url"] or "",
    }


def to_str(v):
    return ("" if v is None else str(v)).strip()


async def payload(request):
    """取请求体：优先 JSON，兼容表单提交。"""
    ctype = (request.headers.get("content-type") or "").lower()
    if "application/json" in ctype:
        try:
            data = await request.json()
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    try:
        form = await request.form()
        return dict(form)
    except Exception:
        return {}


def ids_of(data):
    """从请求体取待删除 id 列表，兼容 {"ids":[...]} 与 "ids=1,2" 两种形式。"""
    ids = data.get("ids")
    if ids is None:
        return []
    if isinstance(ids, str):
        return [int(x) for x in ids.split(",") if x.strip()]
    if isinstance(ids, (list, tuple)):
        return [int(x) for x in ids]
    return []


# ------------------------------------------------------------------ 服务器表
async def srv_api(request):
    if request.method == "GET":
        rows = db.list_srv(
            request.query_params.get("platform", ""),
            request.query_params.get("company", ""),
        )
        return JSONResponse([srv_public(r) for r in rows])

    if request.method == "POST":
        d = await payload(request)
        new_id = db.create_srv(
            to_str(d.get("plat_name")),
            to_str(d.get("base_url")),
            to_str(d.get("comp_name")),
            to_str(d.get("home_url")),
        )
        return JSONResponse({"ok": True, "id": new_id}, status_code=201)

    if request.method in ("PUT", "PATCH"):
        d = await payload(request)
        srv_id = d.get("id")
        if srv_id in (None, ""):
            return JSONResponse({"ok": False, "error": "缺少 id"}, status_code=400)
        n = db.update_srv(
            int(srv_id),
            to_str(d.get("plat_name")),
            to_str(d.get("base_url")),
            to_str(d.get("comp_name")),
            to_str(d.get("home_url")),
        )
        return JSONResponse({"ok": True, "updated": n})

    if request.method == "DELETE":
        d = await payload(request)
        return JSONResponse({"ok": True, "deleted": db.delete_srv(ids_of(d))})

    return JSONResponse({"ok": False, "error": "method not allowed"}, status_code=405)


# ------------------------------------------------------------------ 模型表
async def mdl_api(request):
    if request.method == "GET":
        return JSONResponse([mdl_public(r) for r in db.list_mdl()])

    if request.method == "POST":
        d = await payload(request)
        new_id = db.create_mdl(
            to_str(d.get("plat_name")),
            to_str(d.get("mdl_name")),
            to_str(d.get("mdl_type")),
            to_str(d.get("in_price")),
            to_str(d.get("out_price")),
            to_str(d.get("unit")),
            to_str(d.get("url")),
        )
        return JSONResponse({"ok": True, "id": new_id}, status_code=201)

    if request.method in ("PUT", "PATCH"):
        d = await payload(request)
        mdl_id = d.get("id")
        if mdl_id in (None, ""):
            return JSONResponse({"ok": False, "error": "缺少 id"}, status_code=400)
        n = db.update_mdl(
            int(mdl_id),
            to_str(d.get("plat_name")),
            to_str(d.get("mdl_name")),
            to_str(d.get("mdl_type")),
            to_str(d.get("in_price")),
            to_str(d.get("out_price")),
            to_str(d.get("unit")),
            to_str(d.get("url")),
        )
        return JSONResponse({"ok": True, "updated": n})

    if request.method == "DELETE":
        d = await payload(request)
        return JSONResponse({"ok": True, "deleted": db.delete_mdl(ids_of(d))})

    return JSONResponse({"ok": False, "error": "method not allowed"}, status_code=405)


# ------------------------------------------------------------------ 调试：调用真实模型
# 口径：这里不做任何业务校验（必填/网址格式仍只看原型前端），只负责把前端传来的
# 参数组装成 OpenAI 兼容的 messages，转发到 {base_url}/chat/completions，
# 再把模型返回的文本与真实 usage 回传给前端（token 与费用由前端按原型口径展示）。
CHAT_TIMEOUT = int(os.environ.get("PD_CHAT_TIMEOUT", "180"))


def chat_url(base_url):
    """补全成 /chat/completions；base_url 已是完整端点时不重复拼接。"""
    b = (base_url or "").strip().rstrip("/")
    if not b:
        return ""
    if b.endswith("/chat/completions"):
        return b
    return b + "/chat/completions"


def content_text(content):
    """message.content 可能是字符串，也可能是分段数组（多模态返回）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                parts.append(part.get("text") or part.get("content") or "")
            elif part is not None:
                parts.append(str(part))
        return "".join(parts)
    return "" if content is None else str(content)


def call_model(base_url, api_key, model, messages, timeout=CHAT_TIMEOUT):
    """同步调用模型；任何失败都归一成 {"ok": False, "error": ...}，不抛异常。

    唯一的 guard 是「没有 base_url 就连请求都构造不出来」这一物理前提，
    其余一律原样转发、由模型服务自己判定（服务端不复现前端的必填校验）。
    """
    url = chat_url(base_url)
    if not url:
        return {"ok": False, "error": "该服务器未配置 Base_url，无法构造请求地址"}

    body = json.dumps(
        {"model": model, "messages": messages, "stream": False},
        ensure_ascii=False,
    ).encode("utf-8")

    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("Authorization", "Bearer " + api_key)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")
        except Exception:
            pass
        return {"ok": False, "error": "模型服务返回 HTTP %s：%s"
                                      % (exc.code, (detail or "无响应体")[:600])}
    except urllib.error.URLError as exc:
        return {"ok": False, "error": "无法连接模型服务 %s：%s" % (url, exc.reason)}
    except Exception as exc:                                    # 超时 / SSL / 编码等
        return {"ok": False, "error": "请求模型服务失败：%s" % exc}

    try:
        data = json.loads(raw)
    except Exception:
        return {"ok": False, "error": "模型返回的不是 JSON：%s" % raw[:400]}

    if isinstance(data, dict) and data.get("error"):
        err = data["error"]
        msg = err.get("message") if isinstance(err, dict) else str(err)
        return {"ok": False, "error": "模型服务报错：%s" % msg}

    text = ""
    choices = data.get("choices") or []
    if choices:
        msg = choices[0].get("message") or {}
        text = content_text(msg.get("content"))
        if not text:
            # 推理模型（如 Qwen3 / DeepSeek-R1）会把思考过程放在 reasoning_content，
            # 正式回答为空时兜底取它，否则页面上会什么都看不到。
            text = content_text(msg.get("reasoning_content"))
        if not text:
            text = content_text(choices[0].get("text"))

    usage = data.get("usage") or {}
    return {
        "ok": True,
        "text": text,
        "model": data.get("model") or model,
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
    }


async def chat_api(request):
    """POST /api/chat —— 把调试请求转发给真实模型。

    请求体：{"base_url", "api_key", "model", "system", "rounds":[...]}
    轮次口径：rounds 里每一轮依次单独调用一次模型，把上一轮的模型回复作为
              assistant 上下文带进下一轮（真正的多轮对话），逐轮产出回答。
              只有一轮时与单次调用等价。
    响应体：{"ok":true,"text","answers":[...],"prompt_tokens","completion_tokens","model"}
            {"ok":false,"error","answers":[...已拿到的],"round":失败的轮次}
    """
    d = await payload(request)
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

    messages = []
    if system.strip():
        messages.append({"role": "system", "content": system})

    answers = []
    prompt_tokens = 0
    completion_tokens = 0

    for idx, one in enumerate(texts):
        messages.append({"role": "user", "content": one})
        res = await run_in_threadpool(call_model, base_url, api_key, model, messages)
        if not res.get("ok"):
            return JSONResponse({
                "ok": False,
                "error": res.get("error") or "调用失败",
                "answers": answers,
                "round": idx + 1,
            })
        answers.append(res.get("text") or "")
        prompt_tokens += res.get("prompt_tokens") or 0
        completion_tokens += res.get("completion_tokens") or 0
        messages.append({"role": "assistant", "content": res.get("text") or ""})

    return JSONResponse({
        "ok": True,
        "text": "\n\n".join(answers),
        "answers": answers,
        "model": model,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
    })


# ------------------------------------------------------------------ 其它
async def health(request):
    return PlainTextResponse("ok")


async def home(request):
    return RedirectResponse("/" + HOME_PAGE)


@contextlib.asynccontextmanager
async def lifespan(app):
    db.init_db()
    db.seed_if_empty()
    db.seed_mdl_if_empty()
    yield


os.makedirs(STATIC_DIR, exist_ok=True)

routes = [
    Route("/", home),
    Route("/api/health", health),
    Route("/api/srv", srv_api, methods=["GET", "POST", "PUT", "PATCH", "DELETE"]),
    Route("/api/mdl", mdl_api, methods=["GET", "POST", "PUT", "PATCH", "DELETE"]),
    Route("/api/chat", chat_api, methods=["POST"]),
    # 静态原型页放在最后，避免吃掉 /api/*
    Mount("/", app=StaticFiles(directory=STATIC_DIR, html=True)),
]

app = Starlette(routes=routes, lifespan=lifespan)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="PromptDeck · Starlette 服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8502)
    args = parser.parse_args()

    import uvicorn

    db.init_db()
    print("建库完成，服务器 %d 条 / 模型 %d 条" % (db.count_srv(), db.count_mdl()))
    print("PromptDeck -> http://%s:%d/" % (args.host, args.port))

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
