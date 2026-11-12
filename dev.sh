#!/usr/bin/env bash
# 统一入口。uv 管不了 npm，所以前端单独一组命令。
# 用法：./dev.sh <命令> [参数]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
command="${1:-help}"
shift || true

run() {
  local dir="$1"
  shift
  ( cd "$ROOT/$dir" && "$@" )
}

case "$command" in
  sync)
    run server/backend uv sync
    echo "依赖已同步（server-backend）"
    ;;

  serve|backend)
    # 生产与开发都用同一个入口；--reload 只在开发时加
    run server/backend uv run robot-monitor-server serve "$@"
    ;;

  web|frontend)
    ( cd "$ROOT/server/frontend" && npm run dev )
    ;;

  build)
    ( cd "$ROOT/server/frontend" && npm run build )
    echo "前端产物：server/frontend/dist（由 backend 直接托管）"
    ;;

  test)
    run server/backend uv run pytest -q
    ;;

  lint)
    run server/backend uv run ruff check .
    ;;

  format)
    run server/backend uv run ruff format .
    ;;

  passwd)
    run server/backend uv run robot-monitor-server passwd
    ;;

  pending)
    run server/backend uv run robot-monitor-server pending
    ;;

  approve)
    run server/backend uv run robot-monitor-server approve "$@"
    ;;

  reject)
    run server/backend uv run robot-monitor-server reject "$@"
    ;;

  issue-token)
    run server/backend uv run robot-monitor-server issue-token "$@"
    ;;

  revoke-token)
    run server/backend uv run robot-monitor-server revoke-token "$@"
    ;;

  list-agents)
    run server/backend uv run robot-monitor-server list-agents
    ;;

  help|*)
    cat <<'USAGE'
用法：./dev.sh <命令> [参数]

  sync                      同步 server 依赖
  serve [--reload]          启动 server（默认 127.0.0.1:8000）
  web                       前端开发服务器（5173，proxy 到 8000）
  build                     构建前端产物到 server/frontend/dist
  test                      跑 server 测试（含自包含的端到端上传链路）
  lint / format             ruff 检查 / 格式化
  passwd                    改管理员密码（立即生效，不用重启）
  pending                   列出待批准的设备（网页「节点」页也能看/批）
  approve / reject <id>     命令行批准或拒绝（日常用网页点即可）
  issue-token --name X      手工签发 agent token（跳过注册审批，一般不用）
  revoke-token <agent_id>   吊销 agent token
  list-agents               列出节点

机器侧（cloud-agent）不在本仓库：
  cd ../robot/robot-backend && .venv/bin/python -m app.cloud_agent

典型开发流程：
  终端 1: ./dev.sh serve --reload
  终端 2: ./dev.sh web
  终端 3: 在 robot-backend 里启动 cloud-agent，或在机器人页面上配置
USAGE
    ;;
esac
