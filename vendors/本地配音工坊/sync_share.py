# -*- coding: utf-8 -*-
"""
本地配音工坊 · 同步到分享目录（如百度网盘 AISkill）
=================================================
为什么要这个脚本（两个真实踩过的坑）：
  1. **影子文档**：`功能说明书_大白话版.md` 曾经只存在于分享目录、不在源技能目录，
     于是源更新覆盖不到它，两边口径打架（源说 breeze 默认，它还在教 indextts2）。
     → 所以同步前会**反向扫描**：列出「目标有、源没有」的文件，提醒回收进源。
  2. **体积炸弹**：源目录里有 `_deprecated_20260803/`(1.1G)、`outputs/`(15M 测试音频)、
     `audio/ segments/ 音频/`、根目录 `s0*.wav` 等不该上云的东西。
     → 所以有固定排除清单，同步后只有几 MB。

用法：
  python sync_share.py                      # 用默认目标（百度网盘 AISkill 目录）
  python sync_share.py --dst D:\\某处\\本地配音工坊
  python sync_share.py --dry-run            # 只看会同步什么 + 影子文件清单，不动手
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent
DEFAULT_DST = Path(r"D:\BaiduSyncdisk\AISkill\04_音频音乐\本地配音工坊")

# 不上云的目录（体积 / 临时产物 / 历史归档）
EXCLUDE_DIRS = [
    "__pycache__", ".cache",
    "_deprecated_20260803",   # 1.1G 历史归档
    "outputs",                # 测试音频产物
    "audio", "segments", "音频", "runtime",
]
# 不上云的文件
EXCLUDE_FILES = [
    "s01.wav", "s02.wav", "s03.wav", "s04.wav", "s05.wav",   # 根目录散落的测试音频
    "SKILL_backup_v043_混音前_20260816.md",
    "*.pyc",
]


def robocopy(src: Path, dst: Path, dry_run: bool) -> int:
    # /E 复制子目录（含空目录）；**不带 /PURGE** —— 非破坏性，不删目标独有文件
    # /NJH 去掉作业头；保留作业尾（里面有「目录/文件/字节」汇总表）
    cmd = ["robocopy", str(src), str(dst), "/E", "/R:1", "/W:1", "/NP", "/NJH"]
    if dry_run:
        cmd.append("/L")   # /L = 只列出，不实际复制
    if EXCLUDE_DIRS:
        cmd += ["/XD"] + EXCLUDE_DIRS
    if EXCLUDE_FILES:
        cmd += ["/XF"] + EXCLUDE_FILES
    print("[同步] " + " ".join(f'"{c}"' if " " in c else c for c in cmd[:4]) + " ...")
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="gbk", errors="replace")
    out = r.stdout or ""
    for line in out.splitlines():
        if any(k in line for k in ("总数", "目录:", "文件:", "字节:", "失败", "已结束")):
            print("  " + line.strip())
    # robocopy 退出码 0~7 都是成功
    if r.returncode >= 8:
        print(f"[错误] robocopy 失败，code={r.returncode}")
        if r.stderr:
            print(r.stderr[:500])
    return r.returncode


def find_shadow_files(src: Path, dst: Path) -> list:
    """目标有、源没有的文件 —— 这些不会被源更新覆盖，容易变成过期副本。

    注意：这里**故意不用 EXCLUDE_DIRS**，否则临时产物（如 runtime/）会被自己排除掉、
    永远扫不出来。只跳过 __pycache__/.cache 这种纯缓存。
    """
    SKIP_SCAN = {"__pycache__", ".cache"}
    shadows = []
    if not dst.is_dir():
        return shadows
    for root, dirs, files in os.walk(dst):
        dirs[:] = [d for d in dirs if d not in SKIP_SCAN and not d.startswith(".")]
        for f in files:
            fp = Path(root) / f
            rel = fp.relative_to(dst)
            if not (src / rel).exists():
                shadows.append(str(rel))
    return sorted(shadows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(SKILL_DIR), help="源技能目录")
    ap.add_argument("--dst", default=str(DEFAULT_DST), help="分享/同步目标目录")
    ap.add_argument("--dry-run", action="store_true", help="只预演，不复制")
    args = ap.parse_args()

    src, dst = Path(args.src), Path(args.dst)
    if not src.is_dir():
        print(f"[错误] 源目录不存在: {src}")
        sys.exit(1)
    print(f"源: {src}\n目标: {dst}\n")

    shadows = find_shadow_files(src, dst)
    if shadows:
        print(f"⚠️ 目标里有 {len(shadows)} 个「源目录没有」的文件（不会被源更新覆盖）：")
        for s in shadows[:20]:
            print(f"   - {s}")
        if len(shadows) > 20:
            print(f"   ... 还有 {len(shadows) - 20} 个")
        print("   处理：是要长期保留的文档 → copy 回源目录统一维护；"
              "是临时产物 → 直接删，或加进本脚本的 EXCLUDE_DIRS\n")

    code = robocopy(src, dst, args.dry_run)
    if code >= 8:
        sys.exit(code)

    if args.dry_run:
        print("\n[dry-run] 未实际复制。")
        return

    # 同步后自检：目标副本能独立跑起来吗
    print("\n[自检] 目标副本健康度：")
    r = subprocess.run([sys.executable, "dub.py", "--health"], cwd=str(dst),
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    for line in (r.stdout or "").splitlines()[:3]:
        print("  " + line)
    # 动态比对版本号（别写死，升版后会误报）
    def _ver(p: Path) -> str:
        import re
        m = re.search(r'^VERSION\s*=\s*"([\d.]+)"', p.read_text(encoding="utf-8"), re.M)
        return m.group(1) if m else "?"
    vs, vd = _ver(src / "dub.py"), _ver(dst / "dub.py")
    ok = vs != "?" and vs == vd
    print(f"  版本号: 源 {vs} / 目标 {vd}  {'✅' if ok else '⚠️ 不一致，重跑同步'}")


if __name__ == "__main__":
    main()
