# -*- coding: utf-8 -*-
"""上传魔搭创空间前的自检 / 上传后的复查（Django 版）。

两种用法：

  1) 查本地待上传目录（默认 ../promptdeck_modelscope）
       python verify_upload.py
       python verify_upload.py D:/some/promptdeck_modelscope

  2) 查线上仓库根目录（上传完、构建失败时最快的定位手段）
       python verify_upload.py --remote Zhuqiwen112/PromptDeck

为什么需要它：
  创空间 Docker 型构建失败最常见的一条是
      [ERROR] failed to solve: failed to read dockerfile: open Dockerfile: no such file or directory
  含义只有一个 —— 仓库根目录没有 Dockerfile。本项目真实踩过：把 promptdeck/（开发目录）
  当成上传包传了上去，那个目录里根本没有 Dockerfile，却有一堆 verify_*.py / build_static.py。

本脚本同时是**泄漏闸门**：整包扫 `sk-…` 形态的密钥，命中即失败。
（密钥字面量不写在这里 —— 从 config/settings.py 现读，否则脚本自己会变成泄漏点。）

退出码：0 = 通过，1 = 有问题（会把问题逐条列出来）。
"""
import json
import os
import re
import sqlite3
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_LOCAL = os.path.join(ROOT, "promptdeck_modelscope")

# 上传到创空间后，这些必须能直接在「仓库根目录」看到
REQUIRED_ROOT = [
    "Dockerfile", "start.sh", "ms_deploy.json", "requirements.txt",
    "manage.py", "config", "core", "static", "data", ".gitignore",
]
REQUIRED_UNDER = {
    # 9 个页面，一个都不能少
    "static": ["Table_Structure_Extraction.html", "foundation1_models.html",
               "list_server.html", "model_add.html", "new_server.html",
               "prompt.html", "prompt_scheme.html", "prompt_test.html",
               "prompt_test_results_list.html"],
    "data": ["seed.sqlite", "seed.version"],
    "config": ["__init__.py", "settings.py", "urls.py", "wsgi.py", "asgi.py"],
    "core": ["__init__.py", "apps.py", "models.py", "store.py", "chat.py",
             "schema.py", "dbx.py", "views.py"],
}
# 开发目录（promptdeck/）独有的东西，混进上传包就说明选错了要传的文件夹
DEV_LEAK = [
    # 构建 / 校验脚本
    "build_static.py", "build_components.py", "pack_modelscope.py",
    "verify_upload.py", "verify_pages.py", "verify_django.py", "verify_db.py",
    "verify_ui.py", "verify_ui_tp.py", "verify_ui_pt.py", "verify_ui_res.py",
    "verify_ui_ts.py", "verify_modelscope.py", "verify_start.py",
    "migrate_to_twe.py", "setup_judge_key.py",
    # 上一版（Starlette）的入口与数据层：core/ 对它们零引用，不该进包
    "db.py", "server.py", "app.py",
    # 开发期的库 / 探针
    "data/prompt_test_DB.db", "data/prompt_test_DB_twe.db",
    "data/preview_local.sqlite", "comp",
]
# 包里必须存在的业务表（少了说明种子库不是这一版的）
SEED_TABLES = ["srv", "mdl", "tst_plan", "tst_plan_prompt",
               "tst_api_key", "tst_rec", "tst_rec_dtl", "tst_db_conn"]
# 明文密钥形态：settings.py 里那把是 sk- + 32 位十六进制
KEY_RE = re.compile(r"sk-[A-Za-z0-9]{20,}")
# 真正的「根绝对路径 URL 引用」。**不能**用简单的 `="/` 判断 ——
# prompt_test.html 导出 XLSX 时会写 ZIP 内部条目名（`="/xl/workbook.xml"`），
# 那是以斜杠开头的**文件名**，不是 URL，改了反而错。
ROOT_URL_RE = re.compile(
    r"""(?:href|src|action|poster|data-src)\s*=\s*["']/     # HTML 属性
      | url\(\s*["']?/                                      # CSS url(/…)
      | location(?:\.href|\.assign|\.replace)?\s*[=(]\s*["']/  # JS 跳转
      | fetch\(\s*["']/                                     # fetch("/api/…) 没改干净
    """,
    re.VERBOSE,
)
MAX_FILE = 100 * 1024 * 1024


def read(p):
    with open(p, encoding="utf-8", errors="replace") as f:
        return f.read()


def settings_secrets():
    """从开发目录的 settings.py 现读密钥，**不在本脚本里写死**。

    这样脚本既能当泄漏闸门（把这个值拿去全包扫），
    又不会因为自己含明文而被别的扫描抓出来。
    """
    p = os.path.join(HERE, "config", "settings.py")
    if not os.path.isfile(p):
        return []
    return KEY_RE.findall(read(p))


def check_local(dst):
    """返回 (errors, notes)。打包脚本也直接调用它做流水线内的预检。"""
    errs, notes = [], []
    if not os.path.isdir(dst):
        return ["目录不存在：%s" % dst], notes

    # ---------------- 1. 结构清单 ----------------
    for name in REQUIRED_ROOT:
        if not os.path.exists(os.path.join(dst, name)):
            errs.append("缺少根目录项：%s" % name)
    for parent, names in REQUIRED_UNDER.items():
        for name in names:
            if not os.path.exists(os.path.join(dst, parent, name)):
                errs.append("缺少 %s/%s" % (parent, name))

    for name in DEV_LEAK:
        if os.path.exists(os.path.join(dst, name)):
            errs.append("混入开发期文件：%s —— 这是 promptdeck/ 开发目录的东西，"
                        "选错要传的文件夹了" % name)

    # ---------------- 2. 部署配置 ----------------
    if os.path.isfile(os.path.join(dst, "ms_deploy.json")):
        d = read(os.path.join(dst, "ms_deploy.json"))
        if '"sdk_type": "docker"' not in d:
            errs.append("ms_deploy.json 的 sdk_type 不是 docker")
        if '"port": 7860' not in d:
            errs.append("ms_deploy.json 的 port 不是 7860")

    if os.path.isfile(os.path.join(dst, "start.sh")):
        s = read(os.path.join(dst, "start.sh"))
        if "0.0.0.0" not in s:
            errs.append("start.sh 未绑定 0.0.0.0")
        if "7860" not in s:
            errs.append("start.sh 未使用 7860 端口")
        if "8080" in s:
            errs.append("start.sh 出现平台保留端口 8080")
        if "manage.py runserver" not in s:
            errs.append("start.sh 没有用 manage.py runserver 起服务")
        if "seed.version" not in s:
            errs.append("start.sh 未使用 data/seed.version"
                        "（少了它，重新部署后线上数据不会跟着换）")

    if os.path.isfile(os.path.join(dst, "Dockerfile")):
        d = read(os.path.join(dst, "Dockerfile"))
        for need in ("FROM ", "CMD ", "EXPOSE 7860"):
            if need not in d:
                errs.append("Dockerfile 缺少 `%s`" % need.strip())
        if "python:3.12" not in d:
            errs.append("Dockerfile 基础镜像不是 python:3.12（Django 6 要求 Python>=3.12）")

    # ---------------- 3. 上线补丁真的打上了 ----------------
    sp = os.path.join(dst, "config", "settings.py")
    if os.path.isfile(sp):
        s = read(sp)
        if "DEBUG = os.environ.get(\"PD_DEBUG\")" not in s:
            errs.append("config/settings.py 的 DEBUG 没改写成读 PD_DEBUG"
                        "（线上开着调试页会把配置连同密钥一起打出来）")
        if "PROMPTDECK_DB" not in s:
            errs.append("config/settings.py 没有读 PROMPTDECK_DB（容器里定位不到持久化库）")
    vp = os.path.join(dst, "core", "views.py")
    if os.path.isfile(vp) and 'redirect("/" + HOME_PAGE)' in read(vp):
        errs.append("core/views.py 的首页跳转还是根绝对路径，平台子路径前缀下会跳错")

    # ---------------- 4. 页面：9 页齐全且 fetch 已相对化 ----------------
    static_dst = os.path.join(dst, "static")
    n_pages = 0
    if os.path.isdir(static_dst):
        for fn in sorted(os.listdir(static_dst)):
            if not fn.endswith(".html"):
                continue
            n_pages += 1
            t = read(os.path.join(static_dst, fn))
            if 'fetch("/api/' in t:
                errs.append("static/%s 里还有根绝对路径的 fetch" % fn)
            m = ROOT_URL_RE.search(t)
            if m:
                errs.append("static/%s 里还有根绝对路径的 URL 引用：%r" % (fn, m.group(0)))
    if n_pages != len(REQUIRED_UNDER["static"]):
        errs.append("static/ 里有 %d 个页面，期望 %d 个" % (n_pages, len(REQUIRED_UNDER["static"])))

    # ---------------- 5. 种子库 ----------------
    seed_p = os.path.join(dst, "data", "seed.sqlite")
    if os.path.isfile(seed_p):
        try:
            c = sqlite3.connect(seed_p)
            # 逐张点名缺哪张：整个 try 一次性抛错的话，报出来只会是
            # 「表结构不对」，看不出是「包旧了」还是「库坏了」。
            _missing = []
            for t in SEED_TABLES:
                try:
                    c.execute("select count(*) from %s" % t)
                except sqlite3.Error:
                    _missing.append(t)
            if _missing:
                errs.append("data/seed.sqlite 缺表：%s —— 包是旧的，"
                            "请用当前代码重新打包（pack_modelscope.py）" % "、".join(_missing))
                raise sqlite3.Error("缺表: %s" % "、".join(_missing))
            n_s = c.execute("select count(*) from srv").fetchone()[0]
            n_m = c.execute("select count(*) from mdl").fetchone()[0]
            n_k = c.execute("select count(*) from tst_api_key").fetchone()[0]
            if n_k:
                errs.append("data/seed.sqlite 的 tst_api_key 还有 %d 行密钥 —— 密钥不能带上线" % n_k)
            # 已连接数据库存档同样一条都不该带上线：里面是开发机连过的库
            # （主机 / 库名 / 用户名 / 落盘路径），对使用者没意义还等于公开内网拓扑。
            # ⚠️ 本表本来就没有密码列；这条闸门拦的是「连接信息」本身。
            n_c = c.execute("select count(*) from tst_db_conn").fetchone()[0]
            if n_c:
                errs.append("data/seed.sqlite 的 tst_db_conn 还有 %d 行已保存连接 —— "
                            "连接信息不能带上线（prepare_seed 的 clean_seed 没生效？）" % n_c)
            # 计费单位只允许原型下拉里的四个值
            bad_unit = c.execute(
                "select count(*) from mdl where unit is not null and unit not in "
                "('K','万','十万','百万')").fetchone()[0]
            if bad_unit:
                errs.append("data/seed.sqlite 里有 %d 行计费单位不在原型下拉的取值里" % bad_unit)
            # 同平台重名模型
            dup = c.execute("select count(*) from (select 1 from mdl "
                            "group by srv_id, mdl_name having count(*) > 1)").fetchone()[0]
            if dup:
                errs.append("data/seed.sqlite 里有 %d 组同平台重名模型（去重没生效）" % dup)
            c.close()
            if n_s <= 0:
                errs.append("data/seed.sqlite 里没有任何服务器记录（部署后页面会是空的）")
            else:
                notes.append("种子库：%d 服务器 / %d 模型 / 密钥 %d 行 / 连接存档 %d 行"
                             % (n_s, n_m, n_k, n_c))
        except Exception as exc:                                 # noqa: BLE001
            errs.append("data/seed.sqlite 打不开或表结构不对（不是可用库 / 还是个 LFS 指针）：%s" % exc)
    ver_p = os.path.join(dst, "data", "seed.version")
    if not os.path.isfile(ver_p) or not read(ver_p).strip():
        errs.append("缺少 data/seed.version —— 少了它，覆盖上传后线上不会刷新持久化库，"
                    "改了数据也不生效")

    gi = os.path.join(dst, ".gitignore")
    if os.path.isfile(gi) and "ms_deploy.json" in read(gi):
        errs.append(".gitignore 忽略了 ms_deploy.json —— git push 路线会因此漏掉部署配置")

    # ---------------- 6. 全包逐文件扫描 ----------------
    secrets = settings_secrets()
    root_items, biggest, n_files, leaked = [], 0, 0, []
    pycache = []
    for dp, dns, fn in os.walk(dst):
        if os.path.basename(dp) == "__pycache__":
            pycache.append(os.path.relpath(dp, dst).replace("\\", "/"))
        if dp == dst:
            root_items = sorted(fn + [d for d in dns if d != "__pycache__"])
        for f in fn:
            p = os.path.join(dp, f)
            rel = os.path.relpath(p, dst).replace("\\", "/")
            raw = open(p, "rb").read()
            n_files += 1
            biggest = max(biggest, len(raw))
            if raw.startswith(b"\xef\xbb\xbf"):
                errs.append("%s 带 UTF-8 BOM" % rel)
            if b"\r\n" in raw and rel in ("Dockerfile", "start.sh", "requirements.txt",
                                          "manage.py", ".gitignore"):
                errs.append("%s 是 CRLF 行尾，Linux 容器里会报 \\r 错" % rel)
            if len(raw) >= MAX_FILE:
                errs.append("%s 超过 100MB 单文件上限" % rel)
            if f.endswith(".pyc"):
                errs.append("混入编译产物：%s" % rel)
            hit = KEY_RE.findall(raw.decode("utf-8", "replace"))
            if hit:
                leaked.append((rel, len(hit)))

    if pycache:
        errs.append("混入 __pycache__ 目录：%s" % "  ".join(pycache[:5]))
    for rel, n in leaked:
        errs.append("【密钥泄漏】%s 里出现 %d 处 sk-… 形态的明文" % (rel, n))
    if secrets:
        notes.append("已用开发目录 settings.py 里读到的 %d 个密钥值做全包比对" % len(secrets))

    notes.append("仓库根目录 %d 项：%s" % (len(root_items), "  ".join(root_items)))
    notes.append("共 %d 个文件，最大文件 %.1f KB" % (n_files, biggest / 1024.0))
    return errs, notes


# ------------------------------------------------------------------ 线上复查
def _remote_files(repo, root=""):
    url = ("https://www.modelscope.cn/api/v1/studio/%s/repo/files"
           "?Revision=master&Root=%s" % (repo, root))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=25) as r:
        data = json.loads(r.read().decode("utf-8"))
    return (data.get("Data") or {}).get("Files") or []


def check_remote(repo):
    """查线上仓库根目录。repo 形如 ns/name。"""
    errs, notes = [], []
    try:
        files = _remote_files(repo)
    except Exception as e:                                       # noqa: BLE001
        return ["拉取线上仓库清单失败：%s" % e], notes
    if not files:
        return ["线上仓库根目录是空的（或空间不存在 / 未公开 / 接口变更）"], notes

    names = {f["Name"] for f in files}
    notes.append("线上仓库根目录 %d 项：%s" % (len(files), "  ".join(sorted(names))))

    for name in REQUIRED_ROOT:
        if name not in names:
            errs.append("线上根目录缺少：%s" % name)
    for parent, subs in REQUIRED_UNDER.items():
        if parent not in names:
            continue
        try:
            sub = {f["Name"] for f in _remote_files(repo, parent)}
        except Exception:                                        # noqa: BLE001
            continue
        for name in subs:
            if name not in sub:
                errs.append("线上缺少 %s/%s" % (parent, name))
    for name in DEV_LEAK:
        if name in names:
            errs.append("线上根目录有开发期文件：%s" % name)

    # LFS 检查要连子目录一起看：平台的系统级 .gitattributes 里 `*.db* filter=lfs`
    # 会把 data/*.db 这类种子库变成指针文件，CI 普通 clone 后容器里 SQLite 直接打不开。
    for prefix, items in [("", files)] + [(p, _remote_files(repo, p))
                                          for p in sorted(names & set(REQUIRED_UNDER))]:
        for f in items:
            if f.get("IsLFS"):
                errs.append("线上 %s 被 LFS 托管（IsLFS=true）—— CI 只做普通 clone 时"
                            "会拿到指针文件，容器里 SQLite 会报 not a database" % f["Path"])
    return errs, notes


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:                                            # noqa: BLE001
        pass

    args = sys.argv[1:]
    if "--remote" in args:
        i = args.index("--remote")
        repo = args[i + 1].strip().strip("/").replace("studios/", "")
        target = "线上仓库 " + repo
        errs, notes = check_remote(repo)
    else:
        dst = os.path.abspath(args[0]) if args else DEFAULT_LOCAL
        target = dst
        errs, notes = check_local(dst)

    print("自检目标：%s" % target)
    print("-" * 74)
    for n in notes:
        print("  · " + n)
    print("-" * 74)
    if errs:
        print("  ✗ 发现 %d 个问题：" % len(errs))
        for e in errs:
            print("    - " + e)
        if any("Dockerfile" in e for e in errs):
            print("\n  提示：构建日志若出现 `failed to read dockerfile: open Dockerfile:"
                  " no such file or directory`，与此处的 Dockerfile 缺失是同一个问题。")
        sys.exit(1)
    print("  ✓ 通过：根目录具备 Dockerfile / start.sh / ms_deploy.json / manage.py，"
          "9 页齐全，无明文密钥，可以上传并构建")


if __name__ == "__main__":
    main()
