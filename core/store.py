# -*- coding: utf-8 -*-
"""PromptDeck · 数据层（Django ORM 版 · 第三次迭代）。

函数名与上一版 db.py 一一对应，方便对照 review：
    srv：list_srv / create_srv / update_srv / delete_srv
    mdl：list_mdl / create_mdl / update_mdl / delete_mdl
    tp ：list_tp  / save_tp   / delete_tp / count_tp
    rec：save_rec / list_rec  / delete_rec / plan_key_id / judge_key   ← 第二次迭代新增

本次迭代只做两件事：**换库** + **把「提示词方案」由单表换成三张表**。
下面所有函数的**出入参口径一个字没变**（所以前端 prompt.html、
prompt_scheme.html 零改动），变的只是它们背后读写的表：

    旧库 tp 单表                              新库三张表
    ─────────────────────────────────────     ─────────────────────────────
    name       方案命名                       tst_plan.plan_name
    base       请求地址                       tst_plan.base_url
    server     服务器（平台 / 官方名）         tst_plan.srv_id → srv.plat_name
    model      模型                           tst_plan.mdl_id → mdl.mdl_name
    modelType  模型类型                       （同上）→ mdl.mdl_type
    sys        系统提示词                     tst_plan_prompt(seq_no=0, role=system)
    user:[…]   用户提示词（多轮 = 多元素）     tst_plan_prompt(seq_no=1..N, role=user)
    multi      多轮会话调试                   ← 由 user 条数 > 1 还原（不单独落库）
    apiKey     API_key（明文）                tst_api_key.api_key
    savedAt    保存时间                       ← 原型只写不读，不落库

校验口径（按用户约束，一个字都没变）：
    必填 / 网址格式校验只保留在原型前端，服务端**不做二次校验**；
    这里只做「空串 -> NULL」的类型归一。
    唯一的例外是 save_tp 里那条「按 id / 同名覆盖」的判定 —— 它是原型自带的
    保存逻辑（同名即覆盖），不是新增校验。

计费单价 in_price / out_price 在新库里是 TEXT，按用户输入的原文存，不做四舍五入。
"""
from datetime import datetime

from django.db import transaction

from .models import Mdl, Srv, TstApiKey, TstPlan, TstPlanPrompt, TstRec, TstRecDtl

# tst_plan_prompt.prompt_role 的取值（与 OpenAI messages 口径一致，小写）
ROLE_SYSTEM = "system"
ROLE_USER = "user"

# 迁移时给每个方案配的那把密钥的名称；本次不做多密钥界面
DEFAULT_KEY_NAME = "默认"

# 「提示词测试」页做语义一致性判定时用的判定模型密钥在 tst_api_key 里的标识。
# 这把 key 不归属任何方案（plan_id 为 NULL），只由 setup_judge_key.py 写库、这里读库。
JUDGE_KEY_NAME = "AI判定（DeepSeek）"


# ------------------------------------------------------------------ 小工具
def to_str(v):
    """None -> ""，其余转字符串并去首尾空格。"""
    return ("" if v is None else str(v)).strip()


def price_text(v):
    """计费单价归一：字符串去空格后原样返回，空 -> None（落库为 NULL）。

    新库的列就是 TEXT，不做四舍五入、小数位数原样保留（18.00 不会变成 18）。
    """
    if v is None or isinstance(v, bool):
        return None
    return str(v).strip() or None


def now_str():
    """本地时间字符串（YYYY-MM-DD HH:MM:SS）。

    settings 里 USE_TZ = False（存本地时间），migrate_to_twe.py 也按同一套写，
    保证库里看到的时间就是人看的时间。
    """
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------------ srv
def list_srv(platform="", company=""):
    """按 平台/官方名、公司名 做不区分大小写的模糊匹配，未传则不过滤。"""
    qs = Srv.objects.all()
    if platform:
        qs = qs.filter(plat_name__icontains=platform)
    if company:
        qs = qs.filter(comp_name__icontains=company)
    return list(qs.order_by("id"))


def create_srv(plat_name, base_url="", comp_name="", home_url=""):
    """写入一条服务器记录，返回新主键 id。不做服务器端校验（按项目约定）。"""
    row = Srv.objects.create(
        plat_name=plat_name,
        base_url=base_url or None,
        comp_name=comp_name or None,
        home_url=home_url or None,
    )
    return row.id


def update_srv(srv_id, plat_name, base_url="", comp_name="", home_url=""):
    """按主键更新一条服务器记录，返回受影响行数。空串按 NULL 落库。"""
    return Srv.objects.filter(id=int(srv_id)).update(
        plat_name=plat_name,
        base_url=base_url or None,
        comp_name=comp_name or None,
        home_url=home_url or None,
    )


def delete_srv(ids):
    """批量删除，返回受影响行数。

    新库的外键是 ON DELETE CASCADE，mdl 会由库自己一并清理（与旧库行为一致）。
    """
    ids = [int(i) for i in ids]
    if not ids:
        return 0
    return Srv.objects.filter(id__in=ids).delete()[0]


def get_or_create_srv_id(plat_name, home_url=""):
    """按平台名取 srv.id（忽略大小写与首尾空格）；不存在则新建一条只含平台名的记录。"""
    plat_name = to_str(plat_name)
    row = Srv.objects.filter(plat_name__iexact=plat_name).order_by("id").first()
    if row:
        return row.id
    return Srv.objects.create(plat_name=plat_name, home_url=home_url or None).id


# ------------------------------------------------------------------ mdl
def list_mdl():
    """模型列表：联 srv 取平台名与该平台首页网址（home_url）。
    输出字段与原型 foundation1_models.html 的行对象一一对应。"""
    homes = {s.id: (s.plat_name or "", s.home_url or "") for s in Srv.objects.all()}
    out = []
    for m in Mdl.objects.order_by("id"):
        plat, home = homes.get(m.srv_id, ("", ""))
        out.append({
            "id": m.id,
            "srv_id": m.srv_id,
            "mdl_name": m.mdl_name,
            "mdl_type": m.mdl_type,
            "in_price": m.in_price,
            "out_price": m.out_price,
            "unit": m.unit,
            "url": m.url,
            "plat_name": plat,
            "home_url": home,
        })
    return out


def create_mdl(plat_name, mdl_name, mdl_type="", in_price=None, out_price=None,
               unit="", url=""):
    """写入一条模型记录，返回新主键 id。平台不存在时自动补一条 srv（满足外键）。

    计费单价按用户输入的原文保存（price_text），不做四舍五入；
    不做服务器端校验（必填 / 网址格式只保留在原型前端）。
    """
    with transaction.atomic():
        srv_id = get_or_create_srv_id(plat_name)
        row = Mdl.objects.create(
            srv_id=srv_id,
            mdl_name=to_str(mdl_name),
            mdl_type=to_str(mdl_type) or None,
            in_price=price_text(in_price),
            out_price=price_text(out_price),
            unit=to_str(unit) or None,
            url=to_str(url) or None,
        )
        return row.id


def update_mdl(mdl_id, plat_name, mdl_name, mdl_type="", in_price=None, out_price=None,
               unit="", url=""):
    """按主键更新一条模型记录，返回受影响行数。与 create_mdl 同口径。"""
    with transaction.atomic():
        srv_id = get_or_create_srv_id(plat_name)
        return Mdl.objects.filter(id=int(mdl_id)).update(
            srv_id=srv_id,
            mdl_name=to_str(mdl_name),
            mdl_type=to_str(mdl_type) or None,
            in_price=price_text(in_price),
            out_price=price_text(out_price),
            unit=to_str(unit) or None,
            url=to_str(url) or None,
        )


def delete_mdl(ids):
    """批量删除模型，返回受影响行数。"""
    ids = [int(i) for i in ids]
    if not ids:
        return 0
    return Mdl.objects.filter(id__in=ids).delete()[0]


def get_or_create_mdl_id(srv_id, mdl_name, mdl_type=""):
    """按「同平台 + 模型名」取 mdl.id（忽略大小写与首尾空格）；不存在则补建一条。

    保存方案时用：原型选的是「某个平台下的某个模型」，所以先在同一 srv_id 下找，
    找不到就为新模型补一条记录（只填名字与类型），保证 tst_plan.mdl_id 有落点。
    """
    mdl_name = to_str(mdl_name)
    row = Mdl.objects.filter(srv_id=srv_id, mdl_name__iexact=mdl_name).order_by("id").first()
    if row:
        return row.id
    return Mdl.objects.create(
        srv_id=srv_id,
        mdl_name=mdl_name,
        mdl_type=to_str(mdl_type) or None,
        in_price=None,
        out_price=None,
        unit=None,
        url=None,
    ).id


# ------------------------------------------------------------------ tp（新库：tst_plan 三张表）
def list_tp():
    """提示词方案列表（按 tst_plan.id 升序，与原型列表页的展示顺序一致）。

    三张表在内存里拼成一个方案对象，字段名与原型 prompt.html 保存的 item 一致：
        { id, name, server, base, model, modelType, sys, user:[…], multi, apiKey, savedAt }

    实现说明：一次性把四张表读出来用字典归拢，避免逐个方案回查（N+1）。
    """
    plans = list(TstPlan.objects.order_by("id"))
    if not plans:
        return []

    # 供应商 / 模型的展示名
    srv_map = {s.id: (s.plat_name or "", s.base_url or "") for s in Srv.objects.all()}
    mdl_map = {m.id: (m.mdl_name or "", m.mdl_type or "") for m in Mdl.objects.all()}

    # 提示词：系统一条、用户若干条（按 seq_no 升序）
    plan_ids = [p.id for p in plans]
    sys_map, user_map = {}, {}
    for row in TstPlanPrompt.objects.filter(plan_id__in=plan_ids).order_by("plan_id", "seq_no"):
        role = (row.prompt_role or "").strip().lower()
        if role == ROLE_SYSTEM:
            sys_map.setdefault(row.plan_id, row.prompt_content or "")
        elif role == ROLE_USER:
            user_map.setdefault(row.plan_id, []).append(row.prompt_content or "")

    # 密钥：一个方案取第一把启用的（本次不做多密钥界面）。
    # 判定模型那把 key（plan_id 为 NULL）天然落不进 plan_id__in，不会被带出来。
    key_map = {}
    for k in TstApiKey.objects.filter(plan_id__in=plan_ids, is_active=1).order_by("id"):
        key_map.setdefault(k.plan_id, k.api_key or "")

    out = []
    for p in plans:
        plat, srv_base = srv_map.get(p.srv_id, ("", ""))
        mdl_name, mdl_type = mdl_map.get(p.mdl_id, ("", ""))
        users = user_map.get(p.id, [])
        out.append({
            "id": p.id,
            "name": p.plan_name or "",
            "server": plat,
            "base": p.base_url or srv_base or "",   # 方案上没存就退回服务器的 base_url
            "model": mdl_name,
            "modelType": mdl_type,
            "sys": sys_map.get(p.id, ""),
            "user": users,
            # 多轮开关不单独落库：原型里「多轮开」= 收集第 1 轮 + 追加轮，
            # 故 user 条数 > 1 与 multi 打开完全等价，可无损还原。
            "multi": len(users) > 1,
            "apiKey": key_map.get(p.id, ""),
            "savedAt": "",                          # 新库不存保存时间（原型只写不读）
        })
    return out


def count_tp():
    return TstPlan.objects.count()


def _write_plan_prompts(plan_id, sys_txt, users):
    """重写一个方案的提示词明细：先清空，再按 seq_no 0/1..N 落库。

    seq_no = 0 留给系统提示词，用户提示词从 1 开始 —— 这样按 seq_no 排序，
    系统提示词天然排在最前，读回来也不用再分两趟找。
    """
    TstPlanPrompt.objects.filter(plan_id=plan_id).delete()
    if sys_txt != "":
        TstPlanPrompt.objects.create(
            plan_id=plan_id, seq_no=0,
            prompt_role=ROLE_SYSTEM, prompt_content=sys_txt,
        )
    for i, txt in enumerate(users, start=1):
        TstPlanPrompt.objects.create(
            plan_id=plan_id, seq_no=i,
            prompt_role=ROLE_USER, prompt_content=txt,
        )


def _write_plan_key(plan_id, api_key):
    """重写一个方案的密钥：原型一份方案只带一把 key，先清空再写这一把。"""
    TstApiKey.objects.filter(plan_id=plan_id).delete()
    if api_key:
        stamp = now_str()
        TstApiKey.objects.create(
            plan_id=plan_id, key_name=DEFAULT_KEY_NAME,
            api_key=api_key, is_active=1,
            created_at=stamp, updated_at=stamp,
        )


def save_tp(d):
    """保存一份提示词方案，返回 (id, 结果类型)。

    覆盖判定完全照原型 prompt.html 上「保存方案」那段的逻辑搬：
      ① 带了 id（从「提示词方案」页点「编辑」跳过来的编辑流）→ 按 id 精确覆盖；
      ② 没带 id（或 id 在库里查不到）→ 同名即覆盖，否则新增。
    目标被删掉这种极端情况退化成同名覆盖 / 新增，不报错（原型注释同款口径）。

    落库落到三张表：tst_plan（主体）+ tst_plan_prompt（提示词）+ tst_api_key（密钥）。
    """
    name = to_str(d.get("name"))
    base_url = to_str(d.get("base")) or None
    sys_txt = "" if d.get("sys") is None else str(d.get("sys"))

    # 用户提示词：逐轮收集，空串也照原样留着（原型 collectUserPrompts() 不过滤空值，
    # 条数本身就承载着「多轮开关」的信息，过滤掉会把 multi 弄丢）。
    raw_users = d.get("user")
    if isinstance(raw_users, (list, tuple)):
        users = ["" if x is None else str(x) for x in raw_users]
    elif raw_users in (None, ""):
        users = []
    else:
        users = [str(raw_users)]

    api_key = to_str(d.get("apiKey"))

    target_id = d.get("id")
    try:
        target_id = int(target_id) if target_id not in (None, "") else None
    except (TypeError, ValueError):
        target_id = None

    # 供应商 / 模型：原型传的是名字，这里换回新库的外键 id（没有就补建）
    srv_id = get_or_create_srv_id(d.get("server"), base_url or "")
    mdl_id = get_or_create_mdl_id(srv_id, d.get("model"), d.get("modelType"))

    fields = {
        "plan_name": name,
        "base_url": base_url,
        "srv_id": srv_id,
        "mdl_id": mdl_id,
    }

    with transaction.atomic():
        # ① 编辑流：按 id 精确覆盖
        if target_id:
            hit = TstPlan.objects.filter(id=target_id).first()
            if hit:
                TstPlan.objects.filter(id=hit.id).update(**fields)
                _write_plan_prompts(hit.id, sys_txt, users)
                _write_plan_key(hit.id, api_key)
                return hit.id, "updated"

        # ② 同名即覆盖
        same = TstPlan.objects.filter(plan_name=name).order_by("id").first()
        if same:
            TstPlan.objects.filter(id=same.id).update(**fields)
            _write_plan_prompts(same.id, sys_txt, users)
            _write_plan_key(same.id, api_key)
            return same.id, "overwritten"

        # ③ 新增
        row = TstPlan.objects.create(**fields)
        _write_plan_prompts(row.id, sys_txt, users)
        _write_plan_key(row.id, api_key)
        return row.id, "created"


def delete_tp(ids):
    """批量删除提示词方案，返回受影响行数（= 删掉的方案数）。

    提示词与密钥两张子表跟着一起清：新库的外键本身就是 ON DELETE CASCADE，
    但这里再显式删一遍 —— 这样即使连接上没开 PRAGMA foreign_keys 也不会留孤儿行。
    """
    ids = [int(i) for i in ids]
    if not ids:
        return 0
    with transaction.atomic():
        TstPlanPrompt.objects.filter(plan_id__in=ids).delete()
        TstApiKey.objects.filter(plan_id__in=ids).delete()
        return TstPlan.objects.filter(id__in=ids).delete()[0]


def plan_key_id(plan_id):
    """取一个方案自己的那把密钥的主键（key_name=「默认」，取第一把启用的）。

    用途：写 tst_rec 时把 api_key_id 指到「本次调试对象用的那把 key」。
    判定模型的密钥（plan_id 为 NULL）不会被取到。
    """
    row = (TstApiKey.objects.filter(plan_id=int(plan_id), is_active=1)
           .order_by("id").first())
    return row.id if row else None


# ------------------------------------------------------------------ 判定模型密钥（不归属方案）
def judge_key():
    """读「AI 判定模型」的密钥（tst_api_key 里 plan_id 为 NULL、key_name 固定的那一条）。

    按需求，这把 key **不写进源码**，只落库；这里现读现用。
    库里没有就返回 None，由调用方转成可读的错误信息（不在代码里塞默认值）。
    """
    row = (TstApiKey.objects.filter(key_name=JUDGE_KEY_NAME, plan_id__isnull=True, is_active=1)
           .order_by("id").first())
    return (row.api_key or "") if row else None


# ------------------------------------------------------------------ tst_rec / tst_rec_dtl
def _norm_judge(v):
    """判定值归一：只有 right / wrong 落库，其余（空 = 未判定）落 NULL。"""
    v = ("" if v is None else str(v)).strip().lower()
    return v if v in ("right", "wrong") else None


def save_rec(plan_id, details, test_time=None):
    """保存一次「提示词测试」的结果，返回 (rec_id, 汇总)。

    落两张表：
        tst_rec      一条汇总（针对哪个方案 / 用哪把 key / 题数 / 准确率 / 错题数 / 矫正数）
        tst_rec_dtl  逐题明细（问题 / 标准答案 / 模型输出 / AI 判定 / 是否人工改判）

    四个汇总数字**由逐题明细现算**，不采信前端传来的统计值 —— 与原型
    prompt_test.html 的 updateStat() 同口径（准确率 = 最终判定为「对」的条数 ÷ 总题数），
    这样「页面看到的数」与「库里的数」永远同源。
    """
    pid = int(plan_id)

    rows = []
    for one in (details or []):
        if not isinstance(one, dict):
            continue
        ai = _norm_judge(one.get("ai"))
        judge = _norm_judge(one.get("judge"))
        rows.append({
            "qst": "" if one.get("q") is None else str(one.get("q")),
            "std_ans": "" if one.get("ans") is None else str(one.get("ans")),
            "mdl_out": "" if one.get("out") is None else str(one.get("out")),
            "ai_res": ai,
            # 人工矫正 = AI 有结论、人工也有结论、且两者不一致
            "is_man_fix": 1 if (ai and judge and ai != judge) else 0,
            # 准确率分子：以「最终判定（人工优先）」为准
            "_ok": 1 if (judge or ai) == "right" else 0,
        })

    q_cnt = len(rows)
    err_cnt = sum(1 for r in rows if r["ai_res"] == "wrong")
    fix_cnt = sum(1 for r in rows if r["is_man_fix"])
    ok_cnt = sum(r["_ok"] for r in rows)
    acc_rate = round(ok_cnt * 100.0 / q_cnt, 2) if q_cnt else None

    with transaction.atomic():
        rec = TstRec.objects.create(
            plan_id=pid,
            api_key_id=plan_key_id(pid),
            q_cnt=q_cnt,
            acc_rate=acc_rate,
            err_cnt=err_cnt,
            fix_cnt=fix_cnt,
            test_time=test_time or now_str(),
        )
        for r in rows:
            TstRecDtl.objects.create(
                rec_id=rec.id,
                qst=r["qst"],
                std_ans=r["std_ans"],
                mdl_out=r["mdl_out"],
                ai_res=r["ai_res"],
                is_man_fix=r["is_man_fix"],
            )

    return rec.id, {
        "planId": pid,
        "qCnt": q_cnt,
        "accRate": acc_rate,
        "errCnt": err_cnt,
        "fixCnt": fix_cnt,
    }


def list_rec():
    """测试记录列表（带方案名与逐题明细），供接口与校验脚本核对。"""
    plans = {p.id: (p.plan_name or "") for p in TstPlan.objects.all()}
    recs = list(TstRec.objects.order_by("id"))
    if not recs:
        return []

    ids = [r.id for r in recs]
    dtl = {}
    for d in TstRecDtl.objects.filter(rec_id__in=ids).order_by("rec_id", "id"):
        dtl.setdefault(d.rec_id, []).append({
            "qst": d.qst or "",
            "stdAns": d.std_ans or "",
            "mdlOut": d.mdl_out or "",
            "aiRes": d.ai_res or "",
            "isManFix": d.is_man_fix,
        })

    return [{
        "id": r.id,
        "planId": r.plan_id,
        "planName": plans.get(r.plan_id, ""),
        "apiKeyId": r.api_key_id,
        "qCnt": r.q_cnt,
        "accRate": None if r.acc_rate is None else float(r.acc_rate),
        "errCnt": r.err_cnt,
        "fixCnt": r.fix_cnt,
        "testTime": "" if r.test_time is None else str(r.test_time),
        "details": dtl.get(r.id, []),
    } for r in recs]


def delete_rec(ids):
    """批量删除测试记录（明细一起清），返回删掉的记录数。"""
    ids = [int(i) for i in ids]
    if not ids:
        return 0
    with transaction.atomic():
        TstRecDtl.objects.filter(rec_id__in=ids).delete()
        return TstRec.objects.filter(id__in=ids).delete()[0]
