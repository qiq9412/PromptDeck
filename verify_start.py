# -*- coding: utf-8 -*-
"""验证产物 start.sh 的「容器入口语义」—— 尤其是「重新部署能不能把新数据带上去」。

为什么要有这个脚本：
    /mnt/workspace 是持久化目录，库一旦建好就跨重启保留，**换包不会自动覆盖它**。
    所以 start.sh 里加了一段「种子版本比对」：包里 data/seed.version 与库旁的
    .promptdeck.seed.version 不一致时，用新种子重建库（旧库先备份成 .bak）。
    这个机制直接决定「重新上传后线上数据到底换不换」，必须真跑一遍才算验证过。

跑的是**产物目录里那份真的 start.sh**，不复制、不改写；只通过环境变量把路径指到临时目录：
    PROMPTDECK_DB            库落点（模拟 /mnt/workspace/prompt_test_DB.db）
    PROMPTDECK_SEED          种子库路径（模拟 /app/data/seed.sqlite）
    PROMPTDECK_SEED_VER_FILE 种子版本文件（模拟 /app/data/seed.version）
    PORT                     起服务用的端口（默认 7860 会撞本地预览，这里换空闲端口）

三个场景：
    A 首启     ：没有库                 -> 用种子播种，标记写成 seed.version
    B 换版     ：库是旧数据 + 旧标记     -> 用新种子替换，旧库留 .bak
    C 幂等     ：库已是当前版本 + 手工加过一行 -> 不刷新，手工数据保住
    D 关闭刷新 ：同上但 PROMPTDECK_SEED_REFRESH=off -> 不刷新

用法：python verify_start.py         （退出码 0 = 全过）
    跑之前确保没别的进程占着测试端口；本机 Git Bash 的 sh 需要能找到。
"""
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PKG = os.path.join(ROOT, "promptdeck_modelscope")
# 临时目录放系统 temp：4 个场景会拷 4 份产物（上百个文件），放工程里既脏又容易撞
# 「批量删除」安全策略（沙箱对单轮 >50 个路径的删除直接拒）
TMP = os.path.join(tempfile.gettempdir(), "promptdeck_verify_start")
REPORT = os.path.join(HERE, "_start_check.txt")

SEED_SRC = os.path.join(PKG, "data", "seed.sqlite")
VER_SRC = os.path.join(PKG, "data", "seed.version")
OLD_DB = os.path.join(HERE, "data", "prompt_test_DB_twe.db")  # 开发库（换库后全项目唯一口径）

# 找 sh / coreutils 的候选 Git 安装根目录（本机实测：D:\Git\Git 与 WorkBuddy 自带的 PortableGit）
SH_ROOTS = [
    r"D:\Git\Git",
    r"C:\Program Files\Git",
    r"C:\Program Files (x86)\Git",
    r"C:\Users\Lenovo\.workbuddy\binaries\PortableGit\versions\1.2.0",
]

SH = None        # 运行期由 find_sh() 决定
TOOL_DIRS = []

_LINES = []
_N = [0, 0]


def w(line=""):
    print(line)
    _LINES.append(line)


def chk(label, ok, detail=None):
    if ok:
        _N[0] += 1
        w("  [PASS] %s" % label)
    else:
        _N[1] += 1
        w("  [FAIL] %s%s" % (label, ("  <- " + str(detail)) if detail is not None else ""))


def banner(text):
    w("=" * 74)
    w(text)


def read(p):
    with open(p, encoding="utf-8", errors="replace") as f:
        return f.read()


def find_sh():
    """返回 (sh 可执行文件, 需要一并加进子进程 PATH 的工具目录)。

    Windows 上没有独立的 sh，用 Git for Windows 自带的；`mkdir / cp / rm / dirname`
    这些 coreutils 在同一个 root 的 usr\\bin 下，**必须一起加进 PATH**，
    否则 start.sh 里的 `mkdir -p` / `cp -f` 会 command not found。
    """
    hits = []
    for cand in ("sh", "bash"):
        p = shutil.which(cand)
        if p:
            hits.append(p)
    for root in SH_ROOTS:
        for rel in (("usr", "bin", "sh.exe"), ("bin", "bash.exe"), ("usr", "bin", "bash.exe")):
            p = os.path.join(root, *rel)
            if os.path.isfile(p):
                hits.append(p)
    for p in hits:
        if os.path.isfile(p):
            return p, tool_dirs(p)
    return None, []


def tool_dirs(sh_path):
    d = os.path.dirname(sh_path)
    root = os.path.dirname(d)
    if os.path.basename(d).lower() == "usr":
        root = os.path.dirname(root)
    out = [d]
    for rel in (("usr", "bin"), ("bin",)):
        p = os.path.join(root, *rel)
        if os.path.isdir(p) and p not in out:
            out.append(p)
    return out


def srv_names(db):
    c = sqlite3.connect(db)
    try:
        return [r[0] for r in c.execute("select plat_name from srv order by id")]
    finally:
        c.close()


def make_old_snapshot():
    """造一份「线上旧数据」快照，返回 (路径, 服务器条数)。

    为什么不直接把开发库当 db_from：换库后**种子就是从开发库导出的**，两者同名同量，
    B / D 两个场景里「库已被新种子替换」「备份里是旧数据」这两条断言就退化成恒真、
    什么也测不出来。所以刻意从开发库派生一份**与种子不同**的旧数据：
    删掉最后一条服务器（连带它的模型，PRAGMA foreign_keys=ON 手动级联）。
    """
    os.makedirs(TMP, exist_ok=True)
    dst = os.path.join(TMP, "old_snapshot.db")
    if os.path.exists(dst):
        os.remove(dst)
    shutil.copyfile(OLD_DB, dst)
    c = sqlite3.connect(dst)
    try:
        last = c.execute("select max(id) from srv").fetchone()[0]
        c.execute("delete from srv where id = ?", (last,))
        c.execute("delete from mdl where srv_id = ?", (last,))
        c.commit()
        n = c.execute("select count(*) from srv").fetchone()[0]
    finally:
        c.close()
    return dst, n


def health(port, timeout=25.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/api/health" % port, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    return False


def kill_port(port):
    """兜底清理：万一 exec 没把进程接管干净，按端口把监听者杀掉。"""
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True,
                             encoding="utf-8", errors="replace", timeout=20).stdout or ""
    except Exception:  # noqa: BLE001
        return
    for line in out.splitlines():
        if "LISTENING" in line and (":%d " % port) in line:
            pid = line.split()[-1]
            subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True,
                           timeout=20)


def copy_pkg(dst):
    """把产物整份拷到临时目录（拷贝后由调用方校验关键文件逐字节一致）。"""
    os.makedirs(dst, exist_ok=True)
    for name in os.listdir(PKG):
        src = os.path.join(PKG, name)
        tgt = os.path.join(dst, name)
        if os.path.isdir(src):
            shutil.copytree(src, tgt)
        else:
            shutil.copy2(src, tgt)


def run_case(name, port, *, seed_ver=None, db_from=None, marker=None,
             add_row=False, refresh=None):
    """跑一次真 start.sh，返回 (日志, 运行库绝对路径, 标记绝对路径, 服务是否起来)。

    做法：把产物整份拷到临时目录当「/app」，临时目录下的 ws/ 当「/mnt/workspace」，
    再用**相对路径**通过环境变量指过去 —— 不出现盘符，避免 MSYS 对含 `:` 的值做路径转换。
    拷贝前会断言 start.sh / server.py / db.py 与产物逐字节相同，确保测的是真脚本。
    """
    work = os.path.join(TMP, name)
    if os.path.isdir(work):
        shutil.rmtree(work, ignore_errors=True)
    pkg = os.path.join(work, "pkg")            # 扮演容器里的 /app
    ws = os.path.join(work, "ws")              # 扮演容器里的 /mnt/workspace
    copy_pkg(pkg)
    os.makedirs(ws)

    for fn in ("start.sh", "server.py", "db.py"):
        if read(os.path.join(pkg, fn)) != read(os.path.join(PKG, fn)):
            raise RuntimeError("%s 拷贝后与产物不一致，测试无效" % fn)

    ver = seed_ver if seed_ver is not None else read(VER_SRC).strip()
    with open(os.path.join(pkg, "data", "seed.version"), "w",
              encoding="utf-8", newline="\n") as f:
        f.write(ver + "\n")

    db = os.path.join(ws, "prompt_test_DB.db")
    mark = os.path.join(ws, ".promptdeck.seed.version")
    if db_from:
        shutil.copyfile(db_from, db)
    if marker is not None:
        with open(mark, "w", encoding="utf-8", newline="\n") as f:
            f.write(marker)
    if add_row:
        c = sqlite3.connect(db)
        c.execute("insert into srv (plat_name, base_url, comp_name, home_url) "
                  "values ('CASE_C_手工数据', 'https://example.com/v1', '手工', 'https://example.com')")
        c.commit()
        c.close()

    env = dict(os.environ)
    env["PATH"] = os.pathsep.join(TOOL_DIRS + [os.path.dirname(sys.executable),
                                               env.get("PATH", "")])
    # 相对路径是相对「子进程 cwd = 产物副本」解析的，所以库要用 ../ws 指到副本外面
    env["PROMPTDECK_DB"] = "../ws/prompt_test_DB.db"
    env["PROMPTDECK_SEED"] = "data/seed.sqlite"
    env["PROMPTDECK_SEED_VER_FILE"] = "data/seed.version"
    env["PORT"] = str(port)
    env["PYTHONUNBUFFERED"] = "1"
    if refresh is not None:
        env["PROMPTDECK_SEED_REFRESH"] = refresh

    log_path = os.path.join(work, "start.log")
    with open(log_path, "wb") as logf:
        proc = subprocess.Popen([SH, "start.sh"], cwd=pkg, env=env,
                                stdout=logf, stderr=subprocess.STDOUT)
        try:
            up = health(port)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except Exception:  # noqa: BLE001
                proc.kill()
            time.sleep(0.5)
            kill_port(port)

    return read(log_path), db, mark, up


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    if not os.path.isdir(PKG):
        w("!! 找不到产物目录：%s（先跑 python pack_modelscope.py）" % PKG)
        return 1
    sh = find_sh()
    if not sh[0]:
        w("!! 找不到 sh / bash，无法跑容器入口语义（Windows 上装 Git for Windows 即可）")
        return 1
    global SH, TOOL_DIRS
    SH, TOOL_DIRS = sh
    os.makedirs(TMP, exist_ok=True)

    seed_ver = read(VER_SRC).strip()
    seed_names = srv_names(SEED_SRC)
    old_snap, n_old = make_old_snapshot()          # 派生一份与种子不同的「旧数据」
    old_names = srv_names(old_snap)

    banner("[0] 前置")
    w("  sh            = %s" % SH)
    w("  PATH 附加     = %s" % os.pathsep.join(TOOL_DIRS))
    w("  产物 start.sh = %s" % os.path.join(PKG, "start.sh"))
    w("  种子版本      = %s" % seed_ver)
    w("  种子内容      = %d 服务器 %s" % (len(seed_names), seed_names))
    w("  旧数据快照    = %d 服务器（开发库派生，删掉 1 条）" % n_old)
    chk("种子库与旧库内容不同（否则这次刷新测不出来）", seed_names != old_names)
    chk("种子版本文件非空", seed_ver != "")

    # ---------------- A 首启 ----------------
    banner("[A] 首启：没有库 -> 用种子播种")
    log, db, mark, up = run_case("A_first_boot", 7881)
    chk("服务按容器入口起得来（PORT 覆盖生效）", up, log[-400:])
    chk("日志打印库落点与种子版本",
        "PromptDeck: DB=" in log and seed_ver in log, log[-300:])
    chk("库被种子播种（%d 行）" % len(seed_names), srv_names(db) == seed_names, srv_names(db))
    chk("库旁写入种子版本标记", os.path.isfile(mark) and read(mark).strip() == seed_ver)
    chk("没产生多余备份", not os.path.isfile(db + ".bak"))

    # ---------------- B 数据换版 -> 刷新 ----------------
    banner("[B] 换版：库是旧数据、标记是旧版本 -> 用新种子替换 + 备份旧库")
    log, db, mark, up = run_case("B_seed_changed", 7882, db_from=old_snap, marker="oldver000000")
    chk("服务起得来", up, log[-400:])
    chk("日志说明发生了刷新",
        "种子版本变化" in log, log[-400:])
    chk("库已被新种子替换（%d 行）" % len(seed_names), srv_names(db) == seed_names, srv_names(db))
    chk("旧库备份为 .bak 且内容是旧数据",
        os.path.isfile(db + ".bak") and srv_names(db + ".bak") == old_names)
    chk("标记更新为新版本", read(mark).strip() == seed_ver)

    # ---------------- C 同版本 -> 不刷新（幂等） ----------------
    banner("[C] 幂等：库已是当前版本、且手工加过一行 -> 不刷新")
    log, db, mark, up = run_case("C_same_ver", 7883, db_from=SEED_SRC, marker=seed_ver,
                                 add_row=True)
    chk("服务起得来", up, log[-400:])
    chk("没有触发刷新", "种子版本变化" not in log, log[-300:])
    names = srv_names(db)
    chk("手工加的那行还在（线上数据没被冲掉）", "CASE_C_手工数据" in names, names)
    chk("条数 = 种子 + 1", len(names) == len(seed_names) + 1, len(names))

    # ---------------- D 显式关闭刷新 ----------------
    banner("[D] PROMPTDECK_SEED_REFRESH=off：即使种子换了也不动线上库")
    log, db, mark, up = run_case("D_refresh_off", 7884, db_from=old_snap, marker="oldver000000",
                                 refresh="off")
    chk("服务起得来", up, log[-400:])
    chk("没有触发刷新", "种子版本变化" not in log, log[-300:])
    chk("库里仍是旧数据（%d 行）" % len(old_names), srv_names(db) == old_names,
        len(srv_names(db)))
    chk("标记未被改写", read(mark).strip() == "oldver000000", read(mark).strip())

    banner("汇总")
    w("  PASS=%d  FAIL=%d" % (_N[0], _N[1]))
    w("  结果：" + ("全部通过" if _N[1] == 0 else "有失败项"))

    with open(REPORT, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(_LINES) + "\n")
    w("  报告：%s" % REPORT)

    try:
        shutil.rmtree(TMP, ignore_errors=True)
    except Exception as exc:  # noqa: BLE001
        w("  （临时目录未清干净，可手动删：%s：%s）" % (TMP, exc))
    return 0 if _N[1] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
