# -*- coding: utf-8 -*-
"""数据层自测：建表 -> 灌种子 -> 增 -> 查/搜 -> 级联删。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db

log = []
log.append("DB 文件: %s" % db.DB_PATH)

db.init_db()
n = db.seed_if_empty()
log.append("首启灌入演示数据: %d 条" % n)

all_rows = db.list_srv()
log.append("全量查询: %d 条" % len(all_rows))
log.append("首条: %s" % {k: all_rows[0][k] for k in ("id", "plat_name", "base_url", "comp_name", "home_url")})

log.append("搜索 platform='deep': %s" % [r["plat_name"] for r in db.list_srv(platform="deep")])
log.append("搜索 company='腾讯': %s" % [r["plat_name"] for r in db.list_srv(company="腾讯")])
log.append("搜索 platform='deep' + company='月之': %s" % [r["plat_name"] for r in db.list_srv("deep", "月之")])

new_id = db.create_srv("测试平台 · UnitTest", "https://api.unit.test/v1", "测试公司", "https://unit.test")
log.append("新增一条 id=%s，总数=%d" % (new_id, db.count_srv()))
row = db.get_srv(new_id)
log.append("回读新增行: %s" % row)

# 外键级联：给新服务器挂一个模型，删服务器后模型应一并消失
with db.connect() as conn:
    conn.execute(
        "INSERT INTO mdl (srv_id, mdl_name, mdl_type, in_price, out_price, unit, url)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (new_id, "unit-test-model", "文本生成", 0.001, 0.002, "K", "https://unit.test/doc"),
    )
with db.connect() as conn:
    before = conn.execute("SELECT COUNT(*) FROM mdl WHERE srv_id = ?", (new_id,)).fetchone()[0]
log.append("挂载模型后 mdl 行数: %d" % before)

deleted = db.delete_srv([new_id])
with db.connect() as conn:
    after = conn.execute("SELECT COUNT(*) FROM mdl WHERE srv_id = ?", (new_id,)).fetchone()[0]
log.append("删除该服务器: 影响 %d 行；mdl 级联后残留 %d 行" % (deleted, after))
log.append("删除后总数: %d（应回到 12）" % db.count_srv())

log.append("空 ids 删除: %d（应为 0）" % db.delete_srv([]))

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_db_test.txt")
with open(out, "w", encoding="utf-8") as fp:
    fp.write("\n".join(str(x) for x in log))
print("written:", out)
