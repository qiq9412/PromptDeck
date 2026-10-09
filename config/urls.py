# -*- coding: utf-8 -*-
"""PromptDeck · 路由表。

    /                         → 跳 prompt.html
    /api/health               → 健康检查
    /api/srv                  → 服务器表增删改查（原型 list_server / new_server）
    /api/mdl                  → 模型表增删改查（原型 foundation1_models / model_add）
    /api/chat                 → 调试：转发给真实模型（原型 prompt.html 的「调试」）
    /api/tp                   → 提示词方案表增删改查（本次新增，落 tp 表）
    /api/judge                → AI 判定：标准答案 ↔ 模型答案是否一致（第二次迭代）
    /api/rec                  → 测试记录读写（第二次迭代，落 tst_rec / tst_rec_dtl）
    /api/schema               → 数据库表结构：GET 应用库 / POST 上传的 .sqlite（第二次迭代）
    /api/schema/connect       → 真连 MySQL / PG / SQL Server / Oracle / Db2 并读结构
    /api/schema/desc          → 只给「库里没有注释」的表 / 字段调 DeepSeek 补说明
    /api/dbeaver/link         → 链接数据库调试：唤起 DBeaver + 送 SQL 进编辑器
    /api/database/execute-sql → 页面内调试：服务端真连库执行只读 SQL，回结果集
    /api/db-conn              → 已连接数据库存档：GET 列表 / POST 保存 / DELETE 删除
    /<页面名>.html            → 托管原型产物（白名单，见 core.views.PAGES）

⚠️ /api/* 必须排在 <str:name> 之前，否则会被页面路由吃掉。
"""
from django.urls import path

from core import views

urlpatterns = [
    path("", views.home),
    path("api/health", views.health),
    path("api/srv", views.srv_api),
    path("api/mdl", views.mdl_api),
    path("api/chat", views.chat_api),
    path("api/tp", views.tp_api),
    path("api/judge", views.judge_api),
    path("api/rec", views.rec_api),
    path("api/schema/connect", views.schema_connect_api),  # 三条 schema 路由：更具体的放前面
    path("api/schema/desc", views.schema_desc_api),
    path("api/schema", views.schema_api),
    path("api/dbeaver/link", views.dbeaver_link_api),
    path("api/database/execute-sql", views.sql_execute_api),
    path("api/db-conn", views.db_conn_api),
    # 页面托管放最后
    path("<str:name>", views.page),
]
