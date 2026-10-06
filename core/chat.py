# -*- coding: utf-8 -*-
"""PromptDeck · 模型调用（Django 版）。

两块活儿：
  1. 「提示词构造」页的「调试」—— chat_rounds()：按轮次依次调用被调试的模型；
  2. 「提示词测试」页的「判断」—— judge_pair()：调用固定判定模型（DeepSeek）
     对「标准答案 ↔ 模型答案」做语义一致性判定，结果落 tst_rec_dtl.ai_res。

口径：
  这里不做任何业务校验（必填 / 网址格式仍只看原型前端），只负责把前端传来的
  参数组装成 OpenAI 兼容的 messages，转发到 {base_url}/chat/completions，
  再把模型返回的文本与真实 usage 回传给前端（token 与费用由前端按原型口径展示）。

唯一的 guard 是「没有 base_url 就连请求都构造不出来」这一物理前提。
判定侧多一条：判定密钥必须能从 tst_api_key 里读到（不写死在代码里）。
"""
import json
import os
import re
import urllib.error
import urllib.request

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
    """同步调用模型；任何失败都归一成 {"ok": False, "error": ...}，不抛异常。"""
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


def chat_rounds(base_url, api_key, model, system, texts):
    """按轮次依次调用模型（真正的多轮：把上一轮的回复作为 assistant 上下文带进下一轮）。

    返回 (answers, prompt_tokens, completion_tokens, error, fail_round)。
    """
    messages = []
    if (system or "").strip():
        messages.append({"role": "system", "content": system})

    answers = []
    prompt_tokens = 0
    completion_tokens = 0

    for idx, one in enumerate(texts):
        messages.append({"role": "user", "content": one})
        res = call_model(base_url, api_key, model, messages)
        if not res.get("ok"):
            return answers, prompt_tokens, completion_tokens, res.get("error") or "调用失败", idx + 1
        answers.append(res.get("text") or "")
        prompt_tokens += res.get("prompt_tokens") or 0
        completion_tokens += res.get("completion_tokens") or 0
        messages.append({"role": "assistant", "content": res.get("text") or ""})

    return answers, prompt_tokens, completion_tokens, None, 0


# ====================================================================== AI 判定
# 「提示词测试」页的「判断」列：拿「标准答案」与「模型答案」做一次语义一致性判定。
# 判定固定走 DeepSeek：
#     base_url = https://api.deepseek.com
#     model    = deepseek-v4-pro
#     api_key  = 从 tst_api_key 表里读（不写进源码，见 core/store.py 的 judge_key()）
# 两个环境变量只是给校验脚本把地址指到本机 mock 用的口子，默认值即上面那组。
JUDGE_BASE_URL = os.environ.get("PD_JUDGE_BASE_URL", "https://api.deepseek.com")
JUDGE_MODEL = os.environ.get("PD_JUDGE_MODEL", "deepseek-v4-pro")

JUDGE_SYSTEM = (
    "你是一个严格的答案一致性判定器。判断「模型答案」与「标准答案」是否表达同一个意思。\n"
    "口径：文本含义一致即可，措辞、语序、详略不同都算一致；"
    "如果内容是代码，功能等价即可，变量名与写法不同不算差异。\n"
    "只输出一行 JSON：{\"same\": true} 或 {\"same\": false}，不要输出任何解释。"
)


def parse_judge(raw):
    """把判定模型的返回解析成 right / wrong / ""（解析不出来 = 未判定）。

    模型偶尔会带上 ```json 围栏或多余说明，所以先找第一个 {...} 再兜关键词。
    """
    t = (raw or "").strip()
    if not t:
        return ""

    m = re.search(r"\{[^{}]*\}", t, re.S)
    if m:
        try:
            val = json.loads(m.group(0)).get("same")
        except Exception:
            val = None
        if isinstance(val, bool):
            return "right" if val else "wrong"
        if isinstance(val, str):
            s = val.strip().lower()
            if s in ("true", "1", "yes", "一致", "相同"):
                return "right"
            if s in ("false", "0", "no", "不一致", "不同"):
                return "wrong"

    low = "".join(t.lower().split())
    if '"same":true' in low:
        return "right"
    if '"same":false' in low:
        return "wrong"
    # 中文兜底：先判「不一致」，否则「一致」会把「不一致」也命中
    if "不一致" in t or "不同" in t:
        return "wrong"
    if "一致" in t or "相同" in t:
        return "right"
    return ""


def judge_pair(api_key, std_ans, mdl_out, base_url=None, model=None, timeout=60):
    """判定「标准答案」与「模型答案」是否一致。

    返回 (结果, 原文, 错误)：结果 ∈ {"right", "wrong", ""}；
    "" 有两种来源 —— 有一边是空的（按原型口径不给结论，等人工判定），
    或者模型返回看不懂的内容。错误非空时结果一律为 ""。
    """
    a = ("" if std_ans is None else str(std_ans)).strip()
    b = ("" if mdl_out is None else str(mdl_out)).strip()
    if not a or not b:
        return "", "", None

    if not (api_key or "").strip():
        return "", "", "未在 tst_api_key 表里配置 AI 判定密钥（先跑 setup_judge_key.py）"

    messages = [
        {"role": "system", "content": JUDGE_SYSTEM},
        {"role": "user", "content": "【标准答案】\n%s\n\n【模型答案】\n%s" % (a, b)},
    ]
    res = call_model(base_url or JUDGE_BASE_URL, api_key, model or JUDGE_MODEL,
                     messages, timeout=timeout)
    if not res.get("ok"):
        return "", "", res.get("error") or "判定模型调用失败"
    raw = res.get("text") or ""
    return parse_judge(raw), raw, None
