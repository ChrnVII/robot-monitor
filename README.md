# FileBridge

让一台**没有公网 IP** 的机器（B）把指定文件传到**有公网 IP** 的服务器（A）上，
然后用浏览器（C）在 A 上浏览、下载、管理这些文件。

不装隧道、不做端口映射、不在 B 上开端口：**B 永远是连接的发起方**。

```
        ┌──────────────────── A（公网）────────────────────┐
 C ──── │  FastAPI: /api/*  /api/events(SSE)  /ws/agent   │
（浏览器）│  Vue 3 静态产物（由 FastAPI 托管）               │
        │  SQLite + storage/{staging,files,trash}          │
        └───────▲──────────────────────────▲───────────────┘
                │ ① WSS 控制通道            │ ② HTTPS 分片上传
                │（B 主动拨出，常驻）        │（B 主动发起）
        ┌───────┴──────────────────────────┴───────────────┐
        │           B（内网，只读源，无公网 IP）             │
        │   agent：路径沙箱 + 指令分发 + 分片上传            │
        └──────────────────────────────────────────────────┘
```

## 目录

```
server/             A 端，一个项目、两个技术栈
  backend/          uv + FastAPI（CLI：serve / passwd / pending / approve …）
    src/filebridge_server/
    tests/          单元测试 + 真实 uvicorn/agent 的端到端测试
  frontend/         Vue 3 + JS + Vite + Element Plus
    src/{views,layout,stores,api,composables}

agent/              B 端，独立项目（uv + Python），只读
  src/filebridge_agent/
    protocol.py     线上协议（与 server 那份逐字节相同）
  tests/            沙箱逃逸矩阵、分页、续传、只读不变量扫描

contract/           协议黄金样例（JSON 夹具，只被测试读取，不进任何构建产物）
deploy/             systemd unit、Nginx 反代、env 模板、备份脚本
docs/               调研笔记与实施计划书
dev.sh              统一入口
```

两个 Python 项目**完全独立**：各自 `uv sync`、各自的 `.venv` 与 `uv.lock`、
没有 workspace、没有 path 依赖，可以单独 clone、单独 build、单独部署。

## 协议怎么共享（没有共享包）

`protocol.py` 在两个项目里各有一份，内容必须**逐字节相同**。不抽成第三个包，是
因为它只有 200 行，而抽出去要付三个具体成本：两个 pyproject 都写 path 依赖、
给 B 装 agent 得装两个 wheel、改一行注释要动第三个项目。

复制唯一的风险是漂移，用两道测试兜住（两端都跑）：

1. `test_protocol_copy_is_identical`——逐字节比对两份源码。改一边忘了另一边，立刻红。
2. `test_golden_frames_match`——两端都读 `contract/protocol-frames.json`，解析与
   序列化结果必须与样例一致。两边都改错但改得不一样，也红。

改协议的顺序：先改 `contract/protocol-frames.json`，再改两份 `protocol.py`，
最后跑 `./dev.sh test`。

## 快速开始（本地开发）

```bash
./dev.sh sync                      # 两个项目装依赖（并把 agent 链进 backend 供端到端测试）

./dev.sh serve --reload            # 终端 1：A 端。首次会提示设置管理员密码（输两次）
./dev.sh web                       # 终端 2：前端 5173（proxy 到 8000）

./dev.sh agent --server http://10.0.0.5:8000 --root /srv/share
#                                  # 终端 3：B 端。首次会自动注册，token 落到本地
```

打开 <http://127.0.0.1:5173>，用 `admin` + 刚才设的密码登录。

**没有手工步骤**：密码不用算哈希、token 不用复制粘贴。凭据都存在该存的地方——
密码在 `data/filebridge.db`，agent 的 token 在项目内 `agent/.filebridge/agent.json`（0600，已 gitignore；wheel 安装时在 `~/.local/state/filebridge/`）。

之后再启动，参数都可以省：

```bash
./dev.sh serve                     # 不再问密码
./dev.sh agent                     # 从本地凭据读 server / token / root
```

日常运维：

```bash
./dev.sh passwd                    # 改密码（立即生效，所有已登录会话失效）
./dev.sh pending                   # 看有没有待批准的新设备
./dev.sh approve <id>              # 批准（第一台之后需要，除非 policy=open）
./dev.sh list-agents
./dev.sh revoke-token <agent_id>
```

> 新增设备默认要人工批准一次（`FILEBRIDGE_ENROLL_POLICY=first`）。内网完全可信的话，
> 设 `FILEBRIDGE_ENROLL_POLICY=open` 就是"填 IP 即接入"。

生产构建：`./dev.sh build` → `server/frontend/dist` 由 FastAPI 直接托管，
服务器上不需要 Node。

### 测试

```bash
./dev.sh test      # protocol + agent + backend 全跑
./dev.sh lint
```

端到端测试会真的起 uvicorn、真的跑一个 agent、真的分片上传，验证 sha256、
Range 下载、回收站和 SSE。它比单元测试慢，但这类流程错一步就是线上故障。

## 生产部署

> 逐条命令、验收清单、故障排查表在 **[docs/部署手册.md](docs/部署手册.md)**。
> 下面只是流程概要。

### 先看清单：只需要两个目录

| 目标 | 拷什么 | 不用拷 |
|---|---|---|
| **A（服务器）** | `server/`（backend 源码 + `frontend/dist` 构建产物，合计约 2MB） | `agent/`、`contract/`、`docs/`、`dev.sh` |
| **B（内网机器）** | `agent/` 打出来的**一个** wheel | 其余全部 |

两边都是自包含的，没有跨目录引用。这一点有实测兜底：把 `server/` 单独拷进一个
干净目录，`uv sync` 能成功、服务能起来、页面能打开；`pytest` 里需要 agent 的
那组测试会自动跳过（2 项），不会因为缺少 agent 而报错。

`deploy/` 不参与"部署"：它是配置参考，按需把 systemd unit、Nginx 站点、env 模板
`cp` 到 `/etc` 下。`docs/` 是设计文档，`contract/` 是测试夹具，都不上机器。

### A（服务器）

```bash
# 0. 本机构建前端产物（服务器不需要 Node）
cd server/frontend && npm ci && npm run build

# 1. 整个 server/ 拷到 /opt/filebridge：
#      /opt/filebridge/backend/     后端源码 + .venv
#      /opt/filebridge/frontend/    含 dist
sudo mkdir -p /opt/filebridge /var/lib/filebridge/{data,storage} /etc/filebridge
sudo useradd -r -s /usr/sbin/nologin filebridge
sudo chown -R filebridge:filebridge /var/lib/filebridge
sudo rsync -a --exclude node_modules --exclude .venv server/ /opt/filebridge/

# 2. 后端依赖（server/ 自包含，不需要 agent 目录）
sudo -u filebridge uv sync --project /opt/filebridge/backend --frozen

# 3. 配置（这里没有密码，密码在首次启动时设置）
sudo install -m 640 -o root -g filebridge deploy/filebridge.env.example /etc/filebridge/filebridge.env

# 4. 服务
sudo cp deploy/systemd/filebridge-server.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now filebridge-server

# 首次启动没有终端，会自动生成一个随机密码：
sudo cat /var/lib/filebridge/data/initial-password    # 登录一次后该文件自动删除
# 想立刻换成自己的：sudo -u filebridge .../filebridge-server passwd

# 5. Nginx + TLS
sudo cp deploy/nginx/filebridge.conf /etc/nginx/conf.d/
sudo certbot --nginx -d files.example.com
```

### 端口：严格说只需要 443

| 端口 | 谁能访问 | 用途 |
|---|---|---|
| **443/tcp** | 公网（C 的浏览器 + B 的 agent） | 全部业务：页面、`/api/*`、`/api/events`（SSE）、`/ws/agent`（控制通道）、`/internal/*`（分片上传） |
| **80/tcp** | 公网 | 只做 ACME 证书验证 + 301 跳到 443。没有域名/证书时可以先只开它，但不建议长期 |
| 8000/tcp | **只有本机** | uvicorn，绑 `127.0.0.1`，绝不对公网开放 |

控制通道和上传通道**共用一个 443**，靠路径区分：`/ws/agent` 升级成 WebSocket，
`/internal/*` 是普通 HTTP。这是"控制走 WS、数据走 HTTP"的附带好处——不用为上传
再开端口、再配一张证书、再多一条防火墙规则。

```bash
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp

# 8000 不要放行。确认它只绑在本机：
ss -lntp | grep 8000        # 应为 127.0.0.1:8000，不是 0.0.0.0:8000
```

B 侧不需要任何入站端口：它主动拨出 443。

### B（内网机器）

```bash
# 在 A 上签发 token 后，把 agent 装到 B（不需要 git、不需要源码）
./dev.sh issue-token --name b-office

# 打包：只有一个 wheel，agent 是自包含的
uv build --project agent -o dist/
scp dist/filebridge_agent-0.1.0-py3-none-any.whl b-host:/tmp/

# B 上：装进独立 venv（systemd unit 里写的就是这个路径）
sudo mkdir -p /opt/filebridge-agent /var/log/filebridge
sudo uv venv /opt/filebridge-agent/.venv
sudo uv pip install --python /opt/filebridge-agent/.venv/bin/python \
    /tmp/filebridge_agent-0.1.0-py3-none-any.whl

# 配置共享目录（token 不用填：首次启动会自动注册）
sudo install -m 640 -o root -g filebridge deploy/agent.env.example /etc/filebridge/agent.env

# 先手动跑一次，确认能连上
sudo -u filebridge /opt/filebridge-agent/.venv/bin/filebridge-agent \
    --server https://files.example.com --root /srv/share

# 常驻
sudo cp deploy/systemd/filebridge-agent.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now filebridge-agent
```

常驻用 `deploy/systemd/filebridge-agent.service`（它把共享目录按只读挂载给进程）。

### 备份

```bash
sudo FILEBRIDGE_DATA_DIR=/var/lib/filebridge/data \
     FILEBRIDGE_STORAGE_DIR=/var/lib/filebridge/storage \
     deploy/backup.sh /var/backups/filebridge
```

**不要直接 `cp` SQLite 文件**：WAL 模式下会拿到缺少最近提交的库。脚本里用的是
backup API。

## 平台

A 与 B 都按 **Linux** 设计、验证和部署。agent 的入口会检查 `sys.platform`，不是
`linux` 直接 `exit 2`——这不是技术限制，而是因为路径沙箱、只读挂载、systemd 的
`ReadOnlyPaths` 全都按 POSIX 语义设计：反斜杠是不是普通文件名字符、软链接逃逸
能不能被验证、保留设备名会不会卡住读线程，这些问题换个文件系统答案就变了。
"看起来能跑"比"起不来"更危险。

服务端没有平台相关代码（标准 FastAPI + SQLite），但它同样只按 Linux 部署验证。

单元测试与端到端测试**不经过** CLI 入口，所以在任意开发机上都能跑；开发机不是
Linux 时，软链接逃逸那条用例会 skip。

## 工作方式（协议层面）

一次取件的完整时序：

```
C: POST /api/v1/tasks {agent_id, path}
A: 建 task → stat_file 到 B（10s 超时）→ 磁盘预检 → 建 upload + 预分配 staging
A: start_upload ──▶ B 立刻 ack，真正传输在 B 的后台任务里
B: PUT /internal/uploads/{id}/chunks/{n}  × N   （3 并发，幂等，可重传）
B: POST /internal/uploads/{id}/complete {sha256}
A: 重算 sha256 → 原子 rename → files 行 → task done → SSE 推给 C
```

**进度的唯一真相在 A**：A 知道自己收到第几块，不需要 B 上报进度。B 只在失败时
通过 WS 发一个 `upload_failed`。

**B 重启后能续传**：B 一连上，A 在 welcome 帧里带上"你还有这些 upload 没传完"，
B 从第一个缺失块继续。块是幂等的，重复上传只会多花一点流量。

## 不变量（改代码前先看这几条）

| 不变量 | 为什么 |
|---|---|
| 只有 send loop 能碰 socket | 并发 send 会交错帧，失败是间歇性的 |
| 每个请求一个 `id` + Future + 超时 | 没有超时，agent 不回包就永久泄漏 |
| 指令队列有界且阻塞，遥测满了直接丢 | 阻塞防丢指令，有界防 OOM |
| 命令是封闭枚举，未列举的直接拒 | 即使 A 被攻破，B 能做的事仍是有限集合 |
| 路径先 `resolve()` 再判包含 | 否则软链接能指向全盘 |
| A 也校验路径（双层） | A 是唯一暴露面，不能只靠 B 兜底 |
| 下载只认 `file_id`，不认路径 | A 侧不存在路径穿越面 |
| token/密码比较用 `compare_digest` | `==` 的时序差可被逐字符还原 |
| 服务端重算 sha256 | 信客户端报的哈希等于没校验 |
| uvicorn 单 worker | 连接注册表在进程内存 |
| B 只读，`open()` 只用 `"rb"` | 有一条测试扫描源码强制这一点 |
| 两份 `protocol.py` 必须逐字节一致 | 没有共享包，靠源码比对测试兜住漂移 |

## 状态

| 阶段 | 状态 |
|---|---|
| M0 骨架 / M1 协议（两端各一份 + 契约样例）/ 路径沙箱 | ✅ |
| M2 接入（WS hub、注册、心跳、在线状态） | ✅ |
| M3 浏览（`list_dir` 游标分页、`stat_file`） | ✅ |
| M4 取件（分片、续传、sha256、预分配落盘） | ✅ |
| M5 管理（列表、搜索、Range 下载、回收站） | ✅ |
| M6 前端（五个页面 + SSE） | ✅ |
| M7 加固（限流、磁盘预检、审计日志） | ✅ |
| M8 部署（systemd、Nginx、备份、文档） | ✅ |
| M9 零配置接入（密码入库 + 首次引导、B 自动注册、准入策略与审批） | ✅ |

测试规模：`agent` 111 + `backend` 93，其中 1 条软链接逃逸用例在
当前开发机上跳过（文件系统没能真正建立软链接，测了也是假阳性，Linux 上会执行）。

## 明确不做（v1）

- B 上的删除/重命名/写入——B 是只读源，能力面越小越安全
- 任何命令执行——从设计上消除 RCE 面
- 多租户、配额计费、秒传去重
- 多 A worker 横向扩展（要做先换掉进程内注册表）
