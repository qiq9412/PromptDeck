# -*- coding: utf-8 -*-
"""PromptDeck 数据层。

数据库：SQLite，落盘为 data/prompt_test_DB_twe.db（全项目统一到这一个库文件）。
表结构由用户提供的 MySQL 建表语句等价转换而来：
    - AUTO_INCREMENT  -> INTEGER PRIMARY KEY AUTOINCREMENT
    - VARCHAR / DECIMAL -> TEXT / DECIMAL（SQLite 为动态类型，仅作亲和性声明）
    - 外键级联 ON DELETE CASCADE / ON UPDATE CASCADE 保留，读取连接会打开
      PRAGMA foreign_keys=ON（SQLite 默认关闭，必须每连接显式开启）。
"""
import os
import re
import sqlite3
from contextlib import contextmanager

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 默认落在 data/prompt_test_DB_twe.db —— 与 Django 版（config/settings.py）同一个库文件，
# 换库后全项目只认这一个口径；旧库 data/prompt_test_DB.db 原样保留，只作对照，不再被任何代码引用。
# 部署到容器时用环境变量 PROMPTDECK_DB 指到持久化目录（如创空间的 /mnt/workspace/…），
# 因为容器默认文件系统每次重启都会还原。
DB_PATH = os.environ.get("PROMPTDECK_DB") or os.path.join(BASE_DIR, "data", "prompt_test_DB_twe.db")

# =========================================
# 建表语句（对应 srv / mdl 两张表）
# =========================================
DDL = """
CREATE TABLE IF NOT EXISTS srv (
  id         INTEGER       PRIMARY KEY AUTOINCREMENT,
  plat_name  TEXT          NOT NULL,
  base_url   TEXT          DEFAULT NULL,
  comp_name  TEXT          DEFAULT NULL,
  home_url   TEXT          DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS mdl (
  id         INTEGER       PRIMARY KEY AUTOINCREMENT,
  srv_id     INTEGER       NOT NULL,
  mdl_name   TEXT          NOT NULL,
  mdl_type   TEXT          DEFAULT NULL,
  -- 计费单价按「用户输入的原文」保存，因此用 TEXT：
  -- SQLite 的 DECIMAL 是 NUMERIC 亲和性，会把 '18.00' 归一成 18.0，
  -- 用户输入的小数位数（如 4.135、0.0005）会被吃掉、显示时又被四舍五入。
  in_price   TEXT          DEFAULT NULL,
  out_price  TEXT          DEFAULT NULL,
  unit       TEXT          DEFAULT NULL,
  url        TEXT          DEFAULT NULL,
  CONSTRAINT fk_mdl_srv FOREIGN KEY (srv_id) REFERENCES srv (id)
      ON DELETE CASCADE ON UPDATE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_srv_id ON mdl (srv_id);
"""

# 首次建库时灌入的演示数据（取自 list_server.html 原型表格，home_url 取公司首页）
SEED = [
    ("OpenAI",             "https://api.openai.com/v1",                        "OpenAI, Inc.",                            "https://openai.com"),
    ("Anthropic Claude",   "https://api.anthropic.com/v1",                     "Anthropic PBC",                           "https://www.anthropic.com"),
    ("Google Gemini",      "https://generativelanguage.googleapis.com/v1beta",  "Google LLC",                              "https://about.google"),
    ("DeepSeek 深度求索",   "https://api.deepseek.com/v1",                      "杭州深度求索人工智能基础技术研究有限公司",     "https://www.deepseek.com"),
    ("通义千问 Qwen",       "https://dashscope.aliyuncs.com/api/v1",            "阿里巴巴集团",                             "https://www.aliyun.com"),
    ("文心一言 ERNIE",      "https://qianfan.baidubce.com/v2",                  "北京百度网讯科技有限公司",                   "https://www.baidu.com"),
    ("智谱 GLM",           "https://open.bigmodel.cn/api/paas/v4",             "北京智谱华章科技有限公司",                   "https://www.zhipuai.cn"),
    ("Kimi 月之暗面",       "https://api.moonshot.cn/v1",                       "北京月之暗面科技有限公司",                   "https://www.moonshot.cn"),
    ("讯飞星火 Spark",      "https://spark-api-open.xf-yun.com/v1",             "科大讯飞股份有限公司",                       "https://www.xfyun.cn"),
    ("腾讯混元 Hunyuan",    "https://api.hunyuan.cloud.tencent.com/v1",         "深圳市腾讯计算机系统有限公司",               "https://www.tencent.com"),
    ("火山方舟 Ark",        "https://ark.cn-beijing.volces.com/api/v3",         "北京火山引擎科技有限公司",                   "https://www.volcengine.com"),
    ("百川智能 Baichuan",   "https://api.baichuan-ai.com/v1",                   "北京百川智能科技有限公司",                   "https://www.baichuan-ai.com"),
]

# 首次建库时灌入的模型演示数据（取自 foundation1_models.html 原型表格）
# (平台/官方名, 平台首页网址, 模型名, 类型, 输入计费, 输出计费, 单位)
# 计费单价写成字符串：按用户输入的原文保存，显示时不再四舍五入、小数位数原样保留。
# 平台若不在 SEED 中（如「硅基流动 SiliconFlow」），灌数据时会自动补一条 srv。
SEED_MDL = [
    ("OpenAI",              "https://openai.com",          "gpt-4o",               "多模态",    "18.00",  "72.00", "K"),
    ("OpenAI",              "https://openai.com",          "gpt-4o-mini",          "文本生成",   "1.08",   "4.32", "百万"),
    ("Anthropic Claude",    "https://www.anthropic.com",   "claude-3-5-sonnet",    "多模态",    "21.60", "108.00", "K"),
    ("Google Gemini",       "https://about.google",        "gemini-1.5-pro",       "多模态",     "9.00",  "36.00", "K"),
    ("DeepSeek 深度求索",    "https://www.deepseek.com",    "deepseek-chat",        "文本生成",   "1.00",   "2.00", "百万"),
    ("DeepSeek 深度求索",    "https://www.deepseek.com",    "deepseek-reasoner",    "推理模型",   "4.00",  "16.00", "万"),
    ("通义千问 Qwen",        "https://www.aliyun.com",      "qwen-max",             "文本生成",  "20.00",  "60.00", "K"),
    ("通义千问 Qwen",        "https://www.aliyun.com",      "qwen-vl-max",          "多模态",    "16.00",  "48.00", "K"),
    ("文心一言 ERNIE",       "https://www.baidu.com",       "ernie-4.0-8k",         "文本生成",  "30.00",  "90.00", "K"),
    ("智谱 GLM",            "https://www.zhipuai.cn",      "glm-4-plus",           "文本生成",  "50.00",  "50.00", "K"),
    ("智谱 GLM",            "https://www.zhipuai.cn",      "embedding-3",          "向量嵌入",   "0.50",   "0.00", "百万"),
    ("Kimi 月之暗面",        "https://www.moonshot.cn",     "moonshot-v1-128k",     "文本生成",  "60.00",  "60.00", "K"),
    ("讯飞星火 Spark",       "https://www.xfyun.cn",        "spark-max",            "文本生成",  "30.00",  "30.00", "K"),
    ("腾讯混元 Hunyuan",     "https://www.tencent.com",     "hunyuan-turbo",        "文本生成",  "15.00",  "50.00", "K"),
    ("火山方舟 Ark",         "https://www.volcengine.com",  "doubao-pro-32k",       "文本生成",   "0.80",   "2.00", "百万"),
    ("火山方舟 Ark",         "https://www.volcengine.com",  "doubao-vision-pro",    "多模态",     "3.00",   "9.00", "万"),
    ("百川智能 Baichuan",    "https://www.baichuan-ai.com", "Baichuan4-Turbo",      "文本生成",  "15.00",  "15.00", "K"),
    ("硅基流动 SiliconFlow", "https://www.siliconflow.cn",  "Qwen2.5-72B-Instruct", "文本生成",   "4.13",   "4.13", "十万"),
]


@contextmanager
def connect():
    """打开连接：行工厂为 Row（可当 dict 用），开启外键，正常退出即提交。"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """建表（幂等）+ 计费列迁移。返回值：是否发生过迁移。"""
    with connect() as conn:
        conn.executescript(DDL)
        return migrate_price_columns(conn)


def _num_price_text(n):
    """数值 -> 文本：不四舍五入，按「最短可表示的原始位数」保留小数。

    只用于老库迁移出来的数值列（新数据一律是字符串、走 price_text 原样返回）。
    规则：小数位 <= 2 的补足两位（18.0 -> "18.00"、21.6 -> "21.60"），
          小数位 > 2 的原样保留（4.135 -> "4.135"、0.0005 -> "0.0005"）。
    """
    n = float(n)
    r = repr(n)
    if "e" in r or "E" in r:                     # 科学计数法：展开后去掉多余的 0
        r = ("%.10f" % n).rstrip("0").rstrip(".") or "0"
    if "." in r:
        dec = r.split(".", 1)[1]
        return ("%.2f" % n) if len(dec) <= 2 else r
    return r + ".00"


def price_text(v):
    """把计费单价规范成「原样保留小数位数」的文本。

    - 字符串：去掉首尾空格后原样返回（用户输入什么就存什么，如 "18.00"、"4.135"）
    - 数值：老库遗留的 DECIMAL 值，交给 _num_price_text 按原始位数还原
    - 空值 / 空串：None（对应库里的 NULL）
    """
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, str):
        return v.strip() or None
    if isinstance(v, (int, float)):
        return _num_price_text(v)
    return str(v).strip() or None


def migrate_price_columns(conn):
    """一次性迁移：把 in_price / out_price 从 DECIMAL（NUMERIC 亲和性）换成 TEXT。

    背景：SQLite 没有真正的 DECIMAL，DECIMAL 走 NUMERIC 亲和性，写入 '18.00'
    会被归一成 18.0 —— 用户输入的小数位数当场就没了，前端再 toFixed(2) 又会
    四舍五入一次。改成 TEXT 后可以按原文存取。已存在的数值按 6 位小数规范化写回。
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'mdl'"
    ).fetchone()
    if not row or not row["sql"]:
        return False
    if re.search(r"in_price\s+TEXT", row["sql"], re.I):
        return False

    old = conn.execute(
        "SELECT id, srv_id, mdl_name, mdl_type, in_price, out_price, unit, url FROM mdl"
    ).fetchall()

    conn.execute("DROP INDEX IF EXISTS idx_srv_id")
    conn.execute("DROP TABLE IF EXISTS mdl_text")
    conn.execute(
        """
        CREATE TABLE mdl_text (
          id         INTEGER       PRIMARY KEY AUTOINCREMENT,
          srv_id     INTEGER       NOT NULL,
          mdl_name   TEXT          NOT NULL,
          mdl_type   TEXT          DEFAULT NULL,
          in_price   TEXT          DEFAULT NULL,
          out_price  TEXT          DEFAULT NULL,
          unit       TEXT          DEFAULT NULL,
          url        TEXT          DEFAULT NULL,
          CONSTRAINT fk_mdl_srv FOREIGN KEY (srv_id) REFERENCES srv (id)
              ON DELETE CASCADE ON UPDATE CASCADE
        )
        """
    )
    conn.executemany(
        "INSERT INTO mdl_text (id, srv_id, mdl_name, mdl_type, in_price, out_price, unit, url) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (r["id"], r["srv_id"], r["mdl_name"], r["mdl_type"],
             price_text(r["in_price"]), price_text(r["out_price"]), r["unit"], r["url"])
            for r in old
        ],
    )
    conn.execute("DROP TABLE mdl")
    conn.execute("ALTER TABLE mdl_text RENAME TO mdl")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_srv_id ON mdl (srv_id)")
    return True


def seed_if_empty():
    """srv 表为空时灌入演示数据；返回实际写入条数。"""
    with connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM srv").fetchone()[0]
        if n:
            return 0
        conn.executemany(
            "INSERT INTO srv (plat_name, base_url, comp_name, home_url) VALUES (?, ?, ?, ?)",
            SEED,
        )
        return len(SEED)


# ---------------------------------------------------------------- srv CRUD
def list_srv(platform="", company=""):
    """按 平台/官方名、公司名 做不区分大小写的模糊匹配，未传则不过滤。"""
    sql = "SELECT id, plat_name, base_url, comp_name, home_url FROM srv WHERE 1 = 1"
    args = []
    if platform:
        sql += " AND lower(plat_name) LIKE ?"
        args.append("%" + platform.lower() + "%")
    if company:
        sql += " AND lower(comp_name) LIKE ?"
        args.append("%" + company.lower() + "%")
    sql += " ORDER BY id ASC"
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def get_srv(srv_id):
    with connect() as conn:
        row = conn.execute(
            "SELECT id, plat_name, base_url, comp_name, home_url FROM srv WHERE id = ?",
            (srv_id,),
        ).fetchone()
        return dict(row) if row else None


def create_srv(plat_name, base_url="", comp_name="", home_url=""):
    """写入一条服务器记录，返回新主键 id。不做服务器端校验（按项目约定）。"""
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO srv (plat_name, base_url, comp_name, home_url) VALUES (?, ?, ?, ?)",
            (plat_name, base_url or None, comp_name or None, home_url or None),
        )
        return cur.lastrowid


def update_srv(srv_id, plat_name, base_url="", comp_name="", home_url=""):
    """按主键更新一条服务器记录，返回受影响行数。

    空串按 NULL 落库；不做服务器端校验（必填 / 网址格式只保留在原型前端）。
    home_url 同时是模型列表里「平台名」的外链目标，改这里会同步生效。
    """
    with connect() as conn:
        cur = conn.execute(
            "UPDATE srv SET plat_name = ?, base_url = ?, comp_name = ?, home_url = ? "
            "WHERE id = ?",
            (plat_name, base_url or None, comp_name or None, home_url or None, int(srv_id)),
        )
        return cur.rowcount


def delete_srv(ids):
    """批量删除，返回受影响行数。mdl 由外键 ON DELETE CASCADE 一并清理。"""
    ids = [int(i) for i in ids]
    if not ids:
        return 0
    holders = ",".join("?" * len(ids))
    with connect() as conn:
        cur = conn.execute("DELETE FROM srv WHERE id IN (%s)" % holders, ids)
        return cur.rowcount


def count_srv():
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM srv").fetchone()[0]


# ---------------------------------------------------------------- mdl CRUD
def _get_or_create_srv(conn, plat_name, home_url=""):
    """按平台名取 srv.id（忽略大小写与首尾空格）；不存在则新建一条只含平台名的记录。"""
    plat_name = (plat_name or "").strip()
    row = conn.execute(
        "SELECT id FROM srv WHERE lower(trim(plat_name)) = lower(?)", (plat_name,)
    ).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO srv (plat_name, home_url) VALUES (?, ?)",
        (plat_name, home_url or None),
    )
    return cur.lastrowid


def list_mdl():
    """模型列表：联 srv 取平台名与该平台首页网址（home_url）。
    输出字段与原型 foundation1_models.html 的行对象一一对应。"""
    sql = (
        "SELECT m.id, m.srv_id, m.mdl_name, m.mdl_type, m.in_price, m.out_price, m.unit, m.url, "
        "       s.plat_name, s.home_url "
        "FROM mdl m LEFT JOIN srv s ON s.id = m.srv_id "
        "ORDER BY m.id ASC"
    )
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql).fetchall()]


def create_mdl(plat_name, mdl_name, mdl_type="", in_price=None, out_price=None, unit="", url=""):
    """写入一条模型记录，返回新主键 id。

    平台在该平台上不存在时自动补一条 srv（满足 mdl.srv_id 外键）。
    计费单价按用户输入的原文保存（price_text），不做四舍五入。
    按项目约定不做服务器端校验：必填 / 网址格式只保留在原型前端。
    """
    with connect() as conn:
        srv_id = _get_or_create_srv(conn, plat_name)
        cur = conn.execute(
            "INSERT INTO mdl (srv_id, mdl_name, mdl_type, in_price, out_price, unit, url) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                srv_id,
                (mdl_name or "").strip(),
                (mdl_type or "").strip() or None,
                price_text(in_price),
                price_text(out_price),
                (unit or "").strip() or None,
                (url or "").strip() or None,
            ),
        )
        return cur.lastrowid


def update_mdl(mdl_id, plat_name, mdl_name, mdl_type="", in_price=None, out_price=None,
               unit="", url=""):
    """按主键更新一条模型记录，返回受影响行数。

    与 create_mdl 同口径：平台不存在时自动补 srv，计费单价按原文保存，
    不做服务器端校验。
    """
    with connect() as conn:
        srv_id = _get_or_create_srv(conn, plat_name)
        cur = conn.execute(
            "UPDATE mdl SET srv_id = ?, mdl_name = ?, mdl_type = ?, "
            "in_price = ?, out_price = ?, unit = ?, url = ? WHERE id = ?",
            (
                srv_id,
                (mdl_name or "").strip(),
                (mdl_type or "").strip() or None,
                price_text(in_price),
                price_text(out_price),
                (unit or "").strip() or None,
                (url or "").strip() or None,
                int(mdl_id),
            ),
        )
        return cur.rowcount


def delete_mdl(ids):
    """批量删除模型，返回受影响行数。"""
    ids = [int(i) for i in ids]
    if not ids:
        return 0
    holders = ",".join("?" * len(ids))
    with connect() as conn:
        cur = conn.execute("DELETE FROM mdl WHERE id IN (%s)" % holders, ids)
        return cur.rowcount


def count_mdl():
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM mdl").fetchone()[0]


def seed_mdl_if_empty():
    """mdl 表为空时灌入演示数据（取自 foundation1_models.html 原型表格）；返回写入条数。"""
    with connect() as conn:
        if conn.execute("SELECT COUNT(*) FROM mdl").fetchone()[0]:
            return 0
        n = 0
        for plat, home, name, typ, ip, op, unit in SEED_MDL:
            srv_id = _get_or_create_srv(conn, plat, home)
            conn.execute(
                "INSERT INTO mdl (srv_id, mdl_name, mdl_type, in_price, out_price, unit, url) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (srv_id, name, typ, ip, op, unit, None),
            )
            n += 1
        return n


def schema_report():
    """返回库结构报告文本（建库命令与自检共用）。"""
    lines = []
    with connect() as conn:
        lines.append("库文件 : %s" % DB_PATH)
        lines.append("存在   : %s   大小: %d bytes" % (os.path.exists(DB_PATH), os.path.getsize(DB_PATH)))
        lines.append("--- 表 / 索引 ---")
        for r in conn.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE type IN ('table','index') "
            "AND name NOT LIKE 'sqlite_%' ORDER BY type DESC, name"
        ):
            lines.append("[%s] %s" % (r["type"], r["name"]))
            for ln in (r["sql"] or "").splitlines():
                lines.append("    " + ln.strip())
        lines.append("--- 外键 ---")
        for r in conn.execute("PRAGMA foreign_key_list(mdl)"):
            lines.append("    mdl.%s -> %s.%s  ON DELETE %s ON UPDATE %s"
                         % (r["from"], r["table"], r["to"], r["on_delete"], r["on_update"]))
        lines.append("--- 行数 ---")
        for t in ("srv", "mdl"):
            lines.append("    %-4s = %d" % (t, conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]))
    return "\n".join(lines)


if __name__ == "__main__":
    # 用法：python db.py           建库（幂等）+ 打印库结构
    #       python db.py --seed    建库并灌入演示数据（仅当对应表为空）
    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    init_db()
    if "--seed" in sys.argv:
        print("灌入服务器演示数据 %d 条" % seed_if_empty())
        print("灌入模型演示数据   %d 条" % seed_mdl_if_empty())
    print(schema_report())
