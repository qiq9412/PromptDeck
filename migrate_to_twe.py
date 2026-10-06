# -*- coding: utf-8 -*-
"""PromptDeck · 一次性数据库迁移脚本（第三次迭代）。

把数据库从  data/prompt_test_DB.db  换成  data/prompt_test_DB_twe.db，
并按新库的表结构重建 + 把旧数据搬过去。

── 已拍板的 4 条口径 ────────────────────────────────────────────
  1. tst_plan 加两列 plan_name / base_url
     （新库原本只有 srv_id + mdl_id，装不下原型的「方案命名」与「请求地址」）
  2. 外键全部改成 ON DELETE CASCADE（与旧库一致：删平台连带删它的模型与方案）
  3. mdl.in_price / out_price 改成 TEXT（单价按原文存，页面上的 18.00 不会变成 18）
  4. 只动 5 张表：srv / mdl / tst_plan / tst_plan_prompt / tst_api_key
     tst_rec / tst_rec_dtl 结构原样保留
     （第二次迭代起 tst_rec / tst_rec_dtl 已接入业务 —— 「提示词测试」页「保存结果」
       会往里写记录与逐题明细，但**结构一个字没动**，所以本脚本无需为它们加 DDL。）

── 数据搬运 ────────────────────────────────────────────────────
  srv(20)  → srv               原 id 原样搬
  mdl(23)  → mdl               原 id 原样搬
  tp(5)    → tst_plan(5)       方案主体：plan_name / base_url / srv_id / mdl_id
           + tst_plan_prompt   系统提示词 1 条（seq_no=0）+ 用户提示词逐轮（seq_no=1..N）
           + tst_api_key       方案的 API_key（key_name='默认'）

  方案里的 server / model 是「名字」不是外键，脚本按名字去 srv / mdl 里查 id；
  查不到就补建一条（只填名字），保证外键能落地。

── 安全性 ──────────────────────────────────────────────────────
  新库若已经不是空的（说明库里已有数据），默认拒绝执行；
  确实要重跑（覆盖）时加 --force。
  旧库文件从头到尾只读，不动它。

用法：
    python migrate_to_twe.py            # 干跑前会先做安全检查
    python migrate_to_twe.py --force    # 库里已有数据时强行重建
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
SRC_DB = os.path.join(DATA, "prompt_test_DB.db")          # 旧库（只读）
DST_DB = os.path.join(DATA, "prompt_test_DB_twe.db")      # 新库（重建目标）

# 本次要重建的表（顺序 = 建表顺序，依赖在前）
TABLES = ["srv", "mdl", "tst_plan", "tst_plan_prompt", "tst_api_key"]

# ------------------------------------------------------------------ 目标结构
DDL = {
    # 供应商表：结构与新库一致，不动
    "srv": """
CREATE TABLE srv (
  id         INTEGER      PRIMARY KEY AUTOINCREMENT,
  plat_name  VARCHAR(255) NOT NULL,
  base_url   VARCHAR(512) DEFAULT NULL,
  comp_name  VARCHAR(255) DEFAULT NULL,
  home_url   VARCHAR(512) DEFAULT NULL
)""",

    # 模型表：in_price / out_price 由 DECIMAL(10,6) 改为 TEXT；
    # 外键由 ON DELETE RESTRICT 改为 CASCADE
    "mdl": """
CREATE TABLE mdl (
  id         INTEGER       PRIMARY KEY AUTOINCREMENT,
  srv_id     INTEGER       NOT NULL,
  mdl_name   VARCHAR(255)  NOT NULL,
  mdl_type   VARCHAR(100)  DEFAULT NULL,
  in_price   TEXT          DEFAULT NULL,
  out_price  TEXT          DEFAULT NULL,
  unit       VARCHAR(50)   DEFAULT NULL,
  url        VARCHAR(512)  DEFAULT NULL,
  CONSTRAINT fk_mdl_srv FOREIGN KEY (srv_id) REFERENCES srv (id)
    ON DELETE CASCADE ON UPDATE CASCADE
)""",

    # 测试方案表：新增 plan_name / base_url 两列；外键改 CASCADE
    "tst_plan": """
CREATE TABLE tst_plan (
  id         INTEGER      PRIMARY KEY AUTOINCREMENT,
  plan_name  VARCHAR(255) DEFAULT NULL,
  base_url   VARCHAR(512) DEFAULT NULL,
  srv_id     INTEGER      NOT NULL,
  mdl_id     INTEGER      NOT NULL,
  CONSTRAINT fk_tp_srv FOREIGN KEY (srv_id) REFERENCES srv (id)
    ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT fk_tp_mdl FOREIGN KEY (mdl_id) REFERENCES mdl (id)
    ON DELETE CASCADE ON UPDATE CASCADE
)""",

    # 方案提示词表：结构不动
    # seq_no=0 → 系统提示词；seq_no=1..N → 用户提示词第 N 轮
    "tst_plan_prompt": """
CREATE TABLE tst_plan_prompt (
  id              INTEGER     PRIMARY KEY AUTOINCREMENT,
  plan_id         INTEGER     NOT NULL,
  seq_no          INTEGER     NOT NULL,
  prompt_role     VARCHAR(20) NOT NULL,
  prompt_content  TEXT        NOT NULL,
  CONSTRAINT fk_tpp_plan FOREIGN KEY (plan_id) REFERENCES tst_plan (id)
    ON DELETE CASCADE ON UPDATE CASCADE
)""",

    # 方案密钥表：plan_id 允许 NULL —— 「提示词测试」页的 AI 判定密钥（key_name='AI判定（DeepSeek）'）
    # 不属于任何方案，需落在这张表里；方案自己的密钥仍带 plan_id。
    # ⚠️ 已有库由 setup_judge_key.py 做同口径的结构放宽（幂等），这里是给「重建库」用的。
    "tst_api_key": """
CREATE TABLE tst_api_key (
  id          INTEGER      PRIMARY KEY AUTOINCREMENT,
  plan_id     INTEGER      DEFAULT NULL,
  key_name    VARCHAR(100) DEFAULT NULL,
  api_key     VARCHAR(512) NOT NULL,
  is_active   INTEGER      NOT NULL DEFAULT 1,
  created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT fk_tak_plan FOREIGN KEY (plan_id) REFERENCES tst_plan (id)
    ON DELETE CASCADE ON UPDATE CASCADE
)""",
}

# 提示词角色：与 OpenAI messages 口径一致，小写
ROLE_SYSTEM = "system"
ROLE_USER = "user"
DEFAULT_KEY_NAME = "默认"


# ------------------------------------------------------------------ 小工具
def log(msg=""):
    sys.stdout.write(str(msg) + "\n")
    sys.stdout.flush()


def parse_user_prompt(raw):
    """旧库 tp.user_prompt 是 JSON 数组原文，解析成字符串数组；坏了/为空一律 []。"""
    if not raw:
        return []
    try:
        arr = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(arr, list):
        return []
    return ["" if x is None else str(x) for x in arr]


def norm(v):
    """比对用的归一：None → ''，去首尾空格、转小写。"""
    return ("" if v is None else str(v)).strip().lower()


def table_count(conn, name):
    try:
        return conn.execute('select count(*) from "%s"' % name).fetchone()[0]
    except sqlite3.Error:
        return None


# ------------------------------------------------------------------ 匹配 / 补建
def find_srv_id(conn, plat_name):
    """按平台名查 srv.id（忽略大小写与首尾空格），取 id 最小的那条。"""
    row = conn.execute(
        "select id from srv where lower(trim(plat_name)) = ? order by id limit 1",
        (norm(plat_name),),
    ).fetchone()
    return row[0] if row else None


def find_mdl_id(conn, srv_id, mdl_name):
    """按模型名查 mdl.id：优先同平台下匹配，退一步全局匹配，取 id 最小的。"""
    if srv_id:
        row = conn.execute(
            "select id from mdl where lower(trim(mdl_name)) = ? and srv_id = ?"
            " order by id limit 1",
            (norm(mdl_name), srv_id),
        ).fetchone()
        if row:
            return row[0]
    row = conn.execute(
        "select id from mdl where lower(trim(mdl_name)) = ? order by id limit 1",
        (norm(mdl_name),),
    ).fetchone()
    return row[0] if row else None


def ensure_srv_id(conn, plat_name, base_url=""):
    """取 srv.id；查不到就补建一条只含平台名（+请求地址）的记录。"""
    hit = find_srv_id(conn, plat_name)
    if hit:
        return hit
    cur = conn.execute(
        "insert into srv (plat_name, base_url, comp_name, home_url) values (?,?,?,?)",
        ((plat_name or "").strip(), (base_url or "").strip() or None, None, None),
    )
    return cur.lastrowid


def ensure_mdl_id(conn, srv_id, mdl_name, mdl_type=""):
    """取 mdl.id；查不到就补建一条只含名字（+类型）的记录。"""
    hit = find_mdl_id(conn, srv_id, mdl_name)
    if hit:
        return hit
    cur = conn.execute(
        "insert into mdl (srv_id, mdl_name, mdl_type, in_price, out_price, unit, url)"
        " values (?,?,?,?,?,?,?)",
        (srv_id, (mdl_name or "").strip(), (mdl_type or "").strip() or None,
         None, None, None, None),
    )
    return cur.lastrowid


# ------------------------------------------------------------------ 主流程
def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--force", action="store_true",
                    help="新库里已有数据时，仍然重建（会覆盖新库现有数据）")
    args = ap.parse_args()

    log("=" * 74)
    log("PromptDeck · 数据库迁移（旧库 → 新库 + 结构重建）")
    log("=" * 74)

    # ---------- 0. 前置检查 ----------
    if not os.path.isfile(SRC_DB):
        log("[×] 找不到旧库：%s" % SRC_DB)
        return 1
    if not os.path.isfile(DST_DB):
        log("[×] 找不到新库：%s" % DST_DB)
        return 1

    src = sqlite3.connect("file:%s?mode=ro" % SRC_DB.replace("\\", "/"), uri=True)
    src.row_factory = sqlite3.Row

    missing = [t for t in ("srv", "mdl", "tp")
               if src.execute("select count(*) from sqlite_master where name=?", (t,)).fetchone()[0] == 0]
    if missing:
        log("[×] 旧库缺表：%s" % "、".join(missing))
        return 1

    dst = sqlite3.connect(DST_DB)
    dst.row_factory = sqlite3.Row

    occupied = {t: table_count(dst, t) for t in TABLES}
    busy = {t: n for t, n in occupied.items() if n}
    if busy and not args.force:
        log("[!] 新库里已经有数据，拒绝执行：")
        for t, n in busy.items():
            log("      %-16s %s 行" % (t, n))
        log("    确认要覆盖请重跑： python migrate_to_twe.py --force")
        return 2

    # ---------- 1. 备份新库 ----------
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = "%s.bak_%s" % (DST_DB, stamp)
    shutil.copy2(DST_DB, bak)
    log("[1] 新库已备份 → %s" % os.path.basename(bak))

    # ---------- 2. 读旧库数据 ----------
    srv_rows = [dict(r) for r in src.execute("select * from srv order by id")]
    mdl_rows = [dict(r) for r in src.execute("select * from mdl order by id")]
    tp_rows = [dict(r) for r in src.execute("select * from tp order by id")]
    log("[2] 读到旧库数据：srv %d 条 / mdl %d 条 / tp %d 条"
        % (len(srv_rows), len(mdl_rows), len(tp_rows)))

    # ---------- 3. 重建 5 张表 ----------
    dst.execute("PRAGMA foreign_keys = OFF")
    dst.execute("BEGIN")
    # 按依赖倒序删；tst_rec / tst_rec_dtl 不动
    for t in reversed(TABLES):
        dst.execute('DROP TABLE IF EXISTS "%s"' % t)
    for t in TABLES:
        dst.execute(DDL[t])
    dst.execute("COMMIT")
    dst.execute("PRAGMA foreign_keys = ON")
    log("[3] 已重建 %d 张表：%s" % (len(TABLES), "、".join(TABLES)))

    # ---------- 4. 搬 srv / mdl（保留原 id） ----------
    dst.execute("BEGIN")
    for r in srv_rows:
        dst.execute(
            "insert into srv (id, plat_name, base_url, comp_name, home_url)"
            " values (?,?,?,?,?)",
            (r["id"], r["plat_name"], r.get("base_url"), r.get("comp_name"), r.get("home_url")),
        )
    for r in mdl_rows:
        dst.execute(
            "insert into mdl (id, srv_id, mdl_name, mdl_type, in_price, out_price, unit, url)"
            " values (?,?,?,?,?,?,?,?)",
            (r["id"], r["srv_id"], r["mdl_name"], r.get("mdl_type"),
             # 单价原样搬成文本：旧库本来就是 TEXT，这里再兜一次字符串化
             None if r.get("in_price") is None else str(r["in_price"]),
             None if r.get("out_price") is None else str(r["out_price"]),
             r.get("unit"), r.get("url")),
        )
    dst.execute("COMMIT")
    log("[4] srv / mdl 已搬入（保留原 id，单价按原文转文本）")

    # ---------- 5. 搬 tp → tst_plan + tst_plan_prompt + tst_api_key ----------
    stat = {"system": 0, "user": 0, "key": 0, "补建_srv": 0, "补建_mdl": 0}
    plans = []
    dst.execute("BEGIN")
    for r in tp_rows:
        srv_id = find_srv_id(dst, r.get("srv_name"))
        if not srv_id:
            srv_id = ensure_srv_id(dst, r.get("srv_name"), r.get("base_url"))
            stat["补建_srv"] += 1
        mdl_id = find_mdl_id(dst, srv_id, r.get("mdl_name"))
        if not mdl_id:
            mdl_id = ensure_mdl_id(dst, srv_id, r.get("mdl_name"), r.get("mdl_type"))
            stat["补建_mdl"] += 1

        cur = dst.execute(
            "insert into tst_plan (plan_name, base_url, srv_id, mdl_id) values (?,?,?,?)",
            (r.get("name"), r.get("base_url"), srv_id, mdl_id),
        )
        plan_id = cur.lastrowid

        # 系统提示词：seq_no = 0
        sys_txt = r.get("sys_prompt")
        if sys_txt is not None and str(sys_txt) != "":
            dst.execute(
                "insert into tst_plan_prompt (plan_id, seq_no, prompt_role, prompt_content)"
                " values (?,?,?,?)",
                (plan_id, 0, ROLE_SYSTEM, str(sys_txt)),
            )
            stat["system"] += 1

        # 用户提示词：seq_no = 1..N（多轮的 N 条就是逐轮内容）
        users = parse_user_prompt(r.get("user_prompt"))
        for i, txt in enumerate(users, start=1):
            dst.execute(
                "insert into tst_plan_prompt (plan_id, seq_no, prompt_role, prompt_content)"
                " values (?,?,?,?)",
                (plan_id, i, ROLE_USER, txt),
            )
            stat["user"] += 1

        # API_key：一个方案一把「默认」密钥
        key = r.get("api_key")
        if key is not None and str(key).strip() != "":
            dst.execute(
                "insert into tst_api_key (plan_id, key_name, api_key, is_active,"
                " created_at, updated_at) values (?,?,?,1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
                (plan_id, DEFAULT_KEY_NAME, str(key).strip()),
            )
            stat["key"] += 1

        plans.append((plan_id, r.get("name"), srv_id, mdl_id))
    dst.execute("COMMIT")

    log("[5] tp → 已拆成 tst_plan %d 条 / 系统提示词 %d 条 / 用户提示词 %d 条 / 密钥 %d 把"
        % (len(plans), stat["system"], stat["user"], stat["key"]))
    if stat["补建_srv"] or stat["补建_mdl"]:
        log("    （方案里有名字在库中查不到的，已补建：srv %d 条 / mdl %d 条）"
            % (stat["补建_srv"], stat["补建_mdl"]))
    for pid, name, sid, mid in plans:
        log("      plan#%d  %-18s srv_id=%d  mdl_id=%d" % (pid, name, sid, mid))

    # ---------- 6. 校验 ----------
    log("[6] 落库校验")
    ok = True
    for t in TABLES:
        n = table_count(dst, t)
        log("      %-16s %s 行" % (t, n))
    for t in ("tst_rec", "tst_rec_dtl"):
        log("      %-16s %s 行（本次结构未动）" % (t, table_count(dst, t)))

    bad = dst.execute("select count(*) from mdl where typeof(in_price) != 'text'"
                      " and in_price is not null").fetchone()[0]
    log("      mdl 单价非文本的行数：%d（应为 0）" % bad)
    ok &= (bad == 0)

    dup = dst.execute("select count(*) from ("
                      " select mdl_id from tst_plan where mdl_id not in (select id from mdl))").fetchone()[0]
    orphan = dst.execute("select count(*) from tst_plan where"
                         " srv_id not in (select id from srv) or mdl_id not in (select id from mdl)").fetchone()[0]
    log("      悬空外键的方案数：%d（应为 0）" % orphan)
    ok &= (orphan == 0)

    fk = dst.execute("PRAGMA foreign_key_check").fetchall()
    log("      foreign_key_check 违规：%d 条（应为 0）" % len(fk))
    ok &= (len(fk) == 0)

    src.close()
    dst.commit()
    dst.close()

    log("-" * 74)
    log("结果：%s" % ("迁移完成 ✔" if ok else "迁移完成但有告警，见上 ✘"))
    log("旧库原样保留：%s" % os.path.basename(SRC_DB))
    log("=" * 74)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
