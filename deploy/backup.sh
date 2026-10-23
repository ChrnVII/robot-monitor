#!/usr/bin/env bash
# 备份 A 端的数据库与文件库。
#
# 关键点：SQLite 不能直接 cp。WAL 模式下 `cp filebridge.db` 会拿到一个
# 缺少最近提交的库（甚至撕裂的页）。必须走 backup API 或 `.backup`。
set -euo pipefail

DATA_DIR="${FILEBRIDGE_DATA_DIR:-/var/lib/filebridge/data}"
STORAGE_DIR="${FILEBRIDGE_STORAGE_DIR:-/var/lib/filebridge/storage}"
DEST="${1:-/var/backups/filebridge}"
KEEP_DAYS="${KEEP_DAYS:-14}"

STAMP="$(date +%Y%m%d-%H%M%S)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

mkdir -p "$DEST"

echo "==> 备份数据库"
python3 - "$DATA_DIR/filebridge.db" "$WORK/filebridge.db" <<'PY'
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

tar -czf "$DEST/db-$STAMP.tar.gz" -C "$WORK" filebridge.db

echo "==> 清理超过 ${KEEP_DAYS} 天的备份"
find "$DEST" -name '*.tar.gz' -mtime "+$KEEP_DAYS" -delete

echo "完成：$DEST/db-$STAMP.tar.gz"
echo "      $DEST/files-$STAMP.tar.gz"
