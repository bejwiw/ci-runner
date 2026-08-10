# -*- coding: utf-8 -*-
"""
运行状态：启动时间、运行时长
"""
import time

_start_time = time.time()


def elapsed():
    """运行时长（秒）"""
    return int(time.time() - _start_time)


def started_at():
    return _start_time
