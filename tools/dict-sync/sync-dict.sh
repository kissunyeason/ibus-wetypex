#!/bin/sh
# 一键：预览雾凇词库导入效果（默认不写入）
#   双击 / 直接运行        -> 只预览，列出会导入哪些词
#   ./sync-dict.sh --apply -> 真正导入到 ~/.config/wetypex/dynamic-phrases.txt
#   ./sync-dict.sh --mode hotword --apply -> 写入原版核心「常用语」
#   所有参数原样传给 rime2wetypex.py，可用 --help 查看。
set -eu
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$DIR/rime2wetypex.py" "$@"
