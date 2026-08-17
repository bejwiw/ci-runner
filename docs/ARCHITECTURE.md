# ghbox 架构设计（2026-08-17 更新版）

## 一句话
把 GitHub Actions 免费临时虚拟机（4核/15G/12Gbps，6小时销毁）当"免费云服务器"用，
靠 S3(Tigris) 持久化 + 到期前自动续命 + 进程持久化实现永续在线。

## 架构

```
Manager（ghvps2.kekeke.cc.cd，主账号仓库 qqztceghrgji/ci-runner）
  ├─ 管理实例/账号/任务/隧道/监控/自愈
  ├─ 自动续命 + 自动更新 + 健康巡检（3次失败自动重启）
  └─ Leader 锁（Release 后端，防多 manager 并行写库）

Worker × N（inst{N}.kekeke.cc.cd）
  ├─ Flask API + WSS 终端 + 进程持久化 + 数据备份恢复
  ├─ Cloudflare 隧道（固定域名 + 项目子隧道，公网可达）
  ├─ MCP 服务（独立隧道 mcp-inst{N}.kekeke.cc.cd）
  ├─ 续命（到期前 PRE_WAKE_SECONDS=21300s 预触发下一个 worker）
  └─ run_id 上报（GITHUB_RUN_ID → manager，保证 close 能取消 run）
```

## 存储（单一全量备份架构）

| 数据 | 存储 | S3路径 | 说明 |
|------|------|--------|------|
| 实例清单 | S3 账号0 | `meta/instances.json` | 纯内存 + S3 持久化 |
| 账号配置 | S3 账号0 | `meta/accounts.json` | |
| 任务队列 | S3 账号0 | `meta/tasks.json` | |
| 实例配置 | S3 账号0 | `meta/inst-config/{id}.json` | 含 tunnel_token 等 |
| S3计数器 | S3 账号0 | `meta/s3-counters-{owner}.json` | 按月重置A/B |
| 数据库 | S3 哈希账号 | `inst-data/{id}/db` | 不加密（S3私有） |
| 文件备份 | S3 哈希账号 | `inst-files/{id}/files.tar.gz` | zstd压缩，不加密 |

**备份只有一种：全量备份**（2060行无歧义）
- `backup_database()`：demo.db → S3 + Releases 双写
- `backup_files()`：`tar --zstd` 打包整个 /home/kodebite/ → S3（>=50MB分片并发）+ Releases 双写（<50MB）
- 触发时机：每180秒自动（_backup_loop）、手动(/api/backup/now)、到期前强制、优雅关闭前、SIGTERM
- Releases 为加密降级（AES-256-GCM），S3 不可达时恢复用

**进程快照（snapshot）已不是文件备份**（Bug 6 修复后）
- 只记录元数据（进程名/PID/command/cwd/大小）到 `processes/manifest.json`
- 用于进程列表展示、崩溃恢复判断
- 文件备份完全由全量备份负责，无重叠

## S3 多账号方案（2264个Tigris账号）

- 账号0 (kkk001桶) 存元数据 + 账号列表 `meta/s3-accounts.txt`
- 账号1~2263 用一致性哈希（HashRing，150虚拟节点/账号）分散存实例数据
- **put 不做 fallback**（只写哈希环指定桶）→ put/get 永远对称，杜绝"写桶A读桶B"数据分裂
- used_bytes 用 put 差值维护（覆盖写 = 新大小-旧大小），delete 回扣
- 计数器持久化，重启恢复；`_refresh_storage_sizes` 后台刷新（只查 used_bytes>0 的桶，避免跨 owner 污染）
- 月度自动重置 A/B 计数器（每账号 1万 PUT / 10万 GET 免费额度，2264桶分摊远用不完）

## 文件结构

```
ghbox-new/
├── app.py              # 统一入口（按 ROLE 分发）
├── config.py           # 全局配置 + InstanceConfig
├── log.py              # 日志系统（环形缓冲+文件+请求日志）
├── core/
│   ├── s3.py           # S3 多账号存储池（S3Pool + _S3Client + HashRing）
│   ├── hashring.py     # 一致性哈希环
│   ├── releases.py     # GitHub Releases 加密降级存储
│   ├── crypto.py       # AES-256-GCM 加密（nonce12+tag16）
│   ├── ghapi.py        # GitHub API 封装（连接池+重试）
│   ├── lock.py         # Leader 锁
│   ├── status.py       # 运行状态
│   └── utils.py        # 工具函数
├── manager/
│   ├── app.py          # Flask路由+认证+s3_status+worker_stats+心跳
│   ├── api_instances.py # 实例/账号/任务API（create/close/restart/report）
│   ├── background.py   # 后台线程（自愈/隧道/MCP补创建）
│   ├── store.py        # 实例清单 + purge_instance_data（关闭清数据）
│   ├── accounts.py     # 多账号管理
│   ├── tunnels.py      # CF隧道CRUD
│   ├── monitor.py      # 健康巡检（3次失败重启）
│   ├── tasks.py        # 任务队列
│   └── state.py        # 全局状态
├── worker/
│   ├── app.py          # Flask路由+WSS终端+exec+backup+shutdown
│   ├── boot.py         # 延迟初始化（恢复→备份→进程→MCP→Leader）
│   ├── loops.py        # 后台循环（备份/上报run_id/续命重试/更新/磁盘）
│   ├── persistence.py  # 数据/文件持久化（S3主+Releases降级，检查返回值）
│   ├── tunnel.py       # 主隧道管理
│   ├── terminal.py     # WSS PTY 终端
│   ├── mcp.py          # MCP服务（runner用户启动，PID精准杀）
│   ├── attack.py       # 攻击功能
│   ├── sysconfig.py    # 系统配置
│   ├── state.py        # 全局状态
│   └── process/
│       ├── config.py   # 进程配置（PID/配置在项目目录，不依赖processes/）
│       ├── backup.py   # 进程元数据快照（仅manifest）
│       ├── restore.py  # 进程恢复（install_deps 180s超时，无需sudo）
│       ├── manager.py  # ProcessManager（崩溃恢复/隧道管理）
│       ├── tunnels.py  # 项目隧道（孤儿清理：tunnel_id/完整token匹配）
│       └── api.py      # 进程API Blueprint（adopt/stop/start/restart/log）
├── worker/mcp-server/  # MCP 服务（Node.js，/api/terminal 等）
├── .github/workflows/  # manager.yml（concurrency cancel-in-progress）+ worker.yml
├── requirements.txt    # 依赖（含boto3）
├── tests/              # 130个单元测试
└── docs/               # 文档
```

## 权限模型（重要）

- **MCP 服务 / exec API / install_deps / start_process 全部以 runner 用户运行**（不用 sudo）
- 需要 root 的操作在命令本身加 sudo（runner 有免密 sudo）
- 这保证"装依赖的用户 == 启动进程的用户"，依赖不会装错环境

## 隧道管理（重点）

- 主隧道：worker 启动时 TunnelManager 用 token 启动（inst{N}.kekeke.cc.cd）
- 项目隧道：ProcessManager.start_tunnels 启动（token 或 credentials 方式），带 ingress
- 项目隧道启动前 `_kill_orphan_tunnels` 清理孤儿进程：
  - **只按 tunnel_id（明文）或完整 token 匹配，绝不按 token 前缀**（同一 account 下 token 前缀相同，会误杀主隧道）
- monitor_loop 每180秒 `check_and_restart` 检测隧道崩溃并重启

## 实例生命周期

```
创建 → 实例配置存S3+Releases → dispatch worker run → worker上报run_id
  → 运行期：每180秒备份 + 每60秒上报 → 关闭：
    close API → cancel run（用最新run_id）→ 删隧道 → purge数据（异步）→ 清heartbeat → closed=true
  → 恢复：worker从S3读配置 → load_or_create恢复数据 → restore_all恢复进程 → 全量备份
```

## 已修复的 50+ 个 Bug（2026-08 历次会话累计）

### 数据完整性（本轮重点）
1. S3 计数器虚高 → put() 覆盖写算差值（新-旧），不再无脑累加
2. put/get 换桶不对称 → 去掉 _put_fallback，只写原桶
3. backup_files 不检查返回值 → 检查 put_file / upload_chunked
4. _S3Client.put() 永远 True → 检查 put_object 返回的 ETag
5. 分片计数器虚高 → _put_file_chunked 同样差值
6. _refresh_storage_sizes 只查20个桶 → 后台线程 + 只查 used_bytes>0
7. list_objects_v2 漏超1000对象 → ContinuationToken 分页
8. load_state 不恢复 used_bytes 导致归零 → 改回恢复（put已精确）

### 进程持久化
9. PID/配置依赖 processes/（恢复时被删）→ 改到项目目录
10. 全量备份和进程快照重叠互相覆盖 → 去掉进程快照文件，只有全量备份
11. restore_all 旧格式迁移逻辑 → 删除
12. 进程日志文件名不匹配 → read_process_log_file 按完整路径

### 实例生命周期
13. run_id 不更新（close 无法取消）→ worker 上报 GITHUB_RUN_ID
14. close_instance 不检查取消结果 → 检查状态码
15. _trigger_worker 误取其他 workflow 的 run → 过滤 worker workflow
16. 关闭实例不清数据 → purge_instance_data（单文件+分片+manifest）
17. s3_status 统计含已关闭实例 → 只统计活跃 + close 时清 heartbeat
18. 续命失败无重试 → 3次重试

### 安全/权限
19. MCP 服务用 sudo 启动 → runner 用户
20. exec API 用 sudo -n → 直接 runner 执行
21. 隧道孤儿清理用 token 前缀 → 误杀主隧道！改 tunnel_id/完整token
22. hashring get_nearby_excluding set.append → 修复

### 其他
23. worker_stats_api jsonify 重复参数 → pop 后再展开
24. 优雅关闭/信号处理重复备份 → final_snapshot 去重并返回结果
25. backup_files parts 未定义（UnboundLocalError）→ 修复
26. saved_at 每次扫描覆盖 → setdefault
27. scanner 排除/sudo/deps 等旧问题

## 测试（2026-08-17 130个全过）

| 测试文件 | 覆盖 |
|----------|------|
| test_s3.py | 账号解析/并发/常量/get_to_file优先单文件 |
| test_purge.py | purge单文件/分片/locations兜底/Releases分片 |
| test_tunnel_cleanup.py | token解码/孤儿清理不误杀 |
| test_backup_files.py | 小文件/大文件/S3失败分支 |
| test_hashring.py | 哈希环/get_nearby_excluding |
| test_bugfix.py | 计数器差值/put不fallback/PID路径/ETag |
| test_process_config.py / test_persistence.py 等 | 配置/持久化/锁/日志/任务 |

## 线上验证（2026-08-17）

- 备份循环：创建文件→备份→S3实际下载解压验证→重启→文件恢复 ✅
- 新项目托管：simple-web adopt→运行→重启→自动恢复 ✅
- 隧道崩溃恢复：kill cloudflared→monitor自动拉起→公网可达 ✅
- 优雅关闭：/api/shutdown→备份退出→manager自动拉起新run ✅
- 当前实例：inst3（simple-web/ddg-search/gost-proxy/pinglab 4进程）
- S3：2264账号全活跃，总量 ~36MB（准确无虚高）