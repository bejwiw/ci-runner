# -*- coding: utf-8 -*-
"""
ghbox 全局配置

所有配置从环境变量读取，提供安全默认值。
GitHub Actions 临时虚拟机当"免费云服务器"，
靠 S3(Tigris) 持久化 + 到期前自动续命 + 进程持久化实现永续在线。
"""
import os

# ==================== 运行角色 ====================
ROLE = os.environ.get("INSTANCE_ROLE", "worker")
INSTANCE_ID = os.environ.get("INSTANCE_ID", "worker-1")

# ==================== GitHub ====================
REPO = os.environ.get("REPO", "")
GH_TOKEN = os.environ.get("GH_TOKEN", "")
MAIN_REPO = os.environ.get("MAIN_REPO", "")
CURRENT_SHA = os.environ.get("CURRENT_SHA", "")

# ==================== 安全 ====================
EXEC_TOKEN = os.environ.get("EXEC_TOKEN", "")
# AES-256-GCM 密钥（hex 64 位 = 32 字节）
DEMO_KEY = os.environ.get("DEMO_KEY", "")

# ==================== S3 (Tigris) ====================
# 账号文件路径（AWS profiles 格式，每行一个 profile）
S3_ACCOUNTS_FILE = os.environ.get("S3_ACCOUNTS_FILE", os.path.expanduser("~/s3-accounts.txt"))
S3_ENDPOINT = os.environ.get("S3_ENDPOINT", "https://t3.storage.dev")
S3_REGION = os.environ.get("S3_REGION", "us-west-2")
S3_DATA_PREFIX = "ghbox"  # 所有数据的 S3 key 前缀

# ==================== 隧道 ====================
TUNNEL_TOKEN = os.environ.get("TUNNEL_TOKEN", "")
TUNNEL_HOST = os.environ.get("TUNNEL_HOST", "")
CF_EMAIL = os.environ.get("CF_EMAIL", "")
CF_API_KEY = os.environ.get("CF_API_KEY", "")
CF_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID", "")
CF_ZONE_ID = os.environ.get("CF_ZONE_ID", "")
BASE_DOMAIN = os.environ.get("BASE_DOMAIN", "kekeke.cc.cd")

# ==================== 数据/文件路径 ====================
FILES_DIR = os.environ.get("FILES_DIR", "/home/kodebite")
PROC_DIR = os.path.join(FILES_DIR, "processes")
SYSCONFIG_DIR = os.path.join(FILES_DIR, "sysconfig")
LOGS_DIR = os.path.join(FILES_DIR, "logs")
DB_FILE = os.path.join(FILES_DIR, "demo.db")

# ==================== 服务 ====================
PORT = int(os.environ.get("PORT", "8080"))
BACKUP_INTERVAL = int(os.environ.get("BACKUP_INTERVAL", "180"))

# ==================== Leader 锁 ====================
HEARTBEAT_INTERVAL = int(os.environ.get("HEARTBEAT_INTERVAL", "60"))
HEARTBEAT_TIMEOUT = int(os.environ.get("HEARTBEAT_TIMEOUT", "90"))

# ==================== 无缝衔接（到期前预触发） ====================
PRE_WAKE_SECONDS = int(os.environ.get("PRE_WAKE_SECONDS", "21300"))

# ==================== 进程持久化 ====================
PROC_SCAN_INTERVAL = int(os.environ.get("PROC_SCAN_INTERVAL", "180"))
PROC_MAX_RETRY = int(os.environ.get("PROC_MAX_RETRY", "3"))
PROC_RETRY_DELAY = [5, 15, 45]
PROC_BACKUP_EXCLUDE = os.environ.get(
    "PROC_BACKUP_EXCLUDE",
    "node_modules,.git,__pycache__,.venv,venv,dist,build,.cache,logs,tmp"
).split(",")
PROC_MAX_BACKUP_MB = int(os.environ.get("PROC_MAX_BACKUP_MB", "512"))

# ==================== 磁盘监控 ====================
DISK_WARN_PERCENT = int(os.environ.get("DISK_WARN_PERCENT", "85"))
DISK_CLEAN_TRIGGER_PERCENT = int(os.environ.get("DISK_CLEAN_TRIGGER_PERCENT", "90"))
DISK_CHECK_INTERVAL = int(os.environ.get("DISK_CHECK_INTERVAL", "600"))

# ==================== MCP 服务 ====================
MCP_PORT = int(os.environ.get("MCP_PORT", "3457"))
MCP_PREFIX = os.environ.get("MCP_PREFIX", "mcp")

# ==================== Manager 主机（worker 上报用） ====================
MANAGER_HOST = os.environ.get("MANAGER_HOST", "")

# ==================== 自动更新 ====================
MANAGER_WORKFLOW = os.environ.get("MANAGER_WORKFLOW", "manager.yml")
WORKER_WORKFLOW = os.environ.get("WORKER_WORKFLOW", "worker.yml")

# ==================== Releases 存储常量 ====================
BACKUP_TAG = "backup"
ASSET_LEADER = "leader.json"
# 默认资产名（inst_cfg 为 None 时的降级路径用，避免 AttributeError）
ASSET_DB = "inst-global.db.enc"
ASSET_FILES = "inst-global.files.tar.gz.enc"
ASSET_PROC = "inst-global.processes.tar.gz.enc"

# ==================== WSS 终端 ====================
SESSION_TTL = int(os.environ.get("SESSION_TTL", "180"))

# ==================== 实例配置 ====================
class InstanceConfig:
    """实例配置（worker 启动时从 S3/Releases 读取）"""
    def __init__(self, instance_id, cfg=None):
        self.instance_id = instance_id
        cfg = cfg or {}
        self.asset_db = f"inst-{instance_id}.db.enc"
        self.asset_files = f"inst-{instance_id}.files.tar.gz.enc"
        self.tunnel_token = cfg.get("tunnel_token") or TUNNEL_TOKEN
        self.tunnel_host = cfg.get("hostname") or TUNNEL_HOST
        self.tunnel_id = cfg.get("tunnel_id") or ""
        self.account = cfg.get("account") or ""
        self.account_repo = cfg.get("account_repo") or ""
        self.raw = cfg

# ==================== 配置校验 ====================
def validate_config():
    """启动时校验关键配置。致命错误直接退出，避免带着残缺配置运行。"""
    import sys
    errors = []
    warnings = []

    if not EXEC_TOKEN:
        errors.append("EXEC_TOKEN 未配置（服务无鉴权）")
    if DEMO_KEY:
        try:
            key_bytes = bytes.fromhex(DEMO_KEY)
            if len(key_bytes) != 32:
                errors.append(f"DEMO_KEY 应为32字节(64位hex)，当前{len(key_bytes)}字节")
        except ValueError as e:
            errors.append(f"DEMO_KEY 不是合法 hex: {e}")
    elif ROLE == "worker":
        warnings.append("DEMO_KEY 未配置（Releases 加密降级不可用）")

    if not (1 <= PORT <= 65535):
        errors.append(f"PORT 非法: {PORT}（应在1-65535）")

    if BACKUP_INTERVAL < 30:
        errors.append(f"BACKUP_INTERVAL={BACKUP_INTERVAL} 不能小于30秒")
    if HEARTBEAT_TIMEOUT < HEARTBEAT_INTERVAL:
        errors.append(f"HEARTBEAT_TIMEOUT({HEARTBEAT_TIMEOUT}) < HEARTBEAT_INTERVAL({HEARTBEAT_INTERVAL})")

    _s3b = os.environ.get("S3_BOOTSTRAP", "")
    if _s3b and _s3b.count("|") < 2:
        errors.append("S3_BOOTSTRAP 格式应为 access_key|secret_key|bucket")

    if ROLE == "manager":
        if not GH_TOKEN:
            errors.append("manager 角色需要 GH_TOKEN")
        if not CF_EMAIL or not CF_API_KEY:
            warnings.append("CF_EMAIL/CF_API_KEY 未配置（隧道管理不可用）")
    elif ROLE == "worker":
        if not MANAGER_HOST:
            warnings.append("MANAGER_HOST 未配置（leader 选举 + 上报不可用）")

    for w in warnings:
        print(f"[WARN] {w}")
    if errors:
        for e in errors:
            print(f"[FATAL] {e}")
        sys.exit(1)
    return True
