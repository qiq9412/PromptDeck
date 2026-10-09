# -*- coding: utf-8 -*-
"""临时：列出/清理所有占用 8502 的 runserver 进程（避免残留进程服务旧代码）。"""
import subprocess
import sys

PS = ["powershell", "-NoProfile", "-Command"]

q = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
     "Where-Object { $_.CommandLine -like '*manage.py*runserver*' } | "
     "ForEach-Object { \"$($_.ProcessId)|$($_.CommandLine)\" }")
out = subprocess.run(PS + [q], capture_output=True).stdout.decode("gbk", "replace")
lines = [l.strip() for l in out.splitlines() if l.strip()]

print("=== 当前 runserver 进程 ===")
for l in lines:
    pid, _, cmd = l.partition("|")
    print("  PID=%s  %s" % (pid, cmd[:110]))

if len(sys.argv) > 1 and sys.argv[1] == "kill":
    pids = [l.split("|")[0] for l in lines]
    for pid in pids:
        r = subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True)
        print("  kill %s -> %s" % (pid, r.returncode))
    print("已清理 %d 个" % len(pids))
else:
    print("（加 kill 参数才执行清理）")