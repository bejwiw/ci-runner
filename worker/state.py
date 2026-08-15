# -*- coding: utf-8 -*-
"""Worker 全局状态"""
inst_cfg = None
load_status = "初始化中"
leader = None
proc_mgr = None
tunnel_mgr = None
mcp_mgr = None
s3pool = None
_sid_to_key = {}
shutting_down = False  # 优雅关闭标志，设为 True 后停止上报和备份循环
