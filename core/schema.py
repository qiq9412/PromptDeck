# -*- coding: utf-8 -*-
"""PromptDeck · 数据库表结构（Django 版 · 第二次迭代新增）。

对应原型页 `Table_Structure_Extraction.html`：把库里真实的表结构读出来，
交给页面渲染成「表 / 注释 / 字段」形态的说明文本。

说明文字有两条来源：
  1. **库里本来就有注释** —— SQLite 没有原生注释字段，注释只能写在建表 SQL 里，
     本模块按下面的约定把它抠出来（`parse_sql_comments`）：
       · 表注释：`CREATE TABLE x (` 之后、第一条列定义之前的 `-- …` 行；
       · 字段注释：列定义**行尾**的 `-- …`；
  2. **库里没有注释** —— 交给 DeepSeek 现生成一句说明（`describe_tables`），
     base_url / model / api_key 全部来自 `config/settings.py`，
     口径按用户要求：**只说这张表 / 这个字段「存的是什么」**，
     不臆造业务规则、不写 SQL、不给建议。

边界：
  · 只读结构，不写任何库、不改任何表；
  · 不做任何业务校验（必填 / 格式仍只看原型前端）；
  · 没有网络 / 没有密钥时只是「说明生成为空」，提取本身照常可用。
"""
import json
import os
import re
import sqlite3

from django.conf import settings

from . import dbx
from .chat import call_model

# ---------------------------------------------------------------- 常量
DESC_TIMEOUT = int(os.environ.get("PD_DESC_TIMEOUT", "120"))

# 只描述「存的是什么」——约束模型别写业务规则、别写 SQL
DESC_SYSTEM = (
    "你是数据库结构说明生成器。给定「表名 + 字段名 + 字段类型」，"
    "为这张表和它需要说明的每个字段各写一句简短中文说明。\n"
    "要求：\n"
    "1. 只说这张表 / 这个字段「存的是什么」，不要写业务规则、不要写 SQL、不要给建议；\n"
    "2. 表说明不超过 12 个字，字段说明不超过 16 个字，不要带句号；\n"
    "3. 主键字段的说明里要带「主键」；外键字段的说明里写清它指向哪张表；\n"
    "4. 严格只输出一行 JSON，不要解释、不要 Markdown 代码围栏。\n"
    '输出格式：{"tables":[{"name":"表名","desc":"表说明",'
    '"cols":[{"name":"字段名","desc":"字段说明"}]}]}'
)

# SQLite 自己的内部表（sqlite_sequence / sqlite_stat1 …）不作为业务表展示
_INTERNAL_PREFIX = "sqlite_"

# 列定义行里不该被当成「字段名」的关键字（表级约束）
_CONSTRAINT_KEYWORDS = {
    "primary", "unique", "foreign", "check", "constraint", "key", "index",
}

# 说明缓存：key = (库标识, 表名, 该表需要说明的字段元组) → {表说明, {字段: 说明}}
# 只缓存在进程内，避免「重新生成」时对同一批表反复调模型。
_DESC_CACHE = {}


# ---------------------------------------------------------------- 库路径
def default_db_path():
    """表结构提取默认读哪个库：settings.SCHEMA_DB_PATH → settings.DB_PATH。"""
    p = (getattr(settings, "SCHEMA_DB_PATH", "") or "").strip()
    return p or settings.DB_PATH


# ---------------------------------------------------------------- 注释解析
def _strip_quotes(s):
    s = (s or "").strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'", "`", "["):
        return s[1:-1]
    return s


def parse_sql_comments(sql):
    """从建表 SQL 里抠出「本来就是注释」的部分。

    约定（SQLite 没有原生注释，只能写在 DDL 文本里）：
      · 表注释 = `CREATE TABLE x (` 之后、第一条列定义之前的 `-- …` 行（取第一条）；
      · 字段注释 = 列定义**行尾**的 `-- …`。

    返回 (表注释, {字段名: 注释})；抠不到就返回 ("", {})。
    """
    table_desc = ""
    col_desc = {}
    if not sql:
        return table_desc, col_desc

    i = sql.find("(")
    if i < 0:
        return table_desc, col_desc
    body = sql[i + 1:]

    seen_col = False
    for raw in body.splitlines():
        line = raw.strip().rstrip(",").strip()
        if not line:
            continue

        if line.startswith("--"):
            # 表注释：第一条列定义之前的那一行注释
            if not seen_col and not table_desc:
                table_desc = line.lstrip("-").strip()
            continue

        head = line.split()[0]
        name = _strip_quotes(head)
        if name.lower() in _CONSTRAINT_KEYWORDS or not name:
            continue

        seen_col = True
        # 行尾 `-- 注释`（注意排除出现在引号里的情况：这里只按最后一个 -- 切）
        k = line.rfind("--")
        if k > 0:
            col_desc[name] = line[k + 2:].strip().rstrip(",").strip()

    return table_desc, col_desc


# ---------------------------------------------------------------- 读库结构
def _count_rows(cur, table):
    try:
        return cur.execute('SELECT COUNT(*) FROM "%s"' % table.replace('"', '""')).fetchone()[0]
    except sqlite3.Error:
        return 0


def read_sqlite(path, name=None):
    """读一个 SQLite 库的真实结构。

    返回：
        {
          "ok": True, "name": 库文件名, "kind": "SQLite",
          "tables": [
            {"name": "srv", "rows": 17, "desc": "…", "descFrom": "db"|"",
             "cols": [[字段名, 类型关键字, 字段说明], …]}
          ],
          "relations": [[从表, 从字段, 主表, 主字段, "外键关联"], …]
        }

    `类型关键字`：主键列固定给 `"pk"`（原型靠它渲染「主键」标记并把类型映射成 INTEGER），
    其余列给库里声明的**真实类型原文**（原型 `colType()` 认不出来时原样返回）。
    字段说明：库里写了注释就用注释（`descFrom: "db"`），没有就留空等 DeepSeek 补。
    """
    p = os.path.abspath(path)
    if not os.path.isfile(p):
        return {"ok": False, "error": "找不到数据库文件：%s" % p}

    # 只读用途：文件已确认存在，plain connect 不会写库（不执行任何 INSERT/UPDATE/DDL）。
    # 用 URI（?mode=ro）在中文路径下要额外做百分号转义，这里没必要，保持简单。
    try:
        conn = sqlite3.connect(p)
    except sqlite3.Error as exc:
        return {"ok": False, "error": "打不开数据库：%s" % exc}

    try:
        cur = conn.cursor()
        rows = cur.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()

        tables = []
        relations = []
        for tname, tsql in rows:
            if tname.startswith(_INTERNAL_PREFIX):
                continue

            t_desc, c_desc = parse_sql_comments(tsql or "")
            cols = []
            for col in cur.execute('PRAGMA table_info("%s")' % tname.replace('"', '""')):
                _cid, cname, ctype, _notnull, _dflt, pk = col
                keyword = "pk" if pk else (ctype or "")
                note = c_desc.get(cname, "")
                # 第 4 位是「库里声明的真实类型」——原型只读前 3 位，
                # 这一位是给「缺注释时拼模型提示词」用的（主键的关键字是 "pk"，
                # 直接发给模型它看不懂，得给真实类型）。
                cols.append([cname, keyword, note, ctype or ""])

            tables.append({
                "name": tname,
                "rows": _count_rows(cur, tname),
                "desc": t_desc,
                "descFrom": "db" if t_desc else "",
                "cols": cols,
            })

            # 真实外键（PRAGMA 的顺序是倒的，这里不依赖顺序）
            fk_rows = cur.execute('PRAGMA foreign_key_list("%s")' % tname.replace('"', '""'))
            for fk in fk_rows:
                # (id, seq, table, from, to, on_update, on_delete, match)
                relations.append([tname, fk[3] or "", fk[2] or "", fk[4] or "", "外键关联"])

        return {
            "ok": True,
            "name": (name or os.path.basename(p)),
            "kind": "SQLite",
            "tables": tables,
            "relations": relations,
        }
    except sqlite3.Error as exc:
        return {"ok": False, "error": "读表结构失败：%s" % exc}
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------- 统一入口
def read_db(kind, cfg):
    """按「数据库类型」真连一个库并读回结构（第二次迭代 · 第二批）。

    · `kind == "SQLite"` → 走本地文件（`cfg["path"]`，不给就用应用库）；
    · 其余 5 种           → 交给 `core/dbx.py` 用真实驱动连（pymysql / psycopg2 /
                            pymssql / oracledb / ibm_db_dbi）。

    返回形状与 `read_sqlite` 完全一致；**不做任何字段校验**，
    连不上就把驱动的真实报错原样透出。
    """
    if (kind or "").strip() == "SQLite":
        return read_sqlite(cfg.get("path") or default_db_path(), name=cfg.get("name"))
    return dbx.read_db((kind or "").strip(), cfg)


# ---------------------------------------------------------------- 缺注释的
def missing_tables(schema, only_names=None):
    """从库结构里挑出「还需要说明」的表，交给模型。

    · `only_names` 给了就只看这几张表（页面只提取勾选的）；
    · 表说明为空 ⇒ 需要；
    · 字段说明为空 ⇒ 该字段需要；一个字段都不缺且表说明也有 ⇒ 整张表不发。

    只在**需要**的时候把表/字段列出来，所以「库里本来就有注释的」不会被重复生成。
    """
    want = None if only_names is None else set(only_names)
    out = []
    for t in schema.get("tables") or []:
        if want is not None and t.get("name") not in want:
            continue

        need_cols = [c[0] for c in (t.get("cols") or []) if not (c[2] or "").strip()]
        need_desc = not (t.get("desc") or "").strip()
        if not need_cols and not need_desc:
            continue

        out.append({
            "name": t.get("name"),
            "needDesc": need_desc,
            "need": need_cols,
            # 全字段都带上（不给类型的话模型看不出这字段是什么），但只有 need 里的要它写说明。
            # 类型优先用第 4 位（库里的真实声明类型），前端送来的两元素形态没有第 4 位。
            "cols": [[c[0], (c[3] if len(c) > 3 else "") or c[1] or ""] for c in (t.get("cols") or [])],
        })
    return out


# ---------------------------------------------------------------- 调模型
def build_user_prompt(need_tables):
    """把「缺注释」的表拼成给模型的用户消息（纯文本，省 token 也好读）。"""
    lines = []
    for t in need_tables:
        lines.append("表 %s" % t.get("name"))
        for cname, ctype in t.get("cols") or []:
            mark = "（需说明）" if cname in (t.get("need") or []) else ""
            lines.append("  %s %s%s" % (cname, ctype, mark))
        if not t.get("needDesc"):
            lines.append("  （本表说明已有，不用再写）")
    lines.append("")
    lines.append("请只给上面标了「需说明」的字段写说明；"
                 "没有标「本表说明已有」的表，请给表本身也写一句。")
    return "\n".join(lines)


def parse_desc(raw):
    """把模型返回解析成 {"表名": {"desc":…, "cols": {字段: 说明}}}；解析不出来就空。"""
    t = (raw or "").strip()
    if not t:
        return {}
    t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
    t = re.sub(r"\s*```$", "", t).strip()

    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}

    out = {}
    for item in data.get("tables") or []:
        if not isinstance(item, dict):
            continue
        name = (item.get("name") or "").strip()
        if not name:
            continue
        cols = {}
        for c in item.get("cols") or []:
            if isinstance(c, dict) and (c.get("name") or "").strip():
                cols[c["name"].strip()] = (c.get("desc") or "").strip()
        out[name] = {"desc": (item.get("desc") or "").strip(), "cols": cols}
    return out


def describe_need(need, db_tag=""):
    """拿着「还缺说明」的表去调一次 DeepSeek。

    `need` 的形状同 `missing_tables()` 的元素：
        {"name", "needDesc", "need": [字段名], "cols": [[字段名, 类型]]}

    返回 {"ok": True, "tables": [{"name","desc","cols":[{"name","desc"}]}], "model": …}
    或   {"ok": False, "error": …}（页面遇到失败只是「说明为空」，提取照常）。
    """
    if not need:
        return {"ok": True, "tables": [], "model": settings.DEEPSEEK_MODEL,
                "skipped": "没有被选中的表"}

    api_key = (getattr(settings, "DEEPSEEK_API_KEY", "") or "").strip()
    if not api_key:
        return {"ok": False, "error": "未在 config/settings.py 里配置 DEEPSEEK_API_KEY"}

    cache_key = (db_tag, tuple(
        (t["name"], bool(t.get("needDesc")), tuple(t.get("need") or [])) for t in need
    ))
    if cache_key in _DESC_CACHE:
        return {"ok": True, "tables": _DESC_CACHE[cache_key],
                "model": settings.DEEPSEEK_MODEL, "cached": True}

    messages = [
        {"role": "system", "content": DESC_SYSTEM},
        {"role": "user", "content": build_user_prompt(need)},
    ]
    res = call_model(settings.DEEPSEEK_BASE_URL, api_key, settings.DEEPSEEK_MODEL,
                     messages, timeout=DESC_TIMEOUT)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error") or "调用说明生成模型失败"}

    parsed = parse_desc(res.get("text") or "")
    if not parsed:
        return {"ok": False, "error": "说明生成模型没返回可解析的 JSON",
                "raw": (res.get("text") or "")[:400]}

    out = []
    for t in need:
        one = parsed.get(t["name"]) or {}
        cols = [{"name": cname, "desc": (one.get("cols") or {}).get(cname, "")}
                for cname in (t.get("need") or [])]
        out.append({
            "name": t["name"],
            "desc": one.get("desc", "") if t.get("needDesc") else "",
            "cols": cols,
        })

    _DESC_CACHE[cache_key] = out
    return {"ok": True, "tables": out, "model": settings.DEEPSEEK_MODEL}


def describe_tables(schema, only_names=None):
    """按库结构算出「缺注释」的表，再交给 describe_need() 调 DeepSeek。"""
    need = missing_tables(schema, only_names)
    if not need:
        # 全都有注释 ⇒ 不用调模型，这本身就是正确结果
        return {"ok": True, "tables": [], "model": settings.DEEPSEEK_MODEL,
                "skipped": "所有表 / 字段在库里都已有注释"}
    return describe_need(need, db_tag=schema.get("name") or "")
