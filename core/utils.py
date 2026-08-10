# -*- coding: utf-8 -*-
"""
工具函数（无状态，纯函数）
"""
import os
import time
import shutil
import subprocess


def is_alive(pid):
    """检查进程是否存活"""
    try:
        pid = int(pid)
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, ValueError, PermissionError):
        return False


def dir_size_mb(path):
    """目录大小（MB）"""
    if not os.path.isdir(path):
        return 0.0
    total = 0
    for root, dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return round(total / (1024 * 1024), 2)


def copy_tree(src, dst, exclude=None):
    """复制目录树（保留权限），返回文件数。exclude 是要跳过的目录名集合。"""
    if not os.path.isdir(src):
        return 0
    exclude = exclude or set()
    count = 0
    for root, dirs, files in os.walk(src):
        rel = os.path.relpath(root, src)
        dirs[:] = [d for d in dirs if d not in exclude]
        for f in files:
            s = os.path.join(root, f)
            d = os.path.join(dst, rel, f) if rel != "." else os.path.join(dst, f)
            try:
                os.makedirs(os.path.dirname(d), exist_ok=True)
                shutil.copy2(s, d)
                count += 1
            except OSError:
                pass
    return count


def run_cmd(cmd, timeout=60, cwd=None):
    """执行命令，返回 (exit_code, stdout, stderr)"""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           timeout=timeout, cwd=cwd, executable="/bin/bash")
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"timeout({timeout}s)"
    except Exception as e:
        return -1, "", str(e)


def safe_remove(path):
    """安全删除文件/目录"""
    try:
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        elif os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def elapsed_since(start_time):
    """计算从 start_time 到现在的秒数"""
    return int(time.time() - start_time)
