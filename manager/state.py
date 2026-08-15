# -*- coding: utf-8 -*-
"""Manager 全局状态（跨模块共享）"""
leader = None
s3pool = None
worker_heartbeats = {}
shutdown_notifications = {}  # {inst_id: timestamp} 实例优雅关闭备份完成通知
