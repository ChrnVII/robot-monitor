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

# 端到端测试要驱动真实的 agent。但 server 的 pyproject 里**不能**写这条依赖——
# 部署 A 时只有 server/，写了会让 uv sync 因 path 解析失败直接退出。
# 所以只在开发环境里把它装进 backend 的 venv，装不上就整组跳过。
link_agent() {
  if [ -d "$ROOT/agent" ]; then
    run server/backend uv pip install -q -e ../../agent
  fi
}

case "$command" in
  sync)
    run agent uv sync
    run server/backend uv sync
    link_agent
    echo "依赖已同步（agent / server-backend；agent 已装入 backend 环境供端到端测试）"
    ;;

  serve|backend)
    # 生产与开发都用同一个入口；--reload 只在开发时加
    run server/backend uv run filebridge-server serve "$@"
    ;;

  agent)
    run agent uv run filebridge-agent "$@"
    ;;

  web|frontend)
    ( cd "$ROOT/server/frontend" && npm run dev )
    ;;

  build)
    ( cd "$ROOT/server/frontend" && npm run build )
    echo "前端产物：server/frontend/dist（由 backend 直接托管）"
    ;;

  test)
    run agent uv run pytest -q
    link_agent
    run server/backend uv run pytest -q
    ;;

  lint)
    run agent uv run ruff check .
    run server/backend uv run ruff check .
    ;;

  format)
    run agent uv run ruff format .
    run server/backend uv run ruff format .
    ;;

  passwd)
    run server/backend uv run filebridge-server passwd
    ;;

  pending)
    run server/backend uv run filebridge-server pending
    ;;

  approve)
    run server/backend uv run filebridge-server approve "$@"
    ;;

  reject)
    run server/backend uv run filebridge-server reject "$@"
    ;;

  hash-password)
    run server/backend uv run filebridge-server hash-password "$@"
    ;;

  issue-token)
    run server/backend uv run filebridge-server issue-token "$@"
    ;;

  revoke-token)
    run server/backend uv run filebridge-server revoke-token "$@"
    ;;

  list-agents)
    run server/backend uv run filebridge-server list-agents
    ;;

  help|*)
    cat <<'USAGE'
用法：./dev.sh <命令> [参数]

  sync                      同步两个 Python 项目的依赖
  serve [--reload]          启动 A 端（默认 127.0.0.1:8000）
  agent --server ... ...    启动 B 端 agent
  web                       前端开发服务器（5173，proxy 到 8000）
  build                     构建前端产物到 server/frontend/dist
  test                      跑两个项目的测试（含两端协议契约测试）
  lint / format             ruff 检查 / 格式化
  passwd                    改管理员密码（立即生效，不用重启）
  pending                   列出待批准的设备
  approve / reject <id>     批准或拒绝一台新设备
  hash-password             兼容用：生成密码哈希（不再需要，密码存在库里）
  issue-token --name X      兼容用：手工签发 agent token（默认自动注册）
  revoke-token <agent_id>   吊销 agent token
  list-agents               列出节点

典型开发流程：
  终端 1: ./dev.sh serve --reload
  终端 2: ./dev.sh web
  终端 3: ./dev.sh agent --server http://127.0.0.1:8000 --root /srv/share
USAGE
    ;;
esac
