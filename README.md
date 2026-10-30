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
  frontend/         Vue 3 + JS + Vite + Element Plus（产物由 backend 托管）

agent/              B 端，独立项目（uv + Python），只读
contract/           协议黄金样例（JSON 夹具，只被测试读取）
deploy/             systemd unit、Nginx 反代、env 模板、备份脚本
docs/               部署手册、设计说明
dev.sh              统一入口
```

两个 Python 项目**完全独立**：各自 `uv sync`、各自的 `.venv` 与 `uv.lock`、没有
workspace、没有 path 依赖，可以单独 clone、单独 build、单独部署。

## 为什么不是拿现成的拼

C 要的三件事——① B 反向接入、② 按需从 B 取件、③ 集中在 A 上管理——开源项目里
没有同时做全的：

| 方案 | 做到了 | 缺哪一块 |
|---|---|---|
| MeshCentral / Tactical RMM | ① ② | 文件只是**流式穿过** A，不落盘 |
| FileBrowser Quantum / copyparty | ③ | 不会主动去 B 取 |
| UrBackup | ① ② ③ | 心智模型是**备份快照**，不是可增删的文件池 |

所以只借它们的通道设计（copyparty 的分块续传、remote-machine-access 的沙箱与
token 校验），自研中间那一层薄服务。

## 协议怎么共享（没有共享包）

`protocol.py` 在两个项目里各有一份，内容必须**逐字节相同**。不抽成第三个包，是因为
它只有 200 行、5 个命令、11 个错误码，而抽出去要付三个具体成本：两个 pyproject 都写
path 依赖、给 B 装 agent 得装两个 wheel、改一行注释要动第三个项目。

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
密码在 `data/filebridge.db`，agent 的 token 在项目内 `agent/.filebridge/agent.json`
（0600，已 gitignore；wheel 安装时在 `~/.local/state/filebridge/`）。

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
./dev.sh test      # protocol + agent + backend 全跑（当前 agent 112 + backend 93，1 条按平台跳过）
./dev.sh lint
```

端到端测试会真的起 uvicorn、真的跑一个 agent、真的分片上传，验证 sha256、
Range 下载、回收站和 SSE。它比单元测试慢，但这类流程错一步就是线上故障。

## 生产部署

> 逐条命令、验收清单、故障排查表在 **[docs/部署手册.md](docs/部署手册.md)**。
> 设计取舍在 **[docs/设计说明.md](docs/设计说明.md)**。

只需要准备两个目录：

| 目标 | 拷什么 | 不用拷 |
|---|---|---|
| **A（服务器）** | `server/`（backend 源码 + `frontend/dist`，合计约 2MB） | `agent/`、`contract/`、`docs/`、`dev.sh` |
| **B（内网机器）** | `agent/` 打出来的**一个** wheel | 其余全部 |

两边都自包含，没有跨目录引用。这一点有实测兜底：把 `server/` 单独拷进一个干净目录，
`uv sync` 能成功、服务能起来、页面能打开；`pytest` 里需要 agent 的那组测试会自动跳过，
不会因为缺少 agent 而报错。

对外只需要 **443**：页面、`/api/*`、`/api/events`、`/ws/agent`、`/internal/*` 共用同一个
端口，靠路径区分；80 只做 ACME 与跳转。uvicorn 绑 `127.0.0.1:8000`，不对公网开放。
B 侧不需要任何入站端口。

`deploy/` 不参与"部署"：它是配置参考，按需把 systemd unit、Nginx 站点、env 模板
`cp` 到 `/etc` 下。`docs/` 是文档，`contract/` 是测试夹具，都不上机器。

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
| 命令走白名单，未列举的直接拒 | 即使 A 被攻破，B 能做的事仍是有限集合 |
| 路径先 `resolve()` 再判包含 | 否则软链接能指向全盘 |
| A 也校验路径（双层） | A 是唯一暴露面，不能只靠 B 兜底 |
| 下载只认 `file_id`，不认路径 | A 侧不存在路径穿越面 |
| token/密码比较用 `compare_digest` | `==` 的时序差可被逐字符还原 |
| 服务端重算 sha256 | 信客户端报的哈希等于没校验 |
| uvicorn 单 worker | 连接注册表在进程内存 |
| B 只读，`open()` 只用 `"rb"` | 有一条测试扫描源码强制这一点 |
| 两份 `protocol.py` 必须逐字节一致 | 没有共享包，靠源码比对测试兜住漂移 |

## 明确不做

- B 上的删除/重命名/写入——B 是只读源，能力面越小越安全
- 任何命令执行——从设计上消除 RCE 面
- 多租户、配额计费、秒传去重
- 多 A worker 横向扩展（要做先换掉进程内注册表）

## 状态

v1 已完成并部署验证（M0–M9：骨架、协议、接入、浏览、取件、管理、前端、加固、
部署、零配置接入）。剩下的扩展方向都写在上面的"明确不做"里。
