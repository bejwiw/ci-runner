# -*- coding: utf-8 -*-
"""
实例清单管理（manager 侧）— 纯内存模式

设计：启动时从 S3 读一次加载到内存，之后只用内存。
状态变动时更新内存 + 写 S3（S3 只做持久化备份，不做实时读取源）。
leader 锁保证只有一个 writer，不存在并发写问题。
"""
import time
import json as _json
import threading
import datetime

import config
import log
from core import releases, crypto

logger = log.setup_logger("store")

_lock = threading.RLock()
_s3pool = None

# 已关闭实例墓碑（防自愈复活）
_closed_ids = set()


# Releases 冗余保存节流（S3 每次存，Releases 低频刷，防配额耗尽）
RELEASES_FLUSH_INTERVAL = 300  # 至少间隔秒数
_last_releases_save = [0.0]
_releases_dirty = [False]

# ==================== 内存数据 ====================
_instances = []
_accounts = []
_tasks = []
_worker_stats = {}       # {inst_id: stats_dict}
_instance_configs = {}    # {inst_id: cfg_dict}
_loaded = False


# ==================== S3 底层 ====================
def _s3_put_json(key, obj):
    if _s3pool and _s3pool.is_ready():
        return _s3pool.put(key, _json.dumps(obj, ensure_ascii=False).encode())
    return False

def _s3_get_json(key):
    if _s3pool and _s3pool.is_ready():
        raw = _s3pool.get(key)
        if raw is not None:
            try:
                return _json.loads(raw.decode())
            except Exception as e:
                logger.warning(f"JSON 解析失败 {key}: {e}")
    return None

def set_s3pool(pool):
    global _s3pool, _loaded
    _s3pool = pool
    # S3 刚设置好，如果之前从 Releases 加载了空数据，重新从 S3 加载
    if _loaded and not _instances and not _accounts:
        _loaded = False
        logger.info("[store] S3 就绪，重新加载（之前从 Releases 加载为空）")
        load_all()


# ==================== 启动加载（只读一次 S3）====================
def load_all():
    """启动时从 S3 加载所有数据到内存"""
    global _loaded, _instances
    if _loaded:
        return
    with _lock:
        if _loaded:
            return
        _load_instances_from_storage()
        _load_accounts_from_storage()
        _load_tasks_from_storage()
        # 净化历史 closed 数据（旧版本可能残留 closed 实例在清单中）
        for inst in list(_instances):
            if inst.get("closed"):
                _closed_ids.add(inst.get("id"))
        _instances = [i for i in _instances if not i.get("closed")]
        _loaded = True
        logger.info(f"内存加载完成: {len(_instances)} 实例, {len(_accounts)} 账号, {len(_tasks)} 任务")


def _load_instances_from_storage():
    global _instances
    if _s3pool and _s3pool.is_ready():
        data = _s3_get_json("meta/instances.json")
        if data is not None and isinstance(data, list):
            _instances = data
            logger.info(f"从 S3 加载 {len(_instances)} 个实例")
            return
    data = releases.load_json_enc("instances.json.enc", default=[])
    _instances = data if isinstance(data, list) else []
    logger.info(f"从 Releases 加载 {len(_instances)} 个实例")


def _load_accounts_from_storage():
    global _accounts
    if _s3pool and _s3pool.is_ready():
        data = _s3_get_json("meta/accounts.json")
        if data is not None and isinstance(data, list):
            _accounts = data
            logger.info(f"从 S3 加载 {len(_accounts)} 个账号")
            return
    _accounts = releases.load_json_enc("accounts.json.enc", default=[])


def _load_tasks_from_storage():
    global _tasks
    if _s3pool and _s3pool.is_ready():
        data = _s3_get_json("meta/tasks.json")
        if data is not None and isinstance(data, list):
            _tasks = data
            return
    _tasks = releases.load_json_enc("tasks.json.enc", default=[])


# ==================== 读取（纯内存）====================
def load_instances():
    if not _loaded:
        load_all()
    return _instances

def load_accounts():
    if not _loaded:
        load_all()
    return _accounts

def load_tasks():
    if not _loaded:
        load_all()
    return _tasks

def list_instances():
    return load_instances()

def get_instance(inst_id):
    for inst in _instances:
        if inst.get("id") == inst_id:
            return inst
    return None


def get_or_create_instance(inst_id, cfg):
    """实例不存在时从配置恢复创建（自愈）；已关闭实例拒绝复活"""
    with _lock:
        if inst_id in _closed_ids:
            logger.warning(f"实例 {inst_id} 已在关闭墓碑中，拒绝自愈复活")
            return None
        for inst in _instances:
            if inst.get("id") == inst_id and not inst.get("closed"):
                return inst
        logger.info(f"实例 {inst_id} 不在内存中，自愈恢复")
        hostname = cfg.get("hostname") or f"{inst_id}.{config.BASE_DOMAIN}"
        new_inst = {
            "id": inst_id,
            "hostname": hostname,
            "account": cfg.get("account", ""),
            "account_repo": cfg.get("account_repo", ""),
            "tunnel_id": cfg.get("tunnel_id", ""),
            "tunnel_token": cfg.get("tunnel_token", ""),
            "mcp_enabled": cfg.get("mcp_enabled", True),
            "mcp_hostname": cfg.get("mcp_hostname", f"mcp-{hostname}"),
            "mcp_tunnel_id": cfg.get("mcp_tunnel_id", ""),
            "mcp_url": f"https://{cfg.get('mcp_hostname', f'mcp-{hostname}')}",
            "run_id": cfg.get("run_id"),
            "status": "running",
            "url": f"https://{hostname}",
            "closed": False,
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "last_seen": time.time(),
        }
        _instances.append(new_inst)
        save_instances(_instances)
        logger.info(f"实例 {inst_id} 已自愈恢复")
        return new_inst


def add_instance(inst):
    with _lock:
        _instances.append(inst)
        save_instances(_instances)


def update_instance(inst_id, **kwargs):
    with _lock:
        for inst in _instances:
            if inst.get("id") == inst_id:
                inst.update(kwargs)
                inst["last_seen"] = time.time()
                save_instances(_instances)
                return inst
    return None


def close_instance(inst_id):
    """关闭实例：标记 closed → 进墓碑 → 从活跃清单移除（存储净化）"""
    with _lock:
        for inst in _instances:
            if inst.get("id") == inst_id:
                inst["closed"] = True
                inst["status"] = "closed"
                _closed_ids.add(inst_id)
                active = [i for i in _instances if i.get("id") != inst_id]
                ok = save_instances(active)
                if not ok:
                    logger.error(f"关闭 {inst_id} 失败：S3/Releases 保存失败")
                    return False
                return True
    return False


def is_closed(inst_id):
    """实例是否在墓碑（已关闭）"""
    with _lock:
        return inst_id in _closed_ids


def next_inst_id():
    nums = []
    for inst in _instances:
        try:
            nums.append(int(inst["id"].replace("inst", "")))
        except (ValueError, KeyError):
            pass
    return f"inst{max(nums) + 1 if nums else 1}"


# ==================== 写入（更新内存 + 写 S3 + Releases）====================
def save_instances(instances):
    """保存实例清单（更新内存 + 写 S3 + Releases）"""
    if not instances:
        logger.warning("拒绝空数据覆盖实例清单")
        return False
    global _instances
    with _lock:
        # 过滤 closed（关闭实例只留墓碑，存储保持干净）
        instances = [i for i in instances if not i.get("closed")]
        for i in instances:
            _closed_ids.discard(i.get("id"))
        _instances = instances
        s3_ok = False
        if _s3pool and _s3pool.is_ready():
            try:
                s3_ok = _s3_put_json("meta/instances.json", instances)
            except Exception as e:
                logger.warning(f"S3 保存失败: {e}")
        _releases_save_throttled("instances.json.enc", instances, force=False)
        if not s3_ok and not _releases_dirty[0]:
            logger.error("S3 保存失败且 Releases 节流中")
    return True


def _releases_save_throttled(name, obj, force=False):
    """Releases 冗余保存：节流（>=300s 或 force）才真上传；否则只标记脏。

    高频调用（如 instance_report 60s 更新 run_id）只写 S3（不费 GitHub 配额），
    Releases 冗余低频刷。
    """
    now = time.time()
    with _lock:
        _releases_dirty[0] = True
        if not force and now - _last_releases_save[0] < RELEASES_FLUSH_INTERVAL:
            return False
        _last_releases_save[0] = now
        _releases_dirty[0] = False
    try:
        releases.save_json_enc(name, obj)
        return True
    except Exception as e:
        logger.warning(f"Releases 保存失败 {name}: {e}")
        with _lock:
            _releases_dirty[0] = True  # 失败重标记，等后台 flush
        return False


def flush_releases_dirty():
    """后台调用：把脏标记的 instances.json 补刷到 Releases。"""
    with _lock:
        if not _releases_dirty[0]:
            return False
    try:
        with _lock:
            insts = list(_instances)
        return _releases_save_throttled("instances.json.enc", insts, force=True)
    except Exception as e:
        logger.warning(f"flush_releases_dirty 失败: {e}")
        return False


def save_accounts(accounts):
    if not accounts:
        logger.warning("拒绝空数据覆盖账号配置")
        return False
    global _accounts
    with _lock:
        _accounts = accounts
        if _s3pool and _s3pool.is_ready():
            try:
                _s3_put_json("meta/accounts.json", accounts)
            except Exception as e:
                logger.warning(f"S3 保存账号失败: {e}")
        releases.save_json_enc("accounts.json.enc", accounts)


def save_tasks(tasks):
    global _tasks
    with _lock:
        _tasks = tasks
        if _s3pool and _s3pool.is_ready():
            try:
                _s3_put_json("meta/tasks.json", tasks)
            except Exception as e:
                logger.warning(f"S3 保存任务失败: {e}")
        releases.save_json_protected("tasks.json.enc", tasks)


# ==================== 实例配置（内存缓存 + S3 持久化）====================
def _inst_config_key(inst_id):
    return f"meta/inst-config/{inst_id}.json"


def save_instance_config(inst_id, cfg):
    """保存实例配置（更新内存 + 写 S3 + Releases）"""
    with _lock:
        _instance_configs[inst_id] = cfg
    if _s3pool and _s3pool.is_ready():
        try:
            _s3_put_json(_inst_config_key(inst_id), cfg)
        except Exception as e:
            logger.warning(f"S3 保存实例配置 {inst_id} 失败: {e}")
    try:
        releases.save_json_enc(f"inst-{inst_id}.json.enc", cfg)
    except Exception as e:
        logger.warning(f"Releases 保存实例配置 {inst_id} 失败: {e}")


def load_instance_config(inst_id):
    """读取实例配置（内存优先，没有则从 S3 读一次后缓存）"""
    if inst_id in _instance_configs:
        return _instance_configs[inst_id]
    if _s3pool and _s3pool.is_ready():
        data = _s3_get_json(_inst_config_key(inst_id))
        if data is not None:
            _instance_configs[inst_id] = data
            return data
    data = releases.load_json_enc(f"inst-{inst_id}.json.enc", default={})
    if data:
        _instance_configs[inst_id] = data
    return data


def delete_instance_config(inst_id):
    """删除实例配置"""
    with _lock:
        _instance_configs.pop(inst_id, None)
    if _s3pool and _s3pool.is_ready():
        try:
            _s3pool.delete(_inst_config_key(inst_id))
        except Exception as e:
            logger.warning(f"S3 删除实例配置失败: {e}")
    releases.delete_asset(f"inst-{inst_id}.json.enc")


def purge_instance_data(inst_id):
    """彻底清理实例所有备份数据（S3 + Releases）

    清理范围：
    - S3: 数据库、文件备份、文件备份分片、进程快照（旧）、进程快照分片
    - Releases: 加密数据库、加密文件备份、分片manifest、分片数据
    处理大文件(>50MB S3分片 / >500MB Releases分片)的完整清理。
    """
    import json as _json

    # ==================== S3 清理 ====================
    if _s3pool and _s3pool.is_ready():
        # 备份时实际的 S3 key（与 persistence.backup_database/backup_files 一致）
        s3_keys = [
            f"inst-data/{inst_id}/db",            # 数据库
            f"inst-files/{inst_id}/files.tar.gz",  # 文件全量备份
            f"inst-proc/{inst_id}/proc.tar.gz",    # 进程快照（旧，已废弃）
        ]
        for key in s3_keys:
            # 1. 查 manifest 获取分片列表（大文件分片存储）
            manifest_data = None
            try:
                manifest_data = _s3pool.get(f"{key}.manifest")
            except Exception:
                pass

            if manifest_data:
                try:
                    m = _json.loads(manifest_data)
                    num_chunks = m.get("chunks", 0)
                    locations = m.get("locations", [])
                    # 2a. 按 manifest 的 locations 精确删除每个分片
                    for loc in locations:
                        chunk_idx = loc.get("chunk", 0)
                        chunk_key = f"{key}.chunk{chunk_idx}"
                        try:
                            if _s3pool.delete(chunk_key):
                                logger.info(f"S3 清理分片 {chunk_key}")
                        except Exception as e:
                            logger.warning(f"S3 清理 {chunk_key} 失败: {e}")
                    # 2b. locations 不完整时用 num_chunks 兜底
                    if len(locations) < num_chunks:
                        for i in range(num_chunks):
                            chunk_key = f"{key}.chunk{i}"
                            try:
                                _s3pool.delete(chunk_key)
                            except Exception:
                                pass
                    # 3. 删除 manifest 本身
                    try:
                        _s3pool.delete(f"{key}.manifest")
                    except Exception as e:
                        logger.warning(f"S3 清理 manifest 失败: {e}")
                    logger.info(f"S3 分片清理 {key} ({num_chunks}块)")
                except Exception as e:
                    logger.warning(f"S3 解析 manifest {key} 失败: {e}")

            # 4. 删除单文件版本
            try:
                if _s3pool.delete(key):
                    logger.info(f"S3 清理 {key}")
            except Exception as e:
                logger.warning(f"S3 清理 {key} 失败: {e}")

    # ==================== Releases 清理 ====================
    # Releases的asset名格式: inst-{id}.xxx.enc
    releases_assets = [
        f"inst-{inst_id}.db.enc",
        f"inst-{inst_id}.files.tar.gz.enc",
        f"inst-{inst_id}.processes.tar.gz.enc",
        f"inst-{inst_id}.json.enc",
    ]

    for asset_name in releases_assets:
        # 1. 查分片manifest
        manifest_blob = None
        try:
            manifest_blob = releases.download_asset(f"{asset_name}.manifest")
        except Exception:
            pass

        if manifest_blob:
            try:
                m = _json.loads(manifest_blob.decode())
                parts = m.get("parts", 0)
                # 2. 逐个删除分片
                for i in range(parts):
                    try:
                        releases.delete_asset(f"{asset_name}.part{i}")
                    except Exception:
                        pass
                logger.info(f"Releases 分片清理 {asset_name} ({parts}片)")
            except Exception as e:
                logger.warning(f"Releases 解析manifest {asset_name} 失败: {e}")

        # 3. 删除manifest本身
        try:
            releases.delete_asset(f"{asset_name}.manifest")
        except Exception:
            pass

        # 4. 删除单文件版本
        try:
            releases.delete_asset(asset_name)
        except Exception as e:
            logger.warning(f"Releases 清理 {asset_name} 失败: {e}")

    logger.info(f"实例 {inst_id} 备份数据已彻底清理 (S3+Releases)")

def get_worker_stats(inst_id):
    """获取 worker 操作统计（纯内存，不读 S3）"""
    return _worker_stats.get(inst_id, {
        "a_count_total": 0, "b_count_total": 0, "storage_mb": 0,
        "backup_history": [], "restore_history": [], "timeline": [],
        "last_backup": 0, "last_restore": 0,
    })


def save_worker_stats(inst_id, stats):
    """保存 worker 统计（纯内存，不写 S3）"""
    with _lock:
        _worker_stats[inst_id] = stats
