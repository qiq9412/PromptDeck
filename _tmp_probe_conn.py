# -*- coding: utf-8 -*-
"""临时：验 /api/db-conn 三个动词 + 密码绝不落库。"""
import json
import os
import sys

sys.path.insert(0, os.getcwd())
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django
django.setup()

from core import store, views


class FakeReq(object):
    def __init__(self, method, body=None):
        self.method = method
        self.content_type = "application/json"
        self._b = json.dumps(body).encode("utf-8") if body is not None else b""

    @property
    def body(self):
        return self._b


def call(method, body=None):
    r = views.db_conn_api(FakeReq(method, body))
    return r.status_code, json.loads(r.content.decode("utf-8"))


PW = "ShouldNeverBeStored_9x8y7z"

print("=== 1. 初始列表 ===")
code, res = call("GET")
print(code, "needSetup" in res, "条数=%d" % len(res.get("list", [])))

print()
print("=== 2. POST 保存（**故意夹带密码**，看它进不进库）===")
code, res = call("POST", {
    "kind": "MySQL", "name": "demo_shop", "mode": "host",
    "host": "10.0.0.9", "port": "3306", "dbname": "shop", "user": "root",
    "pwd": PW, "password": PW, "apiKey": PW,          # 三个都该被丢掉
    "checked": ["orders", "users"],
    "schema": {"tables": [{"name": "orders", "rows": 3, "desc": "订单",
                           "cols": [["id", "pk", "主键", "BIGINT"]]}],
               "relations": [{"fromTable": "orders", "column": "uid",
                              "refTable": "users", "refColumn": "id"}]},
})
print(code, res)

print()
print("=== 3. 库里到底存了什么 ===")
code, res = call("GET")
for it in res.get("list", []):
    print("  ", {k: it[k] for k in ("id", "name", "kind", "mode", "host", "user")})
    print("   schema.tables =", len(it["schema"]["tables"]),
          "relations =", len(it["schema"]["relations"]))
    print("   checked =", it["checked"], " savedAt =", it["savedAt"])

print()
print("=== 4. 全表扫密码（应 0 命中）===")
import sqlite3
from config import settings
c = sqlite3.connect(settings.DB_PATH)
raw = c.execute("select conn_name, conn_kind, host, port, dbname, user_name, file_path,"
                " schema_json, checked_json from tst_db_conn").fetchall()
dump = str(raw)
print("  明文密码命中:", PW in dump)
print("  含 'pwd' 字样:", "pwd" in dump.lower())
print("  tst_db_conn 列名:", [r[1] for r in c.execute("PRAGMA table_info(tst_db_conn)")])
c.close()

print()
print("=== 5. 重复保存同一个库 → 应更新而非新增 ===")
code, res = call("POST", {"kind": "MySQL", "name": "demo_shop", "mode": "host",
                          "host": "10.0.0.9", "port": "3307", "dbname": "shop2",
                          "user": "root", "schema": {"tables": [], "relations": []}})
print(code, res)
code, res = call("GET")
print("  现在条数 =", len(res["list"]), "（应仍是 1）")
print("  port/dbname 已更新 =", res["list"][0]["port"], res["list"][0]["dbname"])

print()
print("=== 6. 老结构（纯表名数组 schema 缺失）也不崩 ===")
code, res = call("POST", {"kind": "SQLite", "name": "legacy.db", "mode": "file",
                          "file": "/tmp/x.db"})
print(code, res)

print()
print("=== 7. DELETE：按 name+kind ===")
code, res = call("DELETE", {"kind": "MySQL", "name": "demo_shop"})
print(code, res)
code, res = call("GET")
print("  剩", len(res["list"]), "条（应 1：只剩 legacy.db）")

print()
print("=== 8. 方法守卫 ===")
r = views.db_conn_api(FakeReq("PUT"))
print("  PUT ->", r.status_code, r.content.decode("utf-8")[:60])

print()
print("=== 9. 清场 ===")
code, res = call("DELETE", {"kind": "SQLite", "name": "legacy.db"})
print(code, res)
code, res = call("GET")
print("  最终条数 =", len(res["list"]), "（应 0，回基线）")