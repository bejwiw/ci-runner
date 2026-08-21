# -*- coding: utf-8 -*-
"""
Worker 延迟初始化（阶段2，后台线程）

优化（2026-08-21）：
- MCP隧道提前启动（在进程恢复之前），减少MCP不可用窗口
- 进程恢复和MCP node启动并行（ThreadPoolExecutor）
- 进程恢复本身也并行化（restore.py），隧道启动并行化（manager.py）
- 整体恢复时间从~100s降至~20s
"""
import os
import time
import threading
import subprocess
from concurrent.futures import ThreadPoolExecutor

import config
import log
from core import lock as core_lock
from worker import state, persistence, terminal
from worker import sysconfig as syscfg
from worker.mcp import McpManager
from worker.loops import start_loops

logger = log.setup_logger("boot")


def deferred_init():
    """延迟初始化：数据恢复 → 系统配置 → MCP隧道 → 并行(进程恢复+MCP node) → Leader → 循环"""
    t0 = time.time()
    logger.info("=== 阶段2：延迟初始化 ===")

    # 1. 数据恢复
    try:
        state.load_status = persistence.load_or_create(state.inst_cfg)
        logger.info(f"数据恢复完成 ({time.time()-t0:.1f}s)")
        persistence.save_prev_backup(state.inst_cfg)
    except Exception as e:
        logger.error(f"数据恢复失败: {e}")

    # 2. 系统配置
    try:
        syscfg.restore_system_config()
    except Exception as e:
        logger.error(f"系统配置恢复失败: {e}")
    threading.Thread(target=_system_trim, daemon=True).start()
    _tune_network()
    threading.Thread(target=_sysbackup_loop_async, daemon=True).start()

    # 3. Shell 配置 + setup.sh
    try:
        _write_shell_profile()
    except Exception as e:
        logger.error(f"Shell 配置失败: {e}")
    try:
        _run_setup()
    except Exception as e:
        logger.error(f"setup.sh 失败: {e}")

    # 4. MCP隧道提前启动（不等进程恢复，减少MCP不可用窗口）
    mcp_enabled = state.inst_cfg.raw.get("mcp_enabled", True) if state.inst_cfg and state.inst_cfg.raw else True
    if mcp_enabled:
        try:
            state.mcp_mgr = McpManager(state.inst_cfg)
            state.mcp_mgr._start_tunnel()
            logger.info(f"MCP 隧道提前启动 ({time.time()-t0:.1f}s)")
        except Exception as e:
            logger.warning(f"MCP 隧道提前启动失败: {e}")

    # 5. 并行：进程恢复 + MCP node启动
    def _restore_procs():
        if not state.proc_mgr:
            return
        try:
            restored, failed = state.proc_mgr.restore_all()
            logger.info(f"进程恢复 {restored} 成功, {failed} 失败 ({time.time()-t0:.1f}s)")
            state.proc_mgr.start_monitor()
        except Exception as e:
            logger.error(f"进程恢复异常: {e}")

    def _start_mcp_node():
        if not mcp_enabled or not state.mcp_mgr:
            return
        try:
            # start()内部检查隧道是否已启动，已启动则跳过
            state.mcp_mgr.start()
        except Exception as e:
            logger.error(f"MCP node启动失败: {e}")

    with ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(_restore_procs)
        f2 = executor.submit(_start_mcp_node)
        f1.result()
        f2.result()

    # 恢复后全量备份
    try:
        persistence.backup_database(state.inst_cfg)
        persistence.backup_files(state.inst_cfg)
        logger.info("恢复后全量备份完成")
    except Exception as e:
        logger.warning(f"恢复后备份失败: {e}")

    # 6. Leader 锁
    try:
        state.leader = core_lock.LeaderLock(backend="manager",
                                             instance_id=config.INSTANCE_ID)
        state.leader.acquire()
        if state.leader.is_leader:
            threading.Thread(target=state.leader.heartbeat_loop, daemon=True).start()
        else:
            def _on_promote():
                logger.info("Follower 升级为 Leader，启动备份循环")
                from worker.loops import _backup_loop
                threading.Thread(target=_backup_loop, daemon=True).start()
            threading.Thread(target=state.leader.follower_loop,
                             args=(_on_promote,), daemon=True).start()
    except Exception as e:
        logger.error(f"Leader 锁失败: {e}")

    # 7. 后台循环
    start_loops()
    terminal.start_cleanup()

    logger.info(f"=== 全部初始化完成 ({time.time()-t0:.1f}s) ===")
    logger.info(f"固定域名: {state.inst_cfg.tunnel_host}")


def _tune_network():
    cmds = [
        "sudo sysctl -w net.core.wmem_default=67108864 2>/dev/null",
        "sudo sysctl -w net.core.rmem_default=67108864 2>/dev/null",
        "sudo sysctl -w net.core.netdev_max_backlog=65536 2>/dev/null",
        "sudo sysctl -w net.ipv4.ip_local_port_range='1024 65535' 2>/dev/null",
        "sudo sysctl -w net.ipv4.tcp_wmem='4096 87380 67108864' 2>/dev/null",
        "sudo sysctl -w net.ipv4.tcp_rmem='4096 87380 67108864' 2>/dev/null",
    ]
    for c in cmds:
        try:
            subprocess.run(c, shell=True, timeout=5)
        except Exception as e:
            logger.debug(f"操作失败: {e}")
    logger.info("网络参数已优化")


def _system_trim():
    services = [
        "php8.3-fpm", "php8.2-fpm", "php-fpm", "ModemManager", "multipathd",
        "walinuxagent", "udisks2", "getty@tty1", "docker", "containerd",
        "docker.socket", "snapd", "snapd.socket",
    ]
    for svc in services:
        try:
            subprocess.run(f"sudo systemctl stop {svc} 2>/dev/null", shell=True, timeout=10)
            subprocess.run(f"sudo systemctl disable {svc} 2>/dev/null", shell=True, timeout=10)
        except Exception as e:
            logger.debug(f"操作失败: {e}")
    logger.info("系统瘦身完成")


def _write_shell_profile():
    persist = config.FILES_DIR
    os.makedirs(persist, exist_ok=True)
    profile = (
        "# ghbox 云端终端配置\n"
        f"export LANG=C.UTF-8\nexport LC_ALL=C.UTF-8\n"
        f"export TERM=xterm-256color\n"
        r"export PS1='\[\e[32m\]kodebite@ghbox\[\e[0m\]:\[\e[34m\]\w\[\e[0m\]$ '"
        f"\ncd {persist} 2>/dev/null || true\n"
    )
    try:
        subprocess.run("sudo mkdir -p /root", shell=True, timeout=5)
        subprocess.run("sudo tee /root/.bashrc > /dev/null", shell=True, timeout=10,
                       input=profile.encode())
        subprocess.run("sudo tee /root/.bash_profile > /dev/null", shell=True, timeout=10,
                       input=b"source ~/.bashrc 2>/dev/null\n")
        home = os.path.expanduser("~")
        with open(os.path.join(home, ".bashrc"), "w") as f:
            f.write(profile)
        with open(os.path.join(home, ".bash_profile"), "w") as f:
            f.write("source ~/.bashrc 2>/dev/null\n")
        subprocess.run("sudo hostname ghbox 2>/dev/null || hostname ghbox 2>/dev/null",
                       shell=True, timeout=5)
    except Exception as e:
        logger.error(f"配置失败: {e}")


def _run_setup():
    setup = os.path.join(config.FILES_DIR, "setup.sh")
    if not os.path.exists(setup):
        return
    logger.info("执行 setup.sh...")
    try:
        subprocess.Popen(["bash", setup],
                         stdout=open("/tmp/setup.log", "w"),
                         stderr=subprocess.STDOUT,
                         start_new_session=True)
    except Exception as e:
        logger.error(f"失败: {e}")


def _sysbackup_loop_async():
    """系统配置定期备份（每10分钟）"""
    while True:
        time.sleep(600)
        try:
            syscfg.backup_system_config()
        except Exception as e:
            logger.error(f"失败: {e}")