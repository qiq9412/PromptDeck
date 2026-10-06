#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""PromptDeck · Django 版管理入口（第二次迭代）。

    python manage.py runserver 8502      # 起服务 -> http://127.0.0.1:8502/
    python manage.py migrate             # 建 tp 表 + 灌入示例方案
    python manage.py check               # 工程自检

说明：本次迭代把 web 框架由 Starlette 换成 Django（用户点名要求）。
     原型页（工作区根目录的 *.html）字段 / 风格 / 排版 / CSS 零改动，
     由 build_static.py 打「数据接线」补丁后生成 static/ 下的产物，
     再由 core.views.page 原样托管；页面内只把数据源换成 fetch("/api/*")。
     旧 Starlette 入口 server.py 仍留在目录里，可直接删掉（已不再使用）。
"""
import os
import sys


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "没找到 Django，请先安装：pip install -r requirements.txt"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
