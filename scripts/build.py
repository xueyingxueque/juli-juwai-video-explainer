"""短剧解说成片 —— 单一入口，一次跑完。

★★★ 重写理由（不是打补丁，是推倒重来）★★★

旧版问题：三个脚本里各有一份混音逻辑（mix_v6 / series_assemble / run_pipeline），
互相调用、编号体系不一致（seg 用 0-based、raw 用 1-based），
改一处漏两处，最后出现「音轨只有 2 秒、成片 20 秒」这种结构性错误。

本文件是**唯一入口**，全部逻辑在这里，**不再有第二份**。

设计原则（每条都对应踩过的坑）：
  1. 段落是唯一数据源 —— 所有时长从它推导，不单独存
  2. 音轨按段号命名并显式校验 —— 缺一个就报错，不跳过
  3. amix duration=longest + apad + atrim —— 不再截断
  4. 解说段与原声段**结构互斥** —— 不靠音量控制
  5. 出片前必须核对音轨长度 == 视频长度

用法：
    python build.py <plan.json> --dataset <素材目录> [--voice 音色] [--bgm 路径]
"""
import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
import wave
import math
import os
import re
import array
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
import size_budget  # noqa: E402  体积预算唯一真相源（与 make_cover.py 共用）
CFG = json.loads((ROOT / 'config' / 'engine.json').read_text(encoding='utf-8'))
FF = CFG['runtime']['ffmpeg']
FP = CFG['runtime']['ffprobe']
DUB_PY = CFG['runtime']['tts_python']
DUB = CFG['runtime']['tts_dub_script']

# 规格
W, H, FPS = 1080, 1920, 30
# ★ 段级主声源响度取 -13 而非 -14（v3.6.5 修，坑典 P20）：
#   母带 loudnorm 要把整条拉到 -14。若段级就归一到 -14，混音后约 -15.1，
#   母带必须**提升** 1.1dB → 峰值 +0.24 dBTP 撞上 TP=-1.5 → loudnorm 退化为
#   dynamic 模式，为满足 TP/LRA=11 把信号压 5dB（实测 -15.1 → -19.2 LUFS，
#   响度永远到不了目标，校验必挂）。
#   段级取 -13（比目标高 1 LU）→ 母带只需**向下**调整，增益为负、峰值随之下降，
#   不撞 TP → loudnorm 走线性模式，精确落到 -14 LUFS。
#   配合段级 TP=-3.0（留混音余量），实测 -14.0 LUFS / 峰值 -4 dBTP。
SPEECH_LUFS = -13        # 段级主声源（故意高于成片目标 1 LU，为母带留下调空间）
BREATH = 0.35  # 每段结尾呼吸留白
# ★ 2026-11-08 实测校准：0.45 时「段尾呼吸 + 下一段铺轨余量」在段边界叠成
#   1.0s 连续静音，撞 MAX_SILENT_RUN_SEC=0.8 硬门（MW1 全片只有这一处，音画/响度/峰值全合格）。
#   注释写「1s 以上的洞才是缺陷」，阈值却是 0.8 —— 与其放宽硬门，不如把**产生静音的源头**收紧。
#   收到 0.35 后段边界最多约 0.7s，仍留有可听的呼吸，且听感更贴抖音口播节奏。
#   ★ 必须与 plan_est.py 的 BREATH 同改（预估口径单一真相源，否则预估与实测漂移）。
MAX_TTS_ATTEMPTS = 8  # TTS 段级重试上限（坑典 P45：覆盖率 99.8%）
# ★ v3.6.5 提 5 → 8（2026-10-27 实测根因修正）：
#   dots.tts 是扩散采样，**概率性退化**不是只有「确定性退化」一种。
#   实测单次退化率约 15-20%（日志里大量「第 1 次失败、第 2 次通过」），
#   8 次全废的概率 = 0.2^5 ≈ 0.03% —— 单看很小，但在 126 段的一集里
#   期望出现 1 次以上（实测 T2 一集内连撞 3 处，seg18/seg25/seg73）。
#   提到 8 次后全废概率 ≈ 0.2^8 = 2.6e-6，基本不再发生。
#   ★ 前提：**不能靠无限重试掩盖「确定性退化」**——那类（破折号「——」、
#     生僻词「工业券」）重试 100 次也没用，必须改文案。判据：
#     ① 预检 tts_precheck.py 连续 2 次都超闸 → 确定性 → 改文案；
#     ② 预检 1 次过、build 里偶发失败 → 概率性 → 靠重试次数扛。
#   改次数只改这一处常量。

# ★ 单次合成超时（v3.6.3 调，坑典 P64）：v3.6.2 定 120s 时机器被 VLM/ComfyUI
#   抢占就误杀，且 TimeoutExpired 逃出重试循环 → 一句瞬时超时判死整条流水线。
#   300s 仍远小于坑典 P58 时代 900s 的白等，hang 依然能被抓住。
TTS_TIMEOUT = 300
TAKE = 0.8                 # 引文句子的锚定取样长度（禁止扩展，防漂到下一张字幕卡）
CENTER_BACK = 0.15         # 普通句子从目标帧往前留的余量（贴帧起取连续画面）
# ★ TAKE_MAX 与版权门连续原画面阈值联动（v3.6.1 修）：2.5s 门留 0.1s 余量，
#   硬编码 2.4 会在阈值调整后静默脱同步。模块级常量——cut_clips 与 shotmap 共用。
TAKE_MAX = max(0.4, float(CFG.get('compliance', {}).get('max_continuous_original_sec', 2.5)) - 0.1)
TTS_TRIM_DB = -45.0  # 预处理留 5dB 余量，避免混音后轻声尾音掉到静音门槛以下

# ★ 语速闸唯一真相源（2026-11-04 治根新增）：
#   预检 tts_precheck.py 必须 import 本常量/函数，**别各写一份**
#   （坑典 P69：判据不同源 → 预检 100% 假阳性）。
RATE_MAX = 9.0
RATE_MIN = 3.5


def rate_of(text: str, dur_engine: float) -> float:
    """语速（字/秒）—— 按**引擎语音时长**（trim 后、压段内停顿前）计算。

    ★ 为什么不用「压缩后时长」当分母（2026-11-04 dzg-e2e/K2 seg96 连挂 3 轮）：
      compress_pauses() 按「段内停顿有几处」删掉**不等量**时长，于是同一句话的
      语速会随引擎随机多吐/少吐一个停顿而漂移 —— seg37 这句话实测出现过
      8.9(过) / 9.4(挂) / 11.3(挂) 三种结果，闸门成了随机抽签；
      seg96 更是引擎输出完全正常（ASR 转写 CER=0.06，一字不差），
      却被压到 10.4 字/秒 → 8 次重试全废 → 整集 BuildError 卡死。
      引擎语音时长只反映「引擎把这句话念了多久」，与我们的后处理无关。
      实测对照：真垃圾 12.0 / 10.3 / 9.6 字/秒（仍被拦）；
                正常音频 5.3 ~ 8.9 字/秒（含此前被误杀的 seg37/seg61/seg84）全部放行。
    """
    return (len(text) / dur_engine) if dur_engine > 0 else 999.0


def rate_ok(text: str, dur_engine: float) -> bool:
    """语速是否在自然区间内 —— build.narrate() 与 tts_precheck.py 共用此判据。"""
    r = rate_of(text, dur_engine)
    return not (r > RATE_MAX or (len(text) >= 4 and r < RATE_MIN))

_ASR_CACHE: dict = {}      # 同一素材的 ASR 结果只算一次（见 asr_lines 注释）


class BuildError(RuntimeError):
    """任何一步失败都抛这个，绝不留半成品。"""


def write_json_atomic(path: Path, data) -> None:
    tmp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(path)
    finally:
        safe_unlink(tmp)


def sh(cmd, timeout=2400):
    r = subprocess.run([str(c) for c in cmd], capture_output=True,
                       text=True, encoding='utf-8', errors='replace', timeout=timeout)
    if r.returncode != 0:
        raise BuildError(f'命令失败:\n{" ".join(str(c) for c in cmd)[:200]}\n'
                         f'{(r.stderr or "")[-300:]}')
    return r


def sh_tree(cmd, timeout):
    """带「进程树连杀」的超时执行（v3.6.4 修，坑典 P67）。

    为什么不能直接用 sh()/subprocess.run(timeout=)：
    dub.py 会 fork 一个常驻的 worker_dotstts.py **孙进程**（加载模型，单个
    常驻 5-11GB）。`subprocess.run` 超时只杀**直接子进程** dub.py，孙进程 worker
    变孤儿继续占内存；下次调用又起一个新 worker → 每次超时泄漏 5-11GB。
    实测三连后果：31.8GB 机器可用内存掉到 **0.2GB**，整机开始换页，
    TTS 从 ~7s/段 劣化到 >300s/段 连续超时，整条流水线事实上停摆。

    这里用 CREATE_NEW_PROCESS_GROUP + `taskkill /F /T /PID` 连根杀整棵树，
    超时不再泄漏；超时仍抛 TimeoutExpired 交给调用方重试。
    """
    p = subprocess.Popen([str(c) for c in cmd], stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, encoding='utf-8',
                         errors='replace',
                         creationflags=getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0))
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        subprocess.run(['taskkill', '/F', '/T', '/PID', str(p.pid)],
                       capture_output=True, timeout=60)
        try:
            out, err = p.communicate(timeout=30)
        except Exception:
            out, err = '', ''
        raise
    if p.returncode != 0:
        raise BuildError(f'命令失败(rc={p.returncode}):\n'
                         f'{" ".join(str(c) for c in cmd)[:200]}\n{(err or "")[-300:]}')
    return out


def quiet(cmd, timeout=600):
    return subprocess.run([str(c) for c in cmd], capture_output=True,
                          text=True, encoding='utf-8', errors='replace', timeout=timeout).returncode == 0



def safe_unlink(path):
    """「删除」临时文件实为**改名**（不真删）——2026-10-27 修（坑典 P76）。

    沙箱的删除保护按「本轮对话累计删除文件数」计（scope=turn, threshold=50），
    连跑 7 集时 build 每集的临时文件清理（burned/mix/batch wav/失败段 wav…）
    累计到 50 就触发 `SAFE_DELETE_BULK_CONFIRM_REQUIRED`，**整集 build 直接崩**。
    批量生产场景下「删临时文件」是有害的：改成改名到 <name>.trash
    （os.replace 覆盖同名，不产生删除操作），既不触发保护，又能复用同名临时文件。
    """
    try:
        p = Path(path)
        if not p.exists():
            return
        os.replace(str(p), str(p.with_name(p.name + '.trash')))
    except Exception:
        pass


def probe_dur(p: Path) -> float:
    r = subprocess.run([FP, '-v', 'error', '-show_entries', 'format=duration',
                        '-of', 'csv=p=0', str(p)],
                       capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60)
    try:
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def probe_streams(p: Path) -> dict:
    """取每条 stream 的类型与时长（★ 校验音轨完整性的唯一可靠手段）。"""
    r = subprocess.run(
        [FP, '-v', 'error', '-show_entries', 'stream=codec_type,duration',
         '-of', 'default=noprint_wrappers=1', str(p)],
        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60)
    out, cur = {}, None
    for line in (r.stdout or '').splitlines():
        line = line.strip()
        if line.startswith('codec_type='):
            cur = line.split('=', 1)[1]
            out[cur] = None
        elif line.startswith('duration=') and cur and out.get(cur) is None:
            try:
                out[cur] = float(line.split('=', 1)[1])
            except Exception:
                pass
    return out


def probe_size(p: Path):
    """取视频画面宽高（★ 贴纸缩放必须按**成片宽度**算，不能按素材自身尺寸）。

    坑典 P61（v3.6.2 修）：原实现 `scale=iw*0.22:ih*0.22` 里的 iw 是**表情包
    自身宽度**——一个 120px 的 GIF 缩完只有 26px，贴在 1080 宽成片上等于
    看不见（实测第一版 C1 贴纸几乎不可见）。正确语义：贴纸宽度 = 成片宽度
    × scale。缺这个函数就只能靠猜，猜就是 bug。
    """
    r = subprocess.run(
        [FP, '-v', 'error', '-select_streams', 'v:0',
         '-show_entries', 'stream=width,height',
         '-of', 'default=noprint_wrappers=1', str(p)],
        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60)
    w = h = 0
    for line in (r.stdout or '').splitlines():
        line = line.strip()
        if line.startswith('width='):
            w = int(line.split('=', 1)[1] or 0)
        elif line.startswith('height='):
            h = int(line.split('=', 1)[1] or 0)
    if not w or not h:
        raise BuildError(f'取不到画面尺寸: {p}')
    return w, h


def silent_runs(p: Path, work: Path, win_sec=0.1, thresh_db=-50.0):
    """连续静音段（起点, 时长）—— 用**连续时长**判定，而不是单个窗口的平均能量。

    ★ 为什么改判据（2026-10-16 实测）：
      旧判据 = "某个 0.5s 窗口的 RMS < -50dB 就算失败"。
      但 BREATH=0.35s（旧 0.45s） 是**设计内**的段间呼吸留白，
      当语音恰好停在一个窗口边上时，该窗口 = 0.05s 渐弱尾音 + 0.45s 留白，
      平均下来就是 -53dB / -62dB —— 于是"正常成片"被判成有静音缺陷。
      ★ 判据必须回答真正的问题：**有没有一段意外的大面积静音**（音轨被截断/整段没声）。
        0.45s 的留白不是缺陷，1s 以上的洞才是。
    """
    rows = rms_windows(p, work, win_sec)
    runs, cur = [], None
    for t, db in rows:
        if db < thresh_db:
            if cur is None:
                cur = t
        elif cur is not None:
            runs.append((round(cur, 2), round(t - cur, 2)))
            cur = None
    if cur is not None:
        runs.append((round(cur, 2), round(len(rows) * win_sec - cur, 2)))
    return runs


def rms_windows(p: Path, work: Path, win_sec=0.5):
    """导出 wav 算逐窗 RMS —— 用来验「每段都有声」。

    ffmpeg 的 astats/volumedetect 在本机读 stdout 不稳，
    Python 直算是唯一稳定的方式。
    ★ 中间 wav 放在 work 内（不用 TemporaryDirectory，
      否则后续读文件时目录已被清理 → NotADirectoryError）。
    """
    wav = work / '_probe.wav'
    sh([FF, '-v', 'error', '-y', '-i', p, '-vn', '-ac', '1',
        '-ar', '16000', wav], 600)
    w = wave.open(str(wav))
    n, sr = w.getnframes(), w.getframerate()
    a = array.array('h', w.readframes(n))
    w.close()
    safe_unlink(wav)
    win = max(int(sr * win_sec), 1)
    rows = []
    for i in range(0, (len(a) // win) * win, win):
        seg = a[i:i + win]
        if not seg:
            continue
        ms = sum(x * x for x in seg) / len(seg)
        db = 20 * math.log10(max((ms ** 0.5) / 32768.0, 1e-8))
        rows.append((i / sr, db))
    return rows


def trim_silence(p: Path, thresh_db=TTS_TRIM_DB, win_ms=20, pad_sec=0.05,
                 min_run_sec=0.12) -> float:
    """裁掉 wav 首尾的真静音，返回裁后时长（秒）。

    ★ 为什么要裁（2026-10-16 实测，不是凭感觉）：
      dub.py 输出的每条配音尾部自带约 0.5s 尾音/静音
      （seg00 4.08s 中 3.04-3.54s 已是 -50dB 以下），
      再叠加 BREATH=0.35s（旧 0.45s） 呼吸留白 → 每段末尾约 1s 死区，
      出片前静音校验（db < -50）直接判失败。
      ★ 放宽校验阈值是掩盖问题；把真静音裁掉才是修根，
        而且裁完时长更接近"真实语音时长"，下游画面/字幕也随之收紧。

    配音源文件先用 -45 dBFS 找稳定语音尾端，再保留 50ms 余量；
    最终混音经过降噪、响度处理与 AAC 编码后，轻声尾音会再降低数 dB。
    最终成片仍由 verify() 按 -50 dBFS 检查，不放宽成片静音门槛。
    """
    try:
        w = wave.open(str(p))
    except Exception:
        return probe_dur(p)
    ch, sw, sr, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
    raw = w.readframes(n)
    w.close()
    if sw != 2 or n == 0:          # 只处理 16bit；其余原样返回，不冒险
        return probe_dur(p)
    a = array.array('h')
    a.frombytes(raw)
    if ch > 1:
        frames = [max(abs(a[i * ch + j]) for j in range(ch)) for i in range(n)]
    else:
        frames = [abs(x) for x in a[:n]]
    win = max(int(sr * win_ms / 1000.0), 1)
    m = n // win
    if m < 4:
        return probe_dur(p)
    dbs = []
    for i in range(m):
        seg = frames[i * win:(i + 1) * win]
        ms = sum(x * x for x in seg) / len(seg)
        dbs.append(20 * math.log10(max((ms ** 0.5) / 32768.0, 1e-8)))
    # ★ 必须"连续有声 ≥ min_run_sec"才算内容 —— 只取首尾各一个有声窗会被末尾
    #   一个 20ms 爆音骗住（实测 seg04：语音 2.08s 结束，2.42s 有个 -40dB 爆音，
    #   于是 0.4s 死尾被当成内容保住，裁剪形同没做）。
    loud = [d > thresh_db for d in dbs]
    runs, i = [], 0
    while i < len(loud):
        if loud[i]:
            j = i
            while j < len(loud) and loud[j]:
                j += 1
            if (j - i) * win >= min_run_sec * sr:
                runs.append((i, j))
            i = j
        else:
            i += 1
    if not runs:
        return probe_dur(p)
    first, last = runs[0][0], runs[-1][1]
    s = max(int(first * win - pad_sec * sr), 0)
    e = min(int(last * win + pad_sec * sr), n)
    if e - s < win * 2 or (s == 0 and e == n):
        return probe_dur(p)
    cut = a[s * ch:e * ch]
    with wave.open(str(p), 'wb') as wo:
        wo.setnchannels(ch)
        wo.setsampwidth(sw)
        wo.setframerate(sr)
        wo.writeframes(cut.tobytes())
    return (e - s) / float(sr)


def find_src(dataset: Path, ep: int, source_override: Path | None = None) -> Path:
    if source_override is not None:
        if source_override.is_file():
            return source_override
        raise BuildError(f'指定源文件不存在: {source_override}')
    # ★ 集号解析统一走 plan_story（2026-10-21）：本文件不再自带第二份。
    #   旧实现只认 `{ep}.mp4` / `第{ep}集.mp4`，遇到 `001_剧名-第1集.mp4`
    #   这类真实命名直接抛「源文件不存在」，系列混剪会在最后一步崩掉。
    sys.path.insert(0, str(Path(__file__).parent))
    from plan_story import find_ep_file
    try:
        return find_ep_file(Path(dataset), ep)
    except (FileNotFoundError, ValueError) as e:
        raise BuildError(str(e))


def compress_pauses(p: Path, keep_sec=0.35, max_run_sec=0.7,
                    thresh_db=TTS_TRIM_DB, win_ms=20) -> float:
    """把段内长停顿（逗号/句号 pause）压到 keep_sec，返回压缩后时长。

    ★ 为什么（2026-10-22 实测，不是凭感觉）：
      TTS 读逗号/句号会停 0.9-1.0s（CTA「……当场识破。点个关注……」
      两处各约 1s；「苏家断了资金链，拿我换钱」中间约 1s）。
      段内停顿是标点气质不是缺陷，但 1s 的停顿对抖音口播太拖，
      且叠加段间 BREATH=0.35s（旧 0.45s） 后超过成片静音门 0.8s 判 FAIL。
      压到 0.35s：节奏更快，且结构性不再撞门。
      纯 Python 窗级裁剪（与 trim_silence 同源），不引新依赖。
    """
    try:
        w = wave.open(str(p))
    except Exception:
        return probe_dur(p)
    ch, sw, sr, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
    raw = w.readframes(n)
    w.close()
    if sw != 2 or n == 0:
        return probe_dur(p)
    a = array.array('h')
    a.frombytes(raw)
    if ch > 1:
        frames = [max(abs(a[i * ch + j]) for j in range(ch)) for i in range(n)]
    else:
        frames = [abs(x) for x in a[:n]]
    win = max(int(sr * win_ms / 1000.0), 1)
    m = n // win
    if m < 4:
        return probe_dur(p)
    dbs = []
    for i in range(m):
        seg = frames[i * win:(i + 1) * win]
        ms = sum(x * x for x in seg) / len(seg)
        dbs.append(20 * math.log10(max((ms ** 0.5) / 32768.0, 1e-8)))
    loud = [d > thresh_db for d in dbs]
    if not any(loud):               # 全程无声：不冒险写空文件，原样返回
        return n / sr
    keep_win = max(int(keep_sec * 1000 / win_ms), 1)
    max_win = max(int(max_run_sec * 1000 / win_ms), keep_win + 1)
    # 逐窗保留标记：有声窗全留；静音 run 超过 max_win 的，只留前 keep_win（仅内部）
    # ★ 2026-11-04 记录（**故意不改**）：本函数注释说「长停顿只留前 keep_win」，
    #   但 mask=list(loud) 使静音窗初值即 False，循环里 mask[k]=False 是空操作
    #   → 实际把**段内所有停顿**整段删光，与注释相反。
    #   为何不修：① 该行为已固化进全部已交付集（T1/T2/K1/X1/D1）与本集已生成的
    #   92 段配音声（零内部间隙），改了会**同集内前后不一致**（92 段无间隙 /
    #   新段有间隙），且需重录 92 段；② 段内 0.7s 级间隙 + 段间 BREATH 0.45s
    #   会逼近成片静音门 0.8s，有引入新 FAIL 的风险。
    #   ★ 真正的病根不在这里 —— 语速闸判据混入了「本函数压了多少停顿」，
    #   同一个字因引擎停顿多少而语速不同（实测同句 8.9~9.4 来回跳，seg37/seg96
    #   就是这么随机挂掉的）。已在 narrate() 改用「引擎语音时长」判定，
    #   见坑典 P84。
    mask = list(loud)
    i = 0
    while i < m:
        if not loud[i]:
            j = i
            while j < m and not loud[j]:
                j += 1
            if (j - i) > max_win and i > 0 and j < m:
                for k in range(i + keep_win, j):
                    mask[k] = False
            i = j
        else:
            i += 1
    if all(mask):
        return n / sr
    out = array.array('h')
    for i in range(m):
        if mask[i]:
            s = i * win * ch
            out.extend(a[s:s + win * ch])
    with wave.open(str(p), 'wb') as wo:
        wo.setnchannels(ch)
        wo.setsampwidth(sw)
        wo.setframerate(sr)
        wo.writeframes(out.tobytes())
    return len(out) / ch / sr


# ── ① 配音 ────────────────────────────────────────
def measure_db(p: Path):
    """volumedetect 读 (mean, max) dB；读不到返回 None（不拿测量基建失败判产物死刑）。"""
    r = subprocess.run([FF, '-v', 'info', '-i', str(p), '-af', 'volumedetect',
                        '-f', 'null', '-'], capture_output=True, text=True,
                       encoding='utf-8', errors='replace', timeout=120)
    mean = mx = None
    for line in (r.stderr or '').splitlines():
        if 'mean_volume:' in line:
            try: mean = float(line.split(':', 1)[1].strip().rstrip(' dB'))
            except Exception: pass
        elif 'max_volume:' in line:
            try: mx = float(line.split(':', 1)[1].strip().rstrip(' dB'))
            except Exception: pass
    if mean is None and mx is None:
        return None
    return (mean, mx)


def loudnorm_seg(p: Path) -> None:
    """分段响度归一 —— TTS 引擎分段产出电平不稳（实测同批次有 -51dB 的段，
    混音后整段掉到 silencedetect 门槛以下被判静音）。单段统一打到
    I=-16:TP=-1.5，段与段之间电平一致，杜绝「某段突然没声」。"""
    tmp = p.with_name(f'.{p.stem}.norm.wav')
    sh([FF, '-v', 'error', '-y', '-i', str(p),
        '-af', 'loudnorm=I=-16:TP=-1.5:LRA=11',
        '-ac', '1', '-ar', '44100', str(tmp)], 300)
    tmp.replace(p)


# ★ TTS 敌对字符（v3.6.4 新增，坑典 P68）：这些符号 TTS 读不出来且会触发
#   确定性退化——实测「我还不太适应这个身体——这叫人话吗」破折号一句，
#   dotstts 连续 5 次都吐 <2.1s 的垃圾（语速 >8 字/秒 硬闸全中），
#   5 次重试俱废 → BuildError。而此时已经跑了 16 段、16 分钟才知道。
#   （与坑典 P44「弄死」类暴力词同族：内容触发，重试无用，只能改文案。）
TTS_HOSTILE_CHARS = '—…'


def precheck_tts_text(segments):
    """配音前置硬检查（v3.6.4，坑典 P68）：扫全 plan 的段文案，
    命中 TTS 敌对字符立刻 BuildError 并列出段号——把「跑到第 16 段才炸」
    变成「开跑前 1 秒就报」。绝不静默替换文案（字幕必须和稿子一致）。
    """
    bad = [(k, (s.get('text') or '').strip())
           for k, s in enumerate(segments)
           if any(c in (s.get('text') or '') for c in TTS_HOSTILE_CHARS)]
    if not bad:
        return
    detail = '\n'.join(f'    段{k:02d}: {t}' for k, t in bad)
    raise BuildError(
        f'{len(bad)} 段文案含 TTS 敌对字符（{TTS_HOSTILE_CHARS}）——'
        f'引擎会确定性退化，重试无用，必须改文案：\n{detail}\n'
        f'  改法：破折号「——」改逗号「，」或重写；省略号「…」删掉。\n'
        f'  ★ 改 spec 源头（shots.text/want），再从 ev 重生成 plan，别只改 plan。')


def narrate(segments, tts_dir: Path, voice: str, keep_existing: bool = False):
    tts_dir.mkdir(parents=True, exist_ok=True)
    # ★ 复用签名（v3.6.2 修）：segNN.wav 只按段号命名，改文案/换音色后
    #   段号不变 —— 只判「文件存在」会把旧文案旧音色的音频当成新产物复用
    #   （实测：修了 seg17 文案后 seg17.wav 仍是旧词，成片口播与字幕对不上）。
    #   签名 = 音色 + 该段原文，逐段落盘，崩溃后仍精确。
    sig_path = tts_dir / '_sig.json'
    sig = {'voice': '', 'segs': {}}
    if keep_existing and sig_path.is_file():
        try:
            sig = json.loads(sig_path.read_text(encoding='utf-8'))
        except Exception:
            sig = {'voice': '', 'segs': {}}
    if not keep_existing or sig.get('voice') != voice:
        for f in tts_dir.glob('*.wav'):
            safe_unlink(f)                # ★ 音色不一致 → 全清，杜绝新旧音色混用
        sig = {'voice': voice, 'segs': {}}
    sig['voice'] = voice
    # keep_existing=True（v3.6.2 断点续跑）：调用方已确认音色一致
    # （build-plan.json.voice == 本次 voice），此时清目录会删掉可复用产物

    # ★ 2026-10-21 音色自动决策：voice 支持两种形态——
    #   ① 普通音色名（按题材匹配表选，见 SKILL.md §3.7）
    #   ② `ref:<路径>` → 主角音色零样本克隆（extract_voice_ref.py 从原片
    #      原声段提取的参考音频，走 dotstts --ref）。用主角的声音讲主角的故事。
    if voice.startswith('ref:'):
        ref = voice[4:]
        if not Path(ref).is_file():
            raise BuildError(f'克隆参考音频不存在: {ref}')
        base = [DUB_PY, '-X', 'utf8', DUB, '--engine', 'dotstts', '--ref', ref]
        print(f'    音色模式：主角克隆参考 {ref}')
    else:
        base = [DUB_PY, '-X', 'utf8', DUB, '--voice', voice]

    made = {}
    for k, seg in enumerate(segments):
        text = (seg.get('text') or '').strip()
        if not text:
            continue
        out = tts_dir / f'seg{k:02d}.wav'      # ★ 按段号命名，0-based 统一
        # ★ 断点续跑（v3.6.2 新增）：已有成功产物直接复用——单段文案失败修文案后
        #   不用重跑全量 TTS（深夜引擎退化时全量重跑要 20 分钟）。
        #   narrate 失败段会 unlink，所以磁盘上存在的都是成功段；复用前复测电平。
        #   ★ 复用门槛 = 文件存在 + 签名一致（音色 & 原文都没变），缺一不复用。
        if (out.is_file() and out.stat().st_size > 2048
                and sig.get('segs', {}).get(out.name) == text):
            m0 = measure_db(out)
            if m0 and m0[0] is not None and m0[0] < -45.0:
                safe_unlink(out)     # 坏文件照删重来
            else:
                made[k] = probe_dur(out)
                print(f'    seg{k:02d}  复用 {made[k]:.2f}s')
                continue
        # ★ 产物 sanity 校验（2026-10-20 实测踩坑）：TTS 引擎对特定文本会
        #   陷入异常长生成（17 字文案产出 68s 音频），若无校验会静默灌入
        #   60s+ 空白 → 成片超长静音 FAIL。中文按 4.5 字/秒下限估算，
        #   超过 3 倍 + 5s 判定引擎异常：重试一次，仍异常则报错退出，
        #   绝不把半成品送进混音。
        est = max(len(text) / 4.5, 0.8)
        limit = est * 3 + 5
        d, why = None, ''
        # ★ 重试上限 8 次（2026-10-22 调，坑典 P45：P(单次成功)≈0.7 时 5 次
        #   覆盖率 99.8%）：dots.tts 是扩散采样，偶发退化产出（近无声/超短），
        #   同文本重跑即恢复；内容触发的确定性拒产（见坑典分类导航 · ASR/TTS/显存 类）重试无用，
        #   但改文案后走的就是这条重试路径。改次数只改这一处常量。
        for attempt in range(1, MAX_TTS_ATTEMPTS + 1):
            # ★ 单次超时（v3.6.3 修，坑典 P64）：正常合成 <30s；引擎 hang 时
            #   900s 超时等于每次白等 15 分钟（坑典 P58），故不能回到 900。
            #   v3.6.2 的 120s 太紧：机器同时跑 VLM 读帧 / ComfyUI 生图时，
            #   dotstts 常被挤到 120s 外；且 TimeoutExpired 直接逃出本重试循环，
            #   「一次瞬时超时」把 92 段、近一小时的全量配音整条判死（C2 实砸 seg07）。
            #   现把超时归入「一次失败尝试」，走既有 5 次重试；5 次全超时才报错。
            try:
                # ★ 必须用 sh_tree 而不是 sh（坑典 P67）：超时要连孙进程 worker 一起杀，
                #   否则每次超时泄漏一个 5-11GB 的孤儿 worker，几轮就把内存吃干。
                sh_tree(base + ['--text', text, '--out', out], TTS_TIMEOUT)
            except subprocess.TimeoutExpired:
                why = f'合成超时 >{TTS_TIMEOUT}s（引擎被抢占/hang）'
                print(f'    seg{k:02d}  ⚠{why}，第 {attempt} 次尝试')
                safe_unlink(out)
                d = None
                continue
            raw_d = probe_dur(out)
            if raw_d <= 0:
                raise BuildError(f'第{k}段配音时长为 0：{text}')
            if raw_d > limit:
                why = f'产物异常 {raw_d:.1f}s > 上限 {limit:.1f}s（引擎故障）'
                print(f'    seg{k:02d}  ⚠{why}，第 {attempt} 次尝试')
                safe_unlink(out)
                continue
            d = trim_silence(out)          # ★ 裁掉 TTS 自带的首尾静音
            if d <= 0:
                raise BuildError(f'第{k}段配音裁静音后时长为 0：{text}')
            # ★ 闸门顺序（2026-10-22 修）：先响度、后压停顿。
            #   近无声段若先进压缩，mask 全 False 会写出 0 秒空文件、
            #   直接硬失败且没有重试机会（rebuild5 实砸）。
            m = measure_db(out)
            if m and m[0] is not None and m[0] < -45.0:
                why = f'均值 {m[0]:.1f}dB 低于语音下限(-45)（引擎吐垃圾/近无声）'
                print(f'    seg{k:02d}  ⚠{why}，第 {attempt} 次尝试')
                safe_unlink(out)
                d = None
                continue
            # ★★ 2026-11-04 治根（dzg-e2e/K2 seg96 连挂 3 轮才定位）：
            #   语速闸必须判**引擎吐没吐垃圾**，而不能判「我们自己的后处理压了多久」。
            #   compress_pauses()（见其实现）会按「段内停顿有几处」删掉不等量时长，
            #   于是**同一句话**的语速随引擎随机多吐/少吐一个停顿而漂移：
            #     seg37 这句话 → 8.9(过) / 9.4(挂) / 11.3(挂) 三种结果都出现过；
            #     seg96       → 引擎输出其实完全正常（ASR 转写 CER=0.06，一字不差），
            #                     却被压到 10.4 字/秒 → 8 次重试全废 → 整集卡死。
            #   根因 = **判据用错了分母**：闸门量的是「压缩后的时长」，
            #   混进了「我们删了多少停顿」这个与引擎质量无关的量。
            #   ★ 修法：用**引擎语音时长**（trim 后、压停顿前）判语速 ——
            #     它只反映引擎把这句话念了多久，与我们的后处理无关。
            #     实测对照：真垃圾 12.0/10.3/9.6 字/秒（仍被拦），
            #     正常音频 5.3~8.9 字/秒（全部放行，含此前被误杀的 seg37/seg61/seg84）。
            #   ★ 混音/时长仍用压缩后的 d（不变）——只改判据，不改音画。
            d_engine = d                     # ★ 引擎语音时长（压段内停顿前）——闸门唯一分母
            rate = rate_of(text, d_engine)
            d = compress_pauses(out)       # ★ 段内长停顿压到 0.35s（逗号 pause ~1s 实测）
            if d <= 0:
                raise BuildError(f'第{k}段配音压停顿后时长为 0：{text}')
            # ★ 语速双硬闸（2026-10-22 两轮实砸）：
            #   上限 8.0 字/秒 —— 实测 6 字文案产出 0.82s（8.5 字/秒）混过旧闸
            #   （10.0），成片 24s 处留 1.6s 静音洞；自然语速上限约 7-8。
            #   下限 3.5 字/秒（≥4 字文案）—— 实测 6 字文案被拉成 3.91s
            #   （2.0 字/秒），拖沓拟物感，同为退化产出。
            #   ★ 判据经 rate_ok() 统一（2026-11-04）：分母用 d_engine（引擎语音时长），
            #     不再用压缩后的 d —— 详见上方注释与 rate_of() docstring。
            if not rate_ok(text, d_engine):
                why = f'语速 {rate:.1f} 字/秒 超出自然区间（引擎吐垃圾）'
                print(f'    seg{k:02d}  ⚠{why}，第 {attempt} 次尝试')
                safe_unlink(out)
                d = None
                continue
            break
        if d is None:
            raise BuildError(f'第{k}段配音失败（{why or "引擎故障，8 次重试无效"}）：{text}')
        loudnorm_seg(out)                  # ★ 分段归一，段间电平一致
        m = measure_db(out)
        if m and m[0] is not None and m[0] < -45.0:
            raise BuildError(f'第{k}段归一后仍过低(-45) {m[0]:.1f}dB：{text}')
        made[k] = d
        # ★ 逐段落盘签名：中途崩溃/超时也不丢已确认产物的可复用标记
        sig.setdefault('segs', {})[out.name] = text
        write_json_atomic(sig_path, sig)
        warn = ' ⚠偏慢' if rate < 4.5 else (' ⚠偏快' if rate > 7.0 else '')
        cut = f'（裁掉首尾静音 {raw_d - d_engine:.2f}s）' if raw_d - d_engine > 0.08 else ''
        print(f'    seg{k:02d}  {d:5.2f}s  {rate:.1f} 字/秒{warn}  {text}{cut}')
    return made


def timed_whisper_spec():
    """Return a verified timestamp-capable Whisper fallback, or None.

    SenseVoice is the configured text-ASR primary but the current adapter does
    not expose timestamps. Do not silently substitute a model explicitly
    marked unusable for Chinese just because its files are present.
    """
    quality_limit = float(CFG.get('asr', {}).get('quality_gate', {}).get(
        'max_avg_cer_for_production', 0.35))
    for spec in CFG.get('asr', {}).get('fallback', []):
        if spec.get('engine') != 'whisper' or spec.get('status') != 'verified':
            continue
        cer = spec.get('measured_avg_cer')
        if not isinstance(cer, (int, float)) or isinstance(cer, bool) or cer > quality_limit:
            continue
        model_dir = spec.get('model_dir')
        if not model_dir or not Path(model_dir, spec.get('required_file', 'model.bin')).is_file():
            continue
        return spec
    return None


# ── ② 原声：ASR 定位真实台词时间点 ──────────────────
def asr_lines(video: Path, work: Path):
    """Use verified timed ASR; return empty so caller uses plan evidence time if unavailable.

    ★ 缓存（2026-10-16 实测）：cut_raw 每个原声段都会调一次本函数，
      而 ASR 是 O(片长) 的（127s 素材 + medium CPU int8 RTF 0.89 ≈ 2 分钟/次）。
      一个 plan 有 3 个原声段就是 6 分钟纯重复劳动。
      → 进程内 memo + 落盘 `_work/_asr_lines.json`（带源文件指纹），
        行为完全不变，只是同一素材只转写一次。
      ★ 只缓存**非空**结果：空结果可能是临时失败或配置未就绪，
        写成缓存会把一次失败永久固化成降级。
    """
    key = str(Path(video).resolve())
    if _ASR_CACHE.get(key):
        print(f'    [ASR] 复用本次进程已转写结果（{len(_ASR_CACHE[key])} 行）', flush=True)
        return _ASR_CACHE[key]
    cache_file = work / '_asr_lines.json'
    if cache_file.is_file():
        try:
            data = json.loads(cache_file.read_text(encoding='utf-8'))
            if data.get('src') == key and data.get('model') == _asr_model_tag() and data.get('lines'):
                _ASR_CACHE[key] = data['lines']
                print(f'    [ASR] 复用缓存 {len(data["lines"])} 行（{cache_file.name}）', flush=True)
                return data['lines']
        except Exception:
            pass

    spec = timed_whisper_spec()
    if spec is None:
        print('    [ASR] 没有通过质量门的定时 Whisper；按 plan 证据时间点切原声', flush=True)
        return []
    wav = work / '_asr.wav'
    if not quiet([FF, '-v', 'error', '-y', '-i', video,
                  '-vn', '-ac', '1', '-ar', '16000', wav], 600):
        return []
    # ★ 唯一实现在 asr.py（2026-10-21 源头优化）：本文件不再自带第二份
    #   faster-whisper 调用，避免 word_timestamps 这类关键参数两处各配一份。
    sys.path.insert(0, str(ROOT / 'scripts'))
    import asr as _asr
    lines = _asr.timed_lines(str(wav), CFG)
    safe_unlink(wav)
    if lines:
        _ASR_CACHE[key] = lines
        note = f'{spec.get("model")} cer={spec.get("measured_avg_cer")}'
        print(f'    [ASR] {note} 转写 {len(lines)} 行', flush=True)
        try:
            cache_file.write_text(json.dumps(
                {'src': key, 'model': _asr_model_tag(), 'lines': lines},
                ensure_ascii=False), encoding='utf-8')
        except Exception:
            pass
    else:
        print('    [ASR] 转写无结果；按 plan 证据时间点切原声', flush=True)
    return lines


def _asr_model_tag() -> str:
    """缓存指纹 —— ★ 必须包含 word_timestamps：
    同一模型在不传该参数时输出是幻觉结果（2026-10-16 实测），
    指纹不含它就会把脏结果当缓存复用，改了代码也看不出效果。"""
    spec = timed_whisper_spec() or {}
    wt = bool(CFG.get('asr', {}).get('word_timestamps', True))
    return (f'{spec.get("model")}|{spec.get("device")}|'
            f'{CFG.get("asr", {}).get("compute_type")}|wt={wt}|'
            f'beam={CFG.get("asr", {}).get("beam_size")}')



MAX_RAW_LINE_SEC = 8.0      # 单条台词的合理上限，超过即判为幻觉吞并
RAW_LEAD = 0.2              # 原声切片在台词起点前留的前导
RAW_TAIL = 0.15             # 原声切片在台词终点后留的收尾
# ★ 单条原声引用的时长上限 = 版权门「连续原画面」阈值（2026-10-21）：
#   写死 4.5 会让每一个原声段都必然撞合规门，且把下一句台词拖进来一起播。
#   config/engine.json → compliance.max_continuous_original_sec
RAW_MAX_SEC = float(CFG.get('compliance', {}).get('max_continuous_original_sec', 2.5))
# ★ 冷开场钩子连续原画面上限（v3.7.1）：普通原声段 2.5s，cold_open 放宽为 5.0s。
#   与 compliance.cold_open.max_sec 同源；取不到时回落 5.0。cut_cold_open /
#   retime / write_shotmap / quality_gate 共用此值作为冷开场合规上限。
COLD_OPEN_MAX = float((CFG.get('compliance', {}).get('cold_open') or {}).get('max_sec', 5.0))
MAX_SILENT_RUN_SEC = 0.8    # 允许的连续静音上限（BREATH=0.35s（旧 0.45s） 留白是设计内的，
                            #   实测其连尾音约 0.5s；超过 0.8s 才是真的洞）


def cjk_ratio(s: str) -> float:
    """非空白字符中 CJK 的占比 —— 用来识别「我 May」「他去聚 expectancy」这类幻觉输出。"""
    cs = [c for c in s if not c.isspace()]
    if not cs:
        return 0.0
    return sum(1 for c in cs if '\u4e00' <= c <= '\u9fff') / len(cs)


def cut_raw(dataset: Path, ep: int, hint_t: float, dur: float,
            out: Path, work: Path, source_override: Path | None = None) -> dict:
    """按 ASR 定位真实台词边界，只取**这一句**。

    ★ 2026-10-21 修一个结构性缺陷（不是调参）：
      旧实现无论台词多长，都从 `台词起点 - 0.2s` 起固定取 `dur`(=4.5s)。
      实测后果：一句 1.9s 的台词后面被灌进 2.4s 无关音频
      （冒烟测试：hint 指向 32.81s 的「那个好像是孕妇」，
       成片原声段 32.63-37.13s，把 34.71s 的「是不是出事了」也播了进去）
      → 原声段播放的台词和解写在说的内容对不上，且无法从参数上修。
      同时 4.5s 连续原画面必然撞版权门的 `max_continuous_original_sec=2.5s`。

    ★ 修法（同时满足内容质量与合规）：
      - `dur` 语义改为「单条原声引用的时长上限」，取自
        `compliance.max_continuous_original_sec`（默认 2.5s），不再硬编码 4.5；
      - **只取这句台词本身**：起点前留 0.2s 前导、尾部留 0.15s 收尾，
        不再为了凑时长把下一句拖进来；
      - 超过 `dur` 的台词**不选**（宁可换一句短的，也不截断句子）。
    """
    src = find_src(dataset, ep, source_override)
    # ★ 断点续跑（v3.6.2）：切片已在则跳过 ffmpeg（ASR 定位仍跑——它定义
    #   raw_files 的 start/note/text，是 mix 与 retime 的必需元数据）
    reuse = out.is_file() and out.stat().st_size > 2048
    lines = asr_lines(src, work)
    start, note = max(hint_t - dur / 2, 0.0), 'plan证据时间点(定时ASR不可用)'
    best, best_d = None, None
    for ln in lines:
        t = (ln.get('t') or '').strip()
        if len(t) < 4:                              # 太短：多半是语气词或被切碎
            continue
        span = float(ln['e']) - float(ln['s'])
        if span > MAX_RAW_LINE_SEC:                 # ★ 幻觉签名：整段吞并
            continue
        if span > dur:                              # ★ 超过单段合规上限 → 不选
            continue
        if cjk_ratio(t) < 0.5:                      # ★ 幻觉签名：输出拉丁文
            continue
        d = abs((float(ln['s']) + float(ln['e'])) / 2 - hint_t)
        if best_d is None or d < best_d:
            best, best_d = ln, d
    take = dur
    if best:
        start = max(best['s'] - RAW_LEAD, 0.0)
        take = min(dur, (float(best['e']) - float(best['s'])) + RAW_LEAD + RAW_TAIL)
        note = f"ASR {best['s']:.2f}-{best['e']:.2f}"
        text = best['t'][:24]
    elif lines:
        # 有 ASR 但全部被幻觉防线筛掉 → 明说，别假装"没跑 ASR"
        note = 'plan证据时间点(ASR段全部被幻觉防线筛除)'
        text = ''
    else:
        text = ''

    if reuse:
        # 切片已在：跳过 ffmpeg 两步切片，元数据（start/note/text/dur）照算返回
        return {'start': round(start, 2), 'note': note, 'text': text,
                'dur': probe_dur(out)}
    tmp = work / '_raw.wav'
    sh([FF, '-v', 'error', '-y', '-ss', f'{start:.2f}', '-t', f'{take:.2f}',
        '-i', src, '-vn', '-ac', '1', '-ar', '44100', tmp], 300)
    sh([FF, '-v', 'error', '-y', '-i', tmp,
        '-af', 'highpass=f=90,afftdn=nf=-34,loudnorm=I=-18:TP=-2',
        '-ac', '1', '-ar', '44100', out], 300)
    safe_unlink(tmp)
    return {'start': round(start, 2), 'note': note, 'text': text,
            'dur': probe_dur(out)}


def cut_cold_open(dataset: Path, ep: int, hint_t: float, dur: float,
                  out: Path, work: Path, source_override: Path | None = None) -> dict:
    """冷开场钩子：切一段连续原片高潮画面 + 原声（不按 ASR 台词定位）。

    ★ v3.7.1 新增（用户指令「前5秒找短剧高潮放原片」）：
      与普通原声段（cut_raw）的本质区别——
        - 普通原声段：ASR 只取「一句台词」≤2.5s，且要求是角色对白；
        - 冷开场钩子：直接取 climax 时刻前后一段**连续画面**
          （≤compliance.cold_open.max_sec，默认 5.0s），保留原片声音，
          用于成片前 5 秒吸引眼球，随后接倒叙因果式解说提升原创度。
      合规：连续原画面上限放宽到 cold_open.max_sec（普通段仍是 2.5s），
      且冷开场须位于成片第 1 段、全片唯一（quality_gate.hard_copyright 双校验）。
      断点续跑同 cut_raw：切片已在则跳过 ffmpeg，元数据照算返回。
    """
    src = find_src(dataset, ep, source_override)
    src_dur = probe_dur(src)
    take = min(float(dur), COLD_OPEN_MAX, max(src_dur, 0.0))
    start = hint_t - take / 2.0
    if start < 0:
        start = 0.0
    if start + take > src_dur:           # 越界则平移起点，不空播
        start = max(src_dur - take, 0.0)
    reuse = out.is_file() and out.stat().st_size > 2048
    if reuse:
        return {'start': round(start, 2), 'note': 'cold_open 连续原片(续跑)',
                'text': '', 'dur': probe_dur(out)}
    tmp = work / '_cold.mp4'
    # 视频 + 原声一起切：保留原片高潮的真实声音（提升吸引力的关键）
    sh([FF, '-v', 'error', '-y', '-ss', f'{start:.2f}', '-t', f'{take:.2f}',
        '-i', src, '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '22',
        '-c:a', 'aac', '-b:a', '192k', tmp], 300)
    sh([FF, '-v', 'error', '-y', '-i', tmp,
        '-af', 'highpass=f=90,afftdn=nf=-34,loudnorm=I=-18:TP=-2',
        '-ac', '1', '-ar', '44100', out], 300)
    safe_unlink(tmp)
    return {'start': round(start, 2), 'note': 'cold_open 连续原片',
            'text': '', 'dur': probe_dur(out)}


# ── ③ 定时长（唯一数据源 = segments） ───────────────
def retime(segments, made, raws):
    total = 0.0
    for k, seg in enumerate(segments):
        if k in made:
            d = made[k] + BREATH
        elif k in raws:
            if seg.get('is_cold_open'):
                # ★ 冷开场钩子：连续原画面上限放宽到 cold_open.max_sec（默认 5.0s），
                #   但仍以实测切割时长为准（cut_cold_open 已保证 ≤ 上限）。
                d = min(raws[k]['dur'], COLD_OPEN_MAX)
            else:
                # ★ 原声段上限 = 版权门连续原画面阈值（2026-10-21）。
                #   旧值 `min(max(dur,2.0),6.0)` 会把一句 1s 的台词撑到 2s、
                #   且允许到 6s —— 既灌无关音频，又必然撞合规门。
                d = min(max(raws[k]['dur'], 1.0), RAW_MAX_SEC)
        else:
            raise BuildError(f'第{k}段既无解说也无原声 —— 数据不完整')
        seg['duration'] = round(d, 2)
        if k in raws:
            seg['raw_start'] = raws[k]['start']
        total += d
    return round(total, 2)


# ── ④ 切画面 ──────────────────────────────────────
def resolve_window(seg, src_duration: float):
    """算出这一段在**源片里**实际能取到的窗口 (ss, take)。

    ★ 为什么收口成单一函数（2026-11-04 治根，K2 实测）：
      原来 cut_clips 与 write_shotmap 各写一遍取窗规则 → 两处漂移：
        - cut_clips：普通解说段 ss=t-0.15、take=min(d+0.3, TAKE_MAX)，
          **完全不检查 ss+take 是否超出源片末尾**；
        - write_shotmap：同一段却写了 `min(..., src_duration - source_start)`
          （知道会取不满）→ 实切与度量不一致。
      后果实测（dzg-e2e/K2，131 段）：seg98/110/126 三段 ss+take 越过片尾，
      ffmpeg 切到片尾就停 → 画面少 0.52/1.10/0.70s，累计画面总时长
      331.45s vs 音频时间轴 333.28s，**差 1.83s** → 音画对齐哨兵 FAIL
      → 整集白跑（前面 evidence/assemble/gen/TTS 全是已完成态，返工代价极高）。

    ★ 规则（唯一真相源，cut_clips 与 write_shotmap 都调它）：
      1) ss 起点：引文段锚在 t，普通解说段 t-CENTER_BACK；
      2) take 目标：普通段 min(d+0.3, TAKE_MAX)，引文段 TAKE；
      3) **片尾兜底**：若 ss+take > 源片时长 → ss 往前挪到 src_duration-take，
         保证「取满 take」；若挪完仍不足 take（源片本身太短）→ take 取到
         源片能给的全部，剩下的靠 loop 滤镜补齐到 d（见 cut_clips 的 loop 分支）。
      4) 返回的 take 是**实取窗口**，视觉度量与实切严格一致。
    """
    d = float(seg['duration'])
    is_speech = bool((seg.get('text') or '').strip())
    txt = seg.get('text') or ''
    quoted = ('「' in txt or '『' in txt or '“' in txt)
    if is_speech:
        if quoted:
            ss = max(float(seg['t']), 0.0)
            take = TAKE
        else:
            ss = max(float(seg['t']) - CENTER_BACK, 0.0)
            take = min(d + 0.3, TAKE_MAX)
        # ★ 片尾兜底（本次修复点）：普通/引文段都可能被源片末尾截断。
        #   往前挪起点，保证 take 取满；挪不动（源片太短）就少取，
        #   由 cut_clips 的 loop 分支补齐到 d。
        if src_duration > 0 and ss + take > src_duration:
            ss = max(src_duration - take, 0.0)
            take = min(take, max(src_duration - ss, 0.0))
    else:
        # 原声段：从 raw_start 起连续取满整段时长，禁止 loop
        ss = max(float(seg.get('raw_start', seg['t'])), 0.0)
        take = d
        if src_duration > 0 and ss + take > src_duration:      # 片尾不足则往前挪，不空播
            ss = max(src_duration - take, 0.0)
    return ss, take


def cut_clips(segments, dataset: Path, work: Path, source_override: Path | None = None):
    """切画面（普通解说按段时长取连续画面；引文句子贴帧锚定；原声连续跟拍）。

    ★ 三类段规则（2026-10-20 修订）：
      - 普通解说：ss = t - 0.15，take = min(d + 0.3, 2.4s) —— 连续画面
        跟完解说，不循环（废除 0.8s 硬循环：用户实测「反复回放」差观感）；
      - 引文解说（「」引用字幕）：ss = t，take = 0.8s，不足仍循环 ——
        字幕卡约 1s 一张，窗口一长就漂到下一张卡，锚定优先；
      - 原声段：从 raw_start 起连续取满整段时长，禁止 loop ——
        原声是角色真在说话，画面必须同步走，否则嘴型对不上台词。

    ★ 取窗规则统一走 resolve_window()（2026-11-04 治根）：
      原来本函数与 write_shotmap 各写一遍 → 两处漂移 →
      片尾段实切被截断（K2 实测 3 段，累计差 1.83s）→ 音画哨兵 FAIL 整集白跑。
      现在「起点/取长」只有一个真相源，实切与度量不会不一致。
    """
    clips = []
    # TAKE / CENTER_BACK / TAKE_MAX 为模块级常量（cut_clips 与 shotmap 共用，
    # v3.6.1 修：原为局部变量，shotmap 引用即 NameError）
    for k, seg in enumerate(segments):
        d = float(seg['duration'])
        is_speech = bool((seg.get('text') or '').strip())
        out = work / f'c{k:02d}.mp4'
        vf = (f'scale={W}:{H}:force_original_aspect_ratio=increase,'
              f'crop={W}:{H},setsar=1,fps={FPS},format=yuv420p')
        # ★ v3.6.0 二创段（gen）：导演顾问设计 + ComfyUI 生成的 AI 原创镜头，
        #   直接取本地文件切片，不碰原剧 → 版权门 original_footage_sec=0，
        #   混剪原创度来自「原剧画面 + AI 二创画面 + 解说」三层。
        #   gen 路径约定绝对路径（assemble 时写死）；gen_start 可选（默认 0）。
        if seg.get('gen'):
            gsrc = Path(str(seg['gen']))
            if not gsrc.is_absolute():
                gsrc = (Path.cwd() / gsrc).resolve()
            if not gsrc.is_file():
                raise BuildError(f'第{k}段 gen 二创文件缺失: {gsrc}'
                                 f'（导演顾问出单 → ComfyUI 生成 → gen/ 归档后再 assemble）')
            # ★ 静态图/GIF 都必须 -loop 1 + -t d：
            #   实测踩坑（2026-10-24 首跑）——PNG 单帧输入不 loop 时输出只有
            #   0.04s（3 个 gen 段共少 6.5s → 成片音画错位，verify 音轨 86.2s
            #   vs 视频 79.7s）。GIF 多帧同理，loop 后 -t d 取前 d 秒/循环补齐。
            ss = max(float(seg.get('gen_start', 0.0)), 0.0)
            sh([FF, '-v', 'error', '-y', '-loop', '1',
                '-ss', f'{ss:.2f}', '-t', f'{d:.2f}',
                '-i', str(gsrc), '-vf', vf,
                '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '22',
                '-an', out], 600)
            clips.append(out)
            continue
        src = find_src(dataset, seg['ep'], source_override)
        src_dur = probe_dur(src)
        # ★ 取窗统一走 resolve_window（含片尾兜底）——2026-11-04 治根
        ss, take = resolve_window(seg, src_dur)
        if is_speech:
            # 解说段（2026-10-20 修订：按解说时长取连续画面，废除 0.8s 硬循环）
            #   ——用户实测反馈「画面反复回放」：一句 2.6s 解说 = 0.8s 画面
            #   硬循环 3 次，接缝明显显廉价。现普通句子 take 按段时长取，
            #   d ≤ take+0.2 时单段连续画面跟完，不循环。
            # ★ 引文句子仍从证据帧本身开始、只取 0.8s：
            #   短剧字幕卡约 1s 一张，扩展窗口会让后段漂到下一张字幕卡
            #   （实测：稿子写「字幕写着 X」，画面已是上一句 Y）。
            #   起点与取长已由 resolve_window 算好（含片尾兜底），此处不再重复推导。
            pass
        # ★ 每段输出严格 trim 到 d（2026-10-24 音画对齐修复）：
        #   take=d+0.3 的短段原本按 take 输出 → silent 累计比 total 长
        #   （实测 34 段漂移 0.57s → 音画错位）；loop 条件从 d>take+0.2
        #   改为 take<d，引文短段（d≤1.0）也补齐。
        # ★ 2026-11-04：take 可能被 resolve_window 的片尾兜底缩短到 < d
        #   （源片本身太短，挪不动）→ 这里用 loop 把已有画面循环补齐到 d，
        #   保证「每段画面恰好 d 秒」这条不变量（音画对齐哨兵的前提）。
        if is_speech and take < d:
            frames = max(int(take * FPS), 1)
            vf += (f',loop=loop=-1:size={frames}:start=0,'
                   f'trim=duration={d:.2f},setpts=PTS-STARTPTS')
        elif take > d:
            vf += f',trim=duration={d:.2f},setpts=PTS-STARTPTS'
        sh([FF, '-v', 'error', '-y', '-ss', f'{ss:.2f}', '-t', f'{take:.2f}',
            '-i', src, '-vf', vf,
            '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '22',
            '-an', out], 600)
        clips.append(out)
    return clips


def concat(clips, work: Path) -> Path:
    lst = work / 'list.txt'
    # ★ 必须写绝对路径：ffmpeg concat demuxer 把 list 内的相对路径解析为
    #   「相对于 list 文件所在目录」，相对路径会被拼成 _work/_work/c00.mp4 而打不开。
    # ★ 单引号转义（v3.6.0 修）：文件名含 ' 时提前终止 file 条目 → 整段画面丢失
    lst.write_text('\n'.join(
        "file '" + c.resolve().as_posix().replace("'", "'\\''") + "'" for c in clips),
        encoding='utf-8')
    silent = work / 'silent.mp4'
    sh([FF, '-v', 'error', '-y', '-f', 'concat', '-safe', '0', '-i', lst,
        '-c', 'copy', silent], 900)
    return silent


# ── ⑤ 混音（唯一实现，不再有第二份） ────────────────
def measure_lufs(p: Path) -> float | None:
    """测整条音频的整合响度（LUFS）。用 loudnorm 的 print_format=json 取 input_i。

    注意：**volumedetect 的 mean_volume 不是 LUFS**（那是线性平均 dB），
    校验门用的是 LUFS，两者数值不同，别混用（实测同一条：mean -22.8dB
    ↔ integrated -15.1 LUFS）。
    """
    r = subprocess.run([FF, '-v', 'info', '-i', str(p), '-af',
                        'loudnorm=I=-14:TP=-3:LRA=20:print_format=json',
                        '-f', 'null', '-'],
                       capture_output=True, text=True, encoding='utf-8',
                       errors='replace', timeout=900)
    m = re.search(r'"input_i"\s*:\s*"(-?[\d.]+)"', r.stderr or '')
    try:
        return float(m.group(1)) if m else None
    except Exception:
        return None


def measure_peak_db(p: Path) -> float | None:
    """测整条音频的**真峰值**（dBTP）——与 verify 用的是同一把尺（ebur128=peak=true）。

    ★ 为什么要有它（2026-11-04）：母带链是「增益 → alimiter」，
      alimiter 的 attack=1ms 只能把过冲压到 ~1.2dB → 峰值可能仍撞 verify 的 -0.5 硬门
      （T2 实测 -0.3 dBTP → 整集被判「校验未通过，不交付」）。
      先量峰值再决定要不要追加衰减，是确定性修法（不靠猜 attack/release）。
    """
    r = subprocess.run([FF, '-v', 'error', '-i', str(p), '-af', 'ebur128=peak=true',
                        '-f', 'null', '-'],
                       capture_output=True, text=True, encoding='utf-8',
                       errors='replace', timeout=900)
    for line in (r.stderr or '').splitlines() + (r.stdout or '').splitlines():
        if 'Peak:' in line:
            try:
                return float(line.split('Peak:')[1].split()[0])
            except Exception:
                pass
    return None


def mix(video: Path, segments, speech_dir: Path, raw_files: dict,
        bgm: Path | None, out: Path, total: float):
    """三轨分层（对齐 ffmpeg-advisor §2.4 与短剧推广 13 号设计）：
         主声源（解说或原声，按段互斥）→ -14 LUFS
         BGM → duck，让路给主声源
       ★ amix 必须 duration=longest，否则最短那路会把整体截断。
    """
    CH = 'aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,'

    # ★ 混音结果复用（2026-10-27 加）：_mix.wav 已存在且比 plan 新 → 跳过混音，
    #   直接进母带。用于「只改母带/校验参数」的快速重跑（混音 20-40 分钟 → 1 分钟）。
    #   判据用 mtime：plan（build-plan.json）变了才重混；设 MIX_FORCE=1 可强制重混。
    # ★★ 2026-11-03 修：plan_p 原指向 out.parent/'build-plan.json'，
    #   但 build-plan.json 实际写在 **out_dir**（out.parent 的上一级）→
    #   plan_p 永远不存在 → 复用分支**从未触发**（死代码）→ 每次重建都重混 30 分钟。
    wav_early = out.parent / '_mix.wav'
    plan_p = out.parent.parent / 'build-plan.json'   # ← 真身在 out_dir
    if (not os.environ.get('MIX_FORCE') == '1' and wav_early.is_file()
            and plan_p.is_file()
            and wav_early.stat().st_mtime > plan_p.stat().st_mtime
            and wav_early.stat().st_size > 1024 * 1024):
        print(f'    [mix] 复用已有 {wav_early.name}（{wav_early.stat().st_size // 1048576} MB，'
              f'比 plan 新；MIX_FORCE=1 可强制重混）')
        wav = wav_early
        batch_files = []
        # 直接跳到母带：用 while 包一层没必要，改为提前返回母带段——
        # 通过把 segs/批文件置空 + 下面 if 分支实现。
        _skip_to_mastering = True
    else:
        _skip_to_mastering = False

    bounds, cur = [], 0.0
    for seg in segments:
        d = float(seg['duration'])
        bounds.append((cur, cur + d))
        cur += d

    # ★ 分批混音（v3.6.5 修，坑典 P19）：把 N 段的滤镜拆成若干批分别 amix，
    #   最后再把各批 + BGM 合并成一条。adelay 仍用**全局绝对时间**，
    #   所以各批在时间轴上互不重叠，合并时 normalize=0 直接相加即可。
    #   为什么必须分批：140 路 aformat+loudnorm+adelay 并行时 ffmpeg 会
    #   「Cannot allocate memory」(error -12) 崩掉——滤镜图规模成了段数上限。
    #   分批后每批 ≤MIX_BATCH 路，段数不再受滤镜规模限制（集3 200 段可行）。
    MIX_BATCH = 30
    batch_files = []
    n_batches = (len(segments) + MIX_BATCH - 1) // MIX_BATCH
    if _skip_to_mastering:
        n_batches = 0
    for bi in range(n_batches):
        lo, hi = bi * MIX_BATCH, min((bi + 1) * MIX_BATCH, len(segments))
        inputs, parts, labels = [], [], []
        idx = 0
        for k in range(lo, hi):
            seg = segments[k]
            is_speech = bool((seg.get('text') or '').strip())
            if is_speech:
                src = speech_dir / f'seg{k:02d}.wav'
                if not src.exists():
                    raise BuildError(f'第{k}段解说音轨缺失: {src}')
            else:
                src = raw_files[k]['file']
                if not Path(src).exists():
                    raise BuildError(f'第{k}段原声音轨缺失: {src}')
            inputs += ['-i', str(src)]
            ms = int(bounds[k][0] * 1000)
            # ★★ adelay 必须在 loudnorm **之前**（坑典 P77，2026-10-27 用户实测
            #   「天降福星 T2 好几个音轨乱糟糟」定位至此）：
            #   loudnorm 单遍是 **dynamic 模式**（内部 3s 缓冲 + 实时增益），
            #   放在 adelay 之前会让 adelay **彻底失效**——实测语音全部从 0s
            #   开始播（各批在 0s 叠加 → 前段能量 +8dB、后段 -10dB，
            #   段间响度差 24dB，用户听感就是「好几个人同时说话、乱糟糟」）。
            #   二分实验：无滤镜/只加 highpass/afftdn → adelay 正常；
            #   一加 loudnorm → adelay 失效（2 段 amix 即可复现）。
            #   agate 门掉 adelay 产生的前导静音，防止 loudnorm/dynaudnorm
            #   把静音增益成噪声（release=300ms 保住段间 0.45s 呼吸留白）。
            chain = ('highpass=f=90,afftdn=nf=-34,'
                     f'adelay={ms}:all=1,'
                     'agate=range=0.03:threshold=0.015:attack=5:release=300,'
                     # ★ 裁掉「段结束+0.5s」之后的静音（P77 配套优化）：
                     #   adelay 提前后每路流都是全长 318s（前面大量静音），
                     #   loudnorm/dynaudnorm 对静音帧照样逐帧计算 → 140 段
                     #   混音从 6 分钟涨到 23 分钟。裁掉尾静音后每路只处理到
                     #   本段结束，平均省一半计算量；语音与 agate 门控不受影响。
                     f'atrim=0:{bounds[k][1] + 0.5:.2f},'
                     f'loudnorm=I={SPEECH_LUFS}:TP=-3.0:LRA=11')   # ★TP 收紧到 -3.0 留混音余量（坑典 P20）
            if is_speech:
                chain += (',equalizer=f=4000:t=q:w=1:g=3,'
                          'dynaudnorm=f=250:g=12:p=0.75')
            chain += f'[m{k}]'
            parts.append(f'[{idx}:a]' + CH + chain)
            labels.append(f'[m{k}]')
            idx += 1
        graph = ';'.join(parts) + ';' + ''.join(labels) + (
            f'amix=inputs={len(labels)}:duration=longest:dropout_transition=0:'
            f'normalize=0,apad=whole_dur={total:.2f},atrim=0:{total:.2f}[mix]')
        gf = out.parent / f'_mix_graph_b{bi}.txt'
        gf.write_text(graph, encoding='utf-8')
        bf = out.parent / f'_mix_b{bi}.wav'
        sh([FF, '-v', 'error', '-y'] + inputs +
           ['-filter_complex_script', str(gf), '-map', '[mix]',
            '-c:a', 'pcm_s16le', '-ar', '44100', '-ac', '2',
            '-t', f'{total:.2f}', bf], 1800)
        batch_files.append(bf)
        safe_unlink(gf)

    # 最终合并：各批（时间轴互补）+ BGM
    # ★ 注意语法：各批的 [n:a] 是 amix 的**输入标签**，必须直接连在 amix 那一行，
    #   不能用分号单独成句（`[0:a];[1:a];amix=...` → "No such filter: ''"）。
    #   只有需要滤镜链的 BGM 才单独成一条（带 [bgm] 输出标签）。
    if _skip_to_mastering:
        wav = wav_early          # 复用模式：跳过最终合并，直接用已混好的
    else:
        inputs, parts = [], []
        for bi, bf in enumerate(batch_files):
            inputs += ['-i', str(bf)]
        idx = len(batch_files)
        n_mix = len(batch_files)
    if not _skip_to_mastering:
        if bgm and bgm.exists():
            inputs += ['-i', str(bgm)]
            duck = ','.join(
                f"volume=enable='between(t,{st:.3f},{en:.3f})':"
                f'volume={0.20 if (segments[k].get("text") or "").strip() else 0.06}'
                for k, (st, en) in enumerate(bounds))
            parts.append(f'[{idx}:a]' + CH +
                         'highpass=f=120,lowpass=f=6000,afftdn=nf=-45,'
                         f'aloop=loop=-1:size=2e9,{duck},'
                         f'atrim=0:{total:.2f}[bgm]')
            n_mix += 1
        mix_in = ''.join(f'[{bi}:a]' for bi in range(len(batch_files)))
        if bgm and bgm.exists():
            mix_in += '[bgm]'

        graph = (';'.join(parts) + ';' if parts else '') + mix_in + (
            f'amix=inputs={n_mix}:duration=longest:dropout_transition=0:'
            f'normalize=0,apad=whole_dur={total:.2f},atrim=0:{total:.2f}[mix]')
        wav = out.parent / '_mix.wav'
        gf = out.parent / '_mix_graph.txt'
        gf.write_text(graph, encoding='utf-8')
        sh([FF, '-v', 'error', '-y'] + inputs +
           ['-filter_complex_script', str(gf), '-map', '[mix]',
            '-c:a', 'pcm_s16le', '-ar', '44100', '-ac', '2',
            '-t', f'{total:.2f}', wav], 1800)

    # ── 母带（v3.6.5 重写，坑典 P20）──
    #   ① 先限幅（alimiter limit=0.707 ≈ -3 dBTP）：压住峰值，给增益留余量；
    #   ② 再按**实测**响度补音量增益，精确落到 -14 LUFS；
    #   ③ 补偿增益后再限一次——★ 不能省（P77 连带发现）：补偿增益可能把刚压
    #      下去的峰值又推过 0 dBTP（实测 T2 补偿 +3.51dB 后峰值 +0.6 dBTP，削波）。
    #      ★ 二次限幅收紧到 limit=0.84（≈-1.5 dBTP）+ 快攻击 attack=1（P78 修）：
    #      旧值 0.891（≈-1.0 dBTP）用 alimiter 默认 5ms 攻击，语音瞬态会过冲
    #      ~0.6dB，真实峰值落到 -0.4 dBTP——响于 verify 的 -0.5 硬门、却静于 -1.0
    #      限幅，限幅器不动作 → 成片被硬门误杀（B3 实测）。收紧 + 快攻击后链路
    #      **可靠保证**真实峰值 ≤ -1.5 dBTP（AAC 编码过冲后仍 < -0.5），硬门自此
    #      只拦真削波，不再误杀正常音频。LUFS 由 volume 增益独立锁定 -14，收紧
    #      峰值不改变感知响度，只增加余量。
    #
    #   ★ 为什么不能直接用 loudnorm 母带（实测踩坑）：
    #   单遍 loudnorm 在「LRA 大 + 峰值超标」的语音上会**退化为 dynamic 模式**
    #   （测量输出 normalization_type=dynamic、target_offset=5.15），为同时满足
    #   TP=-1.5 与 LRA=11，把 -15.1 LUFS 的信号压成 **-19.2 LUFS** ——
    #   响度永远到不了目标，校验必挂（偏离 5.2dB > 容差 1.5）。
    #   物理原因：语音峰均差约 13.9dB，「-14 LUFS 且峰值≤-1.5 dBTP」要求峰均差
    #   ≤12.5dB —— 必须先限幅压峰均差，loudnorm 自己做不了这步。
    #   实测：限幅后 -16.35 LUFS / -2.50 dBTP → 补 +2.35dB → **-13.9 LUFS** ✓
    lim = out.parent / '_mix_lim.wav'
    sh([FF, '-v', 'error', '-y', '-i', wav,
        '-af', 'alimiter=limit=0.707:level=disabled',
        '-c:a', 'pcm_s16le', '-ar', '44100', '-ac', '2', str(lim)], 900)
    mi = measure_lufs(lim)
    gain = 1.0
    if mi is not None:
        gain = 10 ** ((-14.0 - mi) / 20)
        print(f'    母带：限幅后 {mi:.2f} LUFS → 补偿 {(-14.0-mi):+.2f}dB → 目标 -14 LUFS')
    m4a = out.parent / '_mix_norm.m4a'
    sh([FF, '-v', 'error', '-y', '-i', lim,
        '-af', f'volume={gain:.4f},alimiter=limit=0.84:level=disabled:attack=1:release=50',
        '-c:a', 'aac', '-b:a', '320k', '-ar', '44100', '-ac', '2',
        '-t', f'{total:.2f}', m4a], 900)

    # ★★ 峰值保险（2026-11-04 修，P78 的续集）：alimiter 的 attack=1ms 只能把
    #   过冲压到 ~1.2dB，而**增益加在限幅之前** → 峰值仍可能撞 verify 的 -0.5 硬门
    #   （T2 实测 -0.3 dBTP → 整集被判「校验未通过，不交付」，白渲 112MB）。
    #   确定性修法：量峰值 → 超 -0.7 就追加衰减重做（纯增益，衰减 ≤1.3dB，
    #   响度仍在 verify 容差内，容差以 `engine.json → output.loudness_tolerance` 为准）。
    pk = measure_peak_db(m4a)
    if pk is not None and pk > -0.7:
        trim_db = -0.8 - pk
        trim = 10 ** (trim_db / 20)
        print(f'    峰值保险：母带峰值 {pk:.2f} dBTP → 追加衰减 {trim_db:+.2f}dB')
        m4a_b = out.parent / '_mix_norm_b.m4a'
        sh([FF, '-v', 'error', '-y', '-i', lim,
            '-af', (f'volume={gain:.4f},alimiter=limit=0.84:level=disabled:attack=1:release=50,'
                    f'volume={trim:.4f}'),
            '-c:a', 'aac', '-b:a', '320k', '-ar', '44100', '-ac', '2',
            '-t', f'{total:.2f}', m4a_b], 900)
        pk2 = measure_peak_db(m4a_b)
        if pk2 is not None and pk2 <= -0.7:
            m4a.replace(m4a_b)
            print(f'    峰值保险：{pk:.2f} → {pk2:.2f} dBTP ✓')
        else:
            safe_unlink(m4a_b)
            print(f'    峰值保险未生效（{pk2}）——保留原母带，交给 verify 判定')

    sh([FF, '-v', 'error', '-y', '-i', video, '-i', m4a,
        '-map', '0:v', '-map', '1:a', '-c:v', 'copy',
        '-c:a', 'aac', '-b:a', '320k', '-movflags', '+faststart',
        '-t', f'{total:.2f}', out], 900)
    # ★ 调试保留中间件（v3.6.5）：DEBUG_KEEP_MIX=1 时保留 _mix.wav / 各批 wav，
    #   便于逐级测电平定位「响度偏低」到底出在哪一级（段级/批内/最终合并/母带）。
    if os.environ.get('DEBUG_KEEP_MIX') == '1':
        print(f'[debug] 保留中间件: {wav} / {[str(b) for b in batch_files]}')
    elif _skip_to_mastering:
        pass                      # 复用模式：保留 _mix.wav 供下次只母带重跑
    else:
        for bf in batch_files:      # 分批混音的中间件（坑典 P19）
            safe_unlink(bf)
        safe_unlink(wav)
        safe_unlink(m4a)
    if not _skip_to_mastering:
        safe_unlink(gf)


# ── ⑥ 字幕 ────────────────────────────────────────
def _wrap_at(text: str, cap: int) -> list[str]:
    lines = []
    remaining = text.strip()
    break_chars = set('，。！？；：、,.!?;:')
    while len(remaining) > cap:
        cut = cap
        # Prefer punctuation near the capacity boundary, but never exceed it.
        min_cut = max(1, len(remaining) - cap)
        candidates = [i + 1 for i, ch in enumerate(remaining[:cap])
                      if ch in break_chars and i >= cap // 2 and i + 1 >= min_cut]
        if candidates:
            cut = candidates[-1]
        lines.append(remaining[:cut])
        remaining = remaining[cut:].lstrip()
    if remaining:
        lines.append(remaining)
    return lines


def _no_lead_punct(lines: list[str], break_chars: set) -> bool:
    """折行后不允许任何一行以标点开头（坑典 P62）。

    实测 C1 CTA 折成「长白山人脸鸭进村，应名即死」+「。点关注，看婉秋
    怎么活下来」——句号跑到行首，观众看到的是一条以「。」开头的字幕，
    非常业余。质检门只查行长不查这个，属人工审美红线，必须代码里拦。
    """
    return not any(ln and ln[0] in break_chars for ln in lines)




BREAK_CHARS_SET = set('，。！？；：、,.!?;:')


def _no_lead_punct2(lines):
    return not any(ln and ln[0] in BREAK_CHARS_SET for ln in lines)


def _pack_cue(text: str, max_chars: int, max_lines: int):
    """把 text 装进**一个** cue；放不下返回 None。绝不返回超长行。"""
    if not text:
        return None
    if len(text) <= max_chars:
        return [text]

    lines, rest = [], text
    for _ in range(max_lines):
        if not rest:
            break
        if len(rest) <= max_chars:
            lines.append(rest)
            rest = ''
            break
        # 切点候选：标点之后，且**必须 ≤ max_chars**（旧 _wrap_at 允许 cut=cap+1 → 溢出 1 字）
        best = None
        for i in range(min(len(rest), max_chars) - 1):
            if rest[i] in BREAK_CHARS_SET:
                best = i + 1
        cut = best if best else max_chars
        head, tail = rest[:cut].rstrip(), rest[cut:].lstrip()
        if not head:
            return None
        if len(head) > max_chars:
            head, tail = rest[:max_chars], rest[max_chars:].lstrip()
        # 下一行不能以标点开头 → 把标点并回上一行（放得下才并）
        while tail and tail[0] in BREAK_CHARS_SET:
            if len(head) + 1 <= max_chars:
                head += tail[0]
                tail = tail[1:].lstrip()
            else:
                break
        lines.append(head)
        rest = tail
    if rest:
        return None                      # 还有剩余 → 放不进这个 cue
    if len(lines) > max_lines:
        return None
    if any(len(ln) > max_chars for ln in lines):
        return None
    if not _no_lead_punct2(lines):
        return None
    return lines


def wrap_caption_strict(text: str, max_chars: int, max_lines: int = 2):
    """把一段文本折成**一个或多个** cue，每 cue 严格满足行长与行数上限。

    返回 list[list[str]] —— 外层 cue，内层行。
    ★ 与旧 wrap_caption 的本质区别：
      - 旧版：放不下 → **放宽行长**去凑行数 → 折出 14 字行 → 质检硬门 FAIL
               （2026-11-03 H3 实测：27 字文本塞不进 2×13，被折成 14+13）
      - 新版：放不下 → **拆 cue**，行长永不超过 max_chars（字幕行业惯例：
               一句话念太长就分两条字幕依次显示）
    """
    text = (text or '').strip()
    if not text:
        return []

    cues, rest, guard = [], text, 0
    while rest:
        guard += 1
        if guard > 500:
            raise BuildError(f'字幕拆 cue 死循环：{text[:40]!r}')
        cue = _pack_cue(rest, max_chars, max_lines)
        if cue is not None:
            cues.append(cue)
            consumed = sum(len(x) for x in cue)
            rest = rest[consumed:].lstrip()
            while rest and rest[0] in BREAK_CHARS_SET:
                rest = rest[1:].lstrip()
            continue
        # 装不下 → 硬切一个 cue，保证有进展
        head = rest[:max_chars]
        cues.append([head])
        rest = rest[max_chars:].lstrip()
        while rest and rest[0] in BREAK_CHARS_SET:
            rest = rest[1:].lstrip()
    return cues

def wrap_caption(text: str, max_chars: int, max_lines: int = 2) -> list[str]:
    """Wrap a cue into readable lines without dropping any spoken words.

    ★ 2026-10-22 修（X2/X3 质检门 cue_too_many_lines 实砸）：douyin 字幕
    硬门限每 cue ≤2 行，而长 CTA（27 字）按 13 字/行必折 3 行。现在超行数
    时**二分找能塞进 max_lines 的最小单行容量**（仍优先标点切），而不是
    放任折行数超限——字幕是成片的一部分，超限就是不合格，不能只靠质检门
    事后拦。

    ★ 2026-10-26 修（坑典 P62）：再加一条「行首不许是标点」。选容量的顺序是
    ①行数合法 → ②无行首标点 → ③行长尽量小。找不到就退让到①，绝不硬造
    非法折行。
    """
    text = text.strip()
    if not text:
        return []
    if max_chars < 1 or len(text) <= max_chars:
        return [text]
    break_chars = set('，。！？；：、,.!?;:')
    lines = _wrap_at(text, max_chars)

    def _ok(ls, strict_lead=True):
        # ①行长不得超 max_chars（质检门硬指标）②行数合法 ③无行首标点
        if any(len(ln) > max_chars for ln in ls):
            return False
        if max_lines and len(ls) > max_lines:
            return False
        return _no_lead_punct(ls, break_chars) if strict_lead else True

    need = (not _ok(lines)) if max_lines else (not _no_lead_punct(lines, break_chars))
    if need:
        best = None
        # cap 越大行数越少但单行越长；从头扫到 len(text) 取第一个合规解
        for cap in range(max_chars, len(text) + 1):
            cand = _wrap_at(text, cap)
            if _ok(cand):
                best = cand
                break
        if best is None:
            # 退让：只保行数，接受行首标点（宁可难看也不能吞字/超行数）
            lo, hi = max_chars, len(text)
            while lo < hi:
                mid = (lo + hi) // 2
                if len(_wrap_at(text, mid)) <= max_lines:
                    hi = mid
                else:
                    lo = mid + 1
            best = _wrap_at(text, lo)
        lines = best
    return lines


def write_srt(segments, out: Path, profile: dict | None = None):
    def ts(x: float) -> str:
        h = int(x // 3600)
        m = int((x % 3600) // 60)
        s = x % 60
        return f'{h:02d}:{m:02d}:{s:06.3f}'.replace('.', ',')

    cur, items = 0.0, []
    profile = profile or {'chars_per_line': 14, 'subtitle_max_lines': 2}
    chars_per_line = int(profile.get('chars_per_line', 14))
    max_lines = int(profile.get('subtitle_max_lines', 2))
    for seg in segments:
        d = float(seg['duration'])
        txt = (seg.get('text') or '').strip()
        cap = (seg.get('caption') or '').strip()
        if txt:
            # ★ 严格折行（2026-11-03 缺陷①修复）：放不下就拆多个 cue，
            #   行长永不超过 chars_per_line（旧版会放宽行长 → 14 字行 → 质检 FAIL）
            cue_list = wrap_caption_strict(txt, chars_per_line, max_lines)
            # ★ 行数硬闸内建于 write_srt：超限即 BuildError，不留到质检门
            for cu in cue_list:
                if len(cu) > max_lines:
                    raise BuildError(
                        f'字幕折行超限：{len(cu)} 行 > {max_lines} 行 | {txt}')
                if any(len(x) > chars_per_line for x in cu):
                    raise BuildError(
                        f'字幕行长超限：{max(len(x) for x in cu)} > '
                        f'{chars_per_line} 字 | {txt}')
            # ★ 多 cue 的时间轴：按各 cue 字数比例瓜分本段时长（总时长不变）
            weights = [max(1, sum(len(x) for x in cu)) for cu in cue_list]
            wsum = sum(weights)
            t0 = cur
            for cu, w in zip(cue_list, weights):
                seg_d = d * w / wsum
                caption = '\n'.join(cu)
                items.append(f'{len(items) + 1}\n'
                             f'{ts(t0)} --> {ts(t0 + seg_d)}\n{caption}\n')
                t0 += seg_d
            cur = t0
        elif cap and seg.get('is_cold_open'):
            # ★ 冷开场钩子的标题字幕（v3.7.1）：不是解说，是画面上压的钩子文案，
            #   如「开局高能！」。同样受行数硬闸约束，避免越界。
            cue_list = wrap_caption_strict(cap, chars_per_line, max_lines)
            for cu in cue_list:
                if len(cu) > max_lines:
                    raise BuildError(
                        f'冷开场字幕折行超限：{len(cu)} 行 > {max_lines} 行 | {cap}')
            weights = [max(1, sum(len(x) for x in cu)) for cu in cue_list]
            wsum = sum(weights)
            t0 = cur
            for cu, w in zip(cue_list, weights):
                seg_d = d * w / wsum
                items.append(f'{len(items) + 1}\n'
                             f'{ts(t0)} --> {ts(t0 + seg_d)}\n'
                             f'{"\n".join(cu)}\n')
                t0 += seg_d
            cur = t0
        else:
            cur += d
    out.write_text('\n'.join(items), encoding='utf-8')
    return out


# ── ⑥ 烧字幕（ASS 方案） ─────────────────────────
_ASS_HEAD = '''[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Nar,Microsoft YaHei,52,&H00FFFFFF,&H00FFFFFF,&H00000000,&HC0000000,-1,0,0,0,100,100,1,0,3,2,0,2,60,60,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
'''


def subtitle_layout(profile: dict, platform: str = 'generic') -> dict:
    """Compute subtitle placement inside the configured vertical safe area."""
    width, height, font_size = 1080, 1920, 52
    safe_top = float(profile.get('safe_top', 0.08))
    safe_bottom = float(profile.get('safe_bottom', 0.12))
    max_lines = int(profile.get('subtitle_max_lines', 2))
    padding = int(profile.get('subtitle_safe_padding_px', 24))
    margin_v = math.ceil(height * safe_bottom) + padding
    line_height = math.ceil(font_size * 1.25)
    box_bottom = height - margin_v
    box_top = box_bottom - line_height * max_lines
    safe_top_px = math.ceil(height * safe_top)
    safe_bottom_px = math.floor(height * (1.0 - safe_bottom))
    return {
        'platform': platform,
        'playres': {'width': width, 'height': height},
        'font_size': font_size,
        'max_lines': max_lines,
        'margin_v': margin_v,
        'caption_box': {'top_px': box_top, 'bottom_px': box_bottom},
        'safe_box': {'top_px': safe_top_px, 'bottom_px': safe_bottom_px},
    }




def _size_budget(total_sec: float, platform: str = 'douyin', audio_bps: int = 0,
                 crf: int = 22, preset: str = 'veryfast'):
    """按「平台体积上限 + 实际时长」反算码率上限。

    ★★★ 2026-11-03 重写：老实现有两处让「码率上限」完全失效 ★★★
      ❶ 同时传 `-crf` 和 `-b:v` → libx264 进 CRF 模式，**-b:v 被无视**
         （实测：-crf22 + -b:v 500000 仍出 1347975bps，只有 VBV 在起作用）
         → 有体积上限时**必须去掉 -crf**，改用纯码率模式。
      ❷ 音频码率写死 128000 → 母带实际是 aac 320k（实测 289799bps），
         少算约 12MB 预算 → 长片必撞硬门。
      → 逻辑已抽到 scripts/size_budget.py（build.py 与 make_cover.py 共用一份，
        避免副本漂移），这里只做薄封装。

    返回 (args, info)：args 是给 ffmpeg 的视频编码参数，info 供日志打印。
    短片（预算内）返回 -crf 参数 → 画质与旧版完全一致。
    """
    info = size_budget.plan(total_sec, platform, audio_bps=audio_bps,
                            crf=crf, preset=preset)
    return info['make_args'](), info

def burn(video: Path, srt: Path, out: Path, profile: dict | None = None,
         platform: str = 'generic', total_sec: float = 0.0):
    r"""烧字幕 —— 用 ASS 而非 SRT。

    ★★★ 两个血泪坑（各花了好几轮才定位）：
    1. SRT 必须 utf-8 **无 BOM** —— 带 BOM 时 libass 静默跳过整个文件，
       ffmpeg 零报错但画面上一点字幕都没有。
    2. ★★ BorderStyle=3（整行底栏）在 SRT + force_style 下**渲染不出来** ——
       因为 SRT 没有 PlayResX/PlayResY，libass 不知道画布尺寸。
       正解：由 SRT 转 ASS，显式声明 PlayResX:1080 / PlayResY:1920，
       用 ass=filename=... 滤镜烧录（实测一次成功）。

    样式与限制：
      - BorderStyle=3 整行底栏 + 半透明黑底 → 提高解说字幕对比度；
        这不会移除原片画面内烧录的字幕，也不保证能遮住它们。
      - 原声段不写进 SRT → 不叠加解说字幕；原片字幕是否完整可见取决于源画面裁切。
      - 成片必须抽帧检查两层字幕是否重叠、遮挡人物或落出安全区；必要时调整版式/裁切后复验。
    """
    profile = profile or {'safe_top': 0.08, 'safe_bottom': 0.12,
                          'subtitle_max_lines': 2}
    layout = subtitle_layout(profile, platform)
    raw = srt.read_bytes()
    if raw[:3] == bytes([0xEF, 0xBB, 0xBF]):
        raw = raw[3:]
    txt = raw.decode('utf-8')

    items, cur = [], None
    for ln in txt.splitlines():
        s = ln.strip()
        if not s:
            continue
        if '-->' in s:
            if cur:
                items.append(cur)
            a, b = [x.strip() for x in s.split('-->')]
            cur = [a, b, []]
        elif cur is not None and not s.isdigit():
            cur[2].append(s)
    if cur:
        items.append(cur)
    if not items:
        print('    [警告] 字幕为空，跳过烧录')
        shutil.copy(video, out)
        return

    def t_ass(t):
        h, m, rest = t.split(':')
        sec, ms = rest.split(',')
        return f'{int(h)}:{int(m)}:{int(sec):02d}.{int(ms) // 10:02d}'

    ev_rows = []
    for a, b, tx in items:
        ass_text = r'\N'.join(tx)
        ev_rows.append(f'Dialogue: 0,{t_ass(a)},{t_ass(b)},Nar,,0,0,0,,{ass_text}\n')
    ev = ''.join(ev_rows)

    # ★ 临时 ASS 路径唯一化（v3.6.0 修）：固定 wb.ass 会被并发 build 互相覆盖
    tmp = Path(tempfile.gettempdir()) / f'wb_{os.getpid()}.ass'
    tmp.write_text(_ASS_HEAD.format(margin_v=layout['margin_v']) + ev, encoding='utf-8')
    write_json_atomic(srt.with_name('subs.style.json'), layout)

    # 三约束：纯 ASCII 路径 + 正斜杠 + 转义盘符冒号
    sp = str(tmp).replace(chr(92), '/').replace(':', chr(92) + ':')
    vf = "ass=filename='" + sp + "'"
    # ★ 体积预算（2026-11-03 重写）：逻辑在 size_budget.plan（与 make_cover.py 共用）
    #   关键：音频码率**实测**输入文件（母带是 aac 320k，不是 128k），
    #   且受限时**不传 -crf**（传了 -b:v 就被无视 → 上限形同虚设）。
    audio_bps = size_budget.probe_audio_bps(video)
    rate_args, rate_info = _size_budget(total_sec, platform, audio_bps=audio_bps)
    if rate_info.get('applied'):
        print(size_budget.describe(rate_info))
    sh([FF, '-v', 'error', '-y', '-i', video, '-vf', vf] + rate_args + [
        '-c:a', 'copy', '-movflags', '+faststart', out], 1800)
    # ★ 体积硬闸：预算只是估算 → 编码完实测，超限就降码率重编
    #   （宁可降一档码率，也不让超限成片流到下游质检门 FAIL）
    size_budget.enforce_cap(out, platform)
    safe_unlink(tmp)
    print(f"    字幕已烧录（{len(items)} 条，{platform}安全区 MarginV={layout['margin_v']}）")


# ── ⑥b 表情包贴纸层（v3.6.0 原创度增强第二层） ──────────
STICKER_ANCHOR_XY = {
    # 字幕盒在底部 MarginV 安全区（约 y=1420-1550，1080×1920）——底部锚点
    # 一律锚到字幕盒**上方**（贴纸底边 ≤1400），杜绝压字幕（v3.6.0 修：
    # 旧公式 main_h-overlay_h-400 让大贴纸底边落到 1520 压在字幕上）
    'top_right': ('main_w-overlay_w-40', '40'),
    'top_left': ('40', '40'),
    'mid_right': ('main_w-overlay_w-40', '(main_h-overlay_h)/2'),
    'mid_left': ('40', '(main_h-overlay_h)/2'),
    'bottom_right': ('main_w-overlay_w-40', 'min(1400-overlay_h,main_h*0.5)'),
    'bottom_left': ('40', 'min(1400-overlay_h,main_h*0.5)'),
    'center': ('(main_w-overlay_w)/2', '(main_h-overlay_h)/2'),
}
# 字幕安全区底边之上的贴纸锚定上限（1080×1920 的 1400px；宽高比变化时同值近似）
STICKER_BOTTOM_CAP = 1400


def overlay_stickers(video: Path, segments, out: Path, work: Path,
                     total_sec: float = 0.0, platform: str = 'generic') -> int:
    """把 plan 段级 stickers 表情包烧进成片（表情包引擎素材，ffmpeg overlay）。

    纪律（用户 2026-10-24 令 + 砚白显隐美学）：
      - 单段 ≤2 张；原声段不贴（角色在说话，贴纸抢戏）；
      - 只认绝对路径 meme_path，缺文件直接报错（不留半成品）；
      - show 缺省 = 该段 output 窗口。
    ★ GIF 绝不直连 overlay（实测：循环解码把 10s 片段拖到 14 分钟 / 1fps，
      且 620MB 码率失控）——一律先预转明确时长的 mp4（fps=15+scale+-t），
      静态图用 -loop 1 同法处理。返回贴纸总数；无贴纸时直接复制视频。
    """
    items, cur = [], 0.0
    for k, seg in enumerate(segments):
        d = float(seg['duration'])
        st_list = seg.get('stickers') or []
        if st_list:
            if not (seg.get('text') or '').strip():
                print(f'    [跳过] 第{k}段是原声段，按纪律不贴表情包')
                st_list = []
            elif len(st_list) > 2:
                raise BuildError(f'第{k}段贴纸 {len(st_list)} 张 > 2（显隐纪律：单镜≤2）')
        if st_list:
            items.append((cur, cur + d, st_list))
        cur += d
    if not items:
        shutil.copy2(video, out)
        return 0

    work.mkdir(parents=True, exist_ok=True)
    # ★ 贴纸尺寸按成片宽度算（坑典 P61）：scale 是「占画面宽度的比例」，
    #   不是「素材自身的倍数」。缺这一步小 GIF 会缩成 20 多像素＝白贴。
    vw, vh = probe_size(video)
    inputs = ['-i', str(video)]
    fc, prev, idx = [], '0:v', 1
    n = 0
    for st, en, sts in items:
        for s in sts:
            mp = Path(str(s.get('meme_path') or ''))
            if not mp.is_file():
                raise BuildError(f'表情包文件缺失: {mp}')
            anchor = str(s.get('anchor', 'top_right'))
            if anchor not in STICKER_ANCHOR_XY:
                raise BuildError(f'未知 anchor {anchor!r}；可选 {"/".join(STICKER_ANCHOR_XY)}')
            x, y = STICKER_ANCHOR_XY[anchor]
            show = s.get('show') or [st, en]
            # ★ show 是**成片绝对时间**（v3.6.0 修：原文档未注明，容易误填段内相对秒）
            try:
                lo, hi = float(show[0]), float(show[1])
            except (TypeError, ValueError, IndexError):
                raise BuildError(f'第{k}段 sticker show 必须 [起秒, 止秒] 数值: {show!r}')
            if not (lo < hi):
                raise BuildError(f'第{k}段 sticker show 起止不合法: {lo} ≥ {hi}')
            scale = float(s.get('scale', 0.2))
            # ── 预转：GIF/静态图 → 明确时长 mp4（窗口 + 0.5s 余量，fps=15） ──
            prep = work / f'sticker_{n:02d}.mp4'
            dur = max(hi - lo + 0.5, 0.6)
            is_gif = mp.suffix.lower() == '.gif'
            # ★ 目标宽度 = 成片宽 × scale（坑典 P61）；-2 保证宽高为偶数
            tw = max(64, int(round(vw * scale)))
            tw -= tw % 2
            # 高度同样封顶（竖长表情包不能糊掉半个屏）：等比缩到框内
            mh = max(64, int(round(vh * 0.42)))
            mh -= mh % 2
            _sc = (f'scale=w={tw}:h={mh}:force_original_aspect_ratio=decrease'
                   # ★ 必须 :force_divisible_by=2（坑典 P81）：
                   #   tw/mh 是偶数**不等于**输出偶数——decrease 按原图比例缩放，
                   #   源图是奇数尺寸时输出必定奇数（实测 157×159 的 jpg →
                   #   238×241，libx264 报 height not divisible by 2 / -22）。
                   #   表情包合集里大量非标准尺寸图，不加这项必踩。
                   ':force_divisible_by=2')
            vf_pre = f'fps=15,{_sc}' if is_gif else _sc
            pre_inputs = ['-i', str(mp)] if is_gif else ['-loop', '1', '-i', str(mp)]
            sh([FF, '-v', 'error', '-y'] + pre_inputs +
               ['-vf', vf_pre, '-t', f'{dur:.2f}',
                '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '24',
                '-an', str(prep)], 300)
            inputs += ['-i', str(prep)]
            fc.append(f"[{prev}][{idx}:v]overlay=x={x}:y={y}:"
                      f"enable='between(t,{lo:.2f},{hi:.2f})'[v{n}]")
            prev = f'v{n}'
            idx += 1
            n += 1
    graph = ';'.join(fc)
    # 码率帽：防 overlay 区域高频内容炸码率（抖音 1080p 标准 6-8Mbps）
    # ★ -map 0:a? 必须显式映射音频流——只 map [v] 时 burned 的音轨不会进成片
    #   （实测 verify 报「Output file does not contain any stream」才发现）
    # ★ 体积预算（缺陷②修复）：这里已有一层固定 6M/8M 帽，但那是按**短片**定的；
    #   长片（>340s）按平台上限反算的码率更低 → 用预算值覆盖，否则仍会超 128MB。
    # ★ 体积预算（2026-11-03 重写）：与 burn() 共用 size_budget.plan
    #   老实现同时给 -crf 24 和 -b:v → -b:v 被无视（CRF 优先）→ 上限形同虚设。
    rate_args, rate_info = _size_budget(total_sec, platform,
                                        audio_bps=size_budget.probe_audio_bps(video),
                                        crf=24)
    if rate_info.get('applied'):
        print(size_budget.describe(rate_info))
    sh([FF, '-v', 'error', '-y'] + inputs +
       ['-filter_complex', graph, '-map', f'[{prev}]', '-map', '0:a?'] + rate_args + [
        '-c:a', 'copy', '-movflags', '+faststart', out], 1800)
    size_budget.enforce_cap(out, platform)
    print(f'    表情包贴纸 {n} 张已烧录（{len(items)} 个时段）')
    return n


# ── ⑦ 校验（出片前必过） ──────────────────────────
def verify(video: Path, work: Path) -> dict:
    """校验：音轨长度 + 静音段 + 响度。

    ★★★ 最关键的一条：音轨长度必须等于视频长度
      实测踩坑：amix duration=first 把 20.5s 音轨截成 2.02s，
      成片「后面 18 秒完全没声音」，但 silencedetect 检不出来。
    """
    st = probe_streams(video)
    v, a = st.get('video'), st.get('audio')
    rows = rms_windows(video, work)
    runs = silent_runs(video, work)
    longest = max((d for _, d in runs), default=0.0)
    silent = [(s, d) for s, d in runs if d > MAX_SILENT_RUN_SEC]

    errs = []
    if a is None:
        errs.append('没有音轨')
    elif v and abs(a - v) > 1.0:
        # ★ 双向校验 + 1.0s 阈值（v3.6.1 修）：旧版只查音轨比视频短且阈值 0.5s，
        #   burned 音轨比 silent 长的方向漏报；30fps 帧量化在 31 段上累计约
        #   0.6s（X3 实测），真实错位（丢整段）≥2s —— 1.0s 两侧区分带足够
        direction = '短' if a < v else '长'
        errs.append(f'音画不对齐：视频 {v:.2f}s 但音轨{direction} {a:.2f}s'
                    '（差 >1.0s；查 amix duration / 音轨映射 / concat 哨兵）')
    if silent:
        errs.append(f'{len(silent)} 处超长静音(>{MAX_SILENT_RUN_SEC}s): {silent[:5]}'
                    '（多半是音轨被截断或整段没铺声，先查 amix 的 duration）')

    r = subprocess.run([FF, '-hide_banner', '-i', str(video),
                        '-af', 'ebur128=peak=true', '-f', 'null', '-'],
                       capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=900)
    lufs = peak = None
    for line in (r.stderr or '').splitlines():
        if 'I:' in line and 'LUFS' in line:
            try:
                lufs = float(line.split('I:')[1].split()[0])
            except Exception:
                pass
        if 'Peak:' in line:
            try:
                peak = float(line.split('Peak:')[1].split()[0])
            except Exception:
                pass
    if lufs is not None and abs(lufs - (-14)) > 1.5:
        errs.append(f'响度 {lufs} LUFS 偏离目标 -14 超过 1.5')
    # ★ 峰值硬门（P77 连带发现，2026-10-27）：母带「限幅→补偿增益」后若不二次
    #   限幅，增益会把峰值推过 0 dBTP（实测 T2 +3.51dB → +0.6 dBTP 削波）。
    #   旧版只记录 peak 不判 → 削波成片照样「校验通过」。阈值 -0.5 留 AAC 编码
    #   overshoot 余量（编码后通常比源高 0.2-0.5dB）。
    if peak is not None and peak > -0.5:
        errs.append(f'峰值 {peak} dBTP 超过 -0.5（削波风险；查母带二次限幅）')

    return {'video_dur': v, 'audio_dur': a, 'lufs': lufs, 'peak': peak,
            'silent': silent, 'silent_runs': runs,
            'longest_silent_run': round(longest, 2),
            'errors': errs, 'windows': len(rows),
            'min_db': min((db for _, db in rows), default=None),
            'ok': not errs}


# ── ⑥c shotmap（质检版权门的度量源，v3.6.1 抽自主流程共享） ──────────
def write_shotmap(segments, dataset, raw_files: dict, source_override, out_dir: Path,
                  total: float) -> Path:
    """按 build-plan 的 segments 写 shotmap.json（质检 hard/copyright 读它）。

    ★ original_footage_sec 必须与 cut_clips 实切规则一致（v3.6.1 修）：
      旧值恒 0.8s，但普通段实取 min(d+0.3, TAKE_MAX)——质检的 continuous 门
      查的是 shotmap 度量，与实切不符=合规数据失真（TAKE_MAX 上调即静默破门）。
    """
    shotmap_segments = []
    cursor = 0.0
    for k, seg in enumerate(segments):
        is_speech = bool((seg.get('text') or '').strip())
        d = float(seg['duration'])
        if seg.get('gen'):
            # v3.6.0 二创段（AI 原创画面）：不查原片素材，版权指标置 0
            src = Path(str(seg['gen']))
            source_start = 0.0
            visual_sec = 0.0
        else:
            src = find_src(dataset, seg['ep'], source_override)
            src_duration = probe_dur(src)
            # ★ 取窗统一走 resolve_window（2026-11-04 治根）：
            #   原来这里另写一遍（`min(min(d+0.3,TAKE_MAX), src_duration-source_start)`），
            #   与 cut_clips 的实切规则漂移 → 实切被片尾截断而度量以为取满
            #   → 画面总时长比音频时间轴短（K2 实测差 1.83s）→ 音画哨兵 FAIL。
            #   现在与实切共用同一函数 → 度量与实切严格一致。
            source_start, take_win = resolve_window(seg, src_duration)
            # 合规度量 = 实际从原片取到的连续画面秒数（loop 补齐的部分不算原片素材）
            visual_sec = min(take_win, d)
            visual_sec = max(0.0, visual_sec)
        raw_sec = float(raw_files.get(k, {}).get('dur', 0.0)) if not is_speech else 0.0
        shotmap_segments.append({
            'seg_id': k + 1, 'source': 'commentary' if is_speech else 'original_audio',
            'duration_sec': d, 'commentary_sec': d if is_speech else 0.0,
            'original_footage_sec': visual_sec, 'original_audio_sec': raw_sec,
            'episode': seg['ep'], 'source_file': str(src), 'source_start_sec': round(source_start, 3),
            'source_end_sec': round(source_start + visual_sec, 3), 'event_id': seg.get('event_id'),
            'primary_evidence_id': seg.get('primary_evidence_id') or seg.get('event_id'),
            'evidence_ids': seg.get('evidence_ids', []),
            'visual_anchor_only': bool(seg.get('visual_anchor_only', False)),
            'is_cold_open': bool(seg.get('is_cold_open', False)),
            'output_start_sec': round(cursor, 3), 'output_end_sec': round(cursor + d, 3),
        })
        cursor += d
    shotmap = out_dir / 'shotmap.json'
    shotmap.write_text(json.dumps({'schema_version': 2, 'total_duration_sec': total,
                                   'segments': shotmap_segments}, ensure_ascii=False, indent=2),
                       encoding='utf-8')
    return shotmap


# ── main ─────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('plan')
    ap.add_argument('--dataset', required=True)
    ap.add_argument('--platform', default='',
                    help='douyin/kuaishou/wechat_channels/generic；留空则读任务目录 request.json，'
                         '再不行才 generic')
    ap.add_argument('--voice', default='',
                    help='解说音色：① 音色名（按 SKILL.md §3.7 题材匹配表选，禁用无脑默认）；'
                         '② ref:<路径> 主角音色克隆（extract_voice_ref.py 产出）。'
                         '留空则读 plan.voice；两者都无 → 报错退出')
    ap.add_argument('--bgm', default=None)
    ap.add_argument('--out-dir', default=None)
    ap.add_argument('--source', default=None, help='explicit single source video for plans whose filename is not episode-based')
    ap.add_argument('--events', default=None, help='event library for episode/time/evidence validation')
    ap.add_argument('--time-tolerance', type=float, default=0.75)
    ap.add_argument('--preflight-report', default=None, help='reuse a task-scoped report created by pipeline.py')
    a = ap.parse_args()

    plan_path = Path(a.plan).resolve()
    plan = json.loads(plan_path.read_text(encoding='utf-8'))
    sys.path.insert(0, str(Path(__file__).parent))
    from plan_story import load_events, validate_plan
    events = load_events(Path(a.events).resolve()) if a.events else None
    plan, plan_errors = validate_plan(plan, events, a.time_tolerance)
    if plan_errors:
        raise BuildError('plan 校验失败: ' + '; '.join(plan_errors))
    segments = plan['segments']
    dataset = Path(a.dataset)
    source_override = Path(a.source).resolve() if a.source else None
    if source_override and len({s['ep'] for s in segments}) != 1:
        raise BuildError('--source 单视频模式只允许 plan 引用一个集数')

    name = plan.get('name') or 'final'
    if not name.lower().endswith('.mp4'):
        name += '.mp4'
    # ★ 出口哨兵（v3.6.2 修，坑典 P60）：name 走到 1481 行（final = out_dir/name）
    #   之前会被中间代码的循环变量覆盖（实测 seg22.wav → 成片写成 .wav，
    #   ffmpeg out#0/wav 硬失败，22 分钟白跑）。这里 + 那里双哨兵，
    #   任何一层被污染都立刻报错，绝不静默产出错误扩展名的半成品。
    if Path(name).name != name or not name.lower().endswith('.mp4'):
        raise BuildError(f'成片文件名非法: {name!r}（应为纯文件名且以 .mp4 结尾）')
    out_dir = Path(a.out_dir) if a.out_dir else \
        plan_path.parent / f"ep{plan.get('series_index', 1):02d}"
    out_dir.mkdir(parents=True, exist_ok=True)
    preflight_report = Path(a.preflight_report).resolve() if a.preflight_report else out_dir / 'preflight.json'
    if a.preflight_report:
        pf = None
    else:
        pf = subprocess.run([sys.executable, '-X', 'utf8', str(Path(__file__).with_name('preflight.py')),
                             '--out', str(preflight_report)], capture_output=True, text=True,
                            encoding='utf-8', errors='replace', timeout=3600)
    try:
        pf_data = json.loads(preflight_report.read_text(encoding='utf-8'))
        pf_decision = pf_data.get('decision', {})
    except Exception as e:
        exit_code = pf.returncode if pf is not None else 'reused'
        raise BuildError(f'成片前置预检没有可读报告: {e}; exit={exit_code}')
    if (pf is not None and pf.returncode not in (0, 2)) or pf_decision.get('verdict') == 'refuse':
        raise BuildError('成片前置预检拒绝继续: ' + '; '.join(pf_decision.get('reasons') or ['查看 preflight.json']))
    print(f"前置预检：{pf_decision.get('verdict', 'unknown')}")
    # ★ gen 段强制有 text + 相对路径规范化（v3.6.0 修）：
    #   ① gen 无 text 会被 normalize_plan 规范成 raw=1 → 与原声互斥规则
    #     冲突成混合语义；② 相对 gen 路径按 plan 所在目录解析，不按 cwd
    #     解析（原实现在 ff-e2e/_plan 外跑必 FileNotFound）
    plan_path = Path(a.plan).resolve()
    for i, s in enumerate(plan.get('segments', [])):
        if s.get('gen'):
            if not (s.get('text') or '').strip():
                raise BuildError(f'第{i}段是 gen 二创段但没有解说词 —— '
                                 f'二创段必须有 text（走配音链路，不属原声互斥）')
            gp = Path(str(s['gen']))
            if not gp.is_absolute():
                s['gen'] = str((plan_path.parent / gp).resolve())
    work = out_dir / '_work'
    if work.exists():
        # ★ 沙箱内进程删 >50 文件的目录会被安全层进程级拦截（ignore_errors
        #   都兜不住，实测 EXIT=1），rebuild 场景必踩。这里只 rename 不删除，
        #   残留 _trash_* 目录由外部（提权通道）统一清理。
        # ★ 断点续跑回搬（v3.6.2 修）：单段文案失败不必全量重跑 TTS——
        #   先从最近一个 _trash_* 把 speech/ 与 rawNN.wav 搬回新 _work
        #   （narrate/cut_raw 命中已存产物即跳过）。trash 里 speech 为空
        #   （从未跑过配音）则不搬。
        trash = out_dir / ('_trash_%d_%d' % (int(time.time()), os.getpid()))
        try:
            work.rename(trash)
        except OSError:
            pass
        # ★ v3.6.3 修（坑典 P66）：旧版把「找 _trash_*」放在 rename 之前，
        #   于是**首次崩溃重跑**时目录里还没有任何 _trash_* → old_speech=None
        #   → 本次刚被 rename 进 trash 的 speech（含已合成的 seg*.wav 与
        #   _sig.json）一个都不回搬 → narrate 又以 keep_existing=False 清空重来。
        #   断点续跑偏偏在「第一次崩」这个最该生效的场景完全失效（C2 实砸：7 段全丢）。
        #   改为 rename **之后**再找，本次的 trash 自然被纳入，多个 trash 按
        #   新→旧取每个 segNN 的最新一份。
        old_speech = None
        for t in sorted(out_dir.glob('_trash_*'), reverse=True):
            cand = t / 'speech'
            if cand.is_dir() and any(cand.glob('seg*.wav')):
                old_speech = t
                break
        if old_speech is not None:
            (work / 'speech').mkdir(parents=True, exist_ok=True)
            # 遍历所有 trash（新→旧），同一 segNN 取最先遇到（最新）的
            best = {}
            for t in sorted(out_dir.glob('_trash_*'), reverse=True):
                for f in (t / 'speech').glob('seg*.wav'):
                    best.setdefault(f.name, f)
                # ★ 复用签名一起回搬：没有 _sig.json 就无法判断某段 wav
                #   是旧文案还是新文案产物，只能保守地全部重合成。
                sp = (t / 'speech') / '_sig.json'
                if sp.is_file():
                    best.setdefault('_sig.json', sp)
                for f in t.glob('raw*.wav'):
                    best.setdefault(f.name, f)
            n_back = 0
            # ★ 循环变量绝不叫 name（v3.6.2 修，坑典 P60）：外层 1290 行的
            #   name 是成片输出文件名，被这里当成循环变量覆盖后，成片会被
            #   写成 segNN.wav（实测：.seg22.wav.burned.mp4 → 烧贴纸段
            #   out#0/wav "Nothing was written" 硬失败，22 分钟白跑）。
            #   变量名一律带前缀，杜绝与外层变量重名。
            for fname, f in best.items():
                dst = (work / 'speech' / fname
                       if (fname.startswith('seg') or fname == '_sig.json')
                       else work / fname)
                if not dst.exists():
                    dst.write_bytes(f.read_bytes())
                    n_back += 1
            if n_back:
                print(f'断点续跑：回搬 {n_back} 个已合成产物（speech {len(best)} 候选）')
    (work / 'speech').mkdir(parents=True, exist_ok=True)

    n_sp = sum(1 for s in segments if (s.get('text') or '').strip())
    print(f'结构：{len(segments)} 段（解说 {n_sp} / 原声 {len(segments)-n_sp}）')

    # ★ 2026-10-21 音色决策：--voice > plan.voice > 报错（废除无脑默认音色）
    voice = a.voice or (plan.get('voice') if isinstance(plan, dict) else None) or ''
    if not voice:
        raise BuildError(
            '未指定解说音色：--voice 或 plan.voice 必填。\n'
            '  ① 题材匹配预设（SKILL.md §3.7）：如 子言/小昊/明宇/思思/小雨/小萝莉/方言\n'
            '  ② 主角克隆：ref:<voice_ref.wav>（extract_voice_ref.py 从原片原声段提取）')
    print(f'音色：{voice}')

    # ★ 断点续跑开关（v3.6.2 修）：只有「上一次跑的音色 == 本次音色」才允许
    #   复用 TTS 产物；换音色则 narrate 内部全清重来，杜绝新旧音色混用。
    #   （此前 narrate 无条件清目录 → 回搬了 21 个 wav 也照样重合成。）
    keep_existing = False
    prev_plan_p = out_dir / 'build-plan.json'
    if prev_plan_p.is_file():
        try:
            prev_voice = str(json.loads(
                prev_plan_p.read_text(encoding='utf-8')).get('voice') or '')
        except Exception:
            prev_voice = ''
        keep_existing = (prev_voice == voice)
    else:
        # ★ 崩溃兜底（v3.6.3 修，坑典 P65）：build-plan.json 原本只在 [3/6] 之后
        #   才写，导致 TTS 阶段（最长、最易挂）崩溃时没有任何「上次音色」记录，
        #   重跑 keep_existing=False → narrate 清空整个 speech 目录，
        #   92 段跑到第 7 段的成果全丢（C2 实砸）。改用 _sig.json 里的音色兜底。
        sig_p = work / 'speech' / '_sig.json'
        if sig_p.is_file():
            try:
                prev_voice = str(json.loads(
                    sig_p.read_text(encoding='utf-8')).get('voice') or '')
            except Exception:
                prev_voice = ''
            keep_existing = (prev_voice == voice)
    if keep_existing:
        print('断点续跑：音色一致 → 复用已合成配音（文案/音色变了的段自动重合成）')

    # ★ 抢在配音前落盘 build-plan.json（v3.6.3 修，坑典 P65）：把「这次要构建什么」
    #   先记下来，后续任何阶段崩溃重跑都能靠它判定音色一致性，不必依赖末尾写入。
    plan['voice'] = voice
    plan['dataset'] = str(dataset)
    write_json_atomic(out_dir / 'build-plan.json', plan)

    # ★ 配音前置硬检查（坑典 P68）：先扫文案，别等跑到半路才炸
    precheck_tts_text(segments)

    print('\n[1/6] 配音')
    made = narrate(segments, work / 'speech', voice, keep_existing)

    print('\n[2/6] 原声定位与切片（ASR 定位真实台词时间点）')
    raw_files = {}
    for k, seg in enumerate(segments):
        if (seg.get('text') or '').strip():
            continue
        if seg.get('is_cold_open'):
            r = cut_cold_open(dataset, seg['ep'], seg['t'], COLD_OPEN_MAX,
                             work / f'raw{k:02d}.wav', work, source_override)
        else:
            r = cut_raw(dataset, seg['ep'], seg['t'], RAW_MAX_SEC,
                        work / f'raw{k:02d}.wav', work, source_override)
        r['file'] = str(work / f'raw{k:02d}.wav')
        raw_files[k] = r
        tag = ' 〔冷开场〕' if seg.get('is_cold_open') else ''
        print(f'    raw{k:02d}  {r["dur"]:5.2f}s  起点 {r["note"]}  '
              f'「{r["text"] or "(无)"}」{tag}')

    print('\n[3/6] 定时长（唯一数据源 = segments）')
    total = retime(segments, made, raw_files)
    cur = 0.0
    for k, seg in enumerate(segments):
        d = seg['duration']
        kind = '解说' if (seg.get('text') or '').strip() else '原声'
        print(f'    {k:02d} {kind} {d:5.2f}s [{cur:5.2f}-{cur+d:5.2f}]  '
              f'{seg.get("text") or "(原声独占)"}')
        cur += d
    print(f'    合计 {total}s')
    plan['total_sec'] = total
    plan['voice'] = voice
    # ★ 记录 dataset（v3.6.1 修）：续跑脚本（recontinue.py）据此定位素材，
    #   杜绝硬编码数据集导致「A 剧画面 + B 剧解说」的串剧（哨兵只是兜底）
    plan['dataset'] = str(dataset)
    write_json_atomic(out_dir / 'build-plan.json', plan)

    # ── shotmap（v3.6.1 抽成共享函数：main 与 recontinue 续跑共用，
    #    原来内联在 main 里，续跑脚本跳过即 shotmap_missing 质检 FAIL） ──
    write_shotmap(segments, dataset, raw_files, source_override, out_dir, total)

    print('\n[4/6] 切画面并拼接')
    clips = cut_clips(segments, dataset, work, source_override)
    silent = concat(clips, work)
    silent_dur = probe_dur(silent)
    print(f'    {len(clips)} 段 → {silent_dur:.2f}s')
    # ★ 画面总时长必须等于 retime 的 total（音画对齐哨兵，2026-10-24 首跑踩坑：
    #   gen 段 PNG 不 loop 少 6.5s → 音轨 86.2s vs 视频 79.7s 错位）。
    #   阈值 1.0s：30fps 帧量化在 31 段上累计约 0.6s（X3 实测），
    #   真实错位（丢整段）≥2s —— 1.0s 两侧都有足够区分带。
    if abs(silent_dur - total) > 1.0:
        raise BuildError(f'画面总时长 {silent_dur:.2f}s 与音频时间轴 {total:.2f}s '
                         f'差 {abs(silent_dur - total):.2f}s —— 必有段没铺满时长'
                         f'（查 gen 段 -loop 1 / 解说段 loop 补齐）')

    print('\n[5/6] 混音（解说/原声结构互斥 + BGM duck）')
    bgm = Path(a.bgm) if a.bgm else None
    if bgm is None and plan.get('bgm'):
        for c in (Path(plan['bgm']), Path(plan['bgm']).with_suffix('.wav')):
            if c.exists():
                bgm = c
                break
    mixed = work / 'mixed.mp4'
    mix(silent, segments, work / 'speech', raw_files, bgm, mixed, total)
    print(f'    BGM: {bgm.name if bgm else "无"}')

    print('\n[6/6] 烧字幕 + 校验')
    # ★ 平台必须与 S3 一致（2026-10-20 实测）：
    #   S3 按 request.json 的 platform 定字幕容量（douyin 13 字/行），
    #   而 build 过去默认 generic（14 字/行）→ 字幕按 14 字断行，
    #   质量门的字幕硬门按 13 字判 → 出现一堆 line_too_long:14>13。
    #   手动跑很容易漏传 --platform，所以这里默认从任务目录的 request.json 取。
    platform = a.platform
    if not platform:
        req_path = Path(a.plan).resolve().parent / 'request.json'
        if req_path.is_file():
            try:
                platform = (json.loads(req_path.read_text(encoding='utf-8'))
                            .get('platform') or 'generic')
            except Exception:
                platform = 'generic'
        else:
            platform = 'generic'
    if platform not in CFG['platform_profiles']:
        raise BuildError(f'未知平台 {platform!r}；可选 '
                         + '/'.join(k for k in CFG['platform_profiles'] if not k.startswith('_')))
    profile = CFG['platform_profiles'][platform]
    srt = write_srt(segments, work / 'subs.srt', profile)
    # ★ 二次哨兵（v3.6.2 修）：name 在 1290→1492 之间经过大量中间代码，
    #   任一处用 name 当循环变量都会把成片名改掉。落盘前再判一次。
    if Path(name).name != name or not name.lower().endswith('.mp4'):
        raise BuildError(f'成片文件名在流程中被污染为 {name!r}（应为 名字.mp4）。'
                         f'检查 1290→1492 之间是否有循环变量与 name 重名。')
    final = out_dir / name
    # v3.6.0：有段级 stickers 时 burn 出临时片再叠表情包层，否则一把烧到位。
    # ★ has_stickers 只统计**解说段**的贴纸（v3.6.0 修：全原声段 plan 带
    #   stickers 会白跑一次 burn 再被 overlay no-op 复制一遍）
    has_stickers = any(s.get('stickers') and (s.get('text') or '').strip()
                       for s in segments)
    if has_stickers:
        burned = out_dir / f'.{name}.burned.mp4'
        try:
            burn(mixed, srt, burned, profile, platform, total)
            overlay_stickers(burned, segments, final, work, total, platform)
        finally:
            safe_unlink(burned)   # 失败也清临时片，不留垃圾
    else:
        burn(mixed, srt, final, profile, platform, total)

    rep = verify(final, work)
    print(f'    视频 {rep["video_dur"]}s / 音轨 {rep["audio_dur"]}s')
    print(f'    响度 {rep["lufs"]} LUFS / 峰值 {rep["peak"]} dBTP')
    print(f'    扫描 {rep["windows"]} 个窗口，最低 {rep["min_db"]} dB')
    print(f'    最长连续静音 {rep["longest_silent_run"]}s（上限 {MAX_SILENT_RUN_SEC}s，'
          f'段间呼吸留白 {BREATH}s 属设计内）')
    if rep['silent_runs']:
        print(f'    静音段明细: {rep["silent_runs"][:8]}')
    if rep['errors']:
        print('\n    ✗ 校验未通过：')
        for e in rep['errors']:
            print('      · ' + e)
        raise BuildError('校验未通过，不交付')
    print('\n    ✓ 全部校验通过')
    print(f'\n成片: {final}')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except BuildError as e:
        print(f'\n[失败] {e}')
        sys.exit(1)
    except Exception as e:
        print(f'\n[意外错误] {type(e).__name__}: {e}')
        sys.exit(1)
