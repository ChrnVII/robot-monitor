# filebridge-agent

部署在 **B**（内网机器、无公网 IP，**Linux**）上的只读 agent。

- 主动外连 A 的 `wss://…/ws/agent`，注册、心跳、断线指数退避重连
- 按 A 的指令列目录、stat、分片上传；**不提供任何写操作，也没有命令执行**
- 所有文件系统调用走 `asyncio.to_thread`，避免卡住事件循环把心跳饿死

只支持 Linux：入口检查 `sys.platform`，不是 `linux` 直接 `exit 2`（原因见根目录
README 的「平台」一节）。单元测试与端到端测试不经过 CLI 入口，可在任意开发机上跑。

## 用法

```bash
uv run filebridge-agent --server https://files.example.com --root /srv/share
# 首次会自动向 A 注册，token 由 A 签发并存到
# agent/.filebridge/agent.json（0600，已 gitignore）；之后连参数都可以省
```

`--server` 写 `https://…` 或 `wss://…/ws/agent` 都可以，agent 会自己推出
WS 地址与 HTTP base。

也可以用 TOML / 环境变量（前缀 `FILEBRIDGE_AGENT_`），见 `deploy/agent.env.example`。

## 只读不变量

`tests/test_handlers.py` 里有一条架构测试会扫描本包源码，断言不存在以 `"w"` /
`"a"` / `"r+"` 打开文件的调用。唯一允许写的是自己的日志文件（由 logging 负责）。

## 测试

```bash
uv run pytest
```

沙箱逃逸矩阵、目录分页边界、断点续传（用 `httpx.MockTransport` 精确控制"哪些块
已收到"）、只读扫描。
