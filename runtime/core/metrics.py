# -*- coding: utf-8 -*-
"""Linux 进程内存读取（/proc，不依赖 psutil）。

接口和 Windows 版一模一样：pid_exe / process_list / pid_alive / pids_under，
所以 console.py、admin.py 不用改。
"""
import os


def _read(path):
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8", "replace")
    except Exception:
        return ""


def _all_pids():
    out = []
    try:
        for name in os.listdir("/proc"):
            if name.isdigit():
                out.append(int(name))
    except Exception:
        pass
    return out


def pid_exe(pid):
    """返回进程 exe 全路径，拿不到返回 ""。"""
    try:
        pid = int(pid)
    except Exception:
        return ""
    if pid <= 0:
        return ""
    try:
        return os.readlink("/proc/%d/exe" % pid)
    except Exception:
        return ""


def _proc_info(pid):
    try:
        exe = os.readlink("/proc/%d/exe" % pid)
    except Exception:
        return None
    if not exe:
        return None
    status = _read("/proc/%d/status" % pid)
    ws = 0
    name = os.path.basename(exe)
    for line in status.splitlines():
        if line.startswith("VmRSS:"):
            try:
                ws = int(line.split()[1]) * 1024
            except Exception:
                ws = 0
        elif line.startswith("Name:"):
            try:
                name = line.split(":", 1)[1].strip() or name
            except Exception:
                pass
    return {"pid": pid, "exe": exe, "name": name, "ws": int(ws), "private": int(ws)}


def process_list(paths, extra_pids=()):
    """返回 exe 在给定目录下的进程 + 额外 pid（两者都算）。"""
    roots = [os.path.realpath(p).lower() for p in paths if p]
    out = []
    extra = set(int(x) for x in extra_pids if x)
    for pid in _all_pids():
        if pid == 0:
            continue
        info = _proc_info(pid)
        if not info:
            continue
        exe = os.path.realpath(info["exe"]).lower()
        if pid in extra or any(exe.startswith(r + "/") for r in roots):
            out.append(info)
    out.sort(key=lambda x: x["ws"], reverse=True)
    return out


def pid_alive(pid):
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        os.kill(pid, 0)
        # 只看 kill(0) 不够：被 SIGTERM 结束但父进程没 wait 的进程会变成僵尸（Z），
        # kill(0) 仍然成功；控制台会误以为大脑还活着，内存看门狗退出后就拉不起来。
        try:
            with open("/proc/%d/stat" % pid, "rb") as f:
                raw = f.read().decode("utf-8", "replace")
            state = raw[raw.rfind(")") + 2:raw.rfind(")") + 3]
            if state == "Z":
                return False
        except Exception:
            pass
        return True
    except Exception:
        return False


def pids_under(paths):
    roots = [os.path.realpath(p).lower() for p in paths if p]
    out = []
    for pid in _all_pids():
        if pid == 0:
            continue
        exe = pid_exe(pid)
        if not exe:
            continue
        exe = os.path.realpath(exe).lower()
        if any(exe.startswith(r + "/") for r in roots):
            out.append(pid)
    return out
