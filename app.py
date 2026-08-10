# -*- coding: utf-8 -*-
"""
ghbox 统一入口：按 INSTANCE_ROLE 启动 manager / worker
"""
import config


def main():
    if config.ROLE == "manager":
        from manager.app import run
        run()
    else:
        from worker.app import run
        run()


if __name__ == "__main__":
    main()
