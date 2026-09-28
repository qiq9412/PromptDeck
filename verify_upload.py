# -*- coding: utf-8 -*-
"""上传魔搭创空间前的自检 / 上传后的复查。

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
  当成上传包传了上去，那个目录里根本没有 Dockerfile，却有一堆 verify_*.py / comp/。

退出码：0 = 通过，1 = 有问题（会把问题逐条列出来）。
"""
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_LOCAL = os.path.join(ROOT, "promptdeck_modelscope")

REQUIRED_ROOT = [
    "Dockerfile", "start.sh", "ms_deploy.json", "requirements.txt",
    "server.py", "db.py", "static", "data",
]
REQUIRED_UNDER = {
    "static": ["foundation1_models.html", "list_server.html",
               "model_add.html", "new_server.html", "prompt.html"],
    "data": ["seed.sqlite"],
}
DEV_LEAK = [
    "app.py", "comp", "build_components.py", "build_static.py",
    "verify_db.py", "verify_ui.py", "verify_pages.py",
    "verify_modelscope.py", "verify_streamlit.py", "pack_modelscope.py",
]
MAX_FILE = 100 * 1024 * 1024


def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def check_local(dst):
    """返回 (errors, notes)。"""
    errs, notes = [], []
    if not os.path.isdir(dst):
        return ["目录不存在：%s" % dst], notes

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

    if os.path.isfile(os.path.join(dst, "Dockerfile")):
        d = read(os.path.join(dst, "Dockerfile"))
        for need in ("FROM ", "CMD ", "EXPOSE 7860"):
            if need not in d:
                errs.append("Dockerfile 缺少 `%s`" % need.strip())

    gi = os.path.join(dst, ".gitignore")
    if os.path.isfile(gi) and "ms_deploy.json" in read(gi):
        errs.append(".gitignore 忽略了 ms_deploy.json —— git push 路线会漏掉部署配置")

    root_items, biggest = [], 0
    for dp, _dn, fn in os.walk(dst):
        if dp == dst:
            root_items = sorted(fn + [d for d in _dn])
        for f in fn:
            p = os.path.join(dp, f)
            rel = os.path.relpath(p, dst).replace("\\", "/")
            raw = open(p, "rb").read()
            biggest = max(biggest, len(raw))
            if raw.startswith(b"\xef\xbb\xbf"):
                errs.append("%s 带 UTF-8 BOM" % rel)
            if b"\r\n" in raw and rel in ("Dockerfile", "start.sh", "requirements.txt"):
                errs.append("%s 是 CRLF 行尾，Linux 容器里会报 \\r 错" % rel)
            if len(raw) >= MAX_FILE:
                errs.append("%s 超过 100MB 单文件上限" % rel)

    notes.append("仓库根目录 %d 项：%s" % (len(root_items), "  ".join(root_items)))
    notes.append("最大文件 %.1f KB" % (biggest / 1024.0))
    return errs, notes


def check_remote(repo):
    """查线上仓库根目录。repo 形如 ns/name。"""
    errs, notes = [], []
    url = ("https://www.modelscope.cn/api/v1/studio/%s/repo/files"
           "?Revision=master&Root=" % repo)
    try:
        with urllib.request.urlopen(url, timeout=25) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        return ["拉取线上仓库清单失败：%s" % e], notes

    files = (data.get("Data") or {}).get("Files") or []
    if not files:
        return ["线上仓库根目录是空的（或空间不存在/未公开）"], notes

    names = {f["Name"] for f in files}
    notes.append("线上仓库根目录 %d 项：%s" % (len(files), "  ".join(sorted(names))))

    for name in REQUIRED_ROOT:
        if name not in names:
            errs.append("线上根目录缺少：%s" % name)
    for parent, subs in REQUIRED_UNDER.items():
        if parent not in names:
            continue
        sub = {f["Name"] for f in _remote_files(repo, parent)}
        for name in subs:
            if name not in sub:
                errs.append("线上缺少 %s/%s" % (parent, name))
    for name in DEV_LEAK:
        if name in names:
            errs.append("线上根目录有开发期文件：%s" % name)

    # LFS 检查要连子目录一起看：平台的系统级 .gitattributes 里 `*.db* filter=lfs`
    # 会把 data/*.db 这类种子库变成指针文件，CI 普通 clone 后容器里 SQLite 直接打不开。
    scanned = [("", files)] + [(p, _remote_files(repo, p))
                               for p in sorted(names & set(REQUIRED_UNDER))]
    for prefix, items in scanned:
        for f in items:
            if f.get("IsLFS"):
                errs.append("线上 %s 被 LFS 托管（IsLFS=true）—— CI 只做普通 clone 时"
                            "会拿到指针文件，容器里 SQLite 会报 not a database" % f["Path"])
    return errs, notes


def _remote_files(repo, root):
    url = ("https://www.modelscope.cn/api/v1/studio/%s/repo/files"
           "?Revision=master&Root=%s" % (repo, root))
    try:
        with urllib.request.urlopen(url, timeout=25) as r:
            data = json.loads(r.read().decode("utf-8"))
        return (data.get("Data") or {}).get("Files", [])
    except Exception:  # noqa: BLE001
        return []


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
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
    print("-" * 66)
    for n in notes:
        print("  · " + n)
    print("-" * 66)
    if errs:
        print("  ✗ 发现 %d 个问题：" % len(errs))
        for e in errs:
            print("    - " + e)
        if any("Dockerfile" in e for e in errs):
            print("\n  提示：构建日志若出现 `failed to read dockerfile: open Dockerfile:"
                  " no such file or directory`，与此处的 Dockerfile 缺失是同一个问题。")
        sys.exit(1)
    print("  ✓ 通过：根目录具备 Dockerfile / start.sh / ms_deploy.json，可以上传并构建")


if __name__ == "__main__":
    main()
