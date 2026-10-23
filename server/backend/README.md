# filebridge-server（backend）

部署在 **A** 上的 FastAPI 服务：WebSocket 控制通道、分片接收、文件库、SSE，
以及 `server/frontend/dist` 的静态托管。

## 快速开始

```bash
uv sync
uv run filebridge-server serve --reload         # 127.0.0.1:8000
# 首次启动会提示设置管理员密码（存进数据库，之后用 passwd 改）
```

前端开发时用 `cd ../frontend && npm run dev`（5173，proxy 到 8000）；
生产用 `npm run build`，产物由本服务直接托管，服务器上不需要 Node。

## CLI

| 命令 | 作用 |
|---|---|
| `serve` | 启动服务（强制单 worker）；库里没有密码时会引导设置 |
| `passwd` | 改管理员密码，立即生效并踢掉已有会话 |
| `pending` | 列出待批准的设备注册申请 |
| `approve` / `reject <id>` | 批准或拒绝一台新设备 |
| `revoke-token <id>` | 立即吊销某台设备的 token |
| `list-agents` | 列出节点与在线状态 |
| `issue-token --name X` | 手工签发 token（默认走自动注册，一般用不到） |
| `hash-password` | 兼容用：生成 Argon2 哈希（老部署的环境变量方式） |

## 结构

```
config.py     pydantic-settings，全部可用 FILEBRIDGE_ 前缀覆盖
db.py         SQLite WAL + 单连接 + 写事务
auth.py       C 侧会话、密码（存库）与 B 侧设备 token / 注册申请
hub.py        连接注册表 + 单写者 outbox + pending Future
ws.py         /ws/agent（hello → 注册 → 续传清单 → 收发帧）
storage.py    预分配 .part、偏移写、sha256、原子 rename、回收站
services.py   任务编排与状态机
paths.py      A 侧路径规范化（双层校验第一层）
routes/       auth / agents / tasks / files / uploads / events / system
```

## 测试

```bash
uv run pytest
```

`tests/test_upload_flow.py` 会真的起 uvicorn、真的跑一个 agent、真的分片上传，
覆盖 sha256、Range 206、回收站、取消、匿名 401、SSE 事件。
