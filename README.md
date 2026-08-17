# ghbox

把 GitHub Actions 免费临时虚拟机（4核/15G/12Gbps，6小时到期）当"免费云服务器"用：S3 持久化 + 到期自动续命 + 进程持久化，实现永续在线。

## 快速开始

```bash
# 启动 manager（或 worker）
python3 app.py

# 依赖
pip install -r requirements.txt
```

## 运行角色（环境变量 INSTANCE_ROLE）

| 角色 | 入口 | 职责 |
|------|------|------|
| `manager` | app.py | 实例/账号/隧道/任务管理，健康巡检，自愈续命 |
| `worker` | app.py + INSTANCE_ID | 进程托管、数据备份、WSS终端、MCP服务 |

## 核心文档

- [架构设计](docs/ARCHITECTURE.md) — 存储/备份/隧道/权限模型、50+ Bug 修复记录
- [交接文档](docs/HANDOVER.md) — 账号/凭证/部署流程

## 核心设计

- **备份**：单一全量备份（`tar --zstd` 整个数据目录 → S3 主 + Releases 加密降级），每180秒自动
- **持久化**：进程在到期续命后自动从备份恢复，零人工
- **隧道**：Cloudflare 固定域名，主隧道 + 项目子隧道，崩溃自动恢复
- **安全**：全部以 runner 用户运行，密钥走 GitHub Secrets

## 测试

```bash
python3 -m pytest tests/ -q   # 130 tests
```

## 当前在线（2026-08-17）

- Manager: `https://ghvps2.kekeke.cc.cd`
- Worker inst3: `https://inst3.kekeke.cc.cd`（simple-web / ddg-search / gost-proxy / pinglab）
- S3: 2264 账号全活跃