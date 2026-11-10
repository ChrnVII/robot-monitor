#!/usr/bin/env bash
# 备份 A 端的数据库与文件库。
#
# 关键点：SQLite 不能直接 cp。WAL 模式下 `cp robot-monitor.db` 会拿到一个
# 缺少最近提交的库（甚至撕裂的页）。必须走 backup API 或 `.backup`。
set -euo pipefail

DATA_DIR="${ROBOT_MONITOR_DATA_DIR:-/var/lib/robot-monitor/data}"
STORAGE_DIR="${ROBOT_MONITOR_STORAGE_DIR:-/var/lib/robot-monitor/storage}"
DEST="${1:-/var/backups/robot-monitor}"
KEEP_DAYS="${KEEP_DAYS:-14}"

STAMP="$(date +%Y%m%d-%H%M%S)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

mkdir -p "$DEST"

echo "==> 备份数据库"
python3 - "$DATA_DIR/robot-monitor.db" "$WORK/robot-monitor.db" <<'PY'
import sqlite3, sys
src_path, dst_path = sys.argv[1], sys.argv[2]
src = sqlite3.connect(f"file://{src_path}?mode=ro", uri=True)
dst = sqlite3.connect(dst_path)
with dst:
    src.backup(dst)
dst.close(); src.close()
PY

echo "==> 打包文件库"
# 文件本体是 uuid 命名、不可变，用 -u 只打包变更（增量到 tar 意义不大，
# 但保留 mtime 便于恢复后排查）
tar -czf "$DEST/files-$STAMP.tar.gz" -C "$STORAGE_DIR" files

tar -czf "$DEST/db-$STAMP.tar.gz" -C "$WORK" robot-monitor.db

echo "==> 清理超过 ${KEEP_DAYS} 天的备份"
find "$DEST" -name '*.tar.gz' -mtime "+$KEEP_DAYS" -delete

echo "完成：$DEST/db-$STAMP.tar.gz"
echo "      $DEST/files-$STAMP.tar.gz"
