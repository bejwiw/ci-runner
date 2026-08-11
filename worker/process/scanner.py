# -*- coding: utf-8 -*-
"""
进程扫描与识别

修复旧项目 bug：排除 MCP 进程不再按 "node"+"index.js" 命令名，
改为按 cwd 在 mcp-server 目录下判断，避免误杀用户的其他 node 项目。
"""
import os
import pwd

import log

logger = log.setup_logger("proc.scan")

SELF_ENTRIES = ("app.py", "worker.py", "manager.py", "survival.py", "ghbox-new/")

SYSTEM_BLACKLIST = (
    "systemd", "/sbin/init", "init ", "journald", "udevd", "resolved", "networkd",
    "dbus-daemon", "polkitd", "systemd-logind", "rsyslogd", "cron", "chronyd",
    "haveged", "hv_kvp", "sshd", "agetty", "getty", "modemmanager", "multipathd",
    "udisks", "snapd", "containerd", "kubelet", "Runner.Listener", "Runner.Worker",
    "hosted-compute-agent", "provjobd", "networkd-dispatcher", "sd-pam",
    "systemd --user", "atd", "irqbalance", "acpid", "crond", "dbus", "polkit",
    "unattended-upgr", "apt", "dpkg", "cloud-init", "waagent", "walinuxagent",
    "tuned", "rhsm", "auditd", "rsyslog", "syslog", "kthreadd", "ksoftirqd",
    "migration", "cpuhp", "rcu_", "kworker", "runner/work/_temp", "perl",
    "systemctl", "sudo ", "sudo -", "bash -e", "watchdogd", "kswapd", "scsi_eh",
    "nvme", "hv_balloon", "kcompactd", "khugepaged", "ksmd", "oom_reaper",
    "kauditd", "khungtaskd", "kdevtmpfs", "ecryptfs", "idle_inject", "perf",
    "trace", "agent", "msft", "azure",
)


class ProcessInfo:
    def __init__(self, pid, ppid, user, cmdline, exe, cwd):
        self.pid = pid
        self.ppid = ppid
        self.user = user
        self.cmdline = cmdline
        self.exe = exe
        self.cwd = cwd
        self.name = self._gen_name()

    def _gen_name(self):
        base = ""
        if self.cwd and self.cwd != "/":
            base = os.path.basename(self.cwd.rstrip("/"))
        cmd0 = self.cmdline[0] if self.cmdline else "proc"
        cmd_base = os.path.basename(cmd0).split(".")[0] if cmd0 else "proc"
        if base and base not in cmd_base:
            return f"{base}-{cmd_base}"
        return cmd_base or f"proc-{self.pid}"

    def cmdline_str(self):
        return " ".join(self.cmdline)


def _read_proc(pid):
    try:
        pid = int(pid)
    except Exception:
        return None
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read().replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
        cmdline = raw.split()
    except Exception:
        return None
    if not cmdline:
        return None
    ppid = 0
    try:
        with open(f"/proc/{pid}/stat") as f:
            parts = f.read().split()
            ppid = int(parts[3]) if len(parts) > 3 else 0
    except Exception:
        pass
    user = ""
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("Uid:"):
                    uid = int(line.split()[1])
                    try:
                        user = pwd.getpwuid(uid).pw_name
                    except Exception:
                        user = str(uid)
                    break
    except Exception:
        pass
    exe = ""
    try:
        exe = os.readlink(f"/proc/{pid}/exe")
    except Exception:
        pass
    cwd = ""
    try:
        cwd = os.readlink(f"/proc/{pid}/cwd")
    except Exception:
        pass
    return ProcessInfo(pid, ppid, user, cmdline, exe, cwd)


def find_self_worker_pids():
    pids = set()
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            info = _read_proc(pid)
            if not info:
                continue
            cmd = info.cmdline_str()
            if any(e in cmd for e in SELF_ENTRIES):
                pids.add(info.pid)
    except Exception:
        pass
    return pids


def is_system(info, worker_pids):
    cmd = info.cmdline_str().lower()
    if cmd.startswith("["):
        return True
    for entry in SELF_ENTRIES:
        if entry in cmd:
            return True
    for kw in SYSTEM_BLACKLIST:
        if kw.lower() in cmd:
            return True
    if "cloudflared" in cmd:
        return True
    if "attacker" in cmd:
        return True
    # 修复：MCP 排除改为 cwd 判断（不按 "node"+"index.js" 命令名）
    if info.cwd and "mcp-server" in info.cwd:
        return True
    return False


def scan_user_processes():
    """扫描用户进程（仅保留 cwd 在 ~/files 下的）"""
    import config as _config
    worker_pids = find_self_worker_pids()
    files_dir = os.path.realpath(os.path.expanduser(_config.FILES_DIR))
    user_procs = []
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            info = _read_proc(pid)
            if not info:
                continue
            if is_system(info, worker_pids):
                continue
            if not info.cwd:
                continue
            cwd_real = os.path.realpath(info.cwd)
            if not (cwd_real == files_dir or cwd_real.startswith(files_dir + os.sep)):
                continue
            user_procs.append(info)
    except Exception as e:
        logger.error(f"扫描失败: {e}")
    return user_procs
