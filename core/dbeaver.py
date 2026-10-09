# -*- coding: utf-8 -*-
"""PromptDeck · 链接数据库调试（第三次迭代新增）。

对应「提示词构造」页模型输出区的新按钮：把**最新一次模型生成的 SQL** 送进
DBeaver 的 SQL 编辑器，并按「数据库表结构提取」页记录的连接信息打开对应的库。

实现走 DBeaver 官方命令行（三个平台通用）：
    <dbeaver> -con "driver=…|host=…|port=…|database=…|user=…|name=…|openConsole=true"
              -f <把 SQL 写进去的临时 .sql 文件>

要点：
  · **密码全程不参与**。前端交接时就不登记密码（见 Table_Structure_Extraction.html 的
    pdRememberConn），这里也只把 user 带过去，认证交给 DBeaver 自己已保存的凭据；
  · `-f` 打开的是一个**新写的临时 .sql 文件**，DBeaver 会为它开一个新编辑器标签，
    不会覆盖用户当前编辑器里的内容；
  · 可执行文件位置：先用 settings.DBEAVER_PATH（或环境变量 PROMPTDECK_DBEAVER），
    再按三个平台的常见安装位置探测；都找不到就回明确提示，让用户去配置。

边界：
  · 只在本机起一个 GUI 进程，不做任何服务端连接、不写任何库；
  · 连不上库 / 文件不存在这类问题由 **DBeaver 自己**在界面上报错 —— 命令行只负责
    「打开窗口 + 打开连接 + 打开脚本」，DBeaver 的弹窗文案我们看不见，所以接口成功
    只代表「已成功唤起 DBeaver 并把参数交给它」，这点在返回体里用 launched 明示。
"""
import os
import re
import subprocess
import sys
import tempfile

from django.conf import settings

# 各数据库类型 → DBeaver 驱动 ID（命令行 -con 的 driver=）
DRIVER_ID = {
    "MySQL": "mysql",
    "MariaDB": "mysql",
    "PostgreSQL": "postgresql",
    "SQL Server": "sqlserver",
    "Oracle": "oracle",
    "Db2 for LUW": "db2",
    "Db2": "db2",
    "SQLite": "sqlite",
}

# 唤起后等多久再判断「是不是秒退了」。DBeaver 是 Eclipse 应用，冷启动要十几秒；
# 但**秒退≠ 失败** —— 单实例机制下启动器把参数转交给已运行实例后自己就退出了（rc=0）。
# 所以判据是「非 0 退出码」或「超时仍在跑」，不是「进程还活着」。
LAUNCH_WAIT = 3.0


# ---------------------------------------------------------------- 可执行文件
# 各盘根目录里「可能装着便携版」的子目录名（绿色版解压到哪儿都行，只能浅层找）
_PORTABLE_HINTS = ("db", "dbeaver", "database", "tools", "soft", "software",
                   "apps", "app", "program", "programs", "dev", "sql")

# 浅层扫描的层数上限：便携版通常就躺在盘根 / 一两级子目录里，再深就慢得没意义
_SCAN_DEPTH = 3


def _platform_candidates():
    """按当前平台列出 DBeaver 可执行文件的常见位置（标准安装）。"""
    if sys.platform.startswith("win"):
        roots = []
        for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA", "APPDATA"):
            p = os.environ.get(env)
            if p:
                roots.append(p)
        names = ("dbeaver.exe", "dbeaver-ce.exe")
        subs = ("DBeaver", "DBeaver CE", "DBeaver\\dbeaver", "Programs\\DBeaver")
        out = []
        for r in roots:
            for s in subs:
                for n in names:
                    out.append(os.path.join(r, s, n))
        return out

    if sys.platform == "darwin":
        return [
            "/Applications/DBeaver.app/Contents/MacOS/dbeaver",
            "/Applications/DBeaver CE.app/Contents/MacOS/dbeaver",
            "/Applications/DBeaver EE.app/Contents/MacOS/dbeaver",
            os.path.expanduser("~/Applications/DBeaver.app/Contents/MacOS/dbeaver"),
        ]

    return [
        "/usr/bin/dbeaver",
        "/usr/bin/dbeaver-ce",
        "/opt/dbeaver/dbeaver",
        "/opt/dbeaver/dbeaver-ce",
        "/usr/local/bin/dbeaver",
        os.path.expanduser("~/dbeaver/dbeaver"),
        os.path.expanduser("~/dbeaver-ce/dbeaver"),
    ]


def _is_dbeaver_dir(path):
    """这个目录像不像 DBeaver 的安装目录（有 dbeaver.ini + 启动器）。

    `dbeaver.ini` 是判据：便携版解压出来必然带它，而随便一个叫 db 的目录不会。
    """
    try:
        if not os.path.isdir(path):
            return False
        if not os.path.isfile(os.path.join(path, "dbeaver.ini")):
            return False
        for n in ("dbeaver.exe", "dbeaver-ce.exe", "dbeaver", "dbeaver-ce"):
            if os.path.isfile(os.path.join(path, n)):
                return True
    except OSError:
        pass
    return False


def _scan_portable():
    """浅层扫描各盘根，找**便携版** DBeaver（标准安装扫不到的那种）。

    只往下看 `_SCAN_DEPTH` 层、且只进名字像的目录 —— 便携版的落点没有规律
    （实测用户这台机器就装在 `D:\\DB\\`），只能扫，但必须扫得便宜。
    """
    if os.environ.get("PD_DBEAVER_SCAN", "1") == "0":
        return ""

    if sys.platform.startswith("win"):
        drives = []
        for letter in "CDEFGH":
            root = "%s:\\" % letter
            if os.path.isdir(root):
                drives.append(root)
    else:
        drives = ["/", os.path.expanduser("~")]

    skip = ("windows", "$recycle", "system volume information", "$windows.~bt",
            "node_modules", ".git", "proc", "sys")

    for base in drives:
        try:
            level1 = []
            with os.scandir(base) as it:
                for e in it:
                    try:
                        if e.is_dir() and not e.name.lower().startswith(".") \
                                and e.name.lower() not in skip:
                            level1.append(e.path)
                    except OSError:
                        continue

            # 先看盘根下一层（`D:\DB` 这种）
            cands = list(level1)
            # 再看盘根下两层（`D:\软件\dbeaver` 这种）
            for p in level1:
                if os.path.basename(p).lower() in _PORTABLE_HINTS:
                    try:
                        with os.scandir(p) as it:
                            for e in it:
                                try:
                                    if e.is_dir() and e.name.lower() not in skip:
                                        cands.append(e.path)
                                except OSError:
                                    continue
                    except OSError:
                        continue

            for p in cands:
                if _is_dbeaver_dir(p):
                    for n in ("dbeaver.exe", "dbeaver-ce.exe", "dbeaver", "dbeaver-ce"):
                        f = os.path.join(p, n)
                        if os.path.isfile(f):
                            return f
        except OSError:
            continue
    return ""


def find_exe():
    """找 DBeaver 可执行文件。

    顺序：配置项 → 环境变量 → **已注册的标准安装** → **便携版浅层扫描** → 注册表。
    返回 (路径, "") 或 ("", 缺失原因)。

    ⚠️ 为什么要扫盘：便携版 / 绿色版**不在注册表里、也不在 Program Files**，
    用户装哪儿全凭自己（实测就装在 `D:\\DB\\`）。只扫标准位置会一直报「未检测到」。
    """
    for cand in (getattr(settings, "DBEAVER_PATH", "") or "",
                 os.environ.get("PROMPTDECK_DBEAVER", "")):
        cand = cand.strip().strip('"')
        if not cand:
            continue
        if os.path.isfile(cand):
            return cand, ""
        # 配置的是目录时，往下找几个常见文件名
        if os.path.isdir(cand):
            for n in ("dbeaver.exe", "dbeaver-ce.exe", "dbeaver", "dbeaver-ce"):
                p = os.path.join(cand, n)
                if os.path.isfile(p):
                    return p, ""
        return "", "配置的 DBeaver 路径不存在：%s" % cand

    for p in _platform_candidates():
        if os.path.isfile(p):
            return p, ""

    # 便携版：不在注册表、不在 Program Files，只能浅层扫盘
    found = _scan_portable()
    if found:
        return found, ""

    # 最后查一次注册表（标准安装一般这里就能命中）
    reg = _find_by_registry()
    if reg:
        return reg, ""

    return "", ("未检测到 DBeaver 可执行文件。若是绿色 / 便携版，请在 config/settings.py 里配置 "
                "DBEAVER_PATH（或环境变量 PROMPTDECK_DBEAVER）指向 dbeaver.exe 所在目录。")


def _find_by_registry():
    """Windows：查卸载注册表拿InstallLocation（标准安装的权威来源）。"""
    if not sys.platform.startswith("win"):
        return ""
    try:
        import winreg
    except ImportError:
        return ""
    paths = (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
             r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall")
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for sub in paths:
            try:
                with winreg.OpenKey(root, sub) as k:
                    i = 0
                    while True:
                        try:
                            name = winreg.EnumKey(k, i)
                        except OSError:
                            break
                        i += 1
                        try:
                            with winreg.OpenKey(k, name) as sk:
                                dn = winreg.QueryValueEx(sk, "DisplayName")[0]
                                if "dbeaver" not in str(dn).lower():
                                    continue
                                loc = winreg.QueryValueEx(sk, "InstallLocation")[0]
                        except OSError:
                            continue
                        if loc and os.path.isdir(loc):
                            for n in ("dbeaver.exe", "dbeaver-ce.exe"):
                                f = os.path.join(loc, n)
                                if os.path.isfile(f):
                                    return f
            except OSError:
                continue
    return ""


# ---------------------------------------------------------------- 参数拼装
def _clean(v):
    """去掉会破坏 -con 管道分隔符的字符（'|' 与换行）。"""
    return re.sub(r"[|\r\n]+", " ", str(v or "")).strip()


def build_con(meta):
    """把连接元数据拼成 DBeaver 的 -con 参数串。

    返回 (con 串, "") 或 ("", 原因)。**不带 password**。
    """
    kind = (meta.get("kind") or "").strip()
    if not kind:
        return "", "没有数据库类型"
    driver = DRIVER_ID.get(kind)
    if not driver:
        return "", "暂不支持唤起 DBeaver 的数据库类型：%s" % kind

    name = _clean(meta.get("name")) or ("PromptDeck-" + driver)
    parts = ["driver=" + driver, "name=" + name]

    if kind == "SQLite":
        # SQLite 只有「文件绝对路径」这一项；路径有效性在调用方已查过，这里再兜一层
        path = _clean(meta.get("file"))
        if not path:
            return "", "未检测到 SQLite 数据库文件路径"
        if not os.path.isfile(path):
            return "", "SQLite 文件不存在或路径无效：%s" % path
        parts.append("database=" + path)
    else:
        mode = (meta.get("mode") or "host").strip()
        url = _clean(meta.get("url"))
        if mode == "url" and url:
            # URL 模式：连接串已含主机 / 端口 / 库名 / 凭据，原样交给 DBeaver
            parts.append("url=" + url)
        else:
            host = _clean(meta.get("host")) or "localhost"
            parts.append("host=" + host)
            port = _clean(meta.get("port"))
            if port:
                parts.append("port=" + port)
            dbname = _clean(meta.get("dbname"))
            if not dbname:
                return "", "未检测到数据库名"
            parts.append("database=" + dbname)
        user = _clean(meta.get("user"))
        if user:
            parts.append("user=" + user)

    # 打开该库的 SQL 控制台 —— 粘贴过去的 SQL 就在这个控制台里
    parts.append("openConsole=true")
    return "|".join(parts), ""


def _safe_name(s, fallback="sql"):
    """把表名 / 库名洗成能当文件名的样子（保留中文，只去掉路径与非法字符）。"""
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", str(s or "")).strip(" ._")
    return (s or fallback)[:40]


def write_sql_file(sql, tables=None, db_label=""):
    """把 SQL 写进一个临时 .sql 文件，返回路径。

    每次都新建文件 ⇒ DBeaver 每次开一个**新编辑器标签**，不会覆盖用户已有内容。
    文件名带上目标表（如 `auth_group_pd.sql`）⇒ 标签页直接写明这条 SQL 是给哪张表的，
    不用逐个标签点开确认。
    开头加注释标明目标表 / 来源（纯 `--` 注释，不影响 SQL 执行）。

    ⚠️ DBeaver 命令行**没有「选中导航树里某张表」的参数**（`-con` 只管开连接、`-f` 只管开脚本），
    所以「定位到表」只能做到：连接绑到那个库 + 标签名/注释写明目标表。
    这点已在返回体的 note 里如实说明，不假装能做到「树里高亮那张表」。
    """
    head = []
    if tables:
        head.append("-- 目标表：" + ", ".join(str(t) for t in tables))
    if db_label:
        head.append("-- 目标库：" + db_label)
    head.append("-- 由 PromptDeck「链接数据库调试」生成")
    text = "\n".join(head) + "\n\n" + (sql or "")

    # 文件名带表名：一个表用表名，多个表用第一个 + n
    if tables:
        stem = _safe_name(tables[0])
        if len(tables) > 1:
            stem += "_等%d张表" % len(tables)
    else:
        stem = "debug"
    stem += "_pd"

    fd, path = tempfile.mkstemp(prefix=stem + "_", suffix=".sql")
    with os.fdopen(fd, "w", encoding="utf-8") as fp:
        fp.write(text)
    return path


# ---------------------------------------------------------------- 唤起
def launch(meta, sql):
    """唤起 DBeaver：打开连接 + 打开写着 SQL 的脚本。

    返回 {"ok":bool, ...}。ok=True 只表示「进程已成功启动」——
    之后的连接失败 / SQL 有语法错，由 DBeaver 自己在界面上提示。
    ⚠️ 检查顺序：先说清「缺什么」（元数据 / SQL），最后才找可执行文件。
    """
    # 顺序：先看「缺什么」（元数据 / SQL），最后才找可执行文件 ——
    # 缺项时立刻能说清缺什么，不必先花时间扫一遍安装路径。
    con, why = build_con(meta)
    if not con:
        return {"ok": False, "error": why}

    sql = (sql or "").strip()
    if not sql:
        return {"ok": False, "error": "请先生成 SQL"}

    exe, why = find_exe()
    if not exe:
        return {"ok": False, "error": why}

    db_label = "%s · %s" % (meta.get("kind") or "", meta.get("name") or "")
    sql_file = write_sql_file(sql, meta.get("tables"), db_label)

    # -f 打开脚本（新标签），-con 打开连接；DBeaver 默认会复用已运行的实例。
    # ⚠️ 复用已开着的窗口靠的是 Eclipse 单实例机制，**不要**加 -newInstance。
    args = [exe, "-con", con, "-f", sql_file]

    try:
        # Windows 用 DETACHED_PROCESS 免得弹一个黑框；其余平台走默认
        kw = {}
        if sys.platform.startswith("win"):
            kw["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | \
                                  getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, **kw)
    except OSError as exc:
        try:
            os.remove(sql_file)
        except OSError:
            pass
        return {"ok": False, "error": "启动 DBeaver 失败：%s" % exc}

    # 等一小会儿：**非 0 退出码**才是真失败（路径不对 / 缺依赖 / 工作区锁冲突）。
    # ⚠️ rc=0 后启动器立刻退出是**正常的**——Eclipse 单实例机制：它把 -con / -f
    #    转交给已在运行的 DBeaver 实例（或先起 GUI 再交接），GUI 在别的进程里驻留。
    #    实测：sqlite 库这条链路日志是
    #      Open external file(s) [[...pd_sql_xxx.sql]] → Connect with 'twe' → Connected
    #    而本进程 3 秒内 rc=0 退出。所以「看不到进程」不等于「没唤起」。
    try:
        proc.wait(timeout=LAUNCH_WAIT)
        if proc.returncode:
            try:
                os.remove(sql_file)
            except OSError:
                pass
            return {"ok": False,
                    "error": "DBeaver 启动后异常退出（返回码 %s）。请检查安装是否完整、"
                             "工作区是否被其他实例占用。" % proc.returncode,
                    "exe": exe}
    except subprocess.TimeoutExpired:
        pass                      # 还在跑 = 自己就是这个 GUI 实例，正常

    return {
        "ok": True,
        "exe": exe,
        "sqlFile": sql_file,
        "target": db_label.strip(),
        "tables": meta.get("tables") or [],
        "note": "已把 SQL 送进 DBeaver 的新编辑器标签（标签名带目标表）。"
                "若该连接在 DBeaver 里还没保存凭据，DBeaver 会自己弹窗询问。"
                "DBeaver 命令行无法在导航树里高亮某张表 —— 目标表已写在标签名与脚本首行注释里。",
    }