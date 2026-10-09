# -*- coding: utf-8 -*-
"""PromptDeck · 「已连接数据库」保存表的就位脚本（第四次迭代新增）。

背景
    「数据库表结构提取」页原来把已连接的库**只存在内存里**（`dbs` / `PD_CONN`），
    刷新、跳页、关标签页全丢 —— 下次进来左栏一定是空的，得重新导入 / 重连。
    用户点名：**把已连接好的数据库内容保存下来**（存在服务端，含表结构快照）。

    ⚠️ 保存在这里的只有「连得上哪儿」和「库长什么样」，**一个字都不含密码**：
       密码本来就不登记（见 Table_Structure_Extraction.html 的 pdRememberConn），
       本表连密码列都没有 —— 想存都存不进去。这是刻意的，与本项目
       「密码不过我们的手」这条地基一致。

本脚本做一件事
    **幂等地**建出 `tst_db_conn` 表（已在就跳过），并打印现状。
    为什么单独一个脚本、而不是塞进 migrate_to_twe.py：
    migrate_to_twe.py 是**一次性**的换库脚本，它会把那 5 张表
    DROP 掉重建。把本表加进它的 TABLES 意味着「重跑一次换库脚本」就会
    连用户攒下来的连接记录一起删掉 —— 不能那么做。
    先例：setup_judge_key.py 也是这么单独就位 tst_api_key 的结构放宽的。

用法
    python setup_db_conn.py            # 建表（幂等）+ 看现状
    python setup_db_conn.py --drop     # 清空这张表（只删数据，不删表）

安全性
    · 只 CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS，
      不动任何既有表、不动任何既有行；
    · 重跑无副作用（已存在就跳过）；
    · --drop 只 DELETE 本表数据，且要求二次确认参数 --yes。
"""
import argparse
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("PROMPTDECK_DB") or os.path.join(HERE, "data", "prompt_test_DB_twe.db")

TABLE = "tst_db_conn"

# ⚠️ 没有 password / pwd 列，也不该有。要加列先想清楚为什么。
DDL = """
CREATE TABLE IF NOT EXISTS tst_db_conn (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  conn_name    TEXT    NOT NULL,
  conn_kind    TEXT    NOT NULL,
  conn_mode    TEXT    DEFAULT 'host',
  host         TEXT    DEFAULT NULL,
  port         TEXT    DEFAULT NULL,
  dbname       TEXT    DEFAULT NULL,
  user_name    TEXT    DEFAULT NULL,
  file_path    TEXT    DEFAULT NULL,
  schema_json  TEXT    DEFAULT NULL,
  checked_json TEXT    DEFAULT NULL,
  saved_at     TEXT    DEFAULT NULL
)"""

# 同一个「库名 + 类型」只留一条：与前端 pdFindConn 的键（name|kind）同一口径，
# 这样重复连接同一个库不会在左栏堆出两行。
IDX = ("CREATE UNIQUE INDEX IF NOT EXISTS ux_tst_db_conn_name_kind"
       " ON tst_db_conn (conn_name, conn_kind)")

COLS = ("id, conn_name, conn_kind, conn_mode, host, port, dbname,"
        " user_name, file_path, schema_json, checked_json, saved_at")


def log(msg=""):
    sys.stdout.write(str(msg) + "\n")
    sys.stdout.flush()


def has_table(cur):
    return cur.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
                       (TABLE,)).fetchone()[0] > 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--drop", action="store_true", help="清空本表数据（不删表）")
    ap.add_argument("--yes", action="store_true", help="--drop 时的二次确认")
    args = ap.parse_args()

    log("=" * 74)
    log("PromptDeck · 「已连接数据库」保存表就位（%s）" % TABLE)
    log("库文件：%s" % DB_PATH)
    log("=" * 74)

    if not os.path.isfile(DB_PATH):
        log("[×] 找不到库文件")
        return 1

    cur = sqlite3.connect(DB_PATH).cursor()
    cur.connection.isolation_level = None

    log("[1] 建表（幂等）")
    existed = has_table(cur)
    if existed:
        log("      已存在，跳过")
    else:
        cur.execute(DDL)
        cur.execute(IDX)
        log("      已创建 %s + 唯一索引 (conn_name, conn_kind)" % TABLE)

    log("")
    log("[2] 现状")
    n = cur.execute("SELECT COUNT(*) FROM %s" % TABLE).fetchone()[0]
    log("      共 %d 条已保存的连接" % n)
    for row in cur.execute("SELECT id, conn_name, conn_kind, conn_mode, host, port,"
                           " dbname, user_name, file_path,"
                           " LENGTH(COALESCE(schema_json,'')) AS sj,"
                           " LENGTH(COALESCE(checked_json,'')) AS cj, saved_at"
                           " FROM %s ORDER BY id" % TABLE):
        log("        #%s %-28s %-12s mode=%-5s host=%s port=%s db=%s user=%s file=%s"
            " schema=%sB checked=%sB %s"
            % (row[0], row[1], row[2], row[3], row[4] or "-", row[5] or "-",
               row[6] or "-", row[7] or "-", row[8] or "-", row[9], row[10], row[11] or ""))

    if args.drop:
        if not args.yes:
            log("")
            log("[!] --drop 需要 --yes 二次确认，未执行")
            cur.connection.close()
            return 0
        log("")
        log("[3] 清空")
        d = cur.execute("DELETE FROM %s" % TABLE).rowcount
        log("      已删除 %d 条" % d)

    log("")
    log("[4] 收尾核对")
    log("      %s 存在 = %s" % (TABLE, has_table(cur)))
    log("      表内列：%s" % ", ".join(
        r[1] for r in cur.execute("PRAGMA table_info(%s)" % TABLE)))
    cur.connection.close()

    log("-" * 74)
    log("结果：就位完成 ✔")
    log("=" * 74)
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
