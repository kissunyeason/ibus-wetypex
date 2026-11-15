#!/bin/sh
# 可选增强：把二进制 *.userdb 里的「用户造词 / 调频记录」也导出来。
#
# 只想要 custom_phrase.txt + sync 快照的话，根本不用跑本脚本，
# 直接 sh sync-dict.sh 即可（零依赖、不用 sudo、不用停输入法）。
#
# 本脚本会：缺工具时自动装 Fedora 官方包 librime-tools
#           -> 把 *.userdb 复制到临时目录（绕开输入法独占锁）导出
#           -> 自动出预览。确认后按提示加 --apply 导入。
#
# 注意：不要 sudo 跑整个脚本。只有 dnf install 那一步内部会自己提权。
set -eu

# 误用 sudo 时把 HOME 修回真实用户，否则找不到 ~/.config/ibus/rime
if [ -n "${SUDO_USER:-}" ]; then
    RH=$(getent passwd "$SUDO_USER" 2>/dev/null | cut -d: -f6 || true)
    if [ -n "$RH" ] && [ -d "$RH" ]; then
        HOME="$RH"
        export HOME
        echo "提示：本脚本不需要 sudo，已自动按 $HOME 处理。"
    fi
fi

DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PY="$DIR/rime2wetypex.py"
if [ ! -f "$PY" ]; then
    echo "找不到同目录下的 rime2wetypex.py，请把三个脚本放在同一目录。" >&2
    exit 1
fi

RIME_DIR="${1:-}"
if [ -z "$RIME_DIR" ]; then
    for d in "$HOME/.config/ibus/rime" "$HOME/.local/share/fcitx5/rime" \
             "$HOME/.config/fcitx/rime" "$HOME/.config/rime"; do
        if [ -d "$d" ]; then RIME_DIR="$d"; break; fi
    done
fi
if [ -z "$RIME_DIR" ] || [ ! -d "$RIME_DIR" ]; then
    echo "找不到 Rime 用户目录，请把路径作为第一个参数传入，例如：" >&2
    echo "  sh $0 ~/.config/ibus/rime" >&2
    exit 1
fi

if ! command -v rime_dict_manager >/dev/null 2>&1; then
    echo "缺少 rime_dict_manager，正在安装 Fedora 官方包 librime-tools…"
    if command -v dnf >/dev/null 2>&1; then
        # 只有这一步需要提权；看到 Face Verified 按回车即可
        sudo dnf install -y librime-tools || {
            echo "安装失败，请手动执行 sudo dnf install -y librime-tools 后重跑本脚本。" >&2
            exit 1
        }
    else
        echo "非 Fedora，请手动安装 librime-tools（Debian/Ubuntu 为 librime-bin）后重跑。" >&2
        exit 1
    fi
fi

echo "Rime 用户目录: $RIME_DIR"
echo
python3 "$PY" --rime-dir "$RIME_DIR" --export-db
echo
echo "============================================================"
echo "以上是预览。确认无误后执行导入："
echo "  python3 $PY --rime-dir $RIME_DIR --export-db --apply"
echo "写入原版核心常用语则再加 --mode hotword"
echo "============================================================"
