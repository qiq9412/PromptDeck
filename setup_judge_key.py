# -*- coding: utf-8 -*-
"""PromptDeck · 「AI 判定模型」密钥的就位脚本（第二次迭代新增）。

背景
    提示词测试页点「开始调试」后，每道题要拿「标准答案」与「模型答案」做一次
    语义一致性判定。判定走一个固定的判定模型：
        base_url = https://api.deepseek.com
        model    = deepseek-v4-pro
        api_key  = 由本脚本写进库
    按需求，这个 key **不进源码**，只落在数据库的 tst_api_key 表里；
    运行时由 core/store.py 的 judge_key() 现读。

两件事
    1) 结构放宽：tst_api_key.plan_id 原为 NOT NULL（外键指向 tst_plan），
       而「判定模型的密钥」不属于任何一个提示词方案 ⇒ 放宽成可空。
       方案自己的密钥（key_name='默认'）不受影响，仍按 plan_id 归属。
    2) 写入 / 更新判定密钥：key_name = JUDGE_KEY_NAME，plan_id = NULL。

用法
    # 写入（或覆盖）判定密钥 —— key 从命令行传入，源码里不出现明文
    python setup_judge_key.py --key sk-xxxxxxxx

    # 只看现状（不写库）
    python setup_judge_key.py

安全性
    · 结构放宽是幂等的：已经是可空就直接跳过，不做无谓的建表/搬数据。
    · 只动 tst_api_key 一张表，tst_rec / tst_rec_dtl 原样不动。
    · 重建前后都会跑 PRAGMA foreign_key_check，有违规就回滚。
"""
import argparse
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("PROMPTDECK_DB") or os.path.join(HERE, "data", "prompt_test_DB_twe.db")

# 判定密钥在 tst_api_key 里的标识（与 core/store.py 的 JUDGE_KEY_NAME 必须逐字一致）
JUDGE_KEY_NAME = "AI判定（DeepSeek）"

# 重建后的 tst_api_key 列定义：与 migrate_to_twe.py 的 DDL 唯一差别 = plan_id 允许 NULL
COLS_DDL = """
  id          INTEGER      PRIMARY KEY AUTOINCREMENT,
  plan_id     INTEGER      DEFAULT NULL,
  key_name    VARCHAR(100) DEFAULT NULL,
  api_key     VARCHAR(512) NOT NULL,
  is_active   INTEGER      NOT NULL DEFAULT 1,
  created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT fk_tak_plan FOREIGN KEY (plan_id) REFERENCES tst_plan (id)
    ON DELETE CASCADE ON UPDATE CASCADE
"""

COLS = "id, plan_id, key_name, api_key, is_active, created_at, updated_at"


def log(msg=""):
    sys.stdout.write(str(msg) + "\n")
    sys.stdout.flush()


def plan_id_is_nullable(cur):
    for row in cur.execute("PRAGMA table_info(tst_api_key)"):
        if row[1] == "plan_id":
            return row[3] == 0        # notnull == 0 ⇒ 可空
    raise RuntimeError("tst_api_key 没有 plan_id 列")


def relax_plan_id(cur):
    """把 tst_api_key.plan_id 由 NOT NULL 放宽成可空。

    SQLite 不能直接改列约束，只能「建新表 → 搬数据 → 换名」。
    两个 PRAGMA 是必需的：
      · foreign_keys=OFF  —— 否则 DROP 被 tst_rec 引用着的表会失败；
      · legacy_alter_table=ON —— 否则 RENAME 会把 tst_rec 的外键指向改写成
        中间表名（SQLite 3.25+ 的新行为），换名之后 tst_rec 的外键就悬空了。
    """
    cur.execute("PRAGMA foreign_keys = OFF")
    cur.execute("PRAGMA legacy_alter_table = ON")
    try:
        cur.execute("BEGIN")
        cur.execute("CREATE TABLE tst_api_key__new (%s)" % COLS_DDL)
        cur.execute("INSERT INTO tst_api_key__new (%s) SELECT %s FROM tst_api_key" % (COLS, COLS))
        cur.execute("DROP TABLE tst_api_key")
        cur.execute("ALTER TABLE tst_api_key__new RENAME TO tst_api_key")
        cur.execute("COMMIT")
    except Exception:
        cur.execute("ROLLBACK")
        raise
    finally:
        cur.execute("PRAGMA legacy_alter_table = OFF")
        cur.execute("PRAGMA foreign_keys = ON")


def upsert_judge_key(cur, key):
    """写入（或覆盖）判定密钥：plan_id 为 NULL，key_name 固定。"""
    hit = cur.execute("SELECT id FROM tst_api_key WHERE key_name = ? AND plan_id IS NULL",
                      (JUDGE_KEY_NAME,)).fetchone()
    if hit:
        cur.execute("UPDATE tst_api_key SET api_key = ?, is_active = 1,"
                    " updated_at = CURRENT_TIMESTAMP WHERE id = ?", (key, hit[0]))
        return hit[0], "updated"
    cur.execute("INSERT INTO tst_api_key (plan_id, key_name, api_key, is_active,"
                " created_at, updated_at) VALUES (NULL, ?, ?, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (JUDGE_KEY_NAME, key))
    return cur.lastrowid, "created"


def mask(key):
    """只回显遮掩形态，避免明文刷屏。"""
    k = str(key or "")
    if len(k) <= 8:
        return "•" * max(len(k), 4)
    return k[:3] + "•" * 8 + k[-4:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", default="", help="判定模型（DeepSeek）的 API_key；不传则只查看现状")
    args = ap.parse_args()

    log("=" * 74)
    log("PromptDeck · AI 判定密钥就位（tst_api_key）")
    log("库文件：%s" % DB_PATH)
    log("=" * 74)

    if not os.path.isfile(DB_PATH):
        log("[×] 找不到库文件")
        return 1

    cur = sqlite3.connect(DB_PATH).cursor()
    cur.connection.isolation_level = None

    log("[1] 结构：tst_api_key.plan_id 放宽为可空")
    if plan_id_is_nullable(cur):
        log("      已经是可空，跳过重建")
    else:
        before = cur.execute("SELECT COUNT(*) FROM tst_api_key").fetchone()[0]
        relax_plan_id(cur)
        after = cur.execute("SELECT COUNT(*) FROM tst_api_key").fetchone()[0]
        log("      已重建：%d 行 -> %d 行" % (before, after))
    log("      plan_id 可空 = %s" % plan_id_is_nullable(cur))

    log("")
    log("[2] 判定密钥")
    if args.key.strip():
        kid, kind = upsert_judge_key(cur, args.key.strip())
        log("      %s：id=%d  key_name=%s  key=%s"
            % ("已更新" if kind == "updated" else "已写入", kid, JUDGE_KEY_NAME, mask(args.key)))
    else:
        log("      未传 --key，只查看")

    row = cur.execute("SELECT id, key_name, api_key, is_active FROM tst_api_key"
                      " WHERE key_name = ? AND plan_id IS NULL", (JUDGE_KEY_NAME,)).fetchone()
    log("      当前判定密钥：%s" % (("%s / %s" % (row[1], mask(row[2]))) if row else "（无）"))

    log("")
    log("[3] 收尾核对")
    fk = cur.execute("PRAGMA foreign_key_check").fetchall()
    log("      foreign_key_check 违规：%d 条（应为 0）" % len(fk))
    orphan = cur.execute("SELECT COUNT(*) FROM tst_rec WHERE plan_id NOT IN (SELECT id FROM tst_plan)"
                         ).fetchone()[0]
    log("      tst_rec 悬空方案引用：%d（应为 0）" % orphan)
    tot = cur.execute("SELECT COUNT(*) FROM tst_api_key").fetchone()[0]
    plan_keys = cur.execute("SELECT COUNT(*) FROM tst_api_key WHERE plan_id IS NOT NULL").fetchone()[0]
    log("      tst_api_key 共 %d 行（其中归属方案的 %d 行）" % (tot, plan_keys))
    cur.connection.close()

    log("-" * 74)
    log("结果：%s" % ("就位完成 ✔" if not fk and not orphan else "有告警，见上 ✘"))
    log("=" * 74)
    return 0 if (not fk and not orphan) else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
