# -*- coding: utf-8 -*-
"""
工具函数（无状态，纯函数）+ 通用 HTTP 请求（带重试）

所有跨模块复用的无状态函数放这里。
http_request 是统一的带重试 HTTP 请求封装，替代散落各处的裸 urllib.urlopen。
"""
import os
import time
import shutil
import subprocess
import urllib.request
import urllib.error

import log

logger = log.setup_logger("utils")

# ==================== 进程/文件/目录 ====================

def is_alive(pid):
    """检查进程是否存活"""
    try:
        pid = int(pid)
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, ValueError, PermissionError):
        return False
    except Exception as e:
        logger.debug(f"is_alive({pid}) 异常: {e}")
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
            except OSError as e:
                logger.debug(f"获取文件大小失败 {f}: {e}")
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
            except OSError as e:
                logger.warning(f"复制失败 {s} -> {d}: {e}")
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
    except OSError as e:
        logger.debug(f"删除 {path} 失败: {e}")


def elapsed_since(start_time):
    """计算从 start_time 到现在的秒数"""
    return int(time.time() - start_time)


# ==================== 通用 HTTP 请求（带重试）====================

DEFAULT_RETRY_DELAYS = (1, 3, 5)


def http_request(url, method="GET", data=None, headers=None,
                 timeout=30, retries=3, retry_delays=None,
                 json_body=False, raw_response=True):
    """带重试的 HTTP 请求。

    Returns:
        (status_code, body) — body 是 bytes 或 None
        status_code=0 表示网络完全失败
    """
    retry_delays = retry_delays or DEFAULT_RETRY_DELAYS
    headers = dict(headers) if headers else {}
    if "User-Agent" not in headers:
        headers["User-Agent"] = "Mozilla/5.0 (ghbox)"

    body = None
    if data is not None:
        if isinstance(data, (bytes, bytearray)):
            body = bytes(data)
        elif isinstance(data, str):
            body = data.encode("utf-8")
        else:
            import json as _json
            body = _json.dumps(data, ensure_ascii=False).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
    if json_body:
        headers.setdefault("Content-Type", "application/json")

    last_error = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=body, method=method, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                status = r.status
                if raw_response:
                    return status, raw
                return status, _parse_body(raw)
        except urllib.error.HTTPError as e:
            raw = b""
            try:
                raw = e.read()
            except Exception as ex:
                logger.debug(f"读取 HTTPError body 失败: {ex}")
            if e.code >= 500 and attempt < retries - 1:
                delay = retry_delays[min(attempt, len(retry_delays) - 1)]
                logger.debug(f"{method} {url} -> {e.code}, {delay}s 重试({attempt+1}/{retries})")
                time.sleep(delay)
                continue
            if raw_response:
                return e.code, raw
            return e.code, _parse_body(raw)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last_error = e
            if attempt < retries - 1:
                delay = retry_delays[min(attempt, len(retry_delays) - 1)]
                logger.debug(f"{method} {url} 网络错误: {e}, {delay}s 重试({attempt+1}/{retries})")
                time.sleep(delay)
            else:
                logger.warning(f"{method} {url} 网络失败({retries}次): {e}")
        except Exception as e:
            last_error = e
            if attempt < retries - 1:
                delay = retry_delays[min(attempt, len(retry_delays) - 1)]
                logger.debug(f"{method} {url} 异常: {e}, {delay}s 重试({attempt+1}/{retries})")
                time.sleep(delay)
            else:
                logger.error(f"{method} {url} 异常({retries}次): {e}")
    return 0, None


def _parse_body(raw):
    """尝试解析响应体为 JSON，失败返回原始字符串"""
    if not raw:
        return None
    try:
        import json as _json
        return _json.loads(raw.decode("utf-8"))
    except Exception:
        try:
            return raw.decode("utf-8", errors="replace")
        except Exception:
            return None
