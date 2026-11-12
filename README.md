# Robot Monitor（云端）

让一台**没有公网 IP** 的机器把指定文件传到**有公网 IP** 的服务器上，然后用浏览器
浏览、下载、管理这些文件。不装隧道、不做端口映射、不在机器上开入站端口：
**机器永远是连接的发起方**。

机器侧的实现在另一个仓库：`robot-backend/app/cloud_agent`（`python -m app.cloud_agent`）。
本仓库只剩云端 server 与它的前端。

```
        ┌──────────────────── 云端服务器 ────────────────────┐
浏览器 ─│  FastAPI: /api/*  /api/events(SSE)  /ws/agent       │
        │  Vue 3 静态产物（由 FastAPI 托管）                   │
        │  SQLite + storage/{staging,files}                    │
        └───────▲──────────────────────────▲──────────────────┘
                │ ① WSS 控制通道            │ ② HTTPS 分片上传
                │（机器主动拨出，常驻）      │（机器主动发起）
        ┌───────┴──────────────────────────┴──────────────────┐
        │        机器人工控机（内网，只读源，无公网 IP）        │
        │  cloud-agent：路径沙箱 + 指令分发 + 分片上传         │
        └─────────────────────────────────────────────────────┘
```

## 目录

```
server/            云端，一个项目、两个技术栈
  backend/         uv + FastAPI（CLI：serve / passwd / pending / approve …）
  frontend/        Vue 3 + JS + Vite + Element Plus（产物由 backend 托管）
contract/          协议黄金样例（JSON 夹具，只被测试读取）
deploy/            systemd、Nginx 反代、env 模板、备份脚本
docs/              部署手册、设计说明
dev.sh             统一入口（server 侧）
```

## 协议怎么保持一致

`protocol.py` 只有 server 这一份；机器侧 `app/cloud_agent/protocol.py` 是它的副本。
两边都不抽共享包，靠 `contract/protocol-frames.json` 的黄金样例兜住漂移：

- 两端测试都解析并重新序列化同一批帧，结果必须逐字段一致；
- 改协议的顺序：先改 `contract/protocol-frames.json`，再同步两份实现。

## 快速开始（本地开发）

```bash
./dev.sh sync                      # 装 server 依赖
./dev.sh serve --reload            # 终端 1：首次会提示设置管理员密码（输两次）
./dev.sh web                       # 终端 2：前端 5173（proxy 到 8000）
```

机器侧在 `robot-backend` 仓库：

```bash
.venv/bin/python -m app.cloud_agent \
  --server http://10.0.0.5:8000 --root /srv/share
```

打开 <http://127.0.0.1:5173>，用 `admin` + 刚才设的密码登录。

之后再启动，参数都可以省（server 与 cloud-agent 都会记住凭据）。

日常运维：

```bash
./dev.sh passwd                    # 改密码（立即生效，所有已登录会话失效）
./dev.sh pending                   # 看有没有待批准的新设备
./dev.sh approve <id>              # 命令行批准（网页「节点」页也能点）
./dev.sh list-agents
./dev.sh revoke-token <agent_id>
```

> **任何新增设备都要你批准**（默认 `ROBOT_MONITOR_ENROLL_POLICY=closed`，第一台也一样）。
> 机器启动后会在「节点」页顶出「待批准接入」表格，点 **批准** 即可。

生产构建：`./dev.sh build` → `server/frontend/dist` 由 FastAPI 直接托管，
服务器上不需要 Node。

### 测试

```bash
./dev.sh test                      # server 全量测试
./dev.sh lint
```

端到端测试会真的起 uvicorn、跑一个**协议级测试替身**（`tests/support/mini_agent.py`）、
真的分片上传，验证 sha256、Range 下载、删除和 SSE。替身只实现 server 需要的那部分
协议，不依赖任何外部项目；机器侧自身的测试在 `robot-backend/tests/cloud_agent/`。

## 生产部署

> 逐条命令、验收清单、故障排查表在 **[docs/部署手册.md](docs/部署手册.md)**。
> 设计取舍在 **[docs/设计说明.md](docs/设计说明.md)**。

只需要准备一个目录：`server/`（backend 源码 + `frontend/dist`，合计约 2MB）。
`agent/`、`contract/`、`docs/`、`dev.sh` 都不上服务器。

对外只需要 **443**：页面、`/api/*`、`/api/events`、`/ws/agent`、`/internal/*` 共用同一个
端口，靠路径区分；80 只做 ACME 与跳转。uvicorn 绑 `127.0.0.1:8000`，不对公网开放。
机器侧不需要任何入站端口。

机器侧的部署（`cloud-agent`）见 `robot-backend/deploy/README.md`。

## 工作方式（协议层面）

一次取件的完整时序：

```
C: POST /api/v1/tasks {agent_id, path}
A: 建 task → stat_file 到机器（10s 超时）→ 库里已有同一份就跳过，不传
A: 否则 磁盘预检 → 建 upload + 预分配 staging
A: start_upload ──▶ 机器立刻 ack，真正传输在它的后台任务里
机器: PUT /internal/uploads/{id}/chunks/{n}  × N   （3 并发，幂等，可重传）
机器: POST /internal/uploads/{id}/complete {sha256}
A: 重算 sha256 → 判重/替换/落盘 → files 行 → task done → SSE 推给 C
```

**进度的唯一真相在 A**：A 知道自己收到第几块，不需要机器上报进度。机器只在失败时
通过 WS 发一个 `upload_failed`。

**取一整个目录**：展开由 A 做（`list_dir` 逐层遍历，上限 5000 文件 / 32 层 / 30s），
先返回"将取 128 个文件 / 3.2 GB"让用户确认，再按清单逐项建任务（后台并发 7）。

**机器重启后能续传**：机器一连上，A 在 welcome 帧里带上"你还有这些 upload 没传完"，
它从第一个缺失块继续。块是幂等的，重复上传只会多花一点流量。

## 不变量（改代码前先看这几条）

| 不变量 | 为什么 |
|---|---|
| 只有 send loop 能碰 socket | 并发 send 会交错帧，失败是间歇性的 |
| 每个请求一个 `id` + Future + 超时 | 没有超时，机器不回包就永久泄漏 |
| 指令队列有界且阻塞，遥测满了直接丢 | 阻塞防丢指令，有界防 OOM |
| A 也校验路径（双层） | A 是唯一暴露面，不能只靠机器侧兜底 |
| 下载只认 `file_id`，不认路径 | A 侧不存在路径穿越面 |
| token/密码比较用 `compare_digest` | `==` 的时序差可被逐字符还原 |
| 服务端重算 sha256 | 信客户端报的哈希等于没校验 |
| uvicorn 单 worker | 连接注册表在进程内存 |

## 明确不做

- 机器上的删除/重命名/写入——机器是只读源，能力面越小越安全
- 任何命令执行——从设计上消除 RCE 面
- 多租户、配额计费、秒传去重
- 多 A worker 横向扩展（要做先换掉进程内注册表）

## 状态

v1 已完成并部署验证（M0–M9）。机器侧 agent 已迁移到 `robot-backend/app/cloud_agent`，
本仓库不再包含 B 端代码。
