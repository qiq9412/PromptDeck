# -*- coding: utf-8 -*-
"""ASGI 入口（python manage.py runserver 也可以直接用它）。"""
import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

application = get_asgi_application()
