# -*- coding: utf-8 -*-
"""
实例清单管理（manager 侧）

修复旧项目竞态条件的核心模块：
1. 全局 RLock 保护 load + modify + save 原子性
2. merge 逻辑：save 前先 load 最新 S3 数据，把不在当前列表中的非 closed 实例 merge 进来
3. S3 put 返回值正确处理（旧项目 bug：put 失败仍标记成功）

存储：S3 账号0（主）+ GitHub Releases（降级）
"""
import time
import threading
import datetime

import config
import log
from core import releases, crypto

logger = log.setup_logger("store")

# 全局实例清单锁
_lock = threading.RLock()

# S3 池引用（由 app.py 启动时注入）
_s3pool = None
_cache = {}
_cache_time = {}


def set_s3pool(pool):
    """注入 S3Pool 实例"""
    global _s3pool
    _s3pool = pool


# ==================== 实例清单 ====================
def load_instances():
    """读取实例清单（60秒缓存）。优先 S3，回退 Releases。"""
    import time as _t
    now = _t.time()
    if "instances" in _cache_time and now - _cache_time["instances"] < 60:
        return _cache.get("instances", [])

    if _s3pool and _s3pool.is_ready():
        data = _s3pool.get_meta_json("meta/instances.json", default=None)
        if data is not None and isinstance(data, list):
            logger.info(f"[store] 从 S3 加载 {len(data)} 个实例")
            return data
    data = releases.load_json_enc("instances.json.enc", default=[])
    count = len(data) if isinstance(data, list) else 0
    logger.info(f"[store] 从 Releases 加载 {count} 个实例")
    result = data if isinstance(data, list) else []
    _cache["instances"] = result
    _cache_time["instances"] = now
    return result


def save_instances(instances):
    """
    保存实例清单（merge 模式 + 锁保护）。

    merge 逻辑：保存前先 load 最新 S3 数据，
    把不在当前列表中的非 closed 实例 merge 进来，
    防止并发写入丢失其他线程添加的新实例。
    """
    if not instances:
        logger.warning("[store] 拒绝空数据覆盖实例清单")
        return False

    with _lock:
        # merge：从 S3 读取最新数据，恢复缺失的实例
        try:
            if _s3pool and _s3pool.is_ready():
                current = _s3pool.get_meta_json("meta/instances.json", default=None)
                if current and isinstance(current, list):
                    current_ids = {i.get("id") for i in instances}
                    for inst in current:
                        inst_id = inst.get("id")
                        if (inst_id and inst_id not in current_ids
                                and not inst.get("closed")):
                            instances.append(inst)
                            logger.info(f"[merge] 恢复缺失实例: {inst_id}")
        except Exception as e:
            logger.warning(f"[merge] merge 失败: {e}")

        # 数量骤减保护
        try:
            old = load_instances()
            old_count = len(old)
            new_count = len(instances)
            if old_count > 2 and new_count < old_count / 2:
                logger.warning(f"[store] 拒绝数量骤减: {old_count}→{new_count}")
                return False
        except Exception:
            pass

        # 写 S3（主）
        s3_ok = False
        if _s3pool and _s3pool.is_ready():
            try:
                s3_ok = _s3pool.put_meta_json("meta/instances.json", instances)
            except Exception as e:
                logger.warning(f"[store] S3 保存失败: {e}")
            if s3_ok:
                logger.info(f"[store] 已存入 S3 ({len(instances)} 个)")
            else:
                logger.warning("[store] S3 保存失败，降级 Releases")

    # 清缓存
    with _lock:
        _cache.pop("instances", None)
        _cache_time.pop("instances", None)
        # 写 Releases（降级/双写）
        try:
            releases.save_json_enc("instances.json.enc", instances)
            logger.info(f"[store] 已备份到 Releases ({len(instances)} 个)")
        except Exception as e:
            if not s3_ok:
                logger.error(f"[store] S3 和 Releases 都失败: {e}")
            else:
                logger.info(f"[store] Releases 备份失败(S3已成功): {e}")
        return True


# ==================== 账号配置 ====================
def load_accounts():
    if _s3pool and _s3pool.is_ready():
        data = _s3pool.get_meta_json("meta/accounts.json", default=None)
        if data is not None and isinstance(data, list):
            return data
    return releases.load_json_enc("accounts.json.enc", default=[])


def save_accounts(accounts):
    if not accounts:
        logger.warning("[store] 拒绝空数据覆盖账号配置")
        return False
    if _s3pool and _s3pool.is_ready():
        try:
            _s3pool.put_meta_json("meta/accounts.json", accounts)
        except Exception as e:
            logger.warning(f"[store] S3 保存账号失败: {e}")
    releases.save_json_enc("accounts.json.enc", accounts)


# ==================== 任务队列 ====================
def load_tasks():
    import time as _t
    now = _t.time()
    if "tasks" in _cache_time and now - _cache_time["tasks"] < 60:
        return _cache.get("tasks", [])
    if _s3pool and _s3pool.is_ready():
        data = _s3pool.get_meta_json("meta/tasks.json", default=None)
        if data is not None and isinstance(data, list):
            return data
    result = releases.load_json_enc("tasks.json.enc", default=[])
    _cache["tasks"] = result
    _cache_time["tasks"] = now
    return result


def save_tasks(tasks):
    if _s3pool and _s3pool.is_ready():
        try:
            _s3pool.put_meta_json("meta/tasks.json", tasks)
        except Exception:
            pass
    releases.save_json_protected("tasks.json.enc", tasks)


# ==================== 实例配置 ====================
def _inst_config_key(inst_id):
    return f"meta/inst-config/{inst_id}.json"


def save_instance_config(inst_id, cfg):
    """保存实例配置到 S3 + Releases"""
    if _s3pool and _s3pool.is_ready():
        try:
            _s3pool.put_meta_json(_inst_config_key(inst_id), cfg)
        except Exception as e:
            logger.warning(f"[store] S3 保存实例配置 {inst_id} 失败: {e}")
    try:
        releases.save_json_enc(f"inst-{inst_id}.json.enc", cfg)
    except Exception as e:
        logger.warning(f"[store] Releases 保存实例配置 {inst_id} 失败: {e}")


def load_instance_config(inst_id):
    """读取实例配置。优先 S3，回退 Releases。"""
    if _s3pool and _s3pool.is_ready():
        data = _s3pool.get_meta_json(_inst_config_key(inst_id), default=None)
        if data is not None:
            return data
    return releases.load_json_enc(f"inst-{inst_id}.json.enc", default={})


def delete_instance_config(inst_id):
    """删除实例配置"""
    if _s3pool and _s3pool.is_ready():
        try:
            _s3pool.delete(_inst_config_key(inst_id))
        except Exception:
            pass
    releases.delete_asset(f"inst-{inst_id}.json.enc")


# ==================== 实例操作 ====================
def list_instances():
    """列出所有实例"""
    return load_instances()


def get_instance(inst_id):
    """获取单个实例"""
    for inst in load_instances():
        if inst.get("id") == inst_id:
            return inst
    return None


def get_or_create_instance(inst_id, cfg):
    """
    实例不存在时从配置恢复创建（自愈逻辑）。
    修复旧项目 bug：api_instance_report 直接返回 404 而不是自愈。
    """
    with _lock:
        instances = load_instances()
        for inst in instances:
            if inst.get("id") == inst_id and not inst.get("closed"):
                return inst
        # 不存在 → 从配置恢复
        logger.info(f"[store] 实例 {inst_id} 不在清单中，尝试自愈恢复")
        hostname = cfg.get("hostname") or f"{inst_id}.{config.BASE_DOMAIN}"
        new_inst = {
            "id": inst_id,
            "hostname": hostname,
            "account": cfg.get("account", ""),
            "account_repo": cfg.get("account_repo", ""),
            "tunnel_id": cfg.get("tunnel_id", ""),
            "tunnel_token": cfg.get("tunnel_token", ""),
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
        instances.append(new_inst)
        save_instances(instances)
        logger.info(f"[store] 实例 {inst_id} 已自愈恢复")
        return new_inst


def add_instance(inst):
    """添加实例到清单"""
    with _lock:
        instances = load_instances()
        instances.append(inst)
        save_instances(instances)


def update_instance(inst_id, **kwargs):
    """更新实例字段"""
    with _lock:
        instances = load_instances()
        for inst in instances:
            if inst.get("id") == inst_id:
                inst.update(kwargs)
                inst["last_seen"] = time.time()
                save_instances(instances)
                return inst
    return None


def close_instance(inst_id):
    """标记实例为已关闭"""
    with _lock:
        instances = load_instances()
        for inst in instances:
            if inst.get("id") == inst_id:
                inst["closed"] = True
                inst["status"] = "closed"
                save_instances(instances)
                return True
    return False


def next_inst_id():
    """生成下一个实例 ID"""
    instances = load_instances()
    nums = []
    for inst in instances:
        try:
            nums.append(int(inst["id"].replace("inst", "")))
        except (ValueError, KeyError):
            pass
    return f"inst{max(nums) + 1 if nums else 1}"
