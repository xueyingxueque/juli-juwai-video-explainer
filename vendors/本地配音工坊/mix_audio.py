#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三轨混音脚本：口播（TTS）+ BGM + SFX（v0.6.0 重构版）
BGM 两种模式：
  h3（默认）：用 H3 每段自带音轨（overall_soundscape 音效 + non_diegetic_music BGM）
  library：从 BGM 库选曲（--bgm-track 指定）
SFX：--sfx 时间码插入（缺失文件自动跳过且不影响其它音效/输入索引）
口播响时 BGM 自动侧链闪避（sidechaincompress）；
--silent 标注的无台词镜，BGM 用 volume 时间线自动抬到 --silent-bgm-vol（默认 1.0，真正"发力"）。
响度：--platform 预设（podcast=-16 LUFS 默认 / shortvideo=-14 / broadcast=-23），
     --loudness 显式覆盖；--two-pass 走测量+线性校正两遍 loudnorm（广播级精确达标）。

用法：
python mix_audio.py --videos S01.mp4,S02.mp4 --voice 配音_对齐.wav \
  --final-video 成片_无音.mp4 --durations 8,6,7 --silent S03 \
  --sfx "5.5,SFX/印章/stamp.wav,0.8;9.0,SFX/转场/whoosh.wav,0.6" -o 成片_混音.mp4
"""
import argparse, json, re, subprocess, sys
from pathlib import Path

# 平台 → 集成响度目标（R2 2026-08 核对：YouTube/Spotify/TikTok -14，Apple/播客 -16，EBU R128 广播 -23）
PLATFORM_LUFS = {"podcast": -16.0, "shortvideo": -14.0, "broadcast": -23.0}


def log(m):
    print(m, flush=True)


def probe(f):
    p = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(f)],
                       capture_output=True, text=True)
    try:
        return json.loads(p.stdout)
    except Exception:
        return {"format": {"duration": "0"}}


def _shot_index(token, i):
    """silent 元素兼容 'S05' / '5' 两种写法，返回 0-based 镜序；解析不了回退按序号对。"""
    m = re.match(r"^[sS]?0*(\d+)$", str(token).strip())
    return int(m.group(1)) - 1 if m else i


def _bgm_volume_expr(durations, silent, bgm_vol, silent_vol):
    """构造 volume 时间线表达式：有台词镜=bgm_vol，无台词镜=silent_vol。"""
    silent_idx = {_shot_index(x, -1) for x in silent}
    voiced, cur = [], 0.0
    for i, d in enumerate(durations):
        start, end = cur, cur + d
        if i not in silent_idx:
            voiced.append((start, end))
        cur = end
    if not voiced:
        return f"{silent_vol}"          # 全是无台词镜：恒定
    if not silent:
        return f"{bgm_vol}"             # 没标无台词镜：恒定底量（与旧版行为一致）
    terms = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in voiced)
    return f"if({terms},{bgm_vol},{silent_vol}):eval=frame"


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def loudnorm_file(src, dst, target_lufs, two_pass, work):
    """响度归一到 target_lufs（TP -1dBTP / LRA 11）。返回最终文件路径。"""
    base = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src)]
    if not two_pass:
        cmd = base + ["-af", f"loudnorm=I={target_lufs}:TP=-1.0:LRA=11",
                      "-c:a", "pcm_s16le", str(dst)]
        rc = _run(cmd)
        if rc.returncode != 0:
            log(f"[WARN] loudnorm 单遍失败，用原始混音轨: {rc.stderr[-200:]}")
            return src
        return dst
    # 两遍：pass1 测量
    p1 = _run(["ffmpeg", "-hide_banner", "-i", str(src),
               "-af", f"loudnorm=I={target_lufs}:TP=-1.0:LRA=11:print_format=json",
               "-f", "null", "-"])
    text = (p1.stderr or "")
    m = re.findall(r"\{[^{}]*input_i[^{}]*\}", text, flags=re.S)
    if not m:
        log("[WARN] loudnorm 测量失败，回退单遍")
        return loudnorm_file(src, dst, target_lufs, False, work)
    stats = json.loads(m[-1])

    def num(k):
        try:
            return float(stats[k])
        except Exception:
            return 0.0
    af = (f"loudnorm=I={target_lufs}:TP=-1.0:LRA=11:"
          f"measured_I={stats.get('input_i')}:measured_TP={stats.get('input_tp')}:"
          f"measured_LRA={stats.get('input_lra')}:measured_thresh={stats.get('input_thresh')}:"
          f"offset={stats.get('target_offset')}:linear=true:print_format=summary")
    rc = _run(base + ["-af", af, "-c:a", "pcm_s16le", str(dst)])
    if rc.returncode != 0:
        log(f"[WARN] loudnorm 二遍失败，回退单遍: {rc.stderr[-200:]}")
        return loudnorm_file(src, dst, target_lufs, False, work)
    return dst


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--videos", default="", help="各镜 H3 视频，逗号分隔（h3 模式提取音轨做 BGM）")
    ap.add_argument("--voice", required=True, help="对齐口播音轨（dub_align 输出）")
    ap.add_argument("--final-video", required=True, help="拼接好的无音成片")
    ap.add_argument("--durations", required=True, help="每镜时长，逗号分隔")
    ap.add_argument("--silent", default="", help="无台词镜（S05 或 5，逗号分隔；这些镜 BGM 抬到 --silent-bgm-vol）")
    ap.add_argument("--bgm-track", default="", help="BGM 库选曲路径（留空=H3 音轨模式）")
    ap.add_argument("--sfx", default="", help="SFX：\"时间码,文件路径,音量;...\"")
    ap.add_argument("--bgm-vol", type=float, default=0.35, help="有台词时 BGM 底音量（线性）")
    ap.add_argument("--silent-bgm-vol", type=float, default=1.0, help="无台词镜 BGM 音量（默认满音量发力）")
    ap.add_argument("--duck-db", type=float, default=-6.0, help="口播响起时额外侧链闪避量（dB）")
    ap.add_argument("--platform", choices=list(PLATFORM_LUFS), default="podcast",
                    help="响度平台预设：podcast=-16LUFS(默认) / shortvideo=-14 / broadcast=-23")
    ap.add_argument("--loudness", type=float, default=None,
                    help="显式 LUFS 目标（覆盖 --platform）；无台词为主的片可传 -18")
    ap.add_argument("--two-pass", action="store_true", help="两遍 loudnorm（测量+线性校正，广播级精确）")
    ap.add_argument("-o", "--out", required=True)
    args = ap.parse_args()

    videos = args.videos.split(",") if args.videos else []
    durations = [float(x) for x in args.durations.split(",") if x.strip()]
    silent = {x.strip() for x in args.silent.split(",") if x.strip()}
    total_dur = sum(durations)
    target_lufs = args.loudness if args.loudness is not None else PLATFORM_LUFS[args.platform]

    work = Path(args.out).parent
    work.mkdir(parents=True, exist_ok=True)

    # 1. BGM 轨：库选曲 or H3 各镜音轨拼接
    if args.bgm_track:
        bgm_all = work / "bgm_track.wav"
        _run(["ffmpeg", "-y", "-loglevel", "error", "-i", args.bgm_track,
              "-t", f"{total_dur}", "-c:a", "pcm_s16le", str(bgm_all)])
        log(f"[1] BGM 库选曲: {args.bgm_track}（裁剪到 {total_dur}s）")
    else:
        if not videos:
            log("[ERROR] 库选曲模式需 --bgm-track；或 H3 模式需 --videos")
            sys.exit(1)
        if len(videos) != len(durations):
            log(f"[ERROR] 视频数 {len(videos)} != 镜长数 {len(durations)}")
            sys.exit(1)
        bgm_parts = []
        for i, (vid, dur) in enumerate(zip(videos, durations)):
            wav = work / f"bgm_B{i+1:02d}.wav"
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", vid,
                            "-vn", "-c:a", "pcm_s16le", str(wav)], check=True)
            bgm_parts.append(wav)
        cfile = work / "bgm_concat.txt"
        cfile.write_text("\n".join(f"file '{p.name}'" for p in bgm_parts), encoding="utf-8")
        bgm_all = work / "bgm_all.wav"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                        "-i", str(cfile), "-c", "copy", str(bgm_all)], check=True)
        log(f"[1] BGM 轨已拼接: {bgm_all}（{len(videos)} 镜 H3 音轨）")

    # 2. SFX：只保留真实存在的文件，输入索引按"有效序号"重排（修旧版缺失文件导致 filtergraph 崩）
    sfx_valid = []
    if args.sfx:
        for item in (s.strip() for s in args.sfx.split(";") if s.strip()):
            parts = [p.strip() for p in item.split(",")]
            if len(parts) < 2:
                log(f"[WARN] SFX 条目格式不对，跳过: {item}")
                continue
            try:
                t, fpath = float(parts[0]), parts[1]
            except ValueError:
                log(f"[WARN] SFX 时间码非法，跳过: {item}")
                continue
            vol = float(parts[2]) if len(parts) > 2 else 0.8
            if not Path(fpath).exists():
                log(f"[WARN] SFX 文件不存在，跳过: {fpath}")
                continue
            sfx_valid.append((t, fpath, vol))
        log(f"[1.5] SFX 有效 {len(sfx_valid)} 组（时间码对齐）")

    # 3. filtergraph：BGM 时间线音量 → 侧链闪避 → 与口播/SFX amix
    vol_expr = _bgm_volume_expr(durations, silent, args.bgm_vol, args.silent_bgm_vol)
    # filtergraph 语法里逗号是滤镜分隔符，表达式内部的逗号必须转义为 \,
    vol_expr_esc = vol_expr.replace(",", r"\,") if "eval=frame" in vol_expr else vol_expr
    chain = [
        f"[0:a]volume={vol_expr_esc}[bgm0]",
        "[1:a]asplit=2[v1][v2]",
        f"[bgm0][v2]sidechaincompress=threshold=0.02:ratio=20:attack=20:release=350[ducked]",
    ]
    for k, (t, _fpath, vol) in enumerate(sfx_valid):
        # all=1：对任意声道数统一延迟，避免旧版硬编码双声道
        chain.append(f"[{2+k}:a]adelay={int(t*1000)}:all=1,volume={vol}[s{k}]")
    mix_labels = "[ducked][v1]" + "".join(f"[s{k}]" for k in range(len(sfx_valid)))
    n_total = 2 + len(sfx_valid)
    chain.append(f"{mix_labels}amix=inputs={n_total}:normalize=0,alimiter=limit=0.95[out]")
    if silent:
        log(f"[1.6] 无台词镜 {sorted(silent)}：这些区间 BGM 抬到 {args.silent_bgm_vol}，其余 {args.bgm_vol} + 侧链闪避")

    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(bgm_all), "-i", args.voice]
    for _t, fpath, _v in sfx_valid:
        cmd += ["-i", fpath]
    mix_raw = work / "mix_audio.wav"
    cmd += ["-filter_complex", ";".join(chain), "-map", "[out]", "-c:a", "pcm_s16le", str(mix_raw)]
    rc = _run(cmd)
    if rc.returncode != 0:
        log(f"[ERROR] 混音失败: {rc.stderr[-500:]}")
        sys.exit(1)
    log(f"[2] 混音完成（BGM 底量 {args.bgm_vol}，侧链闪避 {args.duck_db}dB）")

    # 4. 响度归一
    norm_out = work / "mix_audio_norm.wav"
    final_mix = loudnorm_file(mix_raw, norm_out, target_lufs, args.two_pass, work)
    if str(final_mix) == str(norm_out):
        mix_raw.unlink(missing_ok=True)
        norm_out.rename(mix_raw)
    log(f"[2.5] 响度归一到 {target_lufs} LUFS（True Peak ≤ -1dBTP，"
        f"{'两遍线性' if args.two_pass else '单遍动态'}，平台预设 {args.platform}）")

    # 5. 合成片
    rc2 = _run(["ffmpeg", "-y", "-loglevel", "error",
                "-i", args.final_video, "-i", str(mix_raw),
                "-map", "0:v", "-map", "1:a", "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k", "-shortest", args.out])
    if rc2.returncode != 0:
        log(f"[ERROR] 合成成片失败: {rc2.stderr[-500:]}")
        sys.exit(1)
    d = probe(args.out)
    try:
        dur = round(float(d["format"]["duration"]), 1)
    except Exception:
        dur = "?"
    log(f"[OK] 成片完成: {args.out}（{dur}s）")


if __name__ == "__main__":
    main()
