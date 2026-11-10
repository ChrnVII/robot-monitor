# robot-monitor-agent

部署在 **B**（内网机器、无公网 IP，**Linux**）上的只读 agent。

- 主动外连 A 的 `wss://…/ws/agent`，注册、心跳、断线指数退避重连
- 按 A 的指令列目录、stat、算 sha256、分片上传；**不提供任何写操作，也没有命令执行**
- 所有文件系统调用走 `asyncio.to_thread`，避免卡住事件循环把心跳饿死

只支持 Linux：入口检查 `sys.platform`，不是 `linux` 直接 `exit 2`（原因见根目录
README 的「平台」一节）。单元测试与端到端测试不经过 CLI 入口，可在任意开发机上跑。

## 用法

```bash
uv run robot-monitor-agent --server https://files.example.com --root /srv/share
# 首次会自动向 A 注册，token 由 A 签发并存到
# agent/.robot-monitor/agents/<地址>-<hash>.json（0600，已 gitignore）；
# 之后连参数都可以省
```

`--server` 写 `https://…` 或 `wss://…/ws/agent` 都可以，agent 会自己推出
WS 地址与 HTTP base。

### 多台 A：凭据按地址分开存

一台 A 一个凭据文件，互不覆盖。换一台 A（或换域名）时直接带新的 `--server`
启动，agent 会把它当成一台新机器去注册，而不是拿旧 token 去撞——旧的凭据原样
留着，换回去还是原来的 `agent_id`。最近一次成功的那台记在 `.robot-monitor/current`，
所以不带 `--server` 时连的是"上次那台"。

注意这不等于同时连多台：一个进程仍然只连一台。

**A 换了域名/IP 也认得出来。** 每份凭据里记着 A 的 `instance_id`（`/api/v1/health`
里的公开字段，跟着库走、不跟地址走）。地址变了就先问一句"你是哪台"，对上了就
沿用原 token，只把凭据挪到新地址——不会再注册出第二台同名节点。判据刻意没选
"把旧 token 发给新地址试一下"：那等于把一份长期口令交给一个还没确认是谁的地址。

也可以用 TOML / 环境变量（前缀 `ROBOT_MONITOR_AGENT_`），见 `deploy/agent.env.example`。

## 只读不变量

`tests/test_handlers.py` 里有一条架构测试会扫描本包源码，断言不存在以 `"w"` /
`"a"` / `"r+"` 打开文件的调用。唯一允许写的是自己的日志文件（由 logging 负责）。

## 测试

```bash
uv run pytest
```

沙箱逃逸矩阵、目录分页边界、断点续传（用 `httpx.MockTransport` 精确控制"哪些块
已收到"）、只读扫描。
