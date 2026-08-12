# -*- coding: utf-8 -*-
"""
MCP 服务管理（worker 侧）

修复旧项目 bug：不用 pgrep -f "node index.js" 批量杀进程，
改为记录 MCP 进程 PID，用 PID 精准杀，避免误杀用户的其他 node 项目。
"""
import os
import io
import time
import shutil
import tarfile
import threading
import subprocess
import signal

import config
import log
from core import releases
from worker.tunnel import TunnelManager

logger = log.setup_logger("mcp")

MCP_PORT = config.MCP_PORT
MCP_SERVER_DIR = os.path.join(config.FILES_DIR, "mcp-server")
MCP_FILES_DIR = os.path.join(config.FILES_DIR, "mcp-files")
MCP_PID_FILE = os.path.join(config.FILES_DIR, "mcp-server", ".pid")


class McpManager:
    def __init__(self, inst_cfg=None):
        self.inst_cfg = inst_cfg
        self.proc = None
        self.tunnel_mgr = None
        self.ready = False

    def _copy_server_code(self):
        """从解压目录复制 MCP 服务代码"""
        project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        mcp_src = os.path.join(project_dir, "worker", "mcp-server")
        if not os.path.isdir(mcp_src):
            # 尝试从 git 仓库根目录找
            mcp_src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp-server")
        if not os.path.isdir(mcp_src):
            logger.error("[mcp] 未找到 mcp-server 目录")
            return False
        os.makedirs(MCP_SERVER_DIR, exist_ok=True)
        for fname in ("index.js", "package.json"):
            src = os.path.join(mcp_src, fname)
            dst = os.path.join(MCP_SERVER_DIR, fname)
            if os.path.exists(src):
                shutil.copy2(src, dst)
        logger.info("[mcp] 服务代码已复制")
        return True

    def ensure_server(self):
        """确保 MCP 服务代码和依赖可用（每次检查代码是否最新）"""
        node_modules = os.path.join(MCP_SERVER_DIR, "node_modules")
        index_js = os.path.join(MCP_SERVER_DIR, "index.js")
        pkg_path = os.path.join(MCP_SERVER_DIR, "package.json")
        # 读取旧package.json
        old_pkg = ""
        if os.path.exists(pkg_path):
            try:
                with open(pkg_path) as f:
                    old_pkg = f.read()
            except Exception:
                pass
        # 每次复制最新代码（幂等）
        self._copy_server_code()
        # 比较package.json是否变了
        new_pkg = ""
        if os.path.exists(pkg_path):
            try:
                with open(pkg_path) as f:
                    new_pkg = f.read()
            except Exception:
                pass
        pkg_changed = old_pkg != new_pkg
        if pkg_changed and os.path.isdir(node_modules):
            # package.json变了，删除旧node_modules强制重装
            shutil.rmtree(node_modules, ignore_errors=True)
            logger.info("[mcp] package.json 变更，重新安装依赖")
        if os.path.isdir(node_modules) and os.path.exists(index_js) and not pkg_changed:
            return True
        # 尝试从 Releases 下载预编译依赖
        try:
            blob = releases.download_chunked("mcp-deps.tar.gz.enc",
                                             token=config.GH_TOKEN, repo=config.MAIN_REPO)
            if blob:
                with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
                    try:
                        tar.extractall(path=MCP_SERVER_DIR, filter="tar")
                    except TypeError:
                        tar.extractall(path=MCP_SERVER_DIR)
                if os.path.isdir(node_modules):
                    logger.info("[mcp] 预编译依赖已恢复")
                    return True
        except Exception as e:
            logger.warning(f"[mcp] 下载依赖失败: {e}")
        # npm install
        try:
            r = subprocess.run(
                ["npm", "install", "--no-audit", "--no-fund", "--loglevel=warn"],
                cwd=MCP_SERVER_DIR, capture_output=True, text=True, timeout=300)
            if r.returncode == 0:
                logger.info("[mcp] npm install 完成")
                # 安装 Playwright 浏览器
                cloak_cache = os.path.expanduser("~/.cloakbrowser/")
                if not os.path.isdir(cloak_cache) or not os.listdir(cloak_cache):
                    logger.info("[mcp] 安装 CloakBrowser 二进制...")
                    subprocess.run(
                        ["sudo", "-E", "npx", "cloakbrowser", "install"],
                        cwd=MCP_SERVER_DIR, capture_output=True, text=True, timeout=600)
                    logger.info("[mcp] 安装系统依赖...")
                    subprocess.run(
                        ["sudo", "-E", "npx", "playwright-core", "install-deps", "chromium"],
                        cwd=MCP_SERVER_DIR, capture_output=True, text=True, timeout=300)
                    logger.info("[mcp] CloakBrowser 安装完成")
                    # Bug2修复：chmod让chrome GPU子进程能访问二进制目录
                    if os.path.isdir(cloak_cache):
                        subprocess.run(["sudo", "chmod", "-R", "755", cloak_cache], timeout=10)
                        subprocess.run(["sudo", "chmod", "755", os.path.expanduser("~")], timeout=10)
                        logger.info("[mcp] CloakBrowser 权限已修复")
                return os.path.isdir(node_modules)
            logger.error(f"[mcp] npm install 失败: {r.stderr[:300]}")
        except subprocess.TimeoutExpired:
            logger.error("[mcp] npm install 超时")
        except Exception as e:
            logger.error(f"[mcp] npm install 异常: {e}")
        return False

    def _kill_old_mcp(self):
        """用 PID 精准杀旧 MCP 进程（修复：不用 pgrep 批量杀）"""
        if not os.path.exists(MCP_PID_FILE):
            return
        try:
            with open(MCP_PID_FILE) as f:
                old_pid = int(f.read().strip())
            # 检查这个 PID 是否还是 node index.js
            if os.path.exists(f"/proc/{old_pid}/cmdline"):
                with open(f"/proc/{old_pid}/cmdline", "rb") as f:
                    cmd = f.read().replace(b"\x00", b" ").decode(errors="replace")
                if "node" in cmd and "index.js" in cmd:
                    os.kill(old_pid, signal.SIGKILL)
                    logger.info(f"[mcp] 已清理旧 MCP 进程 (pid={old_pid})")
                    time.sleep(0.5)
        except (ValueError, FileNotFoundError, ProcessLookupError, PermissionError):
            pass
        finally:
            try:
                os.remove(MCP_PID_FILE)
            except Exception:
                pass

    def _get_mcp_tunnel_token(self):
        if self.inst_cfg and self.inst_cfg.raw:
            return self.inst_cfg.raw.get("mcp_tunnel_token", "")
        return ""

    def _get_mcp_hostname(self):
        if self.inst_cfg and self.inst_cfg.raw:
            host = self.inst_cfg.raw.get("mcp_hostname", "")
            if host:
                return host
        if self.inst_cfg and self.inst_cfg.tunnel_host:
            return f"mcp-{self.inst_cfg.tunnel_host}"
        return f"mcp-{config.TUNNEL_HOST}"

    def _start_tunnel(self):
        token = self._get_mcp_tunnel_token()
        host = self._get_mcp_hostname()
        if not token:
            return False

        class _Cfg:
            def __init__(self, t, h):
                self.tunnel_token = t
                self.tunnel_host = h

        self.tunnel_mgr = TunnelManager(_Cfg(token, host))
        self.tunnel_mgr.start_async()
        logger.info(f"[mcp] MCP 隧道: https://{host}")
        return True

    def start(self):
        if not self.ensure_server():
            logger.error("[mcp] 依赖准备失败")
            return False
        self._start_tunnel()
        self._kill_old_mcp()
        env = os.environ.copy()
        env["MCP_PORT"] = str(MCP_PORT)
        env["MCP_HOST"] = "0.0.0.0"
        env["MCP_FILES_DIR"] = MCP_FILES_DIR
        mcp_host = self._get_mcp_hostname()
        env["MCP_BASE_URL"] = f"https://{mcp_host}"
        env["EXEC_TOKEN"] = config.EXEC_TOKEN
        env["HOME"] = os.path.expanduser("~")
        os.makedirs(MCP_FILES_DIR, exist_ok=True)
        try:
            self.proc = subprocess.Popen(
                ["sudo", "-E", "node", "index.js"],
                cwd=MCP_SERVER_DIR, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                start_new_session=True)
            # 记录 PID 到文件（修复：用 PID 精准杀，不用 pgrep）
            try:
                with open(MCP_PID_FILE, "w") as f:
                    f.write(str(self.proc.pid))
            except Exception:
                pass
            threading.Thread(target=self._read_output, daemon=True).start()
            self.ready = True
            logger.info(f"[mcp] 服务已启动 (pid={self.proc.pid}, port={MCP_PORT})")
            return True
        except Exception as e:
            logger.error(f"[mcp] 启动失败: {e}")
            return False

    def _read_output(self):
        if not self.proc:
            return
        try:
            for line in self.proc.stdout:
                if line.strip():
                    logger.info(f"[mcp:node] {line.strip()[:300]}")
        except Exception:
            pass

    def stop(self):
        if self.proc:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
                time.sleep(1)
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            except Exception:
                pass
        self.proc = None
        if self.tunnel_mgr:
            self.tunnel_mgr.stop()
        self.ready = False
        try:
            os.remove(MCP_PID_FILE)
        except Exception:
            pass
        logger.info("[mcp] 服务已停止")

    def status(self):
        alive = bool(self.proc and self.proc.poll() is None)
        return {
            "ready": self.ready, "running": alive,
            "pid": self.proc.pid if alive else None,
            "port": MCP_PORT, "hostname": self._get_mcp_hostname(),
        }
