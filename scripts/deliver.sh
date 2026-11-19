#!/bin/bash
# deliver.sh —— 单文件交付（用户 2026-10-24 定：只要成品视频，封面烧第一帧）
#
# 用法：
#   deliver.sh <任务目录> <剧名(素材目录名)> <out 目录下的集目录名> <爆款标题> [文件名前缀]
#   例：deliver.sh "D:/剧里剧外/视频解说/任务/ff-e2e" "福福自带甜趣" F3 "大老虎，被三岁我哄好了"
#   例：deliver.sh ".../tjfx-e2e" "天降福星：沈家小渔" T1 "河里捞回的福星妹妹" "天降福星"
#
# 产出（交付目录只有这一个文件）：
#   D:/BaiduNetdiskDownload/<剧名>/解说成品/<文件名前缀>-<爆款标题>.mp4
#   源 = 产线 out/<out 目录下的集目录名>/<片名>-<out 目录下的集目录名>-封面版.mp4（封面已烧前 1.5s）
#
# ★ 第 5 参「文件名前缀」必填场景（v3.6.5 修）：素材目录名常带副标题/符号
#   （如「天降福星：沈家小渔」「八零小福包：参宝-素材」），旧版把目录名
#   同时当文件名前缀，既导致文件名冗长带全角冒号，又会在目录名与简称
#   不一致时把成品交付到不存在的 D:/BaiduNetdiskDownload/<简称>/ 下（误建空目录）。
#   现在目录名与文件名前缀解耦：目录=第2参，文件名=第5参（缺省回落第2参）。
#
# 质检报告/封面图/发布文案留在产线 out 目录备查，不进交付目录。
set -e
TASK="$1"; SERIES="$2"; EP="$3"; TITLE="$4"; PREFIX="${5:-$2}"
if [ $# -lt 4 ] || [ $# -gt 5 ]; then
  echo "用法: deliver.sh <任务目录> <剧名(素材目录名)> <out 目录下的集目录名> <爆款标题> [文件名前缀]" >&2
  exit 1
fi
FFP="D:/AITools/TTS/Dots.tts/wzf/ffmpeg/bin/ffprobe.exe"
if [ ! -x "$FFP" ]; then
  echo "[FAIL] ffprobe 缺失: $FFP" >&2
  exit 4
fi
# ★ SRC 用 glob 匹配，不硬编码成片名（v3.6.0 修：plan name 变体会漏配）
# shellcheck disable=SC2012
mapfile -t SRCS < <(ls "$TASK/out/$EP/"*-封面版.mp4 2>/dev/null || true)
if [ "${#SRCS[@]}" -eq 0 ]; then
  echo "[FAIL] 找不到封面版成片: $TASK/out/$EP/*-封面版.mp4" >&2
  exit 2
fi
if [ "${#SRCS[@]}" -gt 1 ]; then
  echo "[FAIL] 封面版成片有多个，请人工确认: ${SRCS[*]}" >&2
  exit 2
fi
SRC="${SRCS[0]}"
DST_DIR="D:/BaiduNetdiskDownload/$SERIES/解说成品"
DST="$DST_DIR/${PREFIX}-${TITLE}.mp4"
# ★ 先校验源、再复制（v3.6.0 修：旧版先 cp 后校验，失败已在交付目录留半个文件）
SRC_DUR=$("$FFP" -v error -show_entries format=duration -of csv=p=0 "$SRC" 2>/dev/null)
if [ -z "$SRC_DUR" ]; then
  echo "[FAIL] 源文件时长探测失败: $SRC" >&2
  exit 3
fi
mkdir -p "$DST_DIR"
cp "$SRC" "$DST" || { echo "[FAIL] 复制失败" >&2; rm -f "$DST"; exit 3; }
DUR=$("$FFP" -v error -show_entries format=duration -of csv=p=0 "$DST" 2>/dev/null)
SZ=$(stat -c %s "$DST" 2>/dev/null || wc -c < "$DST")
if [ -z "$DUR" ]; then
  echo "[FAIL] 交付文件时长探测失败，清理" >&2
  rm -f "$DST"
  exit 3
fi
echo "[OK] 交付: $DST"
echo "     大小 ${SZ}B / 时长 ${DUR}s（源 ${SRC_DUR}s）"
echo "     交付目录现有文件:"
ls "$DST_DIR" | sed 's/^/       /'
