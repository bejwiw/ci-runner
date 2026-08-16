# -*- coding: utf-8 -*-
"""
GitHub Releases 加密存储（S3 降级备份）

特性：
- 资产上传/下载（AES-256-GCM 加密）
- 大文件自动分片（500MB/片，并发上传）
- JSON 对象加密存取
- 空数据保护：防止并发覆盖导致数据丢失
"""
import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor

import config
import log
from core import crypto, ghapi

logger = log.setup_logger("releases")

CHUNK_SIZE = 500 * 1024 * 1024
CHUNK_CONCURRENCY = 5
MAX_RETRIES = 3
RETRY_DELAYS = [1, 3, 5]

_release_cache = {}
_release_lock = threading.Lock()


def get_release(token=None, repo=None):
    tok = token or config.GH_TOKEN
    repo = repo or config.REPO
    url = f"{ghapi.API_BASE}/repos/{repo}/releases/tags/{config.BACKUP_TAG}"
    for attempt in range(MAX_RETRIES):
        status, data = ghapi.gh_request("GET", url, token=tok, timeout=30)
        if status == 200:
            return data
        logger.warning(f"get_release {repo} -> {status} (attempt={attempt+1})")
        if attempt < MAX_RETRIES - 1:
            time.sleep(RETRY_DELAYS[attempt])
    return None


def ensure_release(token=None, repo=None):
    tok = token or config.GH_TOKEN
    repo = repo or config.REPO
    cache_key = f"{tok}:{repo}"
    with _release_lock:
        if cache_key in _release_cache:
            return _release_cache[cache_key]
    rel = get_release(token=tok, repo=repo)
    if rel:
        with _release_lock:
            _release_cache[cache_key] = rel["id"]
        return rel["id"]
    url = f"{ghapi.API_BASE}/repos/{repo}/releases"
    data = {"tag_name": config.BACKUP_TAG, "name": "CI artifacts",
            "body": "automated build cache", "draft": False, "prerelease": False}
    status, d = ghapi.gh_request("POST", url, token=tok, data=data, timeout=30)
    if status in (200, 201):
        with _release_lock:
            _release_cache[cache_key] = d.get("id")
        return d.get("id")
    raise RuntimeError(f"创建 release 失败: {status} {d}")


def _find_asset(release, name):
    if not release:
        return None
    for a in release.get("assets", []):
        if a.get("name") == name:
            return a
    return None


def upload_asset(name, data_bytes, token=None, repo=None):
    name = name.replace("/", ".")  # GitHub asset name不能包含/
    """上传加密资产。返回 (size, status)。"""
    tok = token or config.GH_TOKEN
    repo = repo or config.REPO
    rel_id = ensure_release(token=tok, repo=repo)
    rel = get_release(token=tok, repo=repo)
    old = _find_asset(rel, name)
    if old:
        ghapi.gh_request("DELETE",
                         f"{ghapi.API_BASE}/repos/{repo}/releases/assets/{old['id']}",
                         token=tok, timeout=30)
    enc = crypto.encrypt_bytes(data_bytes)
    url = f"{ghapi.UPLOAD_BASE}/repos/{repo}/releases/{rel_id}/assets?name={name}"
    status, _ = ghapi.gh_request("POST", url, token=tok, data=enc,
                                 headers={"Content-Type": "application/octet-stream"},
                                 timeout=180)
    if status in (200, 201):
        logger.info(f"上传 {name} OK ({len(data_bytes)} bytes) -> {repo}")
    else:
        logger.error(f"上传 {name} 失败({status}) -> {repo}")
    return len(data_bytes), status


def download_asset(name, token=None, repo=None):
    name = name.replace("/", ".")  # 同步替换
    """下载并解密资产。返回 bytes 或 None。"""
    tok = token or config.GH_TOKEN
    repo = repo or config.REPO
    for attempt in range(MAX_RETRIES):
        rel = get_release(token=tok, repo=repo)
        a = _find_asset(rel, name)
        if not a:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAYS[attempt])
                continue
            return None
        status, blob = ghapi.gh_request(
            "GET", f"{ghapi.API_BASE}/repos/{repo}/releases/assets/{a['id']}",
            token=tok, raw=True,
            headers={"Accept": "application/octet-stream"}, timeout=120)
        if status == 200 and blob:
            try:
                return crypto.decrypt_bytes(blob)
            except Exception as e:
                logger.error(f"解密 {name} 失败: {e}")
                return None
        if attempt < MAX_RETRIES - 1:
            time.sleep(RETRY_DELAYS[attempt])
    return None


def delete_asset(name, token=None, repo=None):
    name = name.replace("/", ".")
    tok = token or config.GH_TOKEN
    repo = repo or config.REPO
    rel = get_release(token=tok, repo=repo)
    a = _find_asset(rel, name)
    if a:
        ghapi.gh_request("DELETE",
                         f"{ghapi.API_BASE}/repos/{repo}/releases/assets/{a['id']}",
                         token=tok, timeout=30)


def upload_chunked(name, data_bytes, token=None, repo=None):
    """加密分片上传。小数据走单文件，大数据自动分片并发。"""
    tok = token or config.GH_TOKEN
    repo = repo or config.REPO
    if len(data_bytes) <= CHUNK_SIZE:
        size, status = upload_asset(name, data_bytes, token=tok, repo=repo)
        return size, 1

    parts = (len(data_bytes) + CHUNK_SIZE - 1) // CHUNK_SIZE
    chunks = [(i, data_bytes[i * CHUNK_SIZE:(i + 1) * CHUNK_SIZE]) for i in range(parts)]

    def _upload(args):
        i, chunk = args
        upload_asset(f"{name}.part{i}", chunk, token=tok, repo=repo)
        return i, len(chunk)

    with ThreadPoolExecutor(max_workers=CHUNK_CONCURRENCY) as ex:
        list(ex.map(_upload, chunks))
    upload_asset(f"{name}.manifest",
                 json.dumps({"parts": parts}).encode(), token=tok, repo=repo)
    return len(data_bytes), parts


def download_chunked(name, token=None, repo=None):
    """下载并解密（支持分片合并）。返回 bytes 或 None。"""
    tok = token or config.GH_TOKEN
    repo = repo or config.REPO
    manifest_blob = download_asset(f"{name}.manifest", token=tok, repo=repo)
    if manifest_blob:
        try:
            manifest = json.loads(manifest_blob.decode())
            parts = int(manifest["parts"])
            results = [None] * parts

            def _download(i):
                return i, download_asset(f"{name}.part{i}", token=tok, repo=repo)

            with ThreadPoolExecutor(max_workers=CHUNK_CONCURRENCY) as ex:
                for i, data in ex.map(_download, range(parts)):
                    results[i] = data
            if any(d is None for d in results):
                logger.error(f"{name} 部分分片缺失")
                return None
            return b"".join(results)
        except Exception as e:
            logger.error(f"分片合并失败: {e}")
            return None
    return download_asset(name, token=tok, repo=repo)


def save_json_enc(name, obj, token=None, repo=None):
    """加密保存 JSON 对象"""
    return upload_asset(name, json.dumps(obj, ensure_ascii=False).encode(),
                        token=token, repo=repo)


def load_json_enc(name, token=None, repo=None, default=None):
    """读取 JSON 对象（download_asset已解密，直接解析JSON）"""
    blob = download_asset(name, token=token, repo=repo)
    if blob:
        try:
            return json.loads(blob.decode())
        except Exception as e:
            logger.warning(f"解析 {name} 失败: {e}")
    return default


def save_json_protected(name, obj, token=None, repo=None):
    """带空数据保护的 JSON 保存"""
    tok = token or config.GH_TOKEN
    repo = repo or config.REPO
    if not obj:
        existing = load_json_enc(name, token=tok, repo=repo, default=None)
        if existing is not None:
            logger.warning(f"拒绝空数据覆盖 {name}")
            return False
    save_json_enc(name, obj, token=tok, repo=repo)
    return True
