# -*- coding: utf-8 -*-
"""
Cloudflare 隧道管理（worker 侧）

- 启动固定域名隧道
- 连接注册检测
- 异步启动（不阻塞主流程）
"""
import threading
import subprocess

import config
import log

logger = log.setup_logger("tunnel")


class TunnelManager:
    def __init__(self, inst_cfg=None):
        self.inst_cfg = inst_cfg
        self.proc = None
        self.registered = False
        self.url = ""

    def _get_token(self):
        if self.inst_cfg and self.inst_cfg.tunnel_token:
            return self.inst_cfg.tunnel_token
        return config.TUNNEL_TOKEN

    def _get_host(self):
        if self.inst_cfg and self.inst_cfg.tunnel_host:
            return self.inst_cfg.tunnel_host
        return config.TUNNEL_HOST

    def start(self):
        token = self._get_token()
        host = self._get_host()
        if not token:
            logger.warning("无 token，跳过")
            return False
        self.url = f"https://{host}"
        try:
            self.proc = subprocess.Popen(
                ["cloudflared", "tunnel", "--no-autoupdate", "run", "--token", token],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            logger.info(f"启动: {self.url} (pid={self.proc.pid})")
            # 等待连接注册（最长120秒），失败则退出（让run结束，日志可见）
            import time as _t
            deadline = _t.time() + 120
            while _t.time() < deadline:
                line = self.proc.stdout.readline()
                if not line:
                    break
                text = line.strip()
                if "Registered tunnel connection" in text:
                    self.registered = True
                    logger.info("连接已注册")
                    # 继续读取输出（保持管道不堵塞），但不再阻塞主流程
                    for l in self.proc.stdout:
                        if "Registered tunnel connection" in l.strip():
                            pass
                    return True
                if "ERR" in text or "error" in text.lower():
                    logger.warning(f"cloudflared: {text[:200]}")
            if not self.registered:
                logger.error(f"CF 隧道连接失败（120秒超时），退出实例")
                try:
                    self.proc.kill()
                except Exception:
                    pass
                return False
            return True
        except Exception as e:
            logger.error(f"启动失败: {e}")
            return False

    def start_async(self):
        threading.Thread(target=self.start, daemon=True).start()

    def stop(self):
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.kill()
            except Exception as e:
                logger.debug(f"stop异常: {e}")
        self.proc = None
        self.registered = False
