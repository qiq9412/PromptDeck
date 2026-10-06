# -*- coding: utf-8 -*-
"""PromptDeck · Django 设置（第三次迭代）。

刻意做得「极简」：
  · INSTALLED_APPS 只有 core —— 不装 admin / auth / sessions / contenttypes，
    因为 MVP 阶段明确禁止登录、权限、会话（用户约定），也就不需要它们建表；
    业务表（srv / mdl / tst_*）在 models 里全部 managed=False —— 结构由
    migrate_to_twe.py 一次性建好，Django 只负责读写、不建表不改表，
    所以 migrate 落到新库里的只有一张 django_migrations 记账表，
    不会往用户的库里塞一堆用不上的表。
  · MIDDLEWARE 只留 CommonMiddleware，不启用 CSRF —— 本地单机工具，
    没有登录态，页面用的是 fetch 直连（与上一版 Starlette 口径一致）。
  · TEMPLATES 为空 —— 页面直接托管原型产物，不走模板引擎，
    免得原型里的 { } 与 Django 模板语法打架。
  · 唯一一处「配置项」是文件末尾的 DeepSeek 三项 —— 「数据库表结构提取」页
    要拿它去补「库里没有注释」的表 / 字段说明，按用户要求写在这里而不是库里。
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent          # → promptdeck/
PROJECT_ROOT = BASE_DIR.parent                             # → 工作区根（原型所在地）

SECRET_KEY = os.environ.get("PROMPTDECK_SECRET_KEY") or "django-insecure-promptdeck-local-only"
DEBUG = True
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = ["core"]

MIDDLEWARE = ["django.middleware.common.CommonMiddleware"]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# 页面不走模板引擎（原型产物整体托管），因此这里留空
TEMPLATES = []

# ---------------------------------------------------------------- 数据库
# SQLite，落盘为 promptdeck/data/prompt_test_DB_twe.db（第三次迭代换的库）。
# 表结构：srv / mdl 是基础数据；「提示词方案」在新库里被拆成三张表
#   tst_plan（方案主体：plan_name / base_url / srv_id / mdl_id）
#   tst_plan_prompt（提示词：seq_no=0 系统提示词，1..N 用户提示词逐轮）
#   tst_api_key（方案的密钥）
# tst_rec / tst_rec_dtl 是测试记录表，本次只做模型映射、不接业务。
# 结构由 migrate_to_twe.py 一次性建好并搬完旧数据，models 全部 managed=False。
# 旧库 promptdeck/data/prompt_test_DB.db 原样保留，可随时对照。
# 部署到容器时用环境变量 PROMPTDECK_DB 指到持久化目录。
DB_PATH = os.environ.get("PROMPTDECK_DB") or str(BASE_DIR / "data" / "prompt_test_DB_twe.db")

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": DB_PATH,
    }
}

# 原型页产物目录（build_static.py 的输出，由 core.views.page 托管）
STATIC_PAGES_DIR = str(BASE_DIR / "static")

# ------------------------------------------------- 表结构说明用的 DeepSeek
# 「数据库表结构提取」页（Table_Structure_Extraction.html）要产出「表结构说明」：
# 表名 + 表说明、字段名 + 字段类型 + 字段说明。
# 库里**本来就有注释**的（写在建表 SQL 里的 -- 注释）直接用；
# 库里**没有注释**的表 / 字段，调 DeepSeek 现生成一句说明，只描述「存的是什么」。
# 按用户要求，这三项写在配置文件里（不是数据库表）。
# 环境变量可覆盖，便于用本机 mock 服务跑回归（见 verify_ui_ts.py）。
DEEPSEEK_BASE_URL = os.environ.get("PD_DEEPSEEK_BASE_URL") or "https://api.deepseek.com"
DEEPSEEK_MODEL = os.environ.get("PD_DEEPSEEK_MODEL") or "deepseek-v4-pro"
DEEPSEEK_API_KEY = os.environ.get("PD_DEEPSEEK_API_KEY") or "sk-19746978411e4ed9903490dafa104a88"

# 表结构提取默认读哪个 SQLite 库；留空 = 就用上面的 DB_PATH（应用库本身）
SCHEMA_DB_PATH = os.environ.get("PD_SCHEMA_DB") or ""

DEFAULT_AUTO_FIELD = "django.db.models.AutoField"

LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
USE_I18N = False
USE_TZ = False                       # 存本地时间，库里看到的就是人看的时间
