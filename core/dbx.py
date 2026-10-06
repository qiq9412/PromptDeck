# -*- coding: utf-8 -*-
"""PromptDeck · 真实数据库驱动读结构（第二次迭代 · 第二批）。

对应原型页 `Table_Structure_Extraction.html` 的「连接数据库」弹窗（主机 / URL 两种方式）：
真的用各家的 Python 驱动连上去，把**库里真实的表 / 字段 / 注释 / 主键 / 外键 / 行数**读回来。

支持的类型（与原型左上角下拉的 6 项一一对应）：
    SQLite        —— 见 `core/schema.py::read_sqlite`（本地文件，不走这里）
    MySQL         —— pymysql
    PostgreSQL    —— psycopg2
    SQL Server    —— pymssql
    Oracle        —— oracledb（默认 thin 模式，纯 Python，不需要装 Oracle 客户端）
    Db2 for LUW   —— ibm_db_dbi

统一出口 `read_db(kind, cfg)`，返回形状与 `schema.read_sqlite` **完全一致**：
    {"ok": True, "name", "kind",
     "tables":   [{"name","rows","desc","descFrom","cols":[[字段名, 类型关键字, 字段说明, 真实类型]]}],
     "relations":[[从表, 从字段, 主表, 主字段, "外键关联"]]}
于是页面侧的数据层（`pdApplySchema` / `schemaOf` / `collectRels` / `buildSchemaHtml` / `buildText`）
一行都不用改。`cols[j][1] == "pk"` 表示主键（原型据此标「主键」并把类型映射成 INTEGER）。

三条口径（用户 2026-09-30 二次迭代第二批点名）：
  1. **真接驱动**：不伪造表树，读不到就说读不到；
  2. **服务端不做任何二次校验**：不校验必填 / 不校验端口格式，
     前端送什么就拿着去连，连不上就把**驱动的真实报错**原样回给页面；
  3. **只读**：全部走目录表（information_schema / pg_catalog / sys.* / all_* / SYSCAT.*）
     与 `SELECT COUNT(*)`，不写任何库。
"""
import importlib
import os
import re
from urllib.parse import unquote, urlparse

# 连接 / 读取超时（秒）。真连一个不可达的地址时靠它兜底，避免页面一直转。
CONNECT_TIMEOUT = int(os.environ.get("PD_DB_CONNECT_TIMEOUT", "10"))
READ_TIMEOUT = int(os.environ.get("PD_DB_READ_TIMEOUT", "30"))

# 类型 → 驱动包名（缺驱动时按它给出 pip 提示）
DRIVER_PKG = {
    "MySQL": "pymysql",
    "PostgreSQL": "psycopg2",
    "SQL Server": "pymssql",
    "Oracle": "oracledb",
    "Db2 for LUW": "ibm_db",
}

# 类型 → 默认端口（与原型弹窗里的 DEF_PORT 同值；仅当用户没填端口时兜底）
DEFAULT_PORT = {
    "MySQL": 3306,
    "PostgreSQL": 5432,
    "SQL Server": 1433,
    "Oracle": 1521,
    "Db2 for LUW": 50000,
}

# URL 模式：类型 → 默认 scheme（用户没写 scheme 时补上）
_URL_SCHEME = {
    "MySQL": "mysql",
    "PostgreSQL": "postgresql",
    "SQL Server": "mssql",
    "Oracle": "oracle",
    "Db2 for LUW": "db2",
}

INTERNAL_KINDS = tuple(DRIVER_PKG)

# Oracle 里 notnull 的列类型要带长度 / 精度：这些走「类型(长度)」
_ORA_LEN_TYPES = ("VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR", "RAW")
_ORA_NUM_TYPES = ("NUMBER", "FLOAT", "DECIMAL", "NUMERIC")


# ---------------------------------------------------------------- 工具
def _err(msg):
    return {"ok": False, "error": msg}


def _q(ident, ch='"'):
    """给标识符加引号（防关键字 / 大小写 / 空格）。仅用于我们拼进 SQL 的名字。"""
    return ch + str(ident).replace(ch, ch + ch) + ch


def _mk_col(name, realtype, note, is_pk):
    """一列：`[字段名, 类型关键字, 字段说明, 真实声明类型]`。

    第 2 位固定 `"pk"` 表示主键（原型 → 渲染成 INTEGER + 「主键」标记），
    第 4 位是库里的真实类型原文（原型只读前 3 位，这一位给「拼模型提示词」用）。
    """
    rt = (realtype or "").strip()
    return [name, "pk" if is_pk else rt, note or "", rt]


def _mk_table(name, rows, desc, cols):
    d = (desc or "").strip()
    return {"name": name, "rows": rows, "desc": d,
            "descFrom": "db" if d else "", "cols": cols}


def _count_rows(run, qualified):
    """真实行数。取不到不算错（可能是视图 / 无权限），按 0 处理。"""
    try:
        row = run('SELECT COUNT(*) FROM %s' % qualified)
        if row and row[0] is not None:
            return int(row[0])
    except Exception:
        pass
    return 0


# ---------------------------------------------------------------- 连接信息
def parse_url(url, kind):
    """解析 URL 模式的连接串。

    原型给的模板是 `mysql://用户名:密码@主机:端口/数据库名`，占位符里还出现过
    `jdbc:mysql://...`，所以这里把 `jdbc:` 前缀也吃掉；用户没写 scheme 时按类型补。
    返回值只含解析到的项，缺的由调用方兜底 —— 这里**不做校验**。
    """
    raw = (url or "").strip()
    if not raw:
        return {}
    if raw.lower().startswith("jdbc:"):
        raw = raw[5:]
    if "://" not in raw:
        raw = "%s://%s" % (_URL_SCHEME.get(kind, "db"), raw)
    try:
        u = urlparse(raw)
    except Exception:
        return {}
    out = {}
    if u.hostname:
        out["host"] = u.hostname
    try:
        if u.port:
            out["port"] = u.port
    except ValueError:
        # 端口不是数字 —— 原样留着，让驱动去报错（服务端不校验）
        m = re.search(r":(\d+|[^/@:]*)(?:/|$)", raw)
        if m and m.group(1):
            out["port"] = m.group(1)
    if u.username:
        out["user"] = unquote(u.username)
    if u.password:
        out["pwd"] = unquote(u.password)
    if u.path and u.path.strip("/"):
        out["dbname"] = unquote(u.path.strip("/"))
    return out


def normalize_cfg(kind, cfg):
    """把「连接弹窗送来的连接信息」整成统一形状；**不做任何必填 / 格式校验**。

    · mode == "url"  → 解析 URL（原型：URL 模式下 数据库名/用户名/密码 三个框整体隐藏，
                       这些信息都在 URL 里）；
    · mode == "host" → 用 host/port/user/pwd/dbname 五个框的值；
    · 主机留空 = 本机（原型口径：「非必填，留空表示本机（localhost）」）；
    · 端口留空 = 该类型的默认端口（原型弹窗本来就会预填）。
    """
    cfg = cfg or {}
    mode = (cfg.get("mode") or "host").strip().lower()
    out = {"mode": mode, "host": "", "port": "", "user": "", "pwd": "", "dbname": ""}

    if mode == "url":
        got = parse_url(cfg.get("url") or cfg.get("addr") or "", kind)
        for k in ("host", "port", "user", "pwd", "dbname"):
            if got.get(k) not in (None, ""):
                out[k] = got[k]
    else:
        out["host"] = str(cfg.get("host") or cfg.get("addr") or "").strip()
        out["port"] = str(cfg.get("port") or "").strip()
        out["user"] = str(cfg.get("user") or "").strip()
        out["pwd"] = str(cfg.get("pwd") or "")
        out["dbname"] = str(cfg.get("dbname") or "").strip()

    if not str(out["host"] or "").strip():
        out["host"] = "localhost"
    if not str(out["port"] or "").strip():
        out["port"] = DEFAULT_PORT.get(kind, 0)
    return out


def _port(kind, v):
    try:
        return int(str(v).strip())
    except Exception:
        return DEFAULT_PORT.get(kind, 0)


def _import(pkg, kind=""):
    """import 驱动包；缺了就返回 (None, 错误字典)。

    说明里带上 pip 命令，用户看到页面提示就能自救。
    """
    try:
        return importlib.import_module(pkg), None
    except Exception as exc:
        return None, _err("未安装 %s 驱动，无法连接 %s（pip install %s）：%s"
                          % (pkg, kind or pkg, pkg, exc))


# ---------------------------------------------------------------- 驱动可用性
def driver_status():
    """各类型驱动是否可用（只 import，不连接）。给校验脚本与页面提示用。"""
    out = {}
    for kind, pkg in DRIVER_PKG.items():
        try:
            mod = importlib.import_module(pkg)
            out[kind] = {"pkg": pkg, "ok": True,
                         "version": getattr(mod, "__version__", "")}
        except Exception as exc:
            out[kind] = {"pkg": pkg, "ok": False, "error": str(exc)}
    return out


# ================================================================ MySQL
def _read_mysql(cfg):
    pymysql, err = _import("pymysql", "MySQL")
    if pymysql is None:
        return err
    kw = dict(host=cfg["host"], port=_port("MySQL", cfg["port"]),
              user=cfg["user"] or None, password=cfg["pwd"] or None,
              charset="utf8mb4", connect_timeout=CONNECT_TIMEOUT,
              read_timeout=READ_TIMEOUT, write_timeout=READ_TIMEOUT)
    if cfg["dbname"]:
        kw["database"] = cfg["dbname"]

    conn = pymysql.connect(**kw)
    cur = conn.cursor()
    try:
        cur.execute("SELECT DATABASE()")
        row = cur.fetchone()
        schema = (row[0] if row else None) or cfg["dbname"]
        if not schema:
            return _err("已连上 MySQL，但连接里没有指定数据库名，无法确定要提取哪个库")

        cur.execute(
            "SELECT TABLE_NAME, IFNULL(TABLE_COMMENT, '') FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE' ORDER BY TABLE_NAME",
            (schema,))
        tnames = [(r[0], r[1] or "") for r in cur.fetchall()]

        # 外键一次性取全库
        fk = {}
        try:
            cur.execute(
                "SELECT TABLE_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME "
                "FROM information_schema.KEY_COLUMN_USAGE "
                "WHERE TABLE_SCHEMA = %s AND REFERENCED_TABLE_NAME IS NOT NULL "
                "ORDER BY TABLE_NAME, ORDINAL_POSITION", (schema,))
            for r in cur.fetchall():
                fk.setdefault(r[0], []).append([r[0], r[1] or "", r[2] or "", r[3] or "", "外键关联"])
        except Exception:
            pass

        tables, relations = [], []
        for tname, tdesc in tnames:
            cur.execute(
                "SELECT COLUMN_NAME, COLUMN_TYPE, COLUMN_KEY, IFNULL(COLUMN_COMMENT, '') "
                "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
                "ORDER BY ORDINAL_POSITION", (schema, tname))
            cols = [_mk_col(r[0], r[1] or "", r[3] or "", (r[2] or "").upper() == "PRI")
                    for r in cur.fetchall()]
            rows = _count_rows(lambda sql: (cur.execute(sql), cur.fetchone())[1],
                               "%s.%s" % (_q(schema, "`"), _q(tname, "`")))
            tables.append(_mk_table(tname, rows, tdesc, cols))
            relations.extend(fk.get(tname, []))

        return {"ok": True, "name": str(schema), "kind": "MySQL",
                "tables": tables, "relations": relations}
    finally:
        try:
            cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


# ================================================================ PostgreSQL
def _read_postgresql(cfg):
    psycopg2, err = _import("psycopg2", "PostgreSQL")
    if psycopg2 is None:
        return err
    kw = dict(host=cfg["host"], port=_port("PostgreSQL", cfg["port"]),
              connect_timeout=CONNECT_TIMEOUT)
    if cfg["user"]:
        kw["user"] = cfg["user"]
    if cfg["pwd"]:
        kw["password"] = cfg["pwd"]
    if cfg["dbname"]:
        kw["dbname"] = cfg["dbname"]

    conn = psycopg2.connect(**kw)
    conn.set_session(readonly=True)          # 只读：连上了也不许写
    cur = conn.cursor()
    try:
        cur.execute("SELECT current_schema()")
        row = cur.fetchone()
        schema = (row[0] if row else None) or "public"

        # 表 + 表注释（pg_description）
        cur.execute(
            "SELECT c.relname, COALESCE(obj_description(c.oid, 'pg_class'), '') "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relkind IN ('r', 'p') "
            "AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = c.oid "
            "                AND d.deptype = 'i') "
            "ORDER BY c.relname", (schema,))
        tnames = cur.fetchall()

        # 外键一次性取全 schema（支持多列外键：按 conkey/confkey 的位置逐列配对）
        fkmap = {}
        try:
            cur.execute(
                "SELECT cl.relname, a.attname, fcl.relname, fa.attname "
                "FROM pg_constraint con "
                "JOIN pg_class cl  ON cl.oid  = con.conrelid "
                "JOIN pg_class fcl ON fcl.oid = con.confrelid "
                "JOIN pg_namespace n ON n.oid = cl.relnamespace "
                "JOIN generate_subscripts(con.conkey, 1) s ON true "
                "JOIN pg_attribute a  ON a.attrelid  = con.conrelid  AND a.attnum  = con.conkey[s] "
                "JOIN pg_attribute fa ON fa.attrelid = con.confrelid AND fa.attnum = con.confkey[s] "
                "WHERE con.contype = 'f' AND n.nspname = %s", (schema,))
            for r in cur.fetchall():
                fkmap.setdefault(r[0], []).append([r[0], r[1] or "", r[2] or "", r[3] or "", "外键关联"])
        except Exception:
            pass

        tables, relations = [], []
        for tname, tdesc in tnames:
            qualified = "%s.%s" % (_q(schema), _q(tname))
            cur.execute(
                "SELECT a.attname, format_type(a.atttypid, a.atttypmod), "
                "       COALESCE(col_description(a.attrelid, a.attnum), '') "
                "FROM pg_attribute a "
                "WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped "
                "ORDER BY a.attnum", (qualified,))
            raw = cur.fetchall()

            pks = set()
            try:
                cur.execute(
                    "SELECT a.attname FROM pg_index i "
                    "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
                    "WHERE i.indrelid = %s::regclass AND i.indisprimary", (qualified,))
                pks = {r[0] for r in cur.fetchall()}
            except Exception:
                pass

            cols = [_mk_col(r[0], r[1] or "", r[2] or "", r[0] in pks) for r in raw]
            rows = _count_rows(lambda sql: (cur.execute(sql), cur.fetchone())[1], qualified)
            tables.append(_mk_table(tname, rows, tdesc, cols))
            relations.extend(fkmap.get(tname, []))

        return {"ok": True, "name": str(schema), "kind": "PostgreSQL",
                "tables": tables, "relations": relations}
    finally:
        try:
            cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


# ================================================================ SQL Server
def _read_sqlserver(cfg):
    pymssql, err = _import("pymssql", "SQL Server")
    if pymssql is None:
        return err
    kw = dict(server=cfg["host"], port=_port("SQL Server", cfg["port"]),
              login_timeout=CONNECT_TIMEOUT, timeout=READ_TIMEOUT, charset="UTF-8")
    if cfg["user"]:
        kw["user"] = cfg["user"]
    if cfg["pwd"]:
        kw["password"] = cfg["pwd"]
    if cfg["dbname"]:
        kw["database"] = cfg["dbname"]

    conn = pymssql.connect(**kw)
    cur = conn.cursor(as_dict=False)
    try:
        cur.execute(
            "SELECT s.name, t.name, CAST(ep.value AS NVARCHAR(500)) "
            "FROM sys.tables t "
            "JOIN sys.schemas s ON s.schema_id = t.schema_id "
            "LEFT JOIN sys.extended_properties ep "
            "  ON ep.major_id = t.object_id AND ep.minor_id = 0 AND ep.name = 'MS_Description' "
            "ORDER BY s.name, t.name")
        tnames = cur.fetchall()

        fkmap = {}
        try:
            cur.execute(
                "SELECT OBJECT_SCHEMA_NAME(fkc.parent_object_id), OBJECT_NAME(fkc.parent_object_id), "
                "       pc.name, "
                "       OBJECT_SCHEMA_NAME(fkc.referenced_object_id), OBJECT_NAME(fkc.referenced_object_id), "
                "       rc.name "
                "FROM sys.foreign_key_columns fkc "
                "JOIN sys.columns pc ON pc.object_id = fkc.parent_object_id "
                "     AND pc.column_id = fkc.parent_column_id "
                "JOIN sys.columns rc ON rc.object_id = fkc.referenced_object_id "
                "     AND rc.column_id = fkc.referenced_column_id")
            for r in cur.fetchall():
                fkmap.setdefault((r[0], r[1]), []).append(
                    [r[1] or "", r[2] or "", r[4] or "", r[5] or "", "外键关联"])
        except Exception:
            pass

        tables, relations = [], []
        for sch, tname, tdesc in tnames:
            qualified = "[%s].[%s]" % (sch, tname)
            cur.execute(
                "SELECT c.name, ty.name, c.max_length, c.precision, c.scale, "
                "       CAST(ep.value AS NVARCHAR(500)) "
                "FROM sys.columns c "
                "JOIN sys.types ty ON ty.user_type_id = c.user_type_id "
                "LEFT JOIN sys.extended_properties ep "
                "  ON ep.major_id = c.object_id AND ep.minor_id = c.column_id "
                "     AND ep.name = 'MS_Description' "
                "WHERE c.object_id = OBJECT_ID(%s) ORDER BY c.column_id", (qualified,))
            raw = cur.fetchall()

            pks = set()
            try:
                cur.execute(
                    "SELECT c.name FROM sys.indexes i "
                    "JOIN sys.index_columns ic ON ic.object_id = i.object_id "
                    "     AND ic.index_id = i.index_id "
                    "JOIN sys.columns c ON c.object_id = ic.object_id "
                    "     AND c.column_id = ic.column_id "
                    "WHERE i.object_id = OBJECT_ID(%s) AND i.is_primary_key = 1", (qualified,))
                pks = {r[0] for r in cur.fetchall()}
            except Exception:
                pass

            cols = []
            for cname, tname_, maxlen, prec, scale, note in raw:
                cols.append(_mk_col(cname, _mssql_type(tname_, maxlen, prec, scale),
                                    note or "", cname in pks))
            rows = _count_rows(lambda sql: (cur.execute(sql), cur.fetchone())[1], qualified)
            tables.append(_mk_table(tname, rows, tdesc, cols))
            relations.extend(fkmap.get((sch, tname), []))

        db = cfg["dbname"] or cfg["host"]
        return {"ok": True, "name": str(db), "kind": "SQL Server",
                "tables": tables, "relations": relations}
    finally:
        try:
            cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


def _mssql_type(typename, maxlen, prec, scale):
    """sys.types 的名字 + sys.columns 的长度/精度 → 可读类型（VARCHAR(50) / DECIMAL(10,2)）。"""
    t = (typename or "").lower()
    if t in ("varchar", "char", "varbinary", "binary"):
        n = "max" if maxlen in (-1, None) else str(maxlen)
        return "%s(%s)" % (typename, n)
    if t in ("nvarchar", "nchar"):
        n = "max" if maxlen in (-1, None) else str(int(maxlen) // 2 if maxlen else 0)
        return "%s(%s)" % (typename, n)
    if t in ("decimal", "numeric"):
        return "%s(%s,%s)" % (typename, prec, scale)
    return typename or ""


# ================================================================ Oracle
def _read_oracle(cfg):
    oracledb, err = _import("oracledb", "Oracle")
    if oracledb is None:
        return err
    kw = dict(user=cfg["user"] or "", password=cfg["pwd"] or "",
              host=cfg["host"], port=_port("Oracle", cfg["port"]))
    if cfg["dbname"]:
        kw["service_name"] = cfg["dbname"]      # 原型的「数据库名」在本类型 = 服务名
    try:
        conn = oracledb.connect(**kw)
    except TypeError:
        # 驱动不接受某些 kw（极端版本差异）时，退回用连接串拼
        dsn = oracledb.makedsn(cfg["host"], _port("Oracle", cfg["port"]),
                               service_name=cfg["dbname"] or None)
        conn = oracledb.connect(user=cfg["user"] or "", password=cfg["pwd"] or "", dsn=dsn)

    cur = conn.cursor()
    try:
        cur.execute("SELECT SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA') FROM DUAL")
        row = cur.fetchone()
        owner = (row[0] if row else None) or (cfg["user"] or "").upper()

        cur.execute(
            "SELECT TABLE_NAME, COALESCE(COMMENTS, '') FROM ALL_TAB_COMMENTS "
            "WHERE OWNER = :1 AND TABLE_TYPE = 'TABLE' ORDER BY TABLE_NAME", (owner,))
        tnames = cur.fetchall()

        fkmap = {}
        try:
            cur.execute(
                "SELECT ac.TABLE_NAME, fkcc.COLUMN_NAME, rc.TABLE_NAME, rkcc.COLUMN_NAME "
                "FROM ALL_CONSTRAINTS ac "
                "JOIN ALL_CONS_COLUMNS fkcc ON fkcc.OWNER = ac.OWNER "
                "     AND fkcc.CONSTRAINT_NAME = ac.CONSTRAINT_NAME "
                "JOIN ALL_CONSTRAINTS rc ON rc.OWNER = ac.R_OWNER "
                "     AND rc.CONSTRAINT_NAME = ac.R_CONSTRAINT_NAME "
                "JOIN ALL_CONS_COLUMNS rkcc ON rkcc.OWNER = rc.OWNER "
                "     AND rkcc.CONSTRAINT_NAME = rc.CONSTRAINT_NAME "
                "     AND rkcc.POSITION = fkcc.POSITION "
                "WHERE ac.OWNER = :1 AND ac.CONSTRAINT_TYPE = 'R'", (owner,))
            for r in cur.fetchall():
                fkmap.setdefault(r[0], []).append([r[0], r[1] or "", r[2] or "", r[3] or "", "外键关联"])
        except Exception:
            pass

        tables, relations = [], []
        for tname, tdesc in tnames:
            cur.execute(
                "SELECT c.COLUMN_NAME, c.DATA_TYPE, c.DATA_LENGTH, c.CHAR_LENGTH, "
                "       c.DATA_PRECISION, c.DATA_SCALE, COALESCE(cc.COMMENTS, '') "
                "FROM ALL_TAB_COLUMNS c "
                "LEFT JOIN ALL_COL_COMMENTS cc ON cc.OWNER = c.OWNER "
                "     AND cc.TABLE_NAME = c.TABLE_NAME AND cc.COLUMN_NAME = c.COLUMN_NAME "
                "WHERE c.OWNER = :1 AND c.TABLE_NAME = :2 ORDER BY c.COLUMN_ID", (owner, tname))
            raw = cur.fetchall()

            pks = set()
            try:
                cur.execute(
                    "SELECT fkcc.COLUMN_NAME FROM ALL_CONS_COLUMNS fkcc "
                    "JOIN ALL_CONSTRAINTS ac ON ac.OWNER = fkcc.OWNER "
                    "     AND ac.CONSTRAINT_NAME = fkcc.CONSTRAINT_NAME "
                    "WHERE ac.OWNER = :1 AND ac.TABLE_NAME = :2 AND ac.CONSTRAINT_TYPE = 'P'",
                    (owner, tname))
                pks = {r[0] for r in cur.fetchall()}
            except Exception:
                pass

            cols = [_mk_col(r[0], _ora_type(r[1], r[2], r[3], r[4], r[5]), r[6] or "", r[0] in pks)
                    for r in raw]
            rows = _count_rows(lambda sql: (cur.execute(sql), cur.fetchone())[1],
                               "%s.%s" % (_q(owner), _q(tname)))
            tables.append(_mk_table(tname, rows, tdesc, cols))
            relations.extend(fkmap.get(tname, []))

        return {"ok": True, "name": str(owner), "kind": "Oracle",
                "tables": tables, "relations": relations}
    finally:
        try:
            cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


def _ora_type(dtype, data_len, char_len, prec, scale):
    """Oracle 的 DATA_TYPE + 长度/精度 → 可读类型（VARCHAR2(50) / NUMBER(10,2)）。"""
    t = (dtype or "").upper()
    if t in _ORA_LEN_TYPES:
        n = char_len or data_len
        return "%s(%s)" % (t, n) if n else t
    if t in _ORA_NUM_TYPES:
        if prec and scale:
            return "%s(%s,%s)" % (t, prec, scale)
        if prec and scale in (0, None):
            return "%s(%s)" % (t, prec) if prec else t
        return t
    return t


# ================================================================ Db2 for LUW
def _read_db2(cfg):
    ibm_db_dbi, err = _import("ibm_db_dbi", "Db2 for LUW")
    if ibm_db_dbi is None:
        return err
    dsn = ("DATABASE=%s;HOSTNAME=%s;PORT=%d;PROTOCOL=TCPIP;CONNECTTIMEOUT=%d;"
           % (cfg["dbname"] or "", cfg["host"], _port("Db2 for LUW", cfg["port"]),
              CONNECT_TIMEOUT))
    conn = ibm_db_dbi.connect(dsn, cfg["user"] or "", cfg["pwd"] or "")
    cur = conn.cursor()
    try:
        try:
            cur.execute("VALUES CURRENT SCHEMA")
            row = cur.fetchone()
            owner = (row[0] if row else None) or (cfg["user"] or "").upper()
        except Exception:
            owner = (cfg["user"] or "").upper()
        owner = str(owner).strip().upper()

        cur.execute(
            "SELECT TABNAME, COALESCE(REMARKS, '') FROM SYSCAT.TABLES "
            "WHERE TABSCHEMA = ? AND TYPE = 'T' ORDER BY TABNAME", (owner,))
        tnames = cur.fetchall()

        fkmap = {}
        try:
            cur.execute(
                "SELECT r.TABNAME, fkc.COLNAME, r.REFTABNAME, rkc.COLNAME "
                "FROM SYSCAT.REFERENCES r "
                "JOIN SYSCAT.KEYCOLUSE fkc ON fkc.CONSTNAME = r.CONSTNAME "
                "     AND fkc.TABSCHEMA = r.TABSCHEMA AND fkc.TABNAME = r.TABNAME "
                "JOIN SYSCAT.KEYCOLUSE rkc ON rkc.CONSTNAME = r.REFKEYNAME "
                "     AND rkc.TABSCHEMA = r.REFTABSCHEMA AND rkc.TABNAME = r.REFTABNAME "
                "     AND rkc.COLSEQ = fkc.COLSEQ "
                "WHERE r.TABSCHEMA = ?", (owner,))
            for r in cur.fetchall():
                fkmap.setdefault(r[0], []).append([r[0], r[1] or "", r[2] or "", r[3] or "", "外键关联"])
        except Exception:
            pass

        tables, relations = [], []
        for tname, tdesc in tnames:
            cur.execute(
                "SELECT COLNAME, TYPENAME, LENGTH, SCALE, COALESCE(REMARKS, '') "
                "FROM SYSCAT.COLUMNS WHERE TABSCHEMA = ? AND TABNAME = ? "
                "ORDER BY COLNO", (owner, tname))
            raw = cur.fetchall()

            pks = set()
            try:
                cur.execute(
                    "SELECT k.COLNAME FROM SYSCAT.KEYCOLUSE k "
                    "JOIN SYSCAT.TABCONST tc ON tc.CONSTNAME = k.CONSTNAME "
                    "     AND tc.TABSCHEMA = k.TABSCHEMA AND tc.TABNAME = k.TABNAME "
                    "WHERE tc.TYPE = 'P' AND k.TABSCHEMA = ? AND k.TABNAME = ?",
                    (owner, tname))
                pks = {r[0] for r in cur.fetchall()}
            except Exception:
                pass

            cols = [_mk_col(r[0], _db2_type(r[1], r[2], r[3]), r[4] or "", r[0] in pks)
                    for r in raw]
            rows = _count_rows(lambda sql: (cur.execute(sql), cur.fetchone())[1],
                               "%s.%s" % (_q(owner), _q(tname)))
            tables.append(_mk_table(tname, rows, tdesc, cols))
            relations.extend(fkmap.get(tname, []))

        return {"ok": True, "name": owner, "kind": "Db2 for LUW",
                "tables": tables, "relations": relations}
    finally:
        try:
            cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


def _db2_type(typename, length, scale):
    t = (typename or "").strip().upper()
    if t in ("VARCHAR", "CHARACTER", "CHAR", "GRAPHIC", "VARGRAPHIC", "DECIMAL", "NUMERIC"):
        if scale:
            return "%s(%s,%s)" % (t, length, scale)
        return "%s(%s)" % (t, length) if length else t
    return t


# ---------------------------------------------------------------- 统一出口
_READERS = {
    "MySQL": _read_mysql,
    "PostgreSQL": _read_postgresql,
    "SQL Server": _read_sqlserver,
    "Oracle": _read_oracle,
    "Db2 for LUW": _read_db2,
}


def read_db(kind, cfg):
    """真连一个数据库并读回真实结构。

    返回 {"ok": True, "name", "kind", "tables", "relations"}
     或  {"ok": False, "error": 真实原因}（缺驱动 / 连不上 / 没权限 … 原样透出）。
    """
    if kind not in _READERS:
        return _err("暂不支持的数据库类型：%s" % (kind or "(空)"))

    dsn = normalize_cfg(kind, cfg)
    try:
        return _READERS[kind](dsn)
    except Exception as exc:
        msg = str(exc).strip() or exc.__class__.__name__
        return _err("连接 %s 失败：%s" % (kind, msg))
