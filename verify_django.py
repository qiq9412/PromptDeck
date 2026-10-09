# -*- coding: utf-8 -*-
"""PromptDeck（Django 版）· 接口端到端校验（第四次迭代：新增提示词测试页两块接口）。
用法：
    1) 先起服务：python manage.py runserver 127.0.0.1:8502
    2) 再跑本脚本：python verify_django.py [--base http://127.0.0.1:8502]

覆盖至今的四件事：
    · 原型页能被 Django 托管（prompt.html / prompt_scheme.html / prompt_test.html）；
    · /api/tp 的 GET / POST / DELETE 行为与原型保存逻辑一致
      （编辑流按 id 覆盖 → 同名覆盖 → 新增）；
    · 落库核对：断言以「库里的行」为准，不看页面（本项目踩过的坑）；
      **本次换库后核对的是新库的 tst_plan + tst_plan_prompt + tst_api_key 三张表**；
    · 新库表结构的 4 条拍板口径（加列 / CASCADE / 单价 TEXT / 旧 tp 表已废）；
    · 上一版已有的 /api/srv、/api/mdl、/api/chat 换库后仍可用；
    · base_url 查重（10-09 用户点名）—— POST 撞已有网址回 409（归一化：忽略大小写 / 末尾斜杠），
      PUT 排除自己那条（否则改名称也会被自己判重）；用例自清理，收尾 srv 回基线。

第二次迭代补的两块（「提示词测试」页）：
    · /api/judge —— 判定密钥必须落 tst_api_key（plan_id 为 NULL）、且不写死在源码里；
      任一侧为空 -> 未判定；真调 DeepSeek 一次，语义一致 -> right / 不一致 -> wrong
      （真调失败多为网络原因，记 WARN 不算失败）；
    · /api/rec —— 一次「保存结果」落 tst_rec（一条汇总，四个数字由明细现算）
      + tst_rec_dtl（逐题明细），删除时明细一并清、汇总回到基线。

第二次迭代再补一块（「数据库表结构提取」页）：
    · /api/schema —— GET 读应用库真实结构（表 / 字段 / 行数 / 外键，全部现场算基线，
      不写死表名与条数）；POST 走 multipart 上传 .sqlite，临时打开后读结构；
    · 注释口径 —— 库里写在建表 SQL 里的 `-- 注释` 直接读出来（descFrom=db），
      没有注释的一律留空，交给 /api/schema/desc；
    · /api/schema/desc —— **只**给「缺注释」的表 / 字段调 DeepSeek 补说明，
      真调一次并断言「送几张表就回几张表、每个送去的字段都有说明」。

接口出入参口径没变，所以脚本里对 /api/tp 的调用方式与上一版完全一致 ——
变的只有「落库核对」那一段读的表，外加 3.5 / 4 / 5 三块新接口。
"""
import argparse
import importlib
import json
import os
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("PROMPTDECK_DB") or os.path.join(BASE_DIR, "data", "prompt_test_DB_twe.db")

# 第 5 节要直接打 core/schema.py 做注释解析 / 「只补缺注释」的单测，
# 所以这里把 Django 配置装起来（本脚本本来就是从 promptdeck/ 目录里跑的）。
sys.path.insert(0, BASE_DIR)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django                                            # noqa: E402

django.setup()
from core import schema                                  # noqa: E402

# 本次校验会往库里写这两条基础数据（供应商 / 模型），跑完会清理掉
E2E_SRV = "E2E 供应商"
E2E_MDL = "e2e-model"

FAILS = []


def chk(label, ok, detail=""):
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", label, ("  " + str(detail)) if detail else ""))
    if not ok:
        FAILS.append(label)


def warn(label, detail=""):
    """环境相关、不作为失败的提示（例如真调 DeepSeek 时网络不通）。"""
    print("  [WARN] %s%s" % (label, ("  " + str(detail)) if detail else ""))


def req(method, url, body=None, timeout=20):
    """⚠️ timeout 默认 20s 只够本地接口；**凡是要穿透到真模型的那几条，调用方必须放大**
    （服务端自己对说明生成留了 120s，客户端比它短就会先超时 —— 踩过一次）。"""
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, raw
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def jreq(method, url, body=None, timeout=20):
    code, raw = req(method, url, body, timeout=timeout)
    try:
        return code, json.loads(raw)
    except ValueError:
        return code, raw


def multipart_post(url, field, filename, content, timeout=30):
    """发一个 multipart/form-data（上传 .sqlite 用），返回 (状态码, 解析后的体)。"""
    boundary = "----pdverify%d" % os.getpid()
    body = b""
    body += ("--%s\r\n" % boundary).encode()
    body += ('Content-Disposition: form-data; name="%s"; filename="%s"\r\n'
             % (field, filename)).encode("utf-8")
    body += b"Content-Type: application/octet-stream\r\n\r\n"
    body += content
    body += ("\r\n--%s--\r\n" % boundary).encode()

    r = urllib.request.Request(url, data=body, method="POST")
    r.add_header("Content-Type", "multipart/form-data; boundary=%s" % boundary)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


# ---------------------------------------------------------------- 库（新库三张表）
def db_count(table):
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]


def db_has_table(name):
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = ?",
                            (name,)).fetchone()[0] > 0


def db_count_like(table, column, pattern):
    """按 LIKE 计数（表名 / 列名由调用方写死，pattern 走参数绑定）。"""
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute("SELECT COUNT(*) FROM %s WHERE %s LIKE ?" % (table, column),
                            (pattern,)).fetchone()[0]


def db_plan(plan_id):
    """把一个方案的完整落库形态从新库三张表拼出来。

    返回 dict 里既有 tst_plan 的原生列（plan_name / base_url / srv_id / mdl_id），
    也有拼出来的对照字段（srv_name / mdl_name / sys_prompt / user_prompt / api_key），
    这样断言可以跟「原型方案对象」逐项对得上。
    """
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        p = conn.execute("SELECT * FROM tst_plan WHERE id = ?", (plan_id,)).fetchone()
        if not p:
            return None
        out = dict(p)

        s = conn.execute("SELECT plat_name FROM srv WHERE id = ?", (p["srv_id"],)).fetchone()
        m = conn.execute("SELECT mdl_name, mdl_type FROM mdl WHERE id = ?", (p["mdl_id"],)).fetchone()
        out["srv_name"] = s["plat_name"] if s else None
        out["mdl_name"] = m["mdl_name"] if m else None
        out["mdl_type"] = m["mdl_type"] if m else None

        rows = conn.execute(
            "SELECT seq_no, prompt_role, prompt_content FROM tst_plan_prompt"
            " WHERE plan_id = ? ORDER BY seq_no", (plan_id,)).fetchall()
        out["sys_prompt"] = ""
        out["user_prompt"] = []
        for r in rows:
            role = (r["prompt_role"] or "").lower()
            if role == "system":
                out["sys_prompt"] = r["prompt_content"]
            elif role == "user":
                out["user_prompt"].append(r["prompt_content"])
        out["multi_turn"] = 1 if len(out["user_prompt"]) > 1 else 0

        k = conn.execute("SELECT api_key FROM tst_api_key WHERE plan_id = ? AND is_active = 1"
                         " ORDER BY id LIMIT 1", (plan_id,)).fetchone()
        out["api_key"] = k["api_key"] if k else None

        # 子表残留计数（核对删除时的级联）
        out["_prompt_rows"] = len(rows)
        out["_key_rows"] = conn.execute("SELECT COUNT(*) FROM tst_api_key WHERE plan_id = ?",
                                        (plan_id,)).fetchone()[0]
        return out


def child_rows_of(ids):
    """给定方案 id 集合，返回两张子表里属于它们的行数（删除后应为 0）。"""
    if not ids:
        return 0, 0
    marks = ",".join("?" * len(ids))
    with sqlite3.connect(DB_PATH) as conn:
        a = conn.execute("SELECT COUNT(*) FROM tst_plan_prompt WHERE plan_id IN (%s)" % marks,
                         list(ids)).fetchone()[0]
        b = conn.execute("SELECT COUNT(*) FROM tst_api_key WHERE plan_id IN (%s)" % marks,
                         list(ids)).fetchone()[0]
    return a, b


def plan_key_pk(plan_id):
    """取方案自己那把密钥的主键（判定密钥 plan_id 为 NULL，不会被取到）。"""
    with sqlite3.connect(DB_PATH) as conn:
        r = conn.execute("SELECT id FROM tst_api_key WHERE plan_id = ? AND is_active = 1"
                         " ORDER BY id LIMIT 1", (plan_id,)).fetchone()
    return r[0] if r else None


def db_rec(rec_id):
    """把一条测试记录 + 它的逐题明细从库里读出来，供落库断言逐项对照。"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        r = conn.execute("SELECT * FROM tst_rec WHERE id = ?", (rec_id,)).fetchone()
        if not r:
            return None
        out = dict(r)
        out["details"] = [dict(x) for x in conn.execute(
            "SELECT * FROM tst_rec_dtl WHERE rec_id = ? ORDER BY id", (rec_id,))]
        return out


def cleanup():
    """清掉本次校验写进去的 E2E 供应商（连带它的模型）+ base_url 查重用例。按名字精确删，幂等。"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("DELETE FROM srv WHERE plat_name = ?", (E2E_SRV,))
        conn.execute("DELETE FROM srv WHERE plat_name LIKE 'E2E 查重%'")
        conn.commit()


# ---------------------------------------------------------------- 桩驱动（第 5.9 节用）
# 本机没有任何 MySQL / PG / SQL Server / Oracle / Db2 服务，但**驱动是真的**。
# 于是：把一个假的驱动模块塞进 sys.modules 顶掉真驱动，让 core/dbx.py 照常
# `import` + `connect()` + 跑它那套方言反射 SQL；假游标按「SQL 里出现的特征串」
# 返回该方言真实的目录表形状 ⇒ 可以逐条断言「读到什么、怎么转换」。
class _FakeCursor(object):
    def __init__(self, rules):
        self.rules = rules
        self.sqls = []
        self._rows = []

    def execute(self, sql, params=None):
        self.sqls.append((sql, params))
        self._rows = []
        for key, rows in self.rules:
            if key in sql:
                self._rows = rows(params) if callable(rows) else rows
                break
        return self

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def close(self):
        pass


class _FakeConn(object):
    def __init__(self, rules, rec):
        self._rules = rules
        self._rec = rec

    def cursor(self, *a, **kw):
        self._rec.setdefault("cursor_kw", []).append(kw)
        self._cur = _FakeCursor(self._rules)
        return self._cur

    def set_session(self, **kw):
        self._rec.setdefault("set_session", []).append(kw)

    def close(self):
        pass


def stub_driver(pkg, rules, rec):
    """造一个只覆盖 connect() 的假驱动模块。"""
    import types
    mod = types.ModuleType(pkg)
    mod.__version__ = "stub"
    mod.makedsn = lambda *a, **kw: "stub-dsn"

    def connect(*a, **kw):
        rec["call"] = {"args": a, "kwargs": kw}
        return _FakeConn(rules, rec)

    mod.connect = connect
    return mod


class patched_module(object):
    """临时把 sys.modules 里的某个包换成假的（出栈时还原）。"""

    def __init__(self, name, fake):
        self.name = name
        self.fake = fake

    def __enter__(self):
        self.old = sys.modules.get(self.name)
        sys.modules[self.name] = self.fake
        return self.fake

    def __exit__(self, *exc):
        if self.old is None:
            sys.modules.pop(self.name, None)
        else:
            sys.modules[self.name] = self.old
        return False


# 各类型的桩目录表：SQL 特征串 -> 该方言真实的返回形状
STUB_RULES = {
    "MySQL": [
        ("SELECT DATABASE()", [("shop",)]),
        ("information_schema.TABLES", [("orders", "订单主表"), ("users", "用户主表")]),
        ("KEY_COLUMN_USAGE", [("orders", "user_id", "users", "id")]),
        ("information_schema.COLUMNS", lambda p: (
            [("id", "int", "PRI", "主键"), ("user_id", "int", "MUL", "下单用户")]
            if p[1] == "orders" else
            [("id", "bigint", "PRI", "主键"), ("name", "varchar(64)", "", "用户名")])),
        ("COUNT(*)", [(7,)]),
    ],
    "PostgreSQL": [
        ("current_schema()", [("public",)]),
        ("c.relkind IN", [("orders", "订单主表"), ("users", "用户主表")]),
        ("generate_subscripts", [("orders", "user_id", "users", "id")]),
        ("pg_index i", [("id",)]),
        ("format_type(", lambda p: (
            [("id", "integer", "主键"), ("user_id", "integer", "下单用户")]
            if "orders" in str(p[0]) else
            [("id", "bigint", "主键"), ("name", "character varying(64)", "用户名")])),
        ("COUNT(*)", [(7,)]),
    ],
    "SQL Server": [
        ("FROM sys.tables t", [("dbo", "orders", "订单主表"), ("dbo", "users", "用户主表")]),
        ("sys.foreign_key_columns",
         [("dbo", "orders", "user_id", "dbo", "users", "id")]),
        ("sys.indexes i", [("id",)]),
        ("FROM sys.columns c", lambda p: (
            [("id", "int", 4, 10, 0, "主键"), ("user_id", "int", 4, 10, 0, "下单用户")]
            if "orders" in str(p[0]) else
            [("id", "bigint", 8, 19, 0, "主键"), ("name", "varchar", 50, 0, 0, "用户名")])),
        ("COUNT(*)", [(7,)]),
    ],
    "Oracle": [
        ("USERENV", [("APP",)]),
        ("ALL_TAB_COMMENTS", [("ORDERS", "订单主表"), ("USERS", "用户主表")]),
        ("CONSTRAINT_TYPE = 'R'", [("ORDERS", "USER_ID", "USERS", "ID")]),
        ("CONSTRAINT_TYPE = 'P'", [("ID",)]),
        ("ALL_TAB_COLUMNS", lambda p: (
            [("ID", "NUMBER", 22, 0, 10, 0, "主键"), ("USER_ID", "NUMBER", 22, 0, 10, 0, "下单用户")]
            if p[1] == "ORDERS" else
            [("ID", "NUMBER", 22, 0, 10, 0, "主键"), ("NAME", "VARCHAR2", 64, 64, None, None, "用户名")])),
        ("COUNT(*)", [(7,)]),
    ],
    "Db2 for LUW": [
        ("VALUES CURRENT SCHEMA", [("DB2INST1",)]),
        ("SYSCAT.TABLES", [("ORDERS", "订单主表"), ("USERS", "用户主表")]),
        ("SYSCAT.REFERENCES", [("ORDERS", "USER_ID", "USERS", "ID")]),
        ("TYPE = 'P'", [("ID",)]),
        ("SYSCAT.COLUMNS", lambda p: (
            [("ID", "INTEGER", 4, 0, "主键"), ("USER_ID", "INTEGER", 4, 0, "下单用户")]
            if p[1] == "ORDERS" else
            [("ID", "BIGINT", 8, 0, "主键"), ("NAME", "VARCHAR", 64, 0, "用户名")])),
        ("COUNT(*)", [(7,)]),
    ],
}

# 类型 -> (驱动包名, 桩库里应读到的表名, 外键两端的大小写照法)
STUB_CASES = [
    ("MySQL", "pymysql", ["orders", "users"]),
    ("PostgreSQL", "psycopg2", ["orders", "users"]),
    ("SQL Server", "pymssql", ["orders", "users"]),
    ("Oracle", "oracledb", ["ORDERS", "USERS"]),
    ("Db2 for LUW", "ibm_db_dbi", ["ORDERS", "USERS"]),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8502")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    print("=" * 74)
    print("PromptDeck（Django 版）· 接口端到端校验  base=%s" % base)
    print("库文件：%s" % DB_PATH)
    print("=" * 74)

    # ---------------------------------------------------------- 0. 换库口径
    print("\n■ 0. 本次换库的 4 条拍板口径（直接查库结构）")
    chk("库文件已指向 prompt_test_DB_twe.db", DB_PATH.endswith("prompt_test_DB_twe.db"), DB_PATH)
    chk("旧 tp 表在新库已不存在", not db_has_table("tp"))

    with sqlite3.connect(DB_PATH) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(tst_plan)")]
    chk("tst_plan 已加 plan_name / base_url 两列",
        "plan_name" in cols and "base_url" in cols, "现有列 %s" % cols)

    with sqlite3.connect(DB_PATH) as conn:
        fk = [r for r in conn.execute("PRAGMA foreign_key_list(mdl)")]
    chk("mdl.srv_id 外键已改成 ON DELETE CASCADE",
        bool(fk) and (fk[0][6] or "").upper() == "CASCADE",
        "on_delete=%s" % (fk[0][6] if fk else None))

    with sqlite3.connect(DB_PATH) as conn:
        t = conn.execute("SELECT typeof(in_price) FROM mdl"
                         " WHERE in_price IS NOT NULL LIMIT 1").fetchone()
    chk("mdl 计费单价按 TEXT 存（原文保留）", bool(t) and t[0] == "text", "typeof=%s" % (t[0] if t else None))

    with sqlite3.connect(DB_PATH) as conn:
        sample = conn.execute("SELECT in_price FROM mdl WHERE in_price LIKE '%.00' LIMIT 1").fetchone()
    chk("单价小数位未被规范化（18.00 不会变 18）",
        bool(sample) and str(sample[0]).endswith(".00"), "样例=%s" % (sample[0] if sample else None))

    # ---------------------------------------------------------- 1. 页面托管
    print("\n■ 1. 原型页托管")
    for page in ("prompt.html", "prompt_scheme.html", "list_server.html",
                 "foundation1_models.html", "prompt_test.html"):
        code, raw = req("GET", "%s/%s" % (base, page))
        chk("GET /%s -> 200 且有内容" % page, code == 200 and len(raw) > 5000,
            "HTTP %s / %d bytes" % (code, len(raw)))

    code, raw = req("GET", "%s/prompt_scheme.html" % base)
    chk("prompt_scheme.html 已接后端（含 /api/tp 拉取）", 'fetch("/api/tp")' in raw)
    code, raw = req("GET", "%s/prompt.html" % base)
    chk("prompt.html 保存已接后端（含 POST /api/tp）", 'fetch("/api/tp"' in raw)
    chk("prompt.html 保存后跳提示词方案列表页",
        'window.location.href = "prompt_scheme.html"' in raw)

    code, _ = req("GET", "%s/no_such_page.html" % base)
    chk("未在白名单里的页面 -> 404", code == 404, "HTTP %s" % code)

    code, _ = req("GET", "%s/" % base)
    chk("根路径可达（跳 prompt.html）", code in (200, 301, 302), "HTTP %s" % code)

    # ---------------------------------------------------------- 2. 既有接口
    print("\n■ 2. 上一版接口换库后仍可用")
    # 基础数据基线：本次校验会临时补一条 E2E 供应商 / 模型，收尾清理后必须回到这个数。
    # 特意在「任何写入之前」取运行时快照，不写死条数 —— 基础数据本来就会随业务增减，
    # 硬编码具体数字的断言会在换库 / 增删数据后变成假失败（本项目踩过）。
    srv0, mdl0 = db_count("srv"), db_count("mdl")
    code, srv = jreq("GET", "%s/api/srv" % base)
    chk("GET /api/srv 返回列表", code == 200 and isinstance(srv, list) and len(srv) > 0,
        "%d 条" % (len(srv) if isinstance(srv, list) else -1))
    code, mdl = jreq("GET", "%s/api/mdl" % base)
    chk("GET /api/mdl 返回列表", code == 200 and isinstance(mdl, list) and len(mdl) > 0,
        "%d 条" % (len(mdl) if isinstance(mdl, list) else -1))
    if isinstance(mdl, list) and mdl:
        chk("mdl 行含联表字段 platform / cUrl", "platform" in mdl[0] and "cUrl" in mdl[0])
        chk("单价经接口出来仍是原文字符串",
            any(str(r.get("inPrice", "")).endswith(".00") for r in mdl),
            "样例 %s" % [(r.get("model"), r.get("inPrice")) for r in mdl[:2]])

    # ---------------------------------------------------------- 2.1 base_url 查重
    # 10-09 用户点名：「服务器 API 网址（base_url）不可重复」，前端提示 + 后端兜底。
    # 这里只打后端那一层；前端那一层在 verify_ui.py [19] 里验。
    print("\n■ 2.1 /api/srv 的 base_url 查重（10-09 用户点名）")
    dup_stamp = str(int(time.time()))[-6:]
    dup_a = "https://e2e-dup-%s.example.com/v1" % dup_stamp
    dup_b = "https://e2e-dup-%s-b.example.com/v1" % dup_stamp
    n_srv = db_count("srv")

    code, ra = jreq("POST", "%s/api/srv" % base,
                    {"plat_name": "E2E 查重A-%s" % dup_stamp, "base_url": dup_a})
    id_a = ra.get("id") if isinstance(ra, dict) else None
    chk("POST 新增（网址没被占用）-> 201", code == 201 and ra.get("ok"), "HTTP %s / %s" % (code, ra))
    code, rb = jreq("POST", "%s/api/srv" % base,
                    {"plat_name": "E2E 查重B-%s" % dup_stamp, "base_url": dup_b})
    id_b = rb.get("id") if isinstance(rb, dict) else None
    chk("POST 新增第二条（另一个网址）-> 201", code == 201 and rb.get("ok"), "HTTP %s / %s" % (code, rb))

    # 撞网址：只差**大小写 + 末尾斜杠**，归一化后是同一条 —— 这正是选定的判定口径
    code, dup = jreq("POST", "%s/api/srv" % base,
                     {"plat_name": "E2E 查重C-%s" % dup_stamp, "base_url": dup_a.upper() + "/"})
    chk("POST 撞已有网址（仅大小写 / 末尾斜杠不同）-> 409",
        code == 409 and isinstance(dup, dict) and dup.get("error") == "已存在服务器API网址",
        "HTTP %s / %s" % (code, dup))
    chk("409 里带回了撞上的那条（dupId / dupUrl）",
        isinstance(dup, dict) and dup.get("dupId") == id_a and dup.get("dupUrl") == dup_a, dup)
    chk("被拦下的那次没有落库（srv 恰好 +2）", db_count("srv") == n_srv + 2, "库 %d" % db_count("srv"))
    chk("被拦下的那条也进了库才算漏 —— 确认没写进去",
        not db_count_like("srv", "plat_name", "E2E 查重C-" + dup_stamp + "%"),
        "E2E 查重C 不应存在")

    # 修改：不排除自己那条的话，改别的字段也会被自己判成重复
    code, r = jreq("PUT", "%s/api/srv" % base,
                   {"id": id_a, "plat_name": "E2E 查重A改-%s" % dup_stamp,
                    "base_url": dup_a.upper() + "/", "comp_name": "", "home_url": ""})
    chk("PUT 保留自己的网址（只是大小写 / 末尾斜杠不同）-> 200（排除自己那条）",
        code == 200 and r.get("ok"), "HTTP %s / %s" % (code, r))
    code, r = jreq("PUT", "%s/api/srv" % base,
                   {"id": id_a, "plat_name": "E2E 查重A改-%s" % dup_stamp,
                    "base_url": dup_b, "comp_name": "", "home_url": ""})
    chk("PUT 改成别人占着的网址 -> 409",
        code == 409 and r.get("error") == "已存在服务器API网址", "HTTP %s / %s" % (code, r))

    jreq("DELETE", "%s/api/srv" % base, {"ids": [id_a, id_b]})
    chk("清掉本次查重用例 -> srv 回到跑之前（自清理）",
        db_count("srv") == n_srv, "库 %d" % db_count("srv"))

    # ---------------------------------------------------------- 3. /api/tp CRUD
    print("\n■ 3. /api/tp（提示词方案 · 落 tst_plan 三张表）")
    code, lst = jreq("GET", "%s/api/tp" % base)
    chk("GET /api/tp 返回列表", code == 200 and isinstance(lst, list),
        "%d 条" % (len(lst) if isinstance(lst, list) else -1))
    n0 = len(lst) if isinstance(lst, list) else 0
    chk("接口条数与库内 tst_plan 行数一致", n0 == db_count("tst_plan"),
        "接口 %d / 库 %d" % (n0, db_count("tst_plan")))
    if lst:
        keys = set(lst[0].keys())
        need = {"id", "name", "server", "base", "model", "modelType", "sys", "user", "multi", "apiKey"}
        chk("列表字段与原型方案对象一致（前端零改动）", need.issubset(keys), "缺 %s" % sorted(need - keys))
        chk("user 已还原成数组（不是 JSON 字符串）", isinstance(lst[0].get("user"), list))
        chk("从三张表还原出了方案名与供应商",
            bool(lst[0].get("name")) and bool(lst[0].get("server")),
            "%s / %s" % (lst[0].get("name"), lst[0].get("server")))
        multi_rows = [r for r in lst if r.get("multi")]
        # ⚠️ 开发库是用户自己在用的，多轮方案**可能一条都没有**（基线一律现场取，不写死）。
        #    有就逐条验「user 确实多于 1 条」；一条都没有就只提示现场情况，不判失败。
        #    （本用例自己新建的多轮方案另有 3.1 那条「多轮由 user 条数还原」在钉。）
        if multi_rows:
            chk("多轮方案（user 多于 1 条）能从提示词表还原成 multi=true",
                all(len(r["user"]) > 1 for r in multi_rows),
                "多轮方案 %d 条：%s" % (len(multi_rows), [r["name"] for r in multi_rows]))
        else:
            chk("库里当前没有多轮方案（跳过该项，非失败）", True,
                "tst_plan 共 %d 条，均为单轮" % len(lst))

    # 3.1 新增（不带 id，库里没有同名）
    payload = {
        "name": "E2E 自动化校验方案",
        "server": E2E_SRV,
        "base": "https://e2e.example.com/v1",
        "model": E2E_MDL,
        "modelType": "文本生成",
        "sys": "你是 E2E 校验用例。",
        "user": ["第一轮问题", "第二轮追问"],
        "multi": True,
        "apiKey": "sk-e2e-1234567890abcd",
    }
    code, res = jreq("POST", "%s/api/tp" % base, payload)
    new_id = res.get("id") if isinstance(res, dict) else None
    chk("POST 新增 -> ok/created", code == 201 and res.get("ok") and res.get("result") == "created",
        "HTTP %s / %s" % (code, res))
    chk("接口条数 +1", len(jreq("GET", "%s/api/tp" % base)[1]) == n0 + 1)
    chk("库内 tst_plan 行数 +1", db_count("tst_plan") == n0 + 1, "库 %d" % db_count("tst_plan"))

    row = db_plan(new_id)
    chk("首行落 tst_plan：方案名 / 请求地址 / 外键齐全",
        bool(row) and row["plan_name"] == payload["name"] and row["base_url"] == payload["base"]
        and row["srv_id"] and row["mdl_id"],
        "库内：%s" % ({k: row[k] for k in ("plan_name", "base_url", "srv_id", "mdl_id")} if row else None))
    chk("供应商 / 模型按名字换成了外键并查得回名字",
        bool(row) and row["srv_name"] == payload["server"] and row["mdl_name"] == payload["model"]
        and row["mdl_type"] == payload["modelType"],
        "%s / %s" % (row["srv_name"], row["mdl_name"]) if row else None)
    chk("系统提示词落 tst_plan_prompt（seq_no=0, role=system）",
        bool(row) and row["sys_prompt"] == payload["sys"])
    chk("用户提示词逐轮落 tst_plan_prompt（seq_no=1..N, role=user）",
        bool(row) and row["user_prompt"] == payload["user"],
        "%s" % (row["user_prompt"] if row else None))
    chk("提示词明细行数 = 1 条系统 + 2 条用户",
        bool(row) and row["_prompt_rows"] == 3, "实际 %s 行" % (row["_prompt_rows"] if row else None))
    chk("API_key 落 tst_api_key（一把「默认」）",
        bool(row) and row["api_key"] == payload["apiKey"] and row["_key_rows"] == 1)
    chk("多轮由 user 条数还原（multi_turn 不单独落库）",
        bool(row) and row["multi_turn"] == 1)

    # 3.2 同名覆盖（不带 id）
    again = dict(payload, sys="你是 E2E 校验用例（改过的系统提示词）。")
    code, res = jreq("POST", "%s/api/tp" % base, again)
    chk("POST 同名 -> overwritten 且沿用原 id",
        res.get("ok") and res.get("result") == "overwritten" and res.get("id") == new_id,
        "%s" % res)
    chk("同名覆盖后条数不变", db_count("tst_plan") == n0 + 1)
    chk("同名覆盖后库内已更新", db_plan(new_id)["sys_prompt"] == again["sys"])
    chk("覆盖时提示词明细被重写、没有堆积（仍是 3 行）",
        db_plan(new_id)["_prompt_rows"] == 3, "实际 %s 行" % db_plan(new_id)["_prompt_rows"])

    # 3.3 编辑流按 id 精确覆盖（改名也认）
    edited = dict(payload, id=new_id, name="E2E 自动化校验方案（改名）")
    code, res = jreq("POST", "%s/api/tp" % base, edited)
    chk("POST 带 id -> updated（改名也按 id 覆盖）",
        res.get("ok") and res.get("result") == "updated" and res.get("id") == new_id, "%s" % res)
    chk("改名生效且条数不变",
        db_plan(new_id)["plan_name"] == edited["name"] and db_count("tst_plan") == n0 + 1)

    # 3.4 空方案名（原型已在前端拦住，服务端只兜脏请求，不复制前端校验）
    code, res = jreq("POST", "%s/api/tp" % base, dict(payload, name="   "))
    chk("方案名为空 -> 400（不落库）", code == 400 and db_count("tst_plan") == n0 + 1,
        "HTTP %s / %s" % (code, res))

    # 3.5 测试记录（/api/rec → tst_rec + tst_rec_dtl）
    #     对应原型「提示词测试」页的「保存结果」：一条汇总 + 逐题明细。
    print("\n■ 3.5 测试记录（/api/rec 落 tst_rec / tst_rec_dtl）")
    rec0, dtl0 = db_count("tst_rec"), db_count("tst_rec_dtl")
    rec_details = [
        {"q": "水的化学式是什么？", "ans": "H2O", "out": "H₂O（两个氢一个氧）", "ai": "right", "judge": ""},
        {"q": "中国首都是哪座城市？", "ans": "北京", "out": "上海", "ai": "wrong", "judge": "right"},
        {"q": "随便说句话", "ans": "", "out": "好的", "ai": "", "judge": ""},
    ]
    code, res = jreq("POST", "%s/api/rec" % base, {"planId": new_id, "details": rec_details})
    rec_id = res.get("id") if isinstance(res, dict) else None
    smary = res.get("summary") if isinstance(res, dict) else None
    chk("POST /api/rec -> 201 且返回 id / summary",
        code == 201 and res.get("ok") and bool(rec_id), "HTTP %s / %s" % (code, res))
    chk("汇总四数由明细现算：题数 3 / 错题 1 / 矫正 1 / 准确率 66.67",
        smary == {"planId": new_id, "qCnt": 3, "accRate": 66.67, "errCnt": 1, "fixCnt": 1},
        "%s" % smary)
    chk("库内 tst_rec +1、tst_rec_dtl +3",
        db_count("tst_rec") == rec0 + 1 and db_count("tst_rec_dtl") == dtl0 + 3,
        "rec %d->%d dtl %d->%d" % (rec0, db_count("tst_rec"), dtl0, db_count("tst_rec_dtl")))

    row = db_rec(rec_id)
    chk("汇总行挂在被调试的方案上，并指向该方案自己的密钥",
        bool(row) and row["plan_id"] == new_id and row["api_key_id"] == plan_key_pk(new_id),
        "plan_id=%s api_key_id=%s" % (row["plan_id"] if row else None,
                                      row["api_key_id"] if row else None))
    chk("汇总行四个数字与明细同源（q_cnt/err_cnt/fix_cnt/acc_rate）",
        bool(row) and (row["q_cnt"], row["err_cnt"], row["fix_cnt"], float(row["acc_rate"]))
        == (3, 1, 1, 66.67),
        "%s" % ({k: row[k] for k in ("q_cnt", "err_cnt", "fix_cnt", "acc_rate")} if row else None))
    d = row["details"] if row else []
    chk("逐题明细 3 行，问题 / 标准答案 / 模型答案原样落库",
        len(d) == 3 and d[0]["qst"] == rec_details[0]["q"] and d[0]["std_ans"] == "H2O"
        and d[0]["mdl_out"] == rec_details[0]["out"], "%d 行" % len(d))
    chk("AI 判定归一落库（right / wrong / 未判定 -> NULL）",
        bool(row) and [x["ai_res"] for x in d] == ["right", "wrong", None],
        "%s" % ([x["ai_res"] for x in d] if row else None))
    chk("人工改判只在「AI 结论与人工结论不一致」那题置 1",
        bool(row) and [x["is_man_fix"] for x in d] == [0, 1, 0],
        "%s" % ([x["is_man_fix"] for x in d] if row else None))

    code, lst = jreq("GET", "%s/api/rec" % base)
    got = [r for r in lst if r.get("id") == rec_id] if isinstance(lst, list) else []
    chk("GET /api/rec 能读回该记录（带方案名与逐题明细）",
        bool(got) and got[0]["planName"] == edited["name"] and len(got[0]["details"]) == 3,
        "planName=%s / %d 条明细" % (got[0]["planName"] if got else None,
                                     len(got[0]["details"]) if got else -1))

    code, _ = jreq("POST", "%s/api/rec" % base, {"details": rec_details})
    chk("缺 planId -> 400（不落库）", code == 400 and db_count("tst_rec") == rec0 + 1, "HTTP %s" % code)
    code, _ = jreq("POST", "%s/api/rec" % base, {"planId": "abc", "details": rec_details})
    chk("planId 非数字 -> 400", code == 400 and db_count("tst_rec") == rec0 + 1, "HTTP %s" % code)

    code, res = jreq("DELETE", "%s/api/rec" % base, {"ids": [rec_id]})
    chk("DELETE /api/rec 删除 -> ok / 1", res.get("ok") and res.get("deleted") == 1, "%s" % res)
    chk("删除后 tst_rec / tst_rec_dtl 双双回到基线（明细无残留）",
        db_count("tst_rec") == rec0 and db_count("tst_rec_dtl") == dtl0,
        "rec %d dtl %d" % (db_count("tst_rec"), db_count("tst_rec_dtl")))
    code, _ = jreq("PUT", "%s/api/rec" % base, {})
    chk("不支持的方法 -> 405", code == 405, "HTTP %s" % code)

    # 3.6 删除（连带清两张子表）
    pr0, ky0 = child_rows_of([new_id])
    chk("删除前该方案在两张子表里都有行", pr0 == 3 and ky0 == 1, "prompt=%s key=%s" % (pr0, ky0))
    code, res = jreq("DELETE", "%s/api/tp" % base, {"ids": [new_id]})
    chk("DELETE 删除 -> ok", res.get("ok") and res.get("deleted") == 1, "%s" % res)
    chk("删除后主表回到基线", db_count("tst_plan") == n0 and db_plan(new_id) is None)
    pr1, ky1 = child_rows_of([new_id])
    chk("删除后提示词 / 密钥两张子表无残留（级联生效）", pr1 == 0 and ky1 == 0,
        "prompt=%s key=%s" % (pr1, ky1))

    code, _ = jreq("PUT", "%s/api/tp" % base, {})
    chk("不支持的方法 -> 405", code == 405, "HTTP %s" % code)

    # ---------------------------------------------------------- 4. AI 判定
    print("\n■ 4. AI 判定（/api/judge · 固定走 DeepSeek，密钥取自 tst_api_key）")
    code, _ = jreq("GET", "%s/api/judge" % base)
    chk("GET /api/judge -> 405", code == 405, "HTTP %s" % code)

    # 4.1 判定密钥必须落库、且不写死在源码里（需求 6）
    with sqlite3.connect(DB_PATH) as conn:
        jrow = conn.execute("SELECT api_key FROM tst_api_key WHERE plan_id IS NULL"
                            " AND key_name = ? AND is_active = 1 ORDER BY id LIMIT 1",
                            ("AI判定（DeepSeek）",)).fetchone()
    judge_secret = (jrow[0] if jrow else "") or ""
    chk("tst_api_key 存在全局判定密钥（plan_id 为 NULL）", bool(judge_secret),
        "长度 %d" % len(judge_secret))

    # ⚠️ 例外：config/settings.py —— 第二次迭代（表结构说明）里用户**明确要求**
    #    把 DeepSeek 的三项配置（含同一把 key）写在这个配置文件里。
    #    所以判定侧的口径收紧成「除了 settings.py 这个用户指定的配置点，
    #    别处不许出现；且运行时必须从 tst_api_key 现读」（后者在第 5.7 节断言）。
    LEAK_ALLOW = {os.path.join("config", "settings.py")}
    leaks = []
    if judge_secret:
        targets = []
        for root, dirs, files in os.walk(BASE_DIR):
            dirs[:] = [x for x in dirs if x not in ("__pycache__", ".git")]
            for fn in files:
                if fn.endswith((".py", ".html")):
                    targets.append(os.path.join(root, fn))
        for p in targets:
            try:
                with open(p, "r", encoding="utf-8", errors="ignore") as fp:
                    if judge_secret in fp.read():
                        rel = os.path.relpath(p, BASE_DIR)
                        if rel.replace("/", os.sep) not in LEAK_ALLOW:
                            leaks.append(rel)
            except OSError:
                pass
    chk("判定密钥除 settings.py（用户指定的配置点）外没有写死在任何 .py / .html 里",
        not leaks, "命中 %s" % leaks)

    # 4.2 任一侧为空 ⇒ 按原型口径不给结论（判定列留空、等人工判定），不发起模型调用
    code, res = jreq("POST", "%s/api/judge" % base, {"ans": "标准答案", "out": ""})
    chk("模型答案为空 -> 未判定（ai 为空、ok 为真）",
        code == 200 and res.get("ok") and res.get("ai") == "", "%s" % res)
    code, res = jreq("POST", "%s/api/judge" % base, {"ans": "", "out": "模型答案"})
    chk("标准答案为空 -> 未判定",
        code == 200 and res.get("ok") and res.get("ai") == "", "%s" % res)

    # 4.3 真调一次 DeepSeek 做一致性判定（网络不通只告警，不计入失败）
    code, res = jreq("POST", "%s/api/judge" % base,
                     {"ans": "1 加 1 等于 2", "out": "一加一的结果就是 2"})
    if code == 200 and res.get("ok") and res.get("ai"):
        chk("语义一致 -> right", res.get("ai") == "right", "ai=%s" % res.get("ai"))
        chk("判定模型为 deepseek-v4-pro（需求指定）",
            res.get("model") == "deepseek-v4-pro", res.get("model"))
        code, res2 = jreq("POST", "%s/api/judge" % base,
                          {"ans": "北京是中国的首都", "out": "苹果是一种水果，可以吃"})
        chk("语义不一致 -> wrong",
            code == 200 and res2.get("ok") and res2.get("ai") == "wrong", "ai=%s" % res2.get("ai"))
    else:
        warn("真判定未成功（多为网络 / 密钥环境问题，不计入失败）：%s" % res)

    # ---------------------------------------------------------- 5. 数据库表结构
    print("\n■ 5. 数据库表结构（/api/schema · /api/schema/desc）")

    # 5.0 先把库里的真实结构自己算一遍当基线（禁止写死表名 / 条数）
    with sqlite3.connect(DB_PATH) as conn:
        real_tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        real_tables = [t for t in real_tables if not t.startswith("sqlite_")]
        real_cols = {t: conn.execute('PRAGMA table_info("%s")' % t).fetchall()
                     for t in real_tables}
        real_rows = {t: conn.execute('SELECT COUNT(*) FROM "%s"' % t).fetchone()[0]
                     for t in real_tables}
        real_fks = {t: len(conn.execute('PRAGMA foreign_key_list("%s")' % t).fetchall())
                    for t in real_tables}

    code, sch = jreq("GET", "%s/api/schema" % base)
    chk("GET /api/schema -> 200 且 ok", code == 200 and isinstance(sch, dict) and sch.get("ok"),
        "HTTP %s" % code)

    got = [t["name"] for t in sch.get("tables") or []]
    chk("表清单 = 库内真实表（%d 张，sqlite 内部表除外）" % len(real_tables),
        got == real_tables, "接口=%s 库内=%s" % (got[:4], real_tables[:4]))
    chk("kind 标为 SQLite、name 是库文件名", sch.get("kind") == "SQLite"
        and sch.get("name") == os.path.basename(DB_PATH), "%s / %s" % (sch.get("name"), sch.get("kind")))

    # 字段：名字 / 主键关键字 / 真实类型 / 行数 / 外键条数，逐表对上
    by_name = {t["name"]: t for t in (sch.get("tables") or [])}
    bad_cols, bad_rows, bad_rel = [], [], []
    pk_ok = True
    for t in real_tables:
        one = by_name.get(t) or {}
        cols = one.get("cols") or []
        if [c[0] for c in cols] != [c[1] for c in real_cols[t]]:
            bad_cols.append(t)
        else:
            for c, raw in zip(cols, real_cols[t]):
                expect_kw = "pk" if raw[5] else (raw[2] or "")
                if c[1] != expect_kw or (len(c) > 3 and c[3] != (raw[2] or "")):
                    pk_ok = False
        if one.get("rows") != real_rows[t]:
            bad_rows.append(t)
    chk("每张表的字段清单 / 类型与 PRAGMA table_info 一致", not bad_cols, "异常表 %s" % bad_cols)
    chk("主键列的关键字统一为 pk（原型靠它标「主键」+ 映射 INTEGER）", pk_ok)
    chk("行数 = 库里真实行数", not bad_rows, "异常表 %s" % bad_rows)

    rels = sch.get("relations") or []
    cnt_by = {}
    for r in rels:
        cnt_by[r[0]] = cnt_by.get(r[0], 0) + 1
    for t in real_tables:
        if cnt_by.get(t, 0) != real_fks[t]:
            bad_rel.append("%s(%s!=%s)" % (t, cnt_by.get(t, 0), real_fks[t]))
    chk("表间关系条数 = PRAGMA foreign_key_list（共 %d 条）" % len(rels), not bad_rel, "%s" % bad_rel)
    chk("关系文案固定为「外键关联」", all(len(r) == 5 and r[4] == "外键关联" for r in rels))

    # 5.1 库里本来就没有注释 ⇒ 一律留空，等 /api/schema/desc 补
    empt = [t["name"] for t in (sch.get("tables") or [])
            if (t.get("desc") or "") or (t.get("descFrom") or "")]
    no_col_note = all(not (c[2] or "") for t in (sch.get("tables") or []) for c in (t.get("cols") or []))
    chk("库里本来没注释：表说明一律为空", not empt, "非空表 %s" % empt)
    chk("库里本来没注释：字段说明一律为空", no_col_note)

    # 5.2 注释解析单测（不走接口，直接打 core.schema）
    ddl_ok = '''
CREATE TABLE prompt (               -- 提示词调试表
  id           INTEGER PRIMARY KEY, -- 主键ID
  prompt_name  TEXT NOT NULL,       -- 提示词名称
  raw_txt      TEXT
)
'''
    ddl_bad = "CREATE TABLE plain (a TEXT, b INTEGER)"
    td, cd = schema.parse_sql_comments(ddl_ok)
    td2, cd2 = schema.parse_sql_comments(ddl_bad)
    chk("注释解析：表注释 = 「提示词调试表」", td == "提示词调试表", "%r" % td)
    chk("注释解析：字段注释逐列对上（id / prompt_name）",
        cd.get("id") == "主键ID" and cd.get("prompt_name") == "提示词名称", "%s" % cd)
    chk("注释解析：没有注释的库返回空（不臆造）", td2 == "" and cd2 == {}, "%r %s" % (td2, cd2))

    # 5.3 「只补缺注释的」：拿一个带注释的库当输入，missing 只该剩没注释的那个字段
    tmpf = os.path.join(tempfile.gettempdir(), "pd_verify_commented.sqlite")
    try:
        if os.path.exists(tmpf):
            os.remove(tmpf)
        # ⚠️ 不能写 `with sqlite3.connect(...)` —— 那是**事务**上下文，不关连接，
        #    Windows 上会一直占着文件，后面 os.remove 直接 WinError 32。
        c2 = sqlite3.connect(tmpf)
        try:
            c2.executescript(ddl_ok)
            c2.commit()
        finally:
            c2.close()
        up_sch = schema.read_sqlite(tmpf)
        miss = schema.missing_tables(up_sch)
        need = (miss[0].get("need") if miss else [])
        chk("带注释的库：表说明从注释里读出来（descFrom=db）",
            up_sch["tables"][0]["desc"] == "提示词调试表"
            and up_sch["tables"][0]["descFrom"] == "db", up_sch["tables"][0]["desc"])
        chk("带注释的库：缺注释的只剩 raw_txt 一个字段（已有注释的字段不重发）",
            len(miss) == 1 and need == ["raw_txt"], "%s" % need)
        chk("带注释的库：表说明已有 ⇒ needDesc 为假", miss and miss[0]["needDesc"] is False,
            "%s" % (miss[0]["needDesc"] if miss else None))

        # 5.4 上传这个 .sqlite（走 multipart），接口应把注释一起读回来
        with open(tmpf, "rb") as fp:
            blob = fp.read()
        code, up = multipart_post("%s/api/schema" % base, "file",
                                  os.path.basename(tmpf), blob)
        chk("POST /api/schema 上传 .sqlite -> 200 且读回注释",
            code == 200 and isinstance(up, dict) and up.get("ok")
            and (up.get("tables") or [{}])[0].get("desc") == "提示词调试表",
            "HTTP %s" % code)
        chk("上传库的字段注释也读回来了",
            any(c[0] == "prompt_name" and c[2] == "提示词名称"
                for c in (up.get("tables") or [{}])[0].get("cols") or []))
    finally:
        try:
            if os.path.exists(tmpf):
                os.remove(tmpf)
        except OSError as exc:
            warn("临时库没能删掉（不影响结论）：%s" % exc)

    # 5.5 方法 / 入参兜底
    code, _ = jreq("GET", "%s/api/schema/desc" % base)
    chk("GET /api/schema/desc -> 405", code == 405, "HTTP %s" % code)
    code, _ = jreq("POST", "%s/api/schema/desc" % base, {"tables": []})
    chk("POST /api/schema/desc 空清单 -> 400", code == 400, "HTTP %s" % code)
    code, _ = jreq("PUT", "%s/api/schema" % base, {})
    chk("PUT /api/schema -> 405", code == 405, "HTTP %s" % code)

    # 5.6 真调一次 DeepSeek 补说明（网络不通只告警，不计入失败）
    #     送两张真实表；断言「回来的字段 = 送去的 need」「表说明已填上」。
    two = [t for t in real_tables if t in ("srv", "tst_plan")] or real_tables[:2]
    payload = {"db": os.path.basename(DB_PATH), "tables": []}
    for t in two:
        payload["tables"].append({
            "name": t,
            "needDesc": True,
            "need": [c[1] for c in real_cols[t]],
            "cols": [[c[1], c[2] or ""] for c in real_cols[t]],
        })
    # 真调模型要等，客户端超时给到 240s（服务端那边是 120s）
    code, res = jreq("POST", "%s/api/schema/desc" % base, payload, timeout=240)
    if code == 200 and isinstance(res, dict) and res.get("ok") and res.get("tables"):
        chk("说明模型为 deepseek-v4-pro（需求指定）",
            res.get("model") == "deepseek-v4-pro", res.get("model"))
        got_names = [x["name"] for x in res["tables"]]
        chk("只回送去的 %d 张表" % len(two), got_names == two, "%s" % got_names)
        miss_cols = [c["name"] for x in res["tables"] for c in x["cols"] if not c["desc"]]
        chk("每个送去的字段都拿到了说明", not miss_cols, "缺 %s" % miss_cols)
        no_desc = [x["name"] for x in res["tables"] if not x["desc"]]
        chk("每张表都拿到了表说明", not no_desc, "缺 %s" % no_desc)
        print("     示例：%s - %s / %s -> %s"
              % (res["tables"][0]["name"], res["tables"][0]["desc"],
                 (res["tables"][0]["cols"] or [{}])[0].get("name"),
                 (res["tables"][0]["cols"] or [{}])[0].get("desc")))
    else:
        warn("真调 DeepSeek 未成功（多为网络 / 密钥环境问题，不计入失败）：%s" % res)

    # 5.7 说明生成不能写死密钥：本页的密钥按用户要求放在 config/settings.py；
    #     判定密钥（上一轮）仍必须从 tst_api_key 现读 —— 两条口径分开断言。
    with open(os.path.join(BASE_DIR, "config", "settings.py"), "r", encoding="utf-8") as fp:
        settings_src = fp.read()
    chk("settings.py 里配了 DeepSeek 三项（表结构说明用）",
        all(k in settings_src for k in ("DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL", "DEEPSEEK_API_KEY")))
    views_src = ""
    with open(os.path.join(BASE_DIR, "core", "views.py"), "r", encoding="utf-8") as fp:
        views_src = fp.read()
    chk("judge_api 仍从 tst_api_key 现读判定密钥（没用 settings 里的常量兜底）",
        "store.judge_key()" in views_src)

    # ---------------------------------------------------------- 5.8 真接驱动：驱动 / 连接信息
    print("\n■ 5.8 真接驱动（core/dbx.py：驱动可用性 + 连接信息解析）")

    from core import dbx

    st = dbx.driver_status()
    for kind, pkg, _tn in STUB_CASES:
        one = st.get(kind) or {}
        chk("%s 驱动已安装（%s）" % (kind, pkg), one.get("ok"),
            one.get("version") or one.get("error"))

    # URL 模式：原型模板 mysql://用户名:密码@主机:端口/数据库名，占位符里还有 jdbc: 前缀
    p1 = dbx.parse_url("mysql://root:123456@localhost:3306/tes", "MySQL")
    chk("URL 解析：完整 mysql 连接串",
        p1 == {"host": "localhost", "port": 3306, "user": "root",
               "pwd": "123456", "dbname": "tes"}, p1)
    p2 = dbx.parse_url("jdbc:mysql://127.0.0.1:3306/shop_db", "MySQL")
    chk("URL 解析：吃掉 jdbc: 前缀",
        p2.get("host") == "127.0.0.1" and p2.get("dbname") == "shop_db", p2)
    p3 = dbx.parse_url("sa:P%40ss@host:1433/master", "SQL Server")
    chk("URL 解析：没写 scheme 也能解 + 密码百分号反转义",
        p3.get("user") == "sa" and p3.get("pwd") == "P@ss"
        and p3.get("dbname") == "master", p3)
    p4 = dbx.parse_url("postgresql://u@h/kb", "PostgreSQL")
    chk("URL 解析：省略端口时不臆造（留给默认端口兜底）", "port" not in p4, p4)

    # host 模式规范化：主机留空=本机、端口留空=该类型默认端口（原型口径）
    n1 = dbx.normalize_cfg("MySQL", {"mode": "host", "host": "", "port": ""})
    chk("host 模式：主机留空 -> localhost（原型口径：留空表示本机）",
        n1["host"] == "localhost", n1)
    n2 = dbx.normalize_cfg("Oracle", {"mode": "host", "host": "db", "port": ""})
    chk("host 模式：端口留空 -> 该类型默认端口 1521", n2["port"] == 1521, n2)
    n3 = dbx.normalize_cfg("MySQL", {"mode": "url", "url": "mysql://a:b@h:3307/k"})
    chk("url 模式：只认 URL 里的信息（URL 下隐藏的五个框不参与）",
        (n3["host"], n3["port"], n3["user"], n3["dbname"]) == ("h", 3307, "a", "k"), n3)

    # ---------------------------------------------------------- 5.9 各方言结构读取（桩 DBAPI）
    print("\n■ 5.9 各类型结构读取（桩驱动：验真方言反射 SQL 与形状转换）")

    for kind, pkg, tnames in STUB_CASES:
        rec = {}
        fake = stub_driver(pkg, STUB_RULES[kind], rec)
        with patched_module(pkg, fake):
            res = dbx.read_db(kind, {"mode": "host", "host": "db.example.com",
                                     "port": "", "user": "u", "pwd": "p", "dbname": "shop"})
        call = rec.get("call") or {}
        kw = call.get("kwargs") or {}
        args = call.get("args") or ()

        chk("%s：真的调了驱动 connect()" % kind, bool(call),
            "kwargs=%s args=%s" % (sorted(kw), args))
        chk("%s：ok=True 且 kind 正确" % kind,
            isinstance(res, dict) and res.get("ok") and res.get("kind") == kind,
            res.get("error") if isinstance(res, dict) else res)
        got_names = [t["name"] for t in (res.get("tables") or [])]
        chk("%s：读到 %d 张真实表" % (kind, len(tnames)), got_names == tnames, got_names)

        t0 = (res.get("tables") or [{}])[0]
        pk = [c for c in (t0.get("cols") or []) if c[1] == "pk"]
        chk("%s：主键列关键字统一为 pk（原型靠它标「主键」）" % kind,
            len(pk) == 1 and pk[0][0].lower() == "id", [c[0] for c in pk])
        chk("%s：字段说明从目录表注释读出来" % kind,
            all((c[2] or "") for c in (t0.get("cols") or [])),
            [(c[0], c[2]) for c in (t0.get("cols") or [])])
        chk("%s：非主键列保留库里的真实声明类型" % kind,
            all(c[3] for c in (t0.get("cols") or []) if c[1] != "pk"),
            [c[3] for c in (t0.get("cols") or []) if c[1] != "pk"])
        chk("%s：行数 = COUNT(*) 的真实返回值" % kind, t0.get("rows") == 7, t0.get("rows"))

        rels = res.get("relations") or []
        fk_ok = (len(rels) == 1 and rels[0][0] == tnames[0]
                 and rels[0][1].lower() == "user_id" and rels[0][2] == tnames[1]
                 and rels[0][3].lower() == "id" and rels[0][4] == "外键关联")
        chk("%s：外键 = 从表.字段 -> 主表.字段" % kind, fk_ok, rels)

    # 连接参数按「运行库真驱动」的传参习惯送到位（MySQL 用 kwargs，Db2 用 DSN）
    rec = {}
    with patched_module("pymysql", stub_driver("pymysql", STUB_RULES["MySQL"], rec)):
        dbx.read_db("MySQL", {"mode": "url",
                              "url": "mysql://alice:s3cret@10.0.0.9:3307/analytics"})
    kw = (rec.get("call") or {}).get("kwargs") or {}
    chk("MySQL：URL 里的主机 / 端口 / 账号 / 库名逐项送到驱动",
        kw.get("host") == "10.0.0.9" and kw.get("port") == 3307
        and kw.get("user") == "alice" and kw.get("password") == "s3cret"
        and kw.get("database") == "analytics", {k: kw.get(k) for k in
                                                ("host", "port", "user", "password", "database")})

    rec = {}
    with patched_module("ibm_db_dbi", stub_driver("ibm_db_dbi", STUB_RULES["Db2 for LUW"], rec)):
        dbx.read_db("Db2 for LUW", {"mode": "host", "host": "db2.local", "port": "50001",
                                    "user": "db2inst1", "pwd": "pw", "dbname": "SAMPLE"})
    dsn = str((rec.get("call") or {}).get("args", ("",))[0])
    chk("Db2：连接串把库名 / 主机 / 端口 / 超时都拼进去了",
        "DATABASE=SAMPLE" in dsn and "HOSTNAME=db2.local" in dsn and "PORT=50001" in dsn,
        dsn)

    # 读结构 = 只读：PG 连接上必须显式设成只读会话
    rec = {}
    with patched_module("psycopg2", stub_driver("psycopg2", STUB_RULES["PostgreSQL"], rec)):
        dbx.read_db("PostgreSQL", {"mode": "host", "host": "pg.local", "port": "5432"})
    chk("PostgreSQL：连接后设为只读会话（只读结构，不写库）",
        rec.get("set_session") and rec["set_session"][0].get("readonly") is True,
        rec.get("set_session"))

    # ---------------------------------------------------------- 5.10 连接接口：真失败如实上报
    print("\n■ 5.10 POST /api/schema/connect（真实驱动连不上就如实上报）")

    code, res = jreq("POST", "%s/api/schema/connect" % base,
                     {"kind": "MySQL", "mode": "host", "host": "127.0.0.1",
                      "port": "3999", "user": "root", "pwd": "x"}, timeout=40)
    chk("连不上时 HTTP 仍是 200（业务结果，不是请求格式错）", code == 200, "HTTP %s" % code)
    chk("连不上时 ok=false 且带真实报错",
        isinstance(res, dict) and res.get("ok") is False
        and len(str(res.get("error") or "")) > 10, res)
    chk("连不上时不返回任何表（绝不伪造表树）",
        isinstance(res, dict) and not res.get("tables"), res)
    err = str(res.get("error") or "")
    chk("报错确实来自真 MySQL 驱动（原样透出）",
        "MySQL" in err and ("2003" in err or "10061" in err or "Can't connect" in err),
        err[:90])

    # 服务端不做二次校验：必填框全空也不该被拦下，而是拿去连、由驱动报错
    code, res2 = jreq("POST", "%s/api/schema/connect" % base,
                      {"kind": "MySQL", "mode": "host", "host": "", "port": ""}, timeout=40)
    chk("必填框留空不会被服务端拦下（服务端不做二次校验）",
        code == 200 and isinstance(res2, dict) and res2.get("ok") is False
        and "为必填项" not in str(res2.get("error") or ""), res2)
    code, res3 = jreq("POST", "%s/api/schema/connect" % base, {"kind": "Sybase"}, timeout=20)
    chk("不认识的类型 -> 200 + ok=false（不抛 500）",
        code == 200 and isinstance(res3, dict) and res3.get("ok") is False, res3)
    code, _ = jreq("PUT", "%s/api/schema/connect" % base, {})
    chk("PUT /api/schema/connect -> 405", code == 405, "HTTP %s" % code)

    # ---------------------------------------------------------- 5.95 链接数据库调试
    # 前端按钮（prompt.html 模型输出区）→ POST /api/dbeaver/link → core/dbeaver.py 唤起 DBeaver。
    # 本节**不真启DBeaver**（会弹真窗口），而是把「拼参数」这一层单独拎出来验 ——
    # 那才是最容易出错、也最该被钉住的部分（管道分隔、驱动映射、密码不外传）。
    print("\n■ 5.95 链接数据库调试（/api/dbeaver/link · 参数拼装与安全口径）")

    code, res = jreq("POST", "%s/api/dbeaver/link" % base, {})
    chk("POST 空体 -> 200 + ok=false（不抛 500）",
        code == 200 and isinstance(res, dict) and res.get("ok") is False, "HTTP %s %s" % (code, res))
    chk("空体时报的是「没有数据库类型」",
        "没有数据库类型" in str(res.get("error") or ""), res.get("error"))

    # 直接验 core/dbeaver 的参数拼装（不经过 HTTP，免得真去启动 GUI）
    db_mod = importlib.import_module("core.dbeaver")
    db_mod = importlib.reload(db_mod)

    con, why = db_mod.build_con({"kind": "MySQL", "name": "shop",
                                 "mode": "host", "host": "127.0.0.1", "port": "3306",
                                 "dbname": "shop_db", "user": "root"})
    chk("MySQL -con 拼出驱动 / 主机 / 端口 / 库名 / 用户 / 控制台",
        con.startswith("driver=mysql") and "host=127.0.0.1" in con and "port=3306" in con
        and "database=shop_db" in con and "user=root" in con and "openConsole=true" in con,
        con)
    chk("-con 里没有 password", "password" not in con.lower(), con)

    con2, why2 = db_mod.build_con({"kind": "SQL Server", "name": "srv1", "mode": "host",
                                   "host": "10.0.0.9", "port": "1433", "dbname": "sales",
                                   "user": "sa", "pwd": "SHOULD-NEVER-APPEAR"})
    chk("SQL Server -> driver=sqlserver", con2.startswith("driver=sqlserver"), con2)
    chk("即便请求体里塞了 pwd 也不进 -con（安全口径）",
        "SHOULD-NEVER-APPEAR" not in con2 and "password" not in con2.lower(), con2)

    con3, why3 = db_mod.build_con({"kind": "SQLite", "name": "本地库", "file": DB_PATH})
    chk("SQLite -> driver=sqlite 且带文件绝对路径",
        con3.startswith("driver=sqlite") and ("database=" + os.path.abspath(DB_PATH)) in con3, con3)

    code4, why4 = db_mod.build_con({"kind": "SQLite", "name": "x", "file": "/no/such/file.db"})
    chk("SQLite 文件不存在 -> 给出「路径无效」而不是崩",
        code4 == "" and "路径无效" in why4, why4)

    code5, why5 = db_mod.build_con({"kind": "Sybase", "name": "x"})
    chk("不认识的类型 -> 明确说不支持",
        code5 == "" and "暂不支持" in why5, why5)

    code6, why6 = db_mod.build_con({"kind": "PostgreSQL", "name": "pg",
                                    "mode": "host", "host": "127.0.0.1", "port": "5432",
                                    "dbname": ""})
    chk("服务器型缺库名 -> 明确说缺库名", code6 == "" and "数据库名" in why6, why6)

    con7, why7 = db_mod.build_con({"kind": "PostgreSQL", "name": "pg", "mode": "url",
                                   "url": "postgresql://u:p@h:5432/app"})
    chk("URL 模式原样交给 DBeaver（不拆主机端口）",
        "url=postgresql://u:p@h:5432/app" in con7, con7)

    # 值里的 '|' 会破坏 DBeaver 的参数分隔 ⇒ 必须被清掉（a|b → a b）
    con8, _ = db_mod.build_con({"kind": "MySQL", "name": "a|b", "mode": "host",
                                "host": "h", "port": "3306", "dbname": "d"})
    segs = con8.split("|")
    chk("值里的 '|' 被替换成空格，参数段数不因此变多",
        con8.count("|") == 5 and "name=a b" in segs, con8)

    # 脚本文件：SQL 写进去 + 表名进注释 + **文件名带表名**（标签页直接写明给哪张表）
    f = db_mod.write_sql_file("SELECT * FROM srv LIMIT 1;", ["srv", "mdl"], "SQLite · twe.db")
    body = open(f, "r", encoding="utf-8").read()
    chk("临时 .sql 里有 SQL 正文与目标表注释",
        "SELECT * FROM srv LIMIT 1;" in body and "srv, mdl" in body, body[:80])
    chk("目标库也写进注释", "SQLite · twe.db" in body, body[:120])
    # ⚠️ 这条是「定位到表」的关键：文件名带表名 ⇒ DBeaver 的编辑器标签直接写明是哪张表
    # ⚠️ 局部变量别叫 `base` —— 外层 `base` 是服务地址（args.base），覆盖掉会让后面所有
    # 接口请求拼成 "xxx.sql/api/..." 而抛 ValueError（踩过一次）。
    fname = os.path.basename(f)
    chk("★ 脚本文件名带目标表（编辑器标签直接标明）",
        fname.startswith("srv") and "2" in fname and fname.endswith(".sql"), fname)
    os.remove(f)

    # 表名里的路径分隔符 / 非法字符必须被洗掉，否则 mkstemp 会直接抛错
    try:
        f2 = db_mod.write_sql_file("SELECT 1;", ["a/b:c*d?", "x"], "")
        ok2 = os.path.isfile(f2)
        os.remove(f2)
    except Exception as exc:
        ok2 = False
        fname = repr(exc)
    chk("表名含 / : * ? 等非法字符也能安全落文件（不抛异常）", ok2 is True, fname if not ok2 else "")

    # 「什么时候不该起 DBeaver」：SQL 为空时压根不找可执行文件
    r = db_mod.launch({"kind": "MySQL", "name": "x", "mode": "host",
                       "host": "h", "port": "3306", "dbname": "d"}, "   ")
    chk("SQL 为空 -> 直接回「请先生成 SQL」，不去启动任何进程",
        r.get("ok") is False and "请先生成 SQL" in str(r.get("error") or ""), r)

    # 找得到 / 找不到可执行文件都要有明确说法（找不到时提示去配置）
    exe, why = db_mod.find_exe()
    chk("find_exe 要么给出路径、要么明确让用户去配置",
        bool(exe) or "DBEAVER_PATH" in why, "exe=%r why=%r" % (exe, why))

    # ⚠️ 便携版探测（本机装在 D:\DB\，注册表无记录、也不在 Program Files）：
    #    只扫标准位置会一直报「未检测到」⇒ 必须浅层扫盘，且判据是「有 dbeaver.ini + 启动器」。
    chk("★ _scan_portable 存在（别退回只扫 Program Files / 注册表）",
        hasattr(db_mod, "_scan_portable") and hasattr(db_mod, "_PORTABLE_HINTS"))
    if exe:
        chk("★ 找到的路径确实带 dbeaver.ini（便携版判据成立）",
            db_mod._is_dbeaver_dir(os.path.dirname(exe)) is True, exe)
    chk("_is_dbeaver_dir 对普通目录返回 False（不会把无关目录当 DBeaver）",
        db_mod._is_dbeaver_dir(BASE_DIR) is False, BASE_DIR)

    # ⚠️ rc=0 秒退是 Eclipse 单实例交接的**正常**行为，不能当成失败
    #（静态断言：确保判据写的是 returncode 真值，而不是「进程还活着吗」）
    src = open(os.path.join(BASE_DIR, "core", "dbeaver.py"), "r", encoding="utf-8").read()
    chk("★ 判「是否失败」用的是退出码而非进程存活（单实例会秒退）",
        "if proc.returncode:" in src and "not in (0, None)" not in src)

    code, _ = jreq("PUT", "%s/api/dbeaver/link" % base, {})
    chk("PUT /api/dbeaver/link -> 405", code == 405, "HTTP %s" % code)

    class _FakeReq(object):
        """直接调视图用（不经过 HTTP）——用来验参数归一的**行为**，
        比在源码里搜字符串可靠。"""
        method = "POST"
        content_type = "application/json"

        def __init__(self, body):
            self._b = body

        @property
        def body(self):
            return self._b

    # ---- 5.94b tables 传「对象数组」时，DBeaver 脚本注释只该写表名 ----
    # 10-09 起因：前端交接的 tables 从「纯表名数组」升级成结构化表清单
    #（页面内调试弹窗要字段），若后端直接 to_str(x) 会把一整串 Python dict
    # 原文写进 .sql 脚本头部注释里。
    code, res_t = jreq("POST", "%s/api/dbeaver/link" % base, {
        "meta": {"kind": "SQLite", "name": "x", "mode": "file",
                 "file": "/no/such/file.db",
                 "tables": [{"name": "tst_plan", "rows": 1, "desc": "方案表",
                             "cols": [["plan_name", "TEXT", "方案名称", "TEXT"]]},
                            {"name": "tst_rec", "rows": 2, "desc": "记录表",
                             "cols": [["rec_id", "pk", "", "INTEGER"]]}]},
        "sql": "SELECT 1;",
    })
    chk("tables 传对象数组时接口不崩（仍按缺文件路径回错）",
        isinstance(res_t, dict) and res_t.get("ok") is False, res_t)
    # 直接验归一源码：只能是表名，不该出现 dict 原文
    _src = open(os.path.join(BASE_DIR, "core", "views.py"), "r", encoding="utf-8").read()
    chk("★ tables 归一那段显式处理了 dict（不会把 dict 原文写进脚本注释）",
        "isinstance(x, dict)" in _src and 'x.get("name")' in _src)
    #行为验（打桩 launch，**不启动任何进程**）：归一结果必须是纯表名
    _got = {}
    _dbeaver_saved = dbeaver_mod_backup = None
    try:
        import core.views as _v
        _dbeaver_saved = _v.dbeaver
        _v.dbeaver = type("M", (), {"launch": staticmethod(
            lambda clean, sql: _got.update(clean=clean) or {"ok": False, "error": "stub"})})
        import json as _json
        _v.dbeaver_link_api(_FakeReq(_json.dumps({
            "meta": {"kind": "SQLite", "name": "x", "mode": "file",
                     "file": "/no/such/x.db",
                     "tables": [{"name": "tst_plan", "rows": 1, "desc": "方案表",
                                 "cols": [["plan_name", "TEXT", "方案名称", "TEXT"]]},
                                {"name": "tst_rec", "rows": 2, "desc": "",
                                 "cols": [["rec_id", "pk", "", "INTEGER"]]}]},
            "sql": "SELECT 1;"}).encode("utf-8")))
        chk("★ 对象数组归一出的 tables 是**纯表名**（脚本注释里不会出现 dict 原文）",
            _got.get("clean", {}).get("tables") == ["tst_plan", "tst_rec"],
            _got.get("clean", {}).get("tables"))
        _got.clear()
        _v.dbeaver_link_api(_FakeReq(_json.dumps({
            "meta": {"kind": "SQLite", "name": "x", "mode": "file",
                     "file": "/no/such/x.db", "tables": ["a", "b"]},
            "sql": "SELECT 1;"}).encode("utf-8")))
        chk("★ 老结构（纯表名数组）照常工作（向后兼容）",
            _got.get("clean", {}).get("tables") == ["a", "b"],
            _got.get("clean", {}).get("tables"))
    finally:
        if _dbeaver_saved is not None:
            _v.dbeaver = _dbeaver_saved

    # ------------------------------------------------- 5.96 页面内调试 execute-sql
    # 与 5.95 的 DBeaver 路线**并存**：同一个按钮旁边多一个「页面内调试」，
    # 由服务端真连库执行**只读** SQL，把结果集回页面。
    print("\n■ 5.96 页面内调试（/api/database/execute-sql · 只读白名单与行数上限）")
    sqlrun = importlib.import_module("core.sqlrun")

    # —— 安全口径：写 / 管理语句必须一律拒（这是本模块存在的全部意义）——
    deny = [
        "DROP TABLE t", "delete from t", "UPDATE t SET a=1", "INSERT INTO t VALUES(1)",
        "ALTER TABLE t ADD c INT", "CREATE TABLE t(a int)", "TRUNCATE TABLE t",
        "GRANT ALL ON t TO u", "REVOKE ALL ON t FROM u",
        "CALL sp_who()", "EXEC sp_executesql N'select 1'", "EXECUTE sp_who",
        "SET GLOBAL x=1", "USE otherdb", "COMMIT", "ROLLBACK",
        "SELECT 1; DROP TABLE t",              # 多语句注入
        "SELECT * INTO backup FROM t",          # T-SQL 建表
        "SELECT pg_sleep(5)",                    # 阻塞型函数
        "SELECT load_extension('x')",
        "SELECT * FROM t1, t2 WHERE 1=1 UNION SELECT * FROM t3",
    ]
    bad = [(s, sqlrun.check_readonly(s)) for s in deny]
    # UNION 本身是读，放行；只把真正该拒的挑出来断言
    union = [x for x in bad if "UNION" in x[0]]
    real_deny = [x for x in bad if "UNION" not in x[0]]
    chk("★ 写 / 管理 / 注入类语句一律被拒（%d 条）" % len(real_deny),
        all(ok is False and why for ok, why in (v for _, v in real_deny)),
        [s for s, (ok, why) in real_deny if ok is not False])
    chk("UNION ALL 属只读，放行",
        sqlrun.check_readonly(union[0][1] if False else
                              "SELECT a FROM t1 UNION SELECT b FROM t2")[0] is True)

    allow = [
        "SELECT * FROM member_group", "select 1",
        "WITH t AS (SELECT 1 AS a) SELECT * FROM t",
        "SELECT COUNT(*) FROM information_schema.TABLES",
        "SELECT a FROM t WHERE b IN (1,2,3) ORDER BY a LIMIT 10",
        "  SELECT 1  ",                            # 前后空白
    ]
    chk("★ 常规只读语句全部放行（%d 条）" % len(allow),
        all(sqlrun.check_readonly(s)[0] is True for s in allow),
        [s for s in allow if sqlrun.check_readonly(s)[0] is not True])
    chk("空 SQL 被拒", sqlrun.check_readonly("")[0] is False)
    chk("注释里藏写语句也拦得住（先剥注释再判）",
        sqlrun.check_readonly("SELECT 1 /* ; DROP TABLE t */")[0] is True
        and sqlrun.check_readonly("-- DROP TABLE t")[0] is False)

    # —— 行数上限：套 LIMIT 时故意多放 1 行当截断探针 ——
    w1, _t1 = sqlrun._wrap_limit("SELECT a FROM t", "SQLite", 501)
    w2, _t2 = sqlrun._wrap_limit("SELECT a FROM t LIMIT 10", "SQLite", 501)
    w3, _t3 = sqlrun._wrap_limit("SELECT a FROM t", "SQL Server", 501)
    chk("★ 无 LIMIT 时补上（cap+1 当截断探针）", w1.endswith("LIMIT 501"), w1)
    chk("用户自带 LIMIT 时不改写语义", w2 == "SELECT a FROM t LIMIT 10", w2)
    chk("SQL Server 走 TOP 写法", "TOP 501" in w3, w3)

    # —— 真跑一个 SQLite：只读、行数截断、报错透出、数据零损伤 ——
    import sqlite3 as _sq
    tmp_db = os.path.join(tempfile.gettempdir(), "pd_verify_sqlrun.db")
    if os.path.exists(tmp_db):
        os.remove(tmp_db)
    _cn = _sq.connect(tmp_db)
    _cn.execute("CREATE TABLE v_probe (id INTEGER PRIMARY KEY, nm TEXT)")
    _cn.executemany("INSERT INTO v_probe VALUES (?,?)", [(i, "r%d" % i) for i in range(1, 61)])
    _cn.commit(); _cn.close()

    r = sqlrun.run_sql("SQLite", {"file": tmp_db}, "SELECT id, nm FROM v_probe ORDER BY id", cap=10)
    chk("★ 真执行 SELECT 返回结果集",
        r.get("ok") is True and r.get("rowCount") == 10
        and [c["title"] for c in r["columns"]] == ["id", "nm"], r)
    chk("★ 超过上限时截断且标记 truncated=True", r.get("truncated") is True, r.get("truncated"))
    chk("耗时字段是毫秒整数",
        isinstance(r.get("elapsed"), int) and r["elapsed"] >= 0, r.get("elapsed"))
    r2 = sqlrun.run_sql("SQLite", {"file": tmp_db}, "SELECT COUNT(*) AS n FROM v_probe")
    chk("未超上限时 truncated=False",
        r2.get("truncated") is False and r2["rows"][0].get("c0") == 60, r2)

    r3 = sqlrun.run_sql("SQLite", {"file": tmp_db}, "DELETE FROM v_probe")
    chk("★ 写语句到执行层仍被拒", r3.get("ok") is False, r3)
    r4 = sqlrun.run_sql("SQLite", {"file": tmp_db}, "SELECT * FROM v_nope")
    chk("驱动报错原样透出（用户看得懂、能自己改）",
        r4.get("ok") is False and "v_nope" in (r4.get("error") or ""), r4)
    #⚠️ with sqlite3.connect(...) 只管事务、**不关连接**，句柄还开着时 os.remove
    #   在 Windows 上会 PermissionError ⇒ 这里显式 close() 再删。
    _cc = _sq.connect(tmp_db)
    try:
        left = _cc.execute("SELECT COUNT(*) FROM v_probe").fetchone()[0]
        still = _cc.execute("SELECT name FROM sqlite_master WHERE name='v_probe'").fetchone()
    finally:
        _cc.close()
    chk("★ 只读执行后数据零损伤（行数不变、表还在）", left == 60 and still is not None,
        "rows=%s tbl=%s" % (left, still))
    r5 = sqlrun.run_sql("SQLite", {"file": tmp_db + ".missing"}, "SELECT 1")
    chk("文件不存在给明确提示（不是 500）", r5.get("ok") is False, r5)

    # —— 连接阶段异常必须变成友好错误，不能 500 白屏 ——
    _pwl = "Sup3rSecret!2026"
    r6 = sqlrun.run_sql("MySQL", {"host": "localhost", "port": 3306, "user": "root",
                                   "pwd": _pwl, "dbname": "nope"}, "SELECT 1")
    chk("★ 连不上库返回友好错误而非抛异常",
        r6.get("ok") is False and (r6.get("error") or "").startswith("连接 MySQL 失败"), r6)
    chk("★ 报错里不含明文密码",
        _pwl not in str(r6), str(r6.get("error"))[:120])
    r7 = sqlrun.run_sql("MongoDB", {}, "SELECT 1")
    chk("未支持的类型给明确提示", r7.get("ok") is False, r7)
    if os.path.exists(tmp_db):
        os.remove(tmp_db)

    # —— HTTP 层：方法守卫 + 真发一次请求 ——
    code, _ = jreq("GET", "%s/api/database/execute-sql" % base)
    chk("GET /api/database/execute-sql -> 405", code == 405, "HTTP %s" % code)
    code, res = jreq("POST", "%s/api/database/execute-sql" % base,
                     {"kind": "SQLite", "meta": {"file": "C:/nope/x.db"},
                      "sql": "SELECT 1"})
    chk("POST execute-sql 缺文件时返回 200 + ok=false（不是 500）",
        code == 200 and isinstance(res, dict) and res.get("ok") is False, (code, res))
    code, res = jreq("POST", "%s/api/database/execute-sql" % base,
                     {"kind": "SQLite", "meta": {"file": "C:/nope/x.db"},
                      "sql": "DELETE FROM t"})
    chk("POST execute-sql 对写语句返回 ok=false",
        code == 200 and res.get("ok") is False, (code, res))

    # —— 密码零留存：全库 + 静态扫描都不得出现 ——
    _pw_probe = "PdPwProbe_9x8y7z"
    code, res = jreq("POST", "%s/api/database/execute-sql" % base,
                     {"kind": "MySQL", "meta": {"host": "127.0.0.1", "port": 59999,
                                                "user": "u", "dbname": "d"},
                      "pwd": _pw_probe, "sql": "SELECT 1"})
    chk("带密码的请求正常返回（不 500）", code == 200, "HTTP %s" % code)
    chk("★ 响应里不含明文密码", _pw_probe not in json.dumps(res, ensure_ascii=False))
    _srcdump = ""
    for _f in ("core/views.py", "core/sqlrun.py", "core/urls.py", "config/urls.py"):
        _p = os.path.join(BASE_DIR, _f)
        if os.path.exists(_p):
            _srcdump += open(_p, "r", encoding="utf-8").read()
    chk("★ 源码里搜不到这次用的探针密码（说明密码没被写死进任何地方）",
        _pw_probe not in _srcdump)
    chk("★ execute-sql 不碰 tst_api_key（不落库）",
        "tst_api_key" not in open(os.path.join(BASE_DIR, "core", "sqlrun.py"),
                                  "r", encoding="utf-8").read())

    # ------------------------------------------------ 5.97 已连接数据库存档 /api/db-conn
    # 10-09 用户点名：「表结构提取」页把已连接好的库存下来，下次进来左栏还在。
    # 本节钉四件事：
    #   ① 新表在建（setup_db_conn 幂等），列里**没有密码位**；
    #   ② 三个动词都对（GET 列表 / POST 保存并覆盖 / DELETE 删除）；
    #   ③ **密码类字段一律不落库**（这是本功能唯一有安全含义的一条）；
    #   ④ 老库缺表时不 500（页面拿到空列表，不影响别的功能）。
    print("\n■ 5.97 已连接数据库存档（/api/db-conn · 连接信息落库、密码不落库）")

    with sqlite3.connect(DB_PATH) as _c0:
        _has_tbl = _c0.execute("SELECT COUNT(*) FROM sqlite_master"
                               " WHERE type='table' AND name='tst_db_conn'").fetchone()[0] > 0
    chk("★ tst_db_conn 表已就位（setup_db_conn.py 幂等建表）", _has_tbl)

    _conn_ids_before = set()
    if _has_tbl:
        with sqlite3.connect(DB_PATH) as _c1:
            _conn_ids_before = {r[0] for r in _c1.execute("SELECT id FROM tst_db_conn")}

    try:
        code, res = jreq("GET", "%s/api/db-conn" % base)
        chk("GET /api/db-conn 返回列表", code == 200 and isinstance(res.get("list"), list),
            (code, str(res)[:80]))

        if _has_tbl:
            _cols = []
            with sqlite3.connect(DB_PATH) as _c2:
                _cols = [r[1] for r in _c2.execute("PRAGMA table_info(tst_db_conn)")]
            chk("★ 表里**没有密码列**（pwd / password / api_key / secret / token 都不该有）",
                not [c for c in _cols if any(
                    k in c.lower() for k in ("pwd", "passw", "api_key", "apikey", "secret", "token"))],
                _cols)

        # 保存一条（**故意夹带三种密码字段**，看它们进不进库）
        _PW = "DbConnPwProbe_9x8y7z"
        _entry = {
            "kind": "MySQL", "name": "E2E-conn-probe", "mode": "host",
            "host": "10.1.2.3", "port": "3306", "dbname": "demo", "user": "root",
            "pwd": _PW, "password": _PW, "apiKey": _PW,
            "checked": ["t1", "t2"],
            "schema": {"tables": [{"name": "t1", "rows": 5, "desc": "表一",
                                   "cols": [["id", "pk", "主键", "INTEGER"]]}],
                       "relations": [{"fromTable": "t1", "column": "pid",
                                      "refTable": "t2", "refColumn": "id"}]},
        }
        code, res = jreq("POST", "%s/api/db-conn" % base, _entry)
        chk("★ POST /api/db-conn 保存成功", code == 200 and res.get("ok") is True
            and res.get("action") in ("created", "updated"), (code, res))

        code, res = jreq("GET", "%s/api/db-conn" % base)
        _mine = [x for x in res.get("list", []) if x.get("name") == "E2E-conn-probe"]
        chk("★ 存进去的连接能被读回来（含表结构快照）",
            len(_mine) == 1 and len(_mine[0]["schema"]["tables"]) == 1
            and _mine[0]["checked"] == ["t1", "t2"], _mine)
        chk("★ 连接信息字段齐（host / port / dbname / user）",
            _mine and _mine[0]["host"] == "10.1.2.3" and _mine[0]["port"] == "3306"
            and _mine[0]["dbname"] == "demo" and _mine[0]["user"] == "root", _mine)

        # ⚠️ 本功能唯一有安全含义的断言：密码绝不落库
        with sqlite3.connect(DB_PATH) as _c3:
            _row = _c3.execute("SELECT conn_name, host, port, dbname, user_name,"
                               " COALESCE(schema_json,''), COALESCE(checked_json,'')"
                               " FROM tst_db_conn WHERE conn_name='E2E-conn-probe'").fetchall()
        chk("★ **明文密码没有落库**（整行扫一遍）", _PW not in str(_row),
            [r[:5] for r in _row])
        chk("★ 落库内容里没有 pwd / password 字样（连键名都不该有）",
            "pwd" not in str(_row).lower() and "password" not in str(_row).lower())
        chk("响应里也不回显密码", _PW not in json.dumps(res, ensure_ascii=False))

        # 覆盖：同一个「库名 + 类型」再存一次 → 更新而非新增
        code, res = jreq("POST", "%s/api/db-conn" % base,
                         {"kind": "MySQL", "name": "E2E-conn-probe", "mode": "host",
                          "host": "10.1.2.9", "port": "3307", "dbname": "demo2",
                          "user": "root", "schema": {"tables": [], "relations": []}})
        code, res = jreq("GET", "%s/api/db-conn" % base)
        _mine2 = [x for x in res.get("list", []) if x.get("name") == "E2E-conn-probe"]
        chk("★ 同一个库重复保存是**覆盖**（不新增行），且字段已更新",
            len(_mine2) == 1 and _mine2[0]["port"] == "3307"
            and _mine2[0]["dbname"] == "demo2", _mine2)

        # 缺库名 / 类型：回 ok=false，但不 500
        code, res = jreq("POST", "%s/api/db-conn" % base, {"name": "", "kind": ""})
        chk("缺库名/类型时回 ok=false（不 500）", code == 200 and res.get("ok") is False,
            (code, res))

        # DELETE 按「库名 + 类型」
        code, res = jreq("DELETE", "%s/api/db-conn" % base,
                         {"name": "E2E-conn-probe", "kind": "MySQL"})
        chk("★ DELETE /api/db-conn 按 name+kind 删除成功",
            code == 200 and res.get("deleted") == 1, (code, res))
        code, res = jreq("GET", "%s/api/db-conn" % base)
        chk("删除后列表里不再有它",
            not [x for x in res.get("list", []) if x.get("name") == "E2E-conn-probe"])

        code, _ = jreq("PUT", "%s/api/db-conn" % base, {})
        chk("PUT /api/db-conn -> 405", code == 405, "HTTP %s" % code)

        # 缺表时要**优雅**（模拟线上老容器）：把表藏起来，看接口是否还活
        _bak = "tst_db_conn__hidden_for_probe"
        with sqlite3.connect(DB_PATH) as _c4:
            _c4.execute("ALTER TABLE tst_db_conn RENAME TO %s" % _bak)
            _c4.commit()
        try:
            code, res = jreq("GET", "%s/api/db-conn" % base)
            chk("★ 表缺失时 GET 不 500，回空列表 + needSetup（线上老库容错）",
                code == 200 and isinstance(res.get("list"), list)
                and res.get("list") == [] and res.get("needSetup") is True, (code, res))
            code, res = jreq("POST", "%s/api/db-conn" % base,
                             {"kind": "MySQL", "name": "x"})
            chk("表缺失时 POST 也不 500（回 ok=false / needSetup）",
                code == 200 and res.get("ok") is False, (code, res))
        finally:
            with sqlite3.connect(DB_PATH) as _c5:
                _c5.execute("ALTER TABLE %s RENAME TO tst_db_conn" % _bak)
                _c5.commit()
        code, res = jreq("GET", "%s/api/db-conn" % base)
        chk("表已还原（后续检查不受影响）", code == 200)
    finally:
        # ⚠️ 硬约束：写库的用例必须自清理，跑完回到基线
        if _has_tbl:
            with sqlite3.connect(DB_PATH) as _c6:
                _c6.execute("DELETE FROM tst_db_conn WHERE conn_name LIKE 'E2E-conn-%'")
                _c6.commit()

    if _has_tbl:
        with sqlite3.connect(DB_PATH) as _c7:
            _conn_ids_after = {r[0] for r in _c7.execute("SELECT id FROM tst_db_conn")}
        chk("★ tst_db_conn 回到跑前的基线（本用例没留下残留行）",
            _conn_ids_after == _conn_ids_before,
            "before=%s after=%s" % (sorted(_conn_ids_before), sorted(_conn_ids_after)))

    # ---------------------------------------------------------- 6. 收尾
    print("\n■ 6. 收尾")
    cleanup()
    with sqlite3.connect(DB_PATH) as conn:
        left_srv = conn.execute("SELECT COUNT(*) FROM srv WHERE plat_name = ?", (E2E_SRV,)).fetchone()[0]
        left_mdl = conn.execute("SELECT COUNT(*) FROM mdl WHERE mdl_name = ?", (E2E_MDL,)).fetchone()[0]
    chk("本次校验写进去的 E2E 供应商 / 模型已清理", left_srv == 0 and left_mdl == 0,
        "srv=%s mdl=%s" % (left_srv, left_mdl))
    chk("基础数据回到基线（srv / mdl 未被污染）",
        db_count("srv") == srv0 and db_count("mdl") == mdl0,
        "srv=%d->%d mdl=%d->%d" % (srv0, db_count("srv"), mdl0, db_count("mdl")))

    print("\n" + "=" * 74)
    print("结果：%s" % ("全部通过" if not FAILS else "失败项 -> " + "; ".join(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
