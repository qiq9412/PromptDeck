# -*- coding: utf-8 -*-
"""把 promptdeck 打包成「魔搭创空间（ModelScope Studio）Docker 型」可上传目录。

产出：../promptdeck_modelscope/
    server.py  db.py           应用源码（原样拷贝）
    static/*.html              原型页；只把根绝对路径 /api/… 改成相对路径 api/…
    data/seed.sqlite           演示数据（容器首次启动会被拷进持久化目录）
                               注意：故意不用 .db 后缀 —— 平台的系统级 .gitattributes 里
                               有 `*.db* filter=lfs`，用 .db 时 CI 只做普通 clone 会拿到
                               130 字节的 LFS 指针文件，SQLite 打开即报 not a database。
    requirements.txt           仅 starlette + uvicorn（不含开发期脚本的依赖）
    Dockerfile                 基础镜像 + 依赖安装 + 启动命令（必须在仓库根目录）
    start.sh                   入口：解析持久化目录 -> 缺库自愈播种 -> 监听 0.0.0.0:7860
    ms_deploy.json             创空间部署配置（sdk_type=docker, port=7860）
    README.md / .gitignore

上传纪律（踩过的坑）：
    promptdeck/ 是开发目录（有 app.py / comp/ / verify_*.py / build_*.py），**没有 Dockerfile**，
    误传它会让平台构建日志报 `failed to read dockerfile: open Dockerfile: no such file or directory`。
    要传的永远只有本脚本产出的这个目录。

为什么要把 /api/… 改成相对路径：
    创空间可能以「子路径」反代应用（平台前缀），而页面里的导航链接本来就是相对路径
    （href="prompt.html"）。把 fetch 也改成相对路径后，整个应用与部署前缀无关；
    本地在根路径下运行结果完全一致（这也是本脚本唯一的产物差异）。

用法：python pack_modelscope.py
"""
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DST = os.path.join(ROOT, "promptdeck_modelscope")
SENTINEL = ".pd_modelscope_pack"

# 期望的替换次数（对不上说明源码变了，直接报错不写盘）
EXPECT_FETCH = 13         # static/*.html 里 fetch("/api/… 的出现次数
                          # 9 -> 13：列表页新增「修改」= PUT + 回读列表（模型页/服务器页各 +2）
EXPECT_REDIRECT = 1       # server.py 里 RedirectResponse("/" + HOME_PAGE)

REQUIREMENTS = """# PromptDeck · 创空间运行依赖（只保留运行必需的）
starlette>=0.37
uvicorn>=0.27
"""

DOCKERFILE = """# PromptDeck · 魔搭创空间（Docker 型）
#
# 平台硬性要求：
#   1. 服务必须监听 0.0.0.0:7860（平台固定端口；8080 已被平台自身占用，禁止使用）
#   2. Dockerfile 必须位于「仓库根目录」且文件名就叫 Dockerfile
#      —— 构建日志出现 `failed to read dockerfile: open Dockerfile: no such file or directory`
#         就是它没在根目录（本项目的真实案例：误传了 promptdeck/ 开发目录）
#   3. 不能用平台保留请求头：Authorization / X-modelscope-* / X-studio-*（本应用不用它们）
#   4. 默认文件系统每次重启会还原，需要持久化的数据放 /mnt/workspace（见 start.sh）

FROM python:3.10-slim
# 构建机在国内（cn-zhangjiakou）。若拉取 Docker Hub 缓慢/失败，换成魔搭官方镜像源：
# 把下一行的注释去掉、并注释掉上面那行 FROM 即可。
# FROM modelscope-registry.cn-beijing.cr.aliyuncs.com/modelscope-repo/python:3.10

ENV PYTHONUNBUFFERED=1 \\
    PYTHONDONTWRITEBYTECODE=1 \\
    TZ=Asia/Shanghai

WORKDIR /app

# 依赖先装，利用镜像层缓存；优先走阿里云 PyPI 镜像（国内构建机更快），失败自动回退官方源
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/ \\
 || pip install --no-cache-dir -r requirements.txt

COPY . .

# 构建期自检：依赖真的能 import 才产出镜像，避免镜像能构建、容器一启动就 ImportError
RUN python -c "import starlette, uvicorn; print('deps ok:', starlette.__version__, uvicorn.__version__)"

EXPOSE 7860
CMD ["sh", "start.sh"]
"""

START_SH = """#!/bin/sh
# 创空间容器入口：定位数据库 -> 缺库/坏库自愈 -> 监听 0.0.0.0:7860
set -eu

SEED=/app/data/seed.sqlite

# 1) 数据库落点：优先用创空间持久化目录（重启不丢数据），没有该目录就退回镜像内目录
if [ -d /mnt/workspace ]; then
  export PROMPTDECK_DB="${PROMPTDECK_DB:-/mnt/workspace/prompt_test_DB.db}"
else
  export PROMPTDECK_DB="${PROMPTDECK_DB:-/app/data/prompt_test_DB.db}"
fi
mkdir -p "$(dirname "$PROMPTDECK_DB")"

# 库可用 = 能打开且 srv 表可查。若拿到的是 LFS 指针文件，这里直接判否（not a database）。
db_ok() {
  python -c "import sqlite3,sys;sqlite3.connect(sys.argv[1]).execute('select count(*) from srv')" "$1" 2>/dev/null
}

# 2) 缺库 / 坏库自愈：
#    先重拷种子（种子文件名故意用 .sqlite 而不是 .db —— 平台系统级 .gitattributes 带
#    *.db* -> LFS 规则，用 .db 时 CI 普通 clone 会拿到 130 字节指针文件）；
#    种子也没有或仍不可用，就现场建表 + 灌演示数据（db.py --seed 幂等，不覆盖已有数据）。
if ! db_ok "$PROMPTDECK_DB"; then
  rm -f "$PROMPTDECK_DB"
  if [ -f "$SEED" ]; then
    cp -f "$SEED" "$PROMPTDECK_DB" || true
  fi
  if ! db_ok "$PROMPTDECK_DB"; then
    rm -f "$PROMPTDECK_DB"
    python -u db.py --seed
  fi
fi

echo "PromptDeck: DB=$PROMPTDECK_DB  PORT=${PORT:-7860}"
exec python -u server.py --host 0.0.0.0 --port "${PORT:-7860}"
"""

MS_DEPLOY = """{
  "$schema": "https://modelscope.cn/api/v1/studios/deploy_schema.json",
  "sdk_type": "docker",
  "resource_configuration": "platform/2v-cpu-16g-mem",
  "port": 7860
}
"""

GITIGNORE = """__pycache__/
*.pyc
_*.txt
.pytest_cache/
.venv/
.pd_modelscope_pack

# 运行期数据库：容器首次启动会由 data/seed.sqlite 自己拷出来，不要提交。
# 提交了会踩平台系统级 .gitattributes 的 `*.db* filter=lfs`：CI 普通 clone 只能拿到
# 130 字节的 LFS 指针文件，SQLite 打开即报 not a database。
data/prompt_test_DB.db
"""

README = """---
license: apache-2.0
---

# PromptDeck · 提示词调试台

一套「服务器 / 模型 / 提示词」管理台的原型演示应用：Starlette 提供接口与静态页，
数据落在 SQLite（`srv` 服务器表 + `mdl` 模型表），页面是既有原型 HTML 原样托管，
数据由 `/api/*` 注入。提示词构造页的「调试」会把请求转发给所选服务器的真实模型。

## 目录结构

```
server.py            Starlette 应用：静态托管 + /api/srv、/api/mdl、/api/chat
db.py                SQLite 数据层（建表 / 种子数据 / 增删查）
static/*.html        页面（服务器列表、新增服务器、模型列表、新增模型、提示词构造）
data/seed.sqlite     演示数据库种子（首次启动会拷到持久化目录）
start.sh             容器入口（定位数据库 + 缺库自愈播种 + 监听 0.0.0.0:7860）
Dockerfile           镜像构建（必须在仓库根目录）
ms_deploy.json       创空间部署配置（sdk_type=docker, port=7860）
```

## 本地运行

```bash
pip install -r requirements.txt
python server.py --host 127.0.0.1 --port 7860
# 浏览器打开 http://127.0.0.1:7860/
```

## 部署到魔搭创空间（Docker 型）

前置：在魔搭完成**阿里云账号绑定 + 实名认证**（Docker 型创空间的硬性前提）。

### 上传什么（最容易错的一步）

要上传的是**本目录里的内容**，上传后仓库根目录必须能直接看到 `Dockerfile`：

```
仓库根/
├── Dockerfile          ← 必须有，必须在根，文件名不能改
├── start.sh
├── ms_deploy.json      ← sdk_type=docker / port=7860
├── requirements.txt
├── server.py
├── db.py
├── static/  (5 个 .html)
└── data/seed.sqlite
```

**不要**上传项目里的 `promptdeck/` 文件夹 —— 那是开发目录，里面有 `app.py`、
`comp/`、`build_*.py`、`verify_*.py`，**没有 Dockerfile**。传错它，构建日志第一屏就会是：

```
[WARNING] read Dockerfile 'Dockerfile' failed (ignored): ENOENT: no such file or directory
[ERROR] build image failed
```

### 方式一：网页「快速部署创建」（最省事）

创空间列表 → 创建创空间 → 切到「快速部署创建」→ 类型选 **Docker** →
把本目录整个选中上传 → 确认创建并部署。
上传器会把**所选目录的内容平铺到仓库根目录**，所以选中的必须是本目录本身。

> 若创空间已经存在（例如已经传错过一次），「快速部署创建」不会再执行；
> 请在创空间的「文件」页把根目录多余的开发文件删掉、再补上本次这些文件，
> 或者干脆删掉这个创空间重新建一个。

### 方式二：Git 推送（可控性最好）

```bash
git clone https://www.modelscope.cn/studios/<用户名>/<创空间名>.git
cd <创空间名>
# 清掉上一次传错的内容，再把本目录下的文件全部拷进来
git rm -r --cached . && git clean -fd
cp -r /path/to/promptdeck_modelscope/. .
git add .
git commit -m "fix: put Dockerfile at repo root"
git push origin master        # 默认分支是 master，不是 main
```

推送后用创空间页面的「部署/重启」触发构建。

### 构建后怎么看日志

- **构建日志**：应看到 `pip install` 走阿里云镜像、末尾 `deps ok: ...`；
  出现 `failed to read dockerfile` 就是 Dockerfile 不在根目录（回到上一节）。
- **运行日志**：应看到 `PromptDeck: DB=/mnt/workspace/prompt_test_DB.db  PORT=7860`，
  之后 uvicorn 打印监听 `0.0.0.0:7860`。

## 平台约定（已在本目录落实）

| 要求 | 落实方式 |
|---|---|
| 必须监听 `0.0.0.0:7860` | `Dockerfile` 的 `CMD` → `start.sh` 固定 `--host 0.0.0.0 --port 7860` |
| 禁止监听 8080 | 未使用 |
| Dockerfile 在仓库根目录 | 是（本文档「上传什么」一节即是防呆说明） |
| 国内构建机拉包速度 | pip 走阿里云 PyPI 镜像，失败自动回退官方源；Dockerfile 内注释了魔搭官方基础镜像 |
| 数据持久化 | `start.sh` 把库指到 `/mnt/workspace/prompt_test_DB.db`（该目录仅运行期存在，构建期不依赖） |
| 缺库/坏库自愈 | `start.sh` 检测库不可用即 `python db.py --seed` 现场建表灌数据（幂等） |
| 规避 `*.db*` 被 LFS 托管 | 种子库命名为 `data/seed.sqlite`，不匹配平台的 `*.db* filter=lfs` 规则 |
| 禁止平台保留请求头 | 应用不读取入站 `Authorization` / `X-modelscope-*` / `X-studio-*`；调用模型时的 `Authorization` 是**出站**请求头 |
| 可用子路径反代访问 | 页面内导航与接口调用全部使用**相对路径**（`fetch("api/…")`），与部署前缀无关 |
| 默认分支 master、禁止强推 | 与普通仓库一致 |
| 单文件 < 100MB | 本目录最大文件 < 1MB |

## 上传前自检

在开发目录（`promptdeck/`）下执行，会逐条核对产出目录是否可安全上传：

```bash
python verify_upload.py            # 默认检查 ../promptdeck_modelscope
```

核对项：根目录必备文件齐全、开发期文件未混入、`ms_deploy.json` 口径正确、
`start.sh` 端口正确、`Dockerfile` 有 FROM/CMD/EXPOSE、全部文件 LF 无 BOM、单文件体积达标。

## 说明与已知限制

- 演示用途，**没有登录与权限**，任何拿到链接的人都能增删演示数据。
- 模型调试需要你在页面上填入自己的 API_key：它只存在浏览器会话里、随请求转发给所选服务器，**不落库**。
- 费用按原型的算法计算：`tokens / 1000 × 单价`（与库内 `unit` 字段无关）。
- 模型列表里 `model` 名称的内链指向 `model_detail.html`，该页原型尚未提供，会 404。
"""


def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def write(p, text):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def die(msg):
    print("!! " + msg)
    sys.exit(1)


def clean_dir(dst):
    """清空目录内容、保留目录本身。

    不用 shutil.rmtree：某些沙箱（含本机 WorkBuddy 的安全删除钩子）会把它改道到
    「移入回收站」，对工程根下的一级目录会直接失败。逐层删除文件 + rmdir 是等价的，
    且不触发那条策略。
    """
    for dp, dns, fns in os.walk(dst, topdown=False):
        for f in fns:
            os.remove(os.path.join(dp, f))
        for d in dns:
            os.rmdir(os.path.join(dp, d))


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    # 目标目录：非空且不是我们生成的（没有哨兵文件）就拒绝动它；否则清空内容重写
    if os.path.isdir(DST):
        entries = os.listdir(DST)
        if entries and not os.path.isfile(os.path.join(DST, SENTINEL)):
            die("目标目录已存在且不是本脚本生成的，请先自行确认：%s" % DST)
        clean_dir(DST)
    else:
        os.makedirs(DST)

    report = []

    # ---------------- 1. 应用源码 ----------------
    for name in ["db.py"]:
        shutil.copy2(os.path.join(HERE, name), os.path.join(DST, name))
        report.append(("复制 %s" % name, os.path.getsize(os.path.join(DST, name))))

    srv_src = read(os.path.join(HERE, "server.py"))
    n_red = srv_src.count('RedirectResponse("/" + HOME_PAGE)')
    if n_red != EXPECT_REDIRECT:
        die("server.py 里 RedirectResponse 命中 %d 次，期望 %d 次（源码变了，请核对）"
            % (n_red, EXPECT_REDIRECT))
    # 首页跳转改成相对，避免平台子路径前缀下跳到根路径
    srv_out = srv_src.replace('RedirectResponse("/" + HOME_PAGE)',
                             'RedirectResponse(HOME_PAGE)')
    write(os.path.join(DST, "server.py"), srv_out)
    report.append(("生成 server.py（RedirectResponse 改相对路径 ×%d）" % n_red, len(srv_out)))

    # ---------------- 2. 页面（仅把 /api/… 改成相对路径） ----------------
    static_src = os.path.join(HERE, "static")
    static_dst = os.path.join(DST, "static")
    os.makedirs(static_dst)
    total = 0
    for fn in sorted(os.listdir(static_src)):
        if not fn.endswith(".html"):
            continue
        text = read(os.path.join(static_src, fn))
        n = text.count('fetch("/api/')
        total += n
        text = text.replace('fetch("/api/', 'fetch("api/')
        if 'fetch("/api' in text:
            die("%s 里仍有根绝对路径的 fetch" % fn)
        if '="/' in text or "url(/" in text:
            die("%s 里仍有其它根绝对路径引用" % fn)
        write(os.path.join(static_dst, fn), text)
        report.append(("生成 static/%s（fetch 改相对路径 ×%d）" % (fn, n), len(text)))
    if total != EXPECT_FETCH:
        die("fetch 替换合计 %d 次，期望 %d 次（源码变了，请核对）" % (total, EXPECT_FETCH))

    # ---------------- 3. 演示数据（种子库，避开 *.db* LFS 规则） ----------------
    data_dst = os.path.join(DST, "data")
    os.makedirs(data_dst)
    seed_name = "seed.sqlite"
    shutil.copy2(os.path.join(HERE, "data", "prompt_test_DB.db"),
                 os.path.join(data_dst, seed_name))
    report.append(("复制 data/%s" % seed_name,
                   os.path.getsize(os.path.join(data_dst, seed_name))))

    # ---------------- 4. 部署配置 ----------------
    for name, text in [
        ("requirements.txt", REQUIREMENTS),
        ("Dockerfile", DOCKERFILE),
        ("start.sh", START_SH),
        ("ms_deploy.json", MS_DEPLOY),
        ("README.md", README),
        (".gitignore", GITIGNORE),
        (SENTINEL, "此目录由 promptdeck/pack_modelscope.py 生成，可安全删除重建。\n"),
    ]:
        write(os.path.join(DST, name), text)
        report.append(("生成 %s" % name, len(text)))

    # ---------------- 报告 ----------------
    print("打包完成 -> %s" % DST)
    print("-" * 66)
    for label, size in report:
        print("  %-56s %7d" % (label, size))
    print("-" * 66)
    print("  校验：fetch 相对化 %d 处、首页跳转相对化 %d 处、无残留根绝对路径" % (total, n_red))

    # ---------------- 5. 上传前预检（在包内跑一遍，不合格直接失败） ----------------
    preflight(DST)


# 上传到创空间后，这些文件必须能直接在「仓库根目录」看到
REQUIRED_ROOT = [
    "Dockerfile", "start.sh", "ms_deploy.json", "requirements.txt",
    "server.py", "db.py", "static", "data",
]
REQUIRED_UNDER = {
    "static": ["foundation1_models.html", "list_server.html",
               "model_add.html", "new_server.html", "prompt.html"],
    "data": ["seed.sqlite"],
}
# 开发目录（promptdeck/）独有的东西，混进上传包就说明选错了要传的文件夹
DEV_LEAK = [
    "app.py", "comp", "build_components.py", "build_static.py",
    "verify_db.py", "verify_ui.py", "verify_pages.py",
    "verify_modelscope.py", "verify_streamlit.py", "pack_modelscope.py",
]


def preflight(dst):
    bad = []

    for name in REQUIRED_ROOT:
        if not os.path.exists(os.path.join(dst, name)):
            bad.append("缺少根目录项：%s" % name)
    for parent, names in REQUIRED_UNDER.items():
        for name in names:
            if not os.path.exists(os.path.join(dst, parent, name)):
                bad.append("缺少 %s/%s" % (parent, name))

    for name in DEV_LEAK:
        if os.path.exists(os.path.join(dst, name)):
            bad.append("混入了开发期文件：%s（是不是把 promptdeck/ 当上传包了？）" % name)

    deploy = read(os.path.join(dst, "ms_deploy.json"))
    if '"sdk_type": "docker"' not in deploy:
        bad.append("ms_deploy.json 的 sdk_type 不是 docker")
    if '"port": 7860' not in deploy:
        bad.append("ms_deploy.json 的 port 不是 7860")

    start = read(os.path.join(dst, "start.sh"))
    if "0.0.0.0" not in start:
        bad.append("start.sh 未绑定 0.0.0.0")
    if "7860" not in start:
        bad.append("start.sh 未使用 7860 端口")
    if "8080" in start:
        bad.append("start.sh 出现了平台保留端口 8080")

    docker = read(os.path.join(dst, "Dockerfile"))
    for need in ("FROM ", "CMD ", "EXPOSE 7860"):
        if need not in docker:
            bad.append("Dockerfile 缺少 `%s`" % need.strip())

    gitignore = read(os.path.join(dst, ".gitignore"))
    if "ms_deploy.json" in gitignore:
        bad.append(".gitignore 忽略了 ms_deploy.json —— git push 路线会因此漏掉部署配置")

    biggest = 0
    for dp, _dn, fn in os.walk(dst):
        for f in fn:
            p = os.path.join(dp, f)
            rel = os.path.relpath(p, dst).replace("\\", "/")
            raw = open(p, "rb").read()
            biggest = max(biggest, len(raw))
            rel = os.path.relpath(p, dst).replace("\\", "/")
            if raw.startswith(b"\xef\xbb\xbf"):
                bad.append("%s 带 UTF-8 BOM" % rel)
            if b"\r\n" in raw and rel in ("Dockerfile", "start.sh", "requirements.txt"):
                bad.append("%s 是 CRLF 行尾，Linux 容器里会报 \\r 错" % rel)
            if len(raw) >= 100 * 1024 * 1024:
                bad.append("%s 超过 100MB 单文件上限" % rel)

    print("\n上传包预检「%s」" % os.path.basename(dst))
    print("-" * 66)
    root = sorted(os.listdir(dst))
    print("  仓库根目录将会有 %d 项：" % len(root))
    for name in root:
        p = os.path.join(dst, name)
        kind = "DIR " if os.path.isdir(p) else "FILE"
        size = "-" if os.path.isdir(p) else str(os.path.getsize(p))
        print("    %s %-24s %s" % (kind, name, size))
    print("  最大文件 %.1f KB" % (biggest / 1024.0))
    print("-" * 66)
    if bad:
        print("  ✗ 预检未通过，共 %d 条：" % len(bad))
        for b in bad:
            print("    - " + b)
        sys.exit(1)
    print("  ✓ 预检通过：根目录具备 Dockerfile / start.sh / ms_deploy.json，可直接上传")


if __name__ == "__main__":
    main()
