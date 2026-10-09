# -*- coding: utf-8 -*-
"""PromptDeck · 页面内 SQL 调试（第四次迭代新增）。

对应「提示词构造」页模型输出区新增的「页面内调试」按钮：把**最新一次模型生成的
SQL** 交给本模块，在**服务端**真连库执行，把结果集回给页面渲染成表格。

与既有 `core/dbeaver.py`（唤起本机 DBeaver GUI）是**两条并行的路**，不是替代关系：
    · `/api/dbeaver/link`   → 唤起 DBeaver，SQL 进它自己的编辑器（凭据走 DBeaver）
    · `/api/database/execute-sql` → 本模块代跑，结果留在页面里（凭据走弹窗临时输入）
两条都在，「链接数据库调试」按钮行为不变。

安全口径（本模块存在的**全部意义**就在这四条，用户 2026-10-09 点名）：
  1. **只放行读语句**：单条 `SELECT` / `WITH`（CTE）开头，且全文再扫一遍
     `INSERT/UPDATE/DELETE/DROP/ALTER/CREATE/TRUNCATE/GRANT/REVOKE/CALL/
     EXEC/EXECUTE/MERGE/REPLACE/SET/USE/COPY/LOAD/SHUTDOWN` 等写/管理关键字，
     以及注释掉语句的 `--` `/* */`，命中即拒。**不靠单条正则蒙混**。
  2. **强制行数上限**：SQL 外面包一层 `LIMIT N`（各家方言取自己认的写法），
     再叠加 fetchmany 的客户端兜底，双保险，避免一条 `SELECT` 拖垮服务。
  3. **连接只读**（能设的地方就设）：PG `set_session(readonly=True)`；
     MySQL / SQL Server 用只读事务；SQLite 用 `mode=ro` 的 URI 打开。
  4. **密码不落库、不写文件**：只在函数入参里活一次，`finally` 里不保留引用；
     调用方（`views.sql_execute_api`）也不把它塞进任何会话或表。
     —— 与 dbeaver 路线「认证交给 DBeaver 自己的凭据」是同一道安全地基。

返回形状（成功）：
    {"ok": True, "columns": [{"key","title"}], "rows": [{...}],
     "elapsed": 24, "rowCount": N, "truncated": bool}
失败：
    {"ok": False, "error": 真实原因}   —— 驱动的报错原样透出，不加工。
"""
import os
import re
import sqlite3
import time

from . import dbx

# 结果集最多给多少行（服务端硬上限；前端分页只是展示层的事）
MAX_ROWS = int(os.environ.get("PD_SQL_MAX_ROWS", "500"))

# 执行超时（秒）—— 只读查询也可能在笛卡尔积上打转，得有兜底
RUN_TIMEOUT = int(os.environ.get("PD_SQL_TIMEOUT", "30"))

# ---------------------------------------------------------------- 读语句判定
# 允许出现在最前面的关键字：SELECT，或 WITH（CTE 本身也是读）
_ALLOW_HEAD = ("select", "with")

# 全文扫描的禁用词。命中即拒 —— 宁可误杀，也不放写语句过去。
_DENY_WORDS = (
    "insert", "update", "delete", "drop", "alter", "create", "truncate",
    "grant", "revoke", "commit", "rollback", "savepoint", "merge", "replace",
    "call", "exec", "execute", "do", "set", "use", "lock", "begin", "start",
    "shutdown", "copy", "load", "attach", "detach", "vacuum", "analyze",
    "declare", "fetch", "close", "open", "listen", "notify", "reset",
    "reindex", "cluster", "comment", "rename", "prepare", "deallocate",
    "handler", "load_file", "outfile", "dumpfile", "pg_read_file",
    "pg_ls_dir", "lo_import", "lo_export", "dblink", "pg_sleep",
    "benchmark", "xp_cmdshell", "sp_executesql", "load_extension",
)

# 注释符：先剥掉，否则 `-- DROP TABLE` 这种能绕过 _DENY_WORDS
_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)


def _err(msg):
    return {"ok": False, "error": msg}


def strip_comments(sql):
    """去掉行注释与块注释，并压掉多余空白（让「注释里藏写语句」这条路也堵住）。"""
    s = _BLOCK_COMMENT.sub(" ", _LINE_COMMENT.sub(" ", sql or ""))
    return re.sub(r"\s+", " ", s).strip()


def _mask(pwd):
    """错误信息里若混进了密码（少数驱动会把 DSN 原文吐出来），遮掩掉。"""
    return ("******" if pwd else "")


def check_readonly(sql):
    """校验这条 SQL 是不是一条可以放行的读语句。

    返回 (True, "") 或 (False, 拒绝原因)。**服务端二次校验是这里刻意加的** ——
    与项目其余部分「不做二次校验」的口径不同，因为这里执行的是用户可见的任意
    SQL 原文，漏一条写语句的后果是数据损坏，不是页面报错。
    """
    raw = strip_comments(sql)
    if not raw:
        return False, "SQL 为空"

    # 分号只能出现在末尾（允许尾随分号），中间出现即视为多语句注入
    body = raw.rstrip().rstrip(";").rstrip()
    if ";" in body:
        return False, "只允许执行单条语句，检测到多条语句"

    head = body.lstrip().split(None, 1)
    if not head or head[0].lower() not in _ALLOW_HEAD:
        return False, "只允许执行 SELECT / WITH 查询语句"

    low = re.sub(r"[^a-z0-9_]+", " ", body.lower())
    hits = [w for w in _DENY_WORDS if re.search(r"\b%s\b" % re.escape(w), low)]
    if hits:
        return False, "检测到不允许的语句类型（%s），只允许只读查询" % "、".join(hits[:3])

    # SELECT … INTO new_table：看着是查（T-SQL），实际是**建表**，属写操作
    if re.search(r"\bselect\b[\s\S]*?\binto\b", low):
        return False, "SELECT … INTO 会创建新表，只允许只读查询"

    return True, ""


def _wrap_limit(sql, kind, cap):
    """给 SQL 套上限。

    没有 LIMIT 才套；已经写了 LIMIT / TOP / FETCH FIRST 的**原样保留**，
    不去改写用户的语义（改写容易把 `LIMIT a, b` 这类改错）。
    """
    low = re.sub(r"[^a-z_a-z0-9]+", " ", (sql or "").lower())
    if re.search(r"\blimit\b", low) or re.search(r"\btop\b", low) \
            or re.search(r"\bfetch\s+first\b", low):
        return sql, False

    body = sql.rstrip().rstrip(";").rstrip()
    if kind == "SQL Server":
        # T-SQL 用 TOP，放在最前面
        return "SELECT TOP %d * FROM (\n%s\n) pd_limited" % (cap, body), True
    return "%s LIMIT %d" % (body, cap), True


def _rows_to_result(cur, rows):
    """把驱动返回的行整成 {columns, rows}，并做 JSON 友好的转换。"""
    cols = []
    for i, d in enumerate(getattr(cur, "description", None) or []):
        name = None
        if isinstance(d, (tuple, list)) and d:
            name = d[0]
        if not name:
            name = "col_%d" % (i + 1)
        cols.append({"key": "c%d" % i, "title": str(name)})

    out = []
    for r in rows:
        item = {}
        for i, c in enumerate(cols):
            item[c["key"]] = _jsonable(r[i] if i < len(r) else None)
        out.append(item)
    return cols, out


def _jsonable(v):
    """把驱动可能吐出的不可 JSON 化对象换成字符串（bytes / Decimal / date / …）。"""
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, (bytes, bytearray, memoryview)):
        try:
            return v.decode("utf-8")
        except Exception:
            return repr(bytes(v))
    if isinstance(v, (list, tuple)):
        return [ _jsonable(x) for x in v ]
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    return str(v)


def _run_on_cursor(cur, sql, cap):
    """在已有 cursor 上执行并取结果（SQLite 与服务器型共用）。

    截断探针：套 LIMIT 时**故意多放1 行**（cap+1），取回后若超过 cap 即说明
    下面还有数据 → 截断。否则外层 LIMIT 恰好等于 cap，多取一行也永远取不到，
    truncated 会恒为 False（UI 就少了一句「已截断」提示）。
    """
    wrapped, _ = _wrap_limit(sql, "generic", cap + 1)
    t0 = time.time()
    cur.execute(wrapped)
    rows = cur.fetchmany(cap + 1)
    truncated = len(rows) > cap
    if truncated:
        rows = rows[:cap]
    cols, data = _rows_to_result(cur, rows)
    return {"ok": True, "columns": cols, "rows": data,
            "elapsed": int((time.time() - t0) * 1000),
            "rowCount": len(data), "truncated": truncated}


# ---------------------------------------------------------------- SQLite
def _run_sqlite(sql, path, cap):
    """本地 SQLite 文件：用 mode=ro 的 URI 打开，从物理上不许写。"""
    if not path or not os.path.isfile(path):
        return _err("SQLite 数据库文件不存在：%s" % (path or "(空)"))
    uri = "file:%s?mode=ro" % os.path.abspath(path).replace("\\", "/").replace("?", "%3f").replace("#", "%23")
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=RUN_TIMEOUT)
    except Exception as exc:
        # 加密库 / 无读权限 / 文件被占用都在这里冒出来
        return _err("打开 SQLite 数据库失败：%s" % (str(exc).strip() or exc.__class__.__name__))
    try:
        conn.row_factory = None
        cur = conn.cursor()
        return _run_on_cursor(cur, sql, cap)
    except sqlite3.Error as exc:
        return _err("执行失败：%s" % (str(exc).strip() or exc.__class__.__name__))
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------- 服务器型
def _connect(kind, cfg):
    """按类型建连接。**连库参数与 dbx 的 _read_* 保持同形状**，
    这样「表结构提取」和「页面内调试」对同一个库的连法完全一致。

    返回 (conn, None) 或 (None, 错误字典)。
    """
    port = dbx._port(kind, cfg.get("port"))

    if kind == "MySQL":
        pymysql, err = dbx._import("pymysql", "MySQL")
        if pymysql is None:
            return None, err
        kw = dict(host=cfg.get("host"), port=port,
                  user=cfg.get("user") or None, password=cfg.get("pwd") or None,
                  charset="utf8mb4", connect_timeout=dbx.CONNECT_TIMEOUT,
                  read_timeout=RUN_TIMEOUT, write_timeout=RUN_TIMEOUT)
        if cfg.get("dbname"):
            kw["database"] = cfg["dbname"]
        return pymysql.connect(**kw), None

    if kind == "PostgreSQL":
        psycopg2, err = dbx._import("psycopg2", "PostgreSQL")
        if psycopg2 is None:
            return None, err
        kw = dict(host=cfg.get("host"), port=port, connect_timeout=dbx.CONNECT_TIMEOUT)
        if cfg.get("user"):
            kw["user"] = cfg["user"]
        if cfg.get("pwd"):
            kw["password"] = cfg["pwd"]
        if cfg.get("dbname"):
            kw["dbname"] = cfg["dbname"]
        conn = psycopg2.connect(**kw)
        conn.set_session(readonly=True)      # 只读事务
        return conn, None

    if kind == "SQL Server":
        pymssql, err = dbx._import("pymssql", "SQL Server")
        if pymssql is None:
            return None, err
        return pymssql.connect(server="%s,%s" % (cfg.get("host"), port),
                               user=cfg.get("user") or None,
                               password=cfg.get("pwd") or None,
                               database=cfg.get("dbname") or None,
                               login_timeout=dbx.CONNECT_TIMEOUT,
                               timeout=RUN_TIMEOUT), None

    if kind == "Oracle":
        oracledb, err = dbx._import("oracledb", "Oracle")
        if oracledb is None:
            return None, err
        kw = dict(user=cfg.get("user") or "", password=cfg.get("pwd") or "",
                  host=cfg.get("host"), port=port)
        if cfg.get("dbname"):
            kw["service_name"] = cfg["dbname"]
        try:
            return oracledb.connect(**kw), None
        except TypeError:
            dsn = oracledb.makedsn(cfg.get("host"), port,
                                   service_name=cfg.get("dbname") or None)
            return oracledb.connect(user=cfg.get("user") or "",
                                    password=cfg.get("pwd") or "", dsn=dsn), None

    if kind == "Db2 for LUW":
        ibm_db, err = dbx._import("ibm_db", "Db2 for LUW")
        if ibm_db is None:
            return None, err
        dsn = ("DATABASE=%s;HOSTNAME=%s;PORT=%s;PROTOCOL=TCPIP;UID=%s;PWD=%s;"
               % (cfg.get("dbname") or "", cfg.get("host"), port,
                  cfg.get("user") or "", cfg.get("pwd") or ""))
        return ibm_db.connect(dsn, "", ""), None

    return None, _err("暂不支持的数据库类型：%s" % (kind or "(空)"))


def run_sql(kind, cfg, sql, cap=None):
    """真连库执行一条只读 SQL。统一出口。

    参数
        kind : "SQLite" / "MySQL" / "PostgreSQL" / "SQL Server" / "Oracle" / "Db2 for LUW"
        cfg  : 连接信息。**`pwd` 只在这里活一次，不落库不写文件**
        sql  : 要执行的 SQL 原文
        cap  : 行数上限，缺省 MAX_ROWS
    """
    cap = int(cap or MAX_ROWS)
    if cap <= 0:
        cap = MAX_ROWS

    ok, why = check_readonly(sql)
    if not ok:
        return _err(why)

    body = strip_comments(sql)

    if kind == "SQLite":
        return _run_sqlite(body, cfg.get("file"), cap)

    #连接也要包try：连不上库是**最常见的用户场景**（服务没起 / 地址写错 / 网络不通），
    # 漏出去就是 500 白屏，而不是页面上那句能看懂的提示。
    pwd0 = cfg.get("pwd") or ""
    try:
        conn, err = _connect(kind, cfg)
    except Exception as exc:
        msg = str(exc).strip() or exc.__class__.__name__
        if pwd0 and pwd0 in msg:
            msg = msg.replace(pwd0, _mask(pwd0))
        return _err("连接 %s 失败：%s" % (kind, msg))
    if err is not None:
        return err

    pwd = cfg.get("pwd") or ""
    try:
        cur = conn.cursor()
        # 各家把「只读」表达得不一样，能设就设，设不了靠上面的关键字白名单兜
        try:
            if kind == "MySQL":
                cur.execute("SET SESSION TRANSACTION READ ONLY")
            elif kind == "SQL Server":
                cur.execute("SET TRANSACTION ISOLATION LEVEL SNAPSHOT")
        except Exception:
            pass        # 驱动/版本不支持只读设置：不因此失败，白名单已经拦过写语句

        wrapped, _ = _wrap_limit(body, kind, cap + 1)
        t0 = time.time()
        cur.execute(wrapped)
        rows = cur.fetchmany(cap + 1)
        truncated = len(rows) > cap
        if truncated:
            rows = rows[:cap]
        cols, data = _rows_to_result(cur, rows)
        return {"ok": True, "columns": cols, "rows": data,
                "elapsed": int((time.time() - t0) * 1000),
                "rowCount": len(data), "truncated": truncated}
    except Exception as exc:
        msg = str(exc).strip() or exc.__class__.__name__
        if pwd and pwd in msg:
            msg = msg.replace(pwd, _mask(pwd))
        return _err("执行失败：%s" % msg)
    finally:
        # 密码不留在任何长命对象上；连接必须还回去
        try:
            cfg["pwd"] = "" if isinstance(cfg, dict) else cfg
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass