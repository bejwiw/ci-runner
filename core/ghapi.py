# -*- coding: utf-8 -*-
"""
GitHub API 封装（requests 连接池 + 重试 + rate limit 缓存）
"""
import time
import threading
import requests

import config
import log

logger = log.setup_logger("ghapi")

API_BASE = "https://api.github.com"
UPLOAD_BASE = "https://uploads.github.com"

# ==================== 连接池/重试常量 ====================
HTTP_POOL_CONNECTIONS = 10
HTTP_POOL_MAXSIZE = 20
HTTP_MAX_RETRIES = 2
HTTP_TIMEOUT_CONNECT = 10
HTTP_RETRY_BACKOFF = 2
RATE_LIMIT_CACHE_TTL = 60
GH_API_PER_PAGE = 100

_session = None
_session_lock = threading.Lock()
_last_rate_limit = {}
_rate_lock = threading.Lock()


def _get_session():
    global _session
    with _session_lock:
        if _session is None:
            _session = requests.Session()
            _session.headers.update({
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/vnd.github.v3+json",
            })
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=HTTP_POOL_CONNECTIONS,
                pool_maxsize=HTTP_POOL_MAXSIZE,
                max_retries=HTTP_MAX_RETRIES)
            _session.mount("https://", adapter)
        return _session


def gh_request(method, url, token=None, data=None, headers=None, raw=False,
               timeout=60, retries=2):
    """通用 GitHub API 请求。返回 (status, data_or_bytes)。"""
    tok = token or config.GH_TOKEN
    sess = _get_session()
    h = {}
    if tok:
        h["Authorization"] = f"token {tok}"
    if headers:
        h.update(headers)
    body = data
    last_status, last_body = 0, None
    for attempt in range(retries + 1):
        try:
            if body is None:
                resp = sess.request(method, url, headers=h, timeout=(HTTP_TIMEOUT_CONNECT, timeout))
            elif isinstance(body, (bytes, str)):
                resp = sess.request(method, url, data=body, headers=h, timeout=(HTTP_TIMEOUT_CONNECT, timeout))
            else:
                resp = sess.request(method, url, json=body, headers=h, timeout=(HTTP_TIMEOUT_CONNECT, timeout))
            last_status = resp.status_code
            if raw:
                last_body = resp.content
                if resp.status_code in (200, 201, 202, 204):
                    return resp.status_code, resp.content
            else:
                try:
                    last_body = resp.json()
                except Exception:
                    last_body = resp.text
                if resp.status_code in (200, 201, 202, 204):
                    return resp.status_code, last_body
            if resp.status_code in (403, 429, 500, 502, 503, 504) and attempt < retries:
                delay = HTTP_RETRY_BACKOFF * (attempt + 1)
                logger.debug(f"{method} {url} -> {resp.status_code}, {delay}s 重试({attempt+1}/{retries})")
                time.sleep(delay)
                continue
            return resp.status_code, last_body
        except requests.exceptions.Timeout:
            if attempt < retries:
                delay = HTTP_RETRY_BACKOFF * (attempt + 1)
                logger.debug(f"{method} {url} 超时, {delay}s 重试({attempt+1}/{retries})")
                time.sleep(delay)
                continue
            logger.warning(f"{method} {url} 超时({retries+1}次)")
            return 0, "timeout"
        except requests.exceptions.ConnectionError as e:
            if attempt < retries:
                delay = HTTP_RETRY_BACKOFF * (attempt + 1)
                logger.debug(f"{method} {url} 连接错误: {e}, {delay}s 重试({attempt+1}/{retries})")
                time.sleep(delay)
                continue
            logger.warning(f"{method} {url} 连接失败({retries+1}次): {e}")
            return 0, str(e)
        except Exception as e:
            if attempt < retries:
                time.sleep(1)
                continue
            logger.error(f"{method} {url} 异常({retries+1}次): {e}")
            return 0, str(e)
    return last_status, last_body


def check_rate_limit(token=None, force=False):
    """查询 rate limit（RATE_LIMIT_CACHE_TTL 秒缓存）"""
    tok = token or config.GH_TOKEN
    now = time.time()
    with _rate_lock:
        cached = _last_rate_limit.get(tok)
        if cached and not force and (now - cached[0]) < RATE_LIMIT_CACHE_TTL:
            return cached[1], cached[2], cached[3]
    status, d = gh_request("GET", f"{API_BASE}/rate_limit", token=tok, timeout=20)
    if status != 200:
        logger.warning(f"rate_limit 查询失败: {status}")
        return 0, 0, 0
    core = d.get("resources", {}).get("core", {})
    remaining = core.get("remaining", 0)
    limit = core.get("limit", 0)
    reset = core.get("reset", 0)
    with _rate_lock:
        _last_rate_limit[tok] = (now, remaining, limit, reset)
    return remaining, limit, reset


def estimate_account_quota(account):
    """估算账号配额健康度（0~1）"""
    try:
        remaining, limit, _ = check_rate_limit(account.get("token"))
        ratio = remaining / limit if limit else 0
        running = 0
        try:
            repo = account.get("repo") or config.REPO
            url = f"{API_BASE}/repos/{repo}/actions/runs?status=in_progress&per_page={GH_API_PER_PAGE}"
            status, d = gh_request("GET", url, token=account.get("token"), timeout=30)
            if status == 200:
                running = sum(1 for r in d.get("workflow_runs", [])
                             if config.WORKER_WORKFLOW in r.get("path", ""))
        except Exception as e:
            logger.debug(f"查询运行数失败: {e}")
        max_c = account.get("max_concurrency", 20)
        health = ratio * 0.6 + (1 - running / max_c if max_c else 0) * 0.4
        return max(0.0, min(1.0, health)), {
            "rate_remaining": remaining, "rate_limit": limit,
            "running": running, "max_concurrency": max_c,
        }
    except Exception as e:
        logger.warning(f"估算配额失败: {e}")
        return 0.0, {"error": str(e)}


def check_account_suspended(token=None):
    """检测账号是否被封"""
    tok = token or config.GH_TOKEN
    status, d = gh_request("GET", f"{API_BASE}/user", token=tok, timeout=20)
    if status == 403:
        msg = d.get("message", "") if isinstance(d, dict) else str(d)
        return "suspended" in str(msg).lower()
    return False
