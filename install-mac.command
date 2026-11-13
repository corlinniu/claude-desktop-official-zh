#!/bin/bash
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="/usr/bin/python3"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"
PATCHER="$DIR/scripts/patch_official_zh.py"

INTERACTIVE=0
ACTION="${CLAUDE_ACTION:-}"
if [ -z "$ACTION" ]; then
  INTERACTIVE=1
  echo "Claude Desktop 中文（官方词库）"
  echo "  [1] 安装简体中文"
  echo "  [2] 安装繁體中文"
  echo "  [3] 还原官方 app"
  read -rp "请输入选项 [1/2/3，默认 1]: " choice
  case "${choice:-1}" in
    2) ACTION="install-zh-TW" ;;
    3) ACTION="restore" ;;
    *) ACTION="install-zh-CN" ;;
  esac
  echo
fi

DRY_RUN=0
for arg in "$@"; do
  [ "$arg" = "--dry-run" ] && DRY_RUN=1
done

run_patcher() {
  if [ "$(id -u)" -ne 0 ] && [ "$DRY_RUN" -eq 0 ]; then
    sudo "$PYTHON" "$PATCHER" --user-home "$HOME" "$@"
  else
    "$PYTHON" "$PATCHER" --user-home "$HOME" "$@"
  fi
}

if [ "$ACTION" = "restore" ]; then
  run_patcher --restore --launch "$@"
else
  LANG_CODE="${CLAUDE_LANG:-${ACTION#install-}}"
  run_patcher --restore-if-backup-exists "$@"
  run_patcher --lang "$LANG_CODE" --launch "$@"
fi

if [ "$INTERACTIVE" -eq 1 ]; then
  echo
  echo "完成。按回车退出。"
  read -r _
fi
