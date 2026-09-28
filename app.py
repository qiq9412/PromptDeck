# -*- coding: utf-8 -*-
"""PromptDeck · Streamlit 版入口。

架构：
    Streamlit 负责「框架 + 路由 + 数据落库」；
    两个页面（服务器列表 / 新增服务器）以原型 HTML 原样内嵌在 Streamlit 组件 iframe 里，
    界面 100% 保真；交互通过组件桥回传事件，由 Python 写库后再把最新数据注入 iframe。

运行：
    streamlit run app.py
    streamlit run app.py --server.port 8501

数据：
    SQLite，库名 prompt_test_DB（data/prompt_test_DB.db），表 srv / mdl，见 db.py。
"""
import os

import streamlit as st
from streamlit.components.v1 import declare_component

import db

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COMP_DIR = os.path.join(BASE_DIR, "comp")

st.set_page_config(page_title="PromptDeck · LLM Workbench", layout="wide",
                   initial_sidebar_state="collapsed")

# 去掉 Streamlit 自带留白，让内嵌的原型页铺满可视区
st.markdown(
    """
    <style>
      header[data-testid="stHeader"]{display:none !important;}
      [data-testid="stToolbar"]{display:none !important;}
      [data-testid="stDecoration"]{display:none !important;}
      [data-testid="stStatusWidget"]{display:none !important;}
      [data-testid="stMainBlockContainer"],
      [data-testid="stAppViewBlockContainer"],
      section.main .block-container,
      .block-container{
        padding:0 !important; margin:0 !important; max-width:100% !important;
      }
      [data-testid="stAppViewContainer"]{padding:0 !important;}
      [data-testid="stVerticalBlock"]{gap:0 !important;}
      iframe[title="streamlit_custom_component"]{display:block !important;}
    </style>
    """,
    unsafe_allow_html=True,
)


# 声明两个静态组件。注册名会被 Streamlit 拼成 "<模块名>.<name>"，
# 静态资源地址即 /component/app.pd_list/ 与 /component/app.pd_new/（目录请求会返回 index.html）。
COMP = {
    "list": declare_component("pd_list", path=os.path.join(COMP_DIR, "list")),
    "new": declare_component("pd_new", path=os.path.join(COMP_DIR, "new")),
}


def to_public(row):
    """srv 行 -> 前端行对象。外链口径：仅公司名指向 home_url。"""
    return {
        "id": row["id"],
        "platform": row["plat_name"],
        "company": row["comp_name"] or "",
        "api": row["base_url"] or "",
        "cUrl": row["home_url"] or "",
    }


def handle(value):
    """处理组件回传的事件：换页 / 删除 / 保存。"""
    if not isinstance(value, dict):
        return

    # 组件值在 rerun 之间会保持，用 nonce 防止同一次操作被重复执行
    nonce = value.get("nonce")
    if nonce:
        if nonce == st.session_state.get("_nonce"):
            return
        st.session_state["_nonce"] = nonce

    event = value.get("event")

    if event == "nav":
        to = value.get("to")
        if to == "list_server":
            st.session_state.page = "list"
        elif to == "new_server":
            st.session_state.page = "new"
        else:
            return          # 未接入的页面由前端自行提示，不换页
        st.rerun()

    elif event == "delete":
        ids = value.get("ids") or []
        if ids:
            db.delete_srv(ids)
        st.rerun()

    elif event == "save":
        data = value.get("data") or {}
        db.create_srv(
            data.get("plat_name", ""),
            data.get("base_url", ""),
            data.get("comp_name", ""),
            data.get("home_url", ""),
        )
        st.session_state.page = "list"
        st.rerun()


def main():
    db.init_db()
    db.seed_if_empty()

    if "page" not in st.session_state:
        st.session_state.page = "list"

    if st.session_state.page == "list":
        rows = [to_public(r) for r in db.list_srv()]
        value = COMP["list"](page="list_server", rows=rows, default=None, key="pd_list")
    else:
        value = COMP["new"](page="new_server", default=None, key="pd_new")

    handle(value)


main()
