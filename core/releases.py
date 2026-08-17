# -*- coding: utf-8 -*-
"""
GitHub Releases 加密存储（S3 降级备份）

特性：
- 资产上传/下载（AES-256-GCM 加密）
- 大文件自动分片（动态大小 + 动态并发）
- JSON 对象加密存取
- 空数据保护：防止并发覆盖导致数据丢失

v2 优化（2026-08-17 实测驱动）：
- 单文件直传上限 1.5GB（实测 1GB 直传 43MB/s，仅 2 配额）
- 版本化命名（{base}.{ts}.enc），不删旧 → 绕开同名 422，省 delete 配额
- 下载优先 CDN 直链（实测 0 配额、无缓存陈旧问题），API 下载仅作降级
- release_id 缓存带 TTL，失效自动重建
- 上传重试 4 次指数退避；404 触发缓存重建
"""
import os
import json
import time
import calendar
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import config
import log
from core import crypto, ghapi

logger = log.setup_logger("releases")

# ==================== 常量（v2）====================
SINGLE_UPLOAD_LIMIT = 1500 * 1024 * 1024  # 单文件直传上限 1.5GB（留0.5GB余量防2GB硬限）
CHUNK_SIZE = 500 * 1024 * 1024            # 分片基准大小
CHUNK_CONCURRENCY = 5                      # 默认并发（v1 兼容）
MAX_RETRIES = 3                            # v1 兼容重试
RETRY_DELAYS = [1, 3, 5]                   # v1 兼容退避
V2_RETRY_DELAYS = [1, 5, 15, 45]           # v2 上传重试退避
V2_MAX_RETRIES = 4
VERSION_KEEP = 2                           # 保留最近 N 个版本
RELEASE_CACHE_TTL = 300                    # release_id 缓存秒数
ASSET_LIST_CACHE_TTL = 60                  # asset 列表短缓存（列表场景）
UPLOAD_TIMEOUT_BIG = 1800                  # 大文件上传超时（秒）
DOWNLOAD_TIMEOUT = 600

# 动态并发：片数 → 并发
def _concurrency_for_parts(parts):
    if parts <= 1:
        return 1
    if parts <= 2:
        return 2
    if parts <= 5:
        return 5
    return 8  # 实测并发8~10 收益递增但配额线性，8 是甜点

# 分片大小策略：优先少片（每片 2 配额）
def _chunk_size_for(total):
    """返回合适的分片大小：目标 2~6 片，最小 250MB"""
    if total <= SINGLE_UPLOAD_LIMIT:
        return 0  # 不分片，直传
    # >1.5GB：目标片数 2~6，片大小取最大(250MB, ceil(total/6))，向上取整到 250MB 倍数
    import math
    per = max(250 * 1024 * 1024, math.ceil(total / 6))
    per = int(math.ceil(per / (250 * 1024 * 1024)) * 250 * 1024 * 1024)
    return min(per, SINGLE_UPLOAD_LIMIT)

# ==================== 缓存 ====================
_release_cache = {}          # (tok, repo) -> (release_id, fetched_at)
_release_lock = threading.Lock()
_asset_cache = {}            # (tok, repo) -> (assets_json, fetched_at)
_asset_lock = threading.Lock()


def get_release(token=None, repo=None):
    """获取 release 对象（无缓存，v1 兼容）"""
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
    """确保 release 存在并返回 id（带 TTL 缓存）。"""
    tok = token or config.GH_TOKEN
    repo = repo or config.REPO
    cache_key = f"{tok}:{repo}"
    now = time.time()
    with _release_lock:
        cached = _release_cache.get(cache_key)
        if cached and now - cached[1] < RELEASE_CACHE_TTL:
            return cached[0]
    rel = get_release(token=tok, repo=repo)
    if rel:
        with _release_lock:
            _release_cache[cache_key] = (rel["id"], time.time())
        return rel["id"]
    url = f"{ghapi.API_BASE}/repos/{repo}/releases"
    # 创建时必须 publish（draft=false），否则 CDN 下载 404（实测）
    data = {"tag_name": config.BACKUP_TAG, "name": "CI artifacts",
            "body": "automated build cache", "draft": False, "prerelease": False}
    status, d = ghapi.gh_request("POST", url, token=tok, data=data, timeout=30)
    if status in (200, 201):
        with _release_lock:
            _release_cache[cache_key] = (d.get("id"), time.time())
        return d.get("id")
    raise RuntimeError(f"创建 release 失败: {status} {d}")


def _invalidate_release(token, repo):
    cache_key = f"{token or config.GH_TOKEN}:{repo or config.REPO}"
    with _release_lock:
        _release_cache.pop(cache_key, None)
    with _asset_lock:
        _asset_cache.pop(cache_key, None)


def list_assets(token=None, repo=None, ttl=ASSET_LIST_CACHE_TTL):
    """列出 release 的全部 assets（短 TTL 缓存）。返回 [asset, ...] 或 []。"""
    tok = token or config.GH_TOKEN
    repo = repo or config.REPO
    cache_key = f"{tok}:{repo}"
    now = time.time()
    with _asset_lock:
        cached = _asset_cache.get(cache_key)
        if cached and now - cached[1] < ttl:
            return cached[0]
    rel_id = ensure_release(token=tok, repo=repo)
    assets = []
    page = 1
    while True:
        url = (f"{ghapi.API_BASE}/repos/{repo}/releases/{rel_id}/assets"
               f"?per_page=100&page={page}")
        status, d = ghapi.gh_request("GET", url, token=tok, timeout=30)
        if status != 200 or not isinstance(d, list):
            break
        assets.extend(d)
        if len(d) < 100:
            break
        page += 1
    with _asset_lock:
        _asset_cache[cache_key] = (assets, time.time())
    return assets


def _find_asset(release, name):
    if not release:
        return None
    for a in release.get("assets", []):
        if a.get("name") == name:
            return a
    return None


# ==================== v1 兼容接口（固定名删旧重传，manager 元数据用）====================
def upload_asset(name, data_bytes, token=None, repo=None):
    name = name.replace("/", ".")
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
    name = name.replace("/", ".")
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
    """v1 分片上传（500MB/片，并发5）。返回 (size, ok_assets)。"""
    tok = token or config.GH_TOKEN
    repo = repo or config.REPO

    def _ok(status):
        return status in (200, 201)

    if len(data_bytes) <= CHUNK_SIZE:
        size, status = upload_asset(name, data_bytes, token=tok, repo=repo)
        return size, 1 if _ok(status) else 0

    parts = (len(data_bytes) + CHUNK_SIZE - 1) // CHUNK_SIZE
    chunks = [(i, data_bytes[i * CHUNK_SIZE:(i + 1) * CHUNK_SIZE]) for i in range(parts)]

    def _upload(args):
        i, chunk = args
        _, status = upload_asset(f"{name}.part{i}", chunk, token=tok, repo=repo)
        return 1 if _ok(status) else 0

    with ThreadPoolExecutor(max_workers=CHUNK_CONCURRENCY) as ex:
        ok_chunks = list(ex.map(_upload, chunks))
    _, m_status = upload_asset(f"{name}.manifest",
                 json.dumps({"parts": parts}).encode(), token=tok, repo=repo)
    ok_assets = sum(ok_chunks) + (1 if _ok(m_status) else 0)
    return len(data_bytes), ok_assets


def download_chunked(name, token=None, repo=None):
    """v1 分片下载。返回 bytes 或 None。"""
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
    return upload_asset(name, json.dumps(obj, ensure_ascii=False).encode(),
                        token=token, repo=repo)


def load_json_enc(name, token=None, repo=None, default=None):
    blob = download_asset(name, token=token, repo=repo)
    if blob:
        try:
            return json.loads(blob.decode())
        except Exception as e:
            logger.warning(f"解析 {name} 失败: {e}")
    return default


def save_json_protected(name, obj, token=None, repo=None):
    tok = token or config.GH_TOKEN
    repo = repo or config.REPO
    if not obj:
        existing = load_json_enc(name, token=tok, repo=repo, default=None)
        if existing is not None:
            logger.warning(f"拒绝空数据覆盖 {name}")
            return False
    save_json_enc(name, obj, token=tok, repo=repo)
    return True


# ==================== v2：版本化上传（备份用）====================
def asset_name_v2(base, ts=None):
    """版本化资产名：{base}.{ts}.enc（ts 为 epoch 秒）"""
    base = base.replace("/", ".")
    if ts is None:
        ts = int(time.time())
    return f"{base}.{ts}.enc"


def _upload_once(rel_id, name, enc_bytes, token, repo, timeout=UPLOAD_TIMEOUT_BIG):
    """单次上传（含 404 重建 release 重试）。返回 status。"""
    url = f"{ghapi.UPLOAD_BASE}/repos/{repo}/releases/{rel_id}/assets?name={name}"
    status, _ = ghapi.gh_request("POST", url, token=token, data=enc_bytes,
                                 headers={"Content-Type": "application/octet-stream"},
                                 timeout=timeout)
    if status == 404:
        # release 可能被删/缓存脏 → 重建
        logger.warning(f"上传 {name} 404，重建 release 后重试")
        _invalidate_release(token, repo)
        try:
            rel_id = ensure_release(token=token, repo=repo)
            url = f"{ghapi.UPLOAD_BASE}/repos/{repo}/releases/{rel_id}/assets?name={name}"
            status, _ = ghapi.gh_request("POST", url, token=token, data=enc_bytes,
                                         headers={"Content-Type": "application/octet-stream"},
                                         timeout=timeout)
        except Exception as e:
            logger.error(f"重建 release 失败: {e}")
    return status


def upload_asset_v2(base, data_or_path, token=None, repo=None, ts=None):
    """版本化上传（不删旧）。支持 bytes 或文件路径（大文件 path 模式）。

    返回 dict: {name, size, status, ok, attempts}
    """
    tok = token or config.GH_TOKEN
    repo = repo or config.REPO
    name = asset_name_v2(base, ts)
    size = len(data_or_path) if isinstance(data_or_path, bytes) else os.path.getsize(data_or_path)

    def load():
        if isinstance(data_or_path, bytes):
            return crypto.encrypt_bytes(data_or_path)
        with open(data_or_path, "rb") as f:
            return crypto.encrypt_bytes(f.read())

    rel_id = ensure_release(token=tok, repo=repo)
    attempts = 0
    last_status = 0
    for attempt in range(V2_MAX_RETRIES):
        attempts += 1
        try:
            enc = load()
            status = _upload_once(rel_id, name, enc, tok, repo,
                                  timeout=UPLOAD_TIMEOUT_BIG if size > 50 * 1024 * 1024 else 180)
            last_status = status
            if status in (200, 201):
                logger.info(f"v2上传 {name} OK ({size} bytes, 尝试{attempts}次)")
                return {"name": name, "size": size, "status": status,
                        "ok": True, "attempts": attempts}
            logger.warning(f"v2上传 {name} -> {status} (尝试{attempt+1}/{V2_MAX_RETRIES})")
        except Exception as e:
            logger.warning(f"v2上传 {name} 异常: {e}")
        if attempt < V2_MAX_RETRIES - 1:
            time.sleep(V2_RETRY_DELAYS[min(attempt, len(V2_RETRY_DELAYS) - 1)])
    logger.error(f"v2上传 {name} 最终失败 (status={last_status})")
    return {"name": name, "size": size, "status": last_status,
            "ok": False, "attempts": attempts}


def upload_chunked_v2(base, path, token=None, repo=None):
    """v2 分片上传：>1.5GB 才分片，动态片大小/并发，版本化命名。

    返回 dict: {ok, name, parts, ok_parts, size, statuses}
    """
    import os as _os
    tok = token or config.GH_TOKEN
    repo = repo or config.REPO
    total = _os.path.getsize(path)
    chunk_size = _chunk_size_for(total)
    ts = int(time.time())

    if chunk_size == 0:
        # 单文件直传
        r = upload_asset_v2(base, path, token=tok, repo=repo, ts=ts)
        return {"ok": r["ok"], "name": r["name"], "parts": 1,
                "ok_parts": 1 if r["ok"] else 0, "size": total, "statuses": [r["status"]]}

    parts = (total + chunk_size - 1) // chunk_size
    concurrency = _concurrency_for_parts(parts)
    results = [None] * parts

    def worker2(i):
        with open(path, "rb") as f:
            f.seek(i * chunk_size)
            data = f.read(chunk_size)
        r = upload_asset_v2(f"{base}.part{i}", data, token=tok, repo=repo, ts=ts)
        results[i] = r

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        list(ex.map(worker2, range(parts)))

    ok_parts = sum(1 for r in results if r.get("ok"))
    # manifest（版本化）
    manifest_name = asset_name_v2(f"{base}.manifest", ts)
    mdata = json.dumps({"parts": parts, "chunk_size": chunk_size,
                        "total_size": total, "ts": ts}).encode()
    mr = upload_asset_v2(f"{base}.manifest", mdata, token=tok, repo=repo, ts=ts)
    name = asset_name_v2(base, ts)
    return {"ok": ok_parts == parts and mr.get("ok"),
            "name": name, "parts": parts, "ok_parts": ok_parts + (1 if mr.get("ok") else 0),
            "size": total, "statuses": [r.get("status") for r in results] + [mr.get("status")]}


# ==================== v2：CDN 下载 ====================
def _cdn_url(name, token=None, repo=None):
    """CDN 直链（0 配额，无需 API）"""
    repo = repo or config.REPO
    return f"https://github.com/{repo}/releases/download/{config.BACKUP_TAG}/{quote(name)}"


def download_cdn(name, token=None, repo=None):
    """CDN 直链下载（不消耗配额）。返回 解密后 bytes 或 None。"""
    tok = token or config.GH_TOKEN
    repo = repo or config.REPO
    url = _cdn_url(name, token=tok, repo=repo)
    try:
        status, blob, _ = ghapi.gh_request("GET", url, token=tok, raw=True, timeout=DOWNLOAD_TIMEOUT)
        if status == 200 and blob:
            try:
                return crypto.decrypt_bytes(blob)
            except Exception as e:
                logger.error(f"CDN 下载 {name} 解密失败: {e}")
                return None
        logger.debug(f"CDN 下载 {name} -> {status}")
        return None
    except Exception as e:
        logger.warning(f"CDN 下载 {name} 异常: {e}")
        return None


def _version_ts(base, name):
    """从版本化资产名解析时间戳。返回 int 或 None（非版本资产）。

    {base}.{ts}.enc → ts；manifest/part 资产不属于直接版本。
    """
    if not name.startswith(base + ".") or not name.endswith(".enc"):
        return None
    core = name[len(base) + 1:-4]
    if "." in core:  # manifest.123 / part0.123 等 → 非单文件版本
        return None
    try:
        return int(core)
    except ValueError:
        return None


def find_latest_asset(base, token=None, repo=None):
    """按名字前缀找最新版本的 asset（版本化命名 {base}.{ts}.enc）。

    只匹配纯时间戳版本（排除 .manifest / .part 资产）。"""
    tok = token or config.GH_TOKEN
    repo = repo or config.REPO
    base = base.replace("/", ".")
    assets = list_assets(token=tok, repo=repo, ttl=ASSET_LIST_CACHE_TTL)
    best = None
    best_ts = -1
    for a in assets:
        ts = _version_ts(base, a.get("name", ""))
        if ts is not None and ts > best_ts:
            best_ts = ts
            best = a
    return best


def _find_latest_manifest(base, token=None, repo=None):
    """找最新分片 manifest（{base}.manifest.{ts}.enc）。"""
    tok = token or config.GH_TOKEN
    repo = repo or config.REPO
    base = base.replace("/", ".")
    assets = list_assets(token=tok, repo=repo, ttl=ASSET_LIST_CACHE_TTL)
    best = None
    best_ts = -1
    for a in assets:
        n = a.get("name", "")
        prefix = base + ".manifest."
        if n.startswith(prefix) and n.endswith(".enc"):
            try:
                ts = int(n[len(prefix):-4])
            except ValueError:
                continue
            if ts > best_ts:
                best_ts = ts
                best = a
    return best


def download_chunked_v2(base, token=None, repo=None, ts=None):
    """v2 分片下载：找最新 manifest → 并发下载各分片 → 解密合并。

    返回解密后完整 bytes 或 None。
    """
    tok = token or config.GH_TOKEN
    repo = repo or config.REPO
    base = base.replace("/", ".")
    # 1. 找 manifest
    m_asset = _find_latest_manifest(base, token=tok, repo=repo)
    if not m_asset:
        return None
    m_name = m_asset["name"]
    m_ts = int(m_name[len(base) + len(".manifest."):-4])
    # 2. 下载 manifest（CDN → API）
    blob = download_cdn(m_name, token=tok, repo=repo)
    if blob is None:
        blob = download_asset(m_name, token=tok, repo=repo)
    if blob is None:
        logger.error(f"分片 manifest {m_name} 下载失败")
        return None
    try:
        manifest = json.loads(blob.decode())
        parts = int(manifest["parts"])
    except Exception as e:
        logger.error(f"manifest 解析失败 {m_name}: {e}")
        return None

    # 3. 并发下载各分片（每片独立加密，逐片解密后拼接）
    results = [None] * parts
    conc = _concurrency_for_parts(parts)

    def dl(i):
        pname = asset_name_v2(f"{base}.part{i}", m_ts)
        data = download_cdn(pname, token=tok, repo=repo)
        if data is None:
            data = download_asset(pname, token=tok, repo=repo)
        results[i] = data

    with ThreadPoolExecutor(max_workers=conc) as ex:
        list(ex.map(dl, range(parts)))

    if any(d is None for d in results):
        logger.error(f"{base} 部分分片缺失")
        return None
    return b"".join(results)


def download_latest(base, token=None, repo=None):
    """下载最新版本资产：单文件版本 CDN 优先 → API 降级；
    若无单文件版本则尝试分片下载。返回 解密 bytes 或 None。"""
    tok = token or config.GH_TOKEN
    repo = repo or config.REPO
    asset = find_latest_asset(base, token=tok, repo=repo)
    if asset:
        name = asset["name"]
        # 1. CDN 直链（0 配额）
        blob = download_cdn(name, token=tok, repo=repo)
        if blob is not None:
            return blob
        # 2. API 降级（1 配额，更快）
        logger.info(f"CDN 不可用，降级 API 下载 {name}")
        return download_asset(name, token=tok, repo=repo)
    # 3. 无单文件版本 → 分片下载
    logger.info(f"{base} 无单文件版本，尝试分片下载")
    return download_chunked_v2(base, token=tok, repo=repo)


# ==================== v2：清理旧版本 ====================
def cleanup_old_versions(base, keep=VERSION_KEEP, older_than=3600, token=None, repo=None):
    """清理 base 前缀的旧版本资产（保留最近 keep 个；只在资产创建 > older_than 秒后删）。

    返回删除的 asset 名列表。删除每个消耗 2 配额，低频调用。
    """
    tok = token or config.GH_TOKEN
    repo = repo or config.REPO
    base = base.replace("/", ".")
    assets = list_assets(token=tok, repo=repo, ttl=ASSET_LIST_CACHE_TTL)
    matches = []
    now = time.time()
    for a in assets:
        n = a.get("name", "")
        ts = _version_ts(base, n)
        if ts is None:
            # 兼容旧固定名（{base}.enc 无时间戳）→ 视为最旧版本
            if n == base + ".enc":
                ts = 0
            else:
                continue
        created = a.get("created_at", "")
        # 解析创建时间戳 UTC（ISO）
        try:
            created_ts = calendar.timegm(time.strptime(created[:19], "%Y-%m-%dT%H:%M:%S")) \
                if created else now
        except Exception:
            created_ts = now
        matches.append((ts, created_ts, a))
    matches.sort(key=lambda x: x[0])
    removed = []
    for i, (ts, created_ts, a) in enumerate(matches):
        if i < len(matches) - keep:
            if now - created_ts > older_than:
                try:
                    st, _ = ghapi.gh_request(
                        "DELETE", f"{ghapi.API_BASE}/repos/{repo}/releases/assets/{a['id']}",
                        token=tok, timeout=30)
                    if st in (200, 204):
                        removed.append(a["name"])
                        logger.info(f"清理旧版本 {a['name']} -> {st}")
                except Exception as e:
                    logger.warning(f"清理 {a['name']} 失败: {e}")
    if removed:
        _invalidate_release(tok, repo)
    return removed