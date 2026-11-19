# -*- coding: utf-8 -*-
"""Breeze TTS 2 worker（本地配音工坊 · breeze 引擎）

跑 T8star Aix Voice Studio 自带的 breeze_infer.api（FastAPI），三种模式：
  ① voice_clone     ：--ref + --ref-text（必须成对）
  ② voice_design    ：仅 --instruction（无需参考音，Breeze 独有能力）
  ③ voice_direction ：--ref + --ref-text + --instruction（克隆音色 + 指令调语气）

与其他引擎的三个关键差异（worker 已内置兜底）：
  1. 服务模式：模型 ~7.3GB，必须常驻。worker 会自动拉起服务并等待就绪（首次约 2-3 分钟）。
  2. 参考音格式：Breeze 用 soundfile 读音频，只认 wav/flac/ogg，**不认 m4a/mp3**
     → worker 自动用 ffmpeg 转成 24k mono wav 并缓存。
  3. ref_text 必填（参考音精确转写）→ 可手动给，或 --auto-transcribe 用内置
     faster-whisper 自动转写（GPU 版缺 cublas，固定走 CPU int8，17s 音频约 2.6s）。

输出：24kHz mono 16bit WAV（服务端返回裸 PCM，worker 封装成 WAV）。
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import wave
from pathlib import Path

# 端口唯一来源 = registry.ENGINE_PORTS（dub.py --serve 的常驻服务也读它）。
# 原来是 7860，与 dub.py 登记的 8735 不一致 —— 两种用法并存会起两个 Breeze 实例
# （各约 7.7GiB 显存，12G 卡直接 OOM），且残留实例无人回收。统一到同一张表。
# worker 可能在别的 Python 下单独跑，import 失败就退回表里的值，不硬崩。
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from registry import engine_port as _engine_port
    DEFAULT_PORT = _engine_port("breeze", 8735)
except Exception:      # noqa: BLE001 —— 拿不到注册表也要能跑，用表内同值兜底
    DEFAULT_PORT = 8735

SR = 24000

# ── 长文本自动分段阈值 ────────────────────────────────────────
# 切段的**唯一目的**是绕开服务端 max_new_tokens=1500 的静默截断。
# 实测：650 字稿不分段 → 只出 106.56s 音频后停住（尾部整段丢失）；
# 中文速率 ≈5.11 字/秒 → 106.56s ≈ 544 字就是硬上限。
#
# 阈值取 350 而不是更小的数，理由：
#   · 分段**不提速** —— 实测 RTF 在 130 字/段与 650 字/段下完全一致（6.95 vs 6.98），
#     说明 Breeze 生成耗时对长度是线性的，没有平方级惩罚。切细了纯亏。
#   · 切太细会伤自然度 —— 每段之间要插静音间隙，普通稿子被切开等于凭空加停顿。
#     30 秒口播稿有 156 字，若阈值 150 就会被切成两段，毫无必要。
#   · 350 字 → 约 68s 音频，对 106s 上限留 36% 余量；语速被 instruction 调慢
#     （按 4 字/秒算 → 87s）也仍有 18% 余量。
DEFAULT_CHUNK_CHARS = 350
SERVICE_TIMEOUT = 420      # 等服务就绪上限（秒）：首次要加载 7.3GB 权重
REQUEST_TIMEOUT = 1800     # 单次合成上限（秒）
WAV_EXTS = {".wav"}        # soundfile 还支持 flac/ogg，但音色包基本是 wav/mp3/m4a


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def find_backend(root: Path) -> Path:
    """定位 resources/backend（GUI 版本号会变，通配匹配）。"""
    hits = sorted(glob.glob(str(root / "T8star-Aix-Voice-Studio-*" / "resources" / "backend")))
    if not hits:
        raise SystemExit(f"[breeze] 未找到 backend 目录（{root} 下 T8star-Aix-Voice-Studio-*/resources/backend）")
    return Path(hits[-1])


def find_python(root: Path) -> Path:
    hits = sorted(glob.glob(str(root / "T8star-Aix-Voice-Studio-*" / "resources" / "python" / "python.exe")))
    if not hits:
        raise SystemExit(f"[breeze] 未找到内置 python（{root}）")
    return Path(hits[-1])


def find_model(root: Path) -> Path:
    # models/breeze_tts/BreezeBlue_Breeze-TTS-2
    hits = sorted(glob.glob(str(root / "models" / "breeze_tts" / "*Breeze-TTS-2*")))
    if not hits:
        raise SystemExit(f"[breeze] 未找到模型目录（{root}/models/breeze_tts/*Breeze-TTS-2*）")
    return Path(hits[-1])


def find_ffmpeg() -> str | None:
    """本机 ffmpeg：优先引擎自带，其次 PATH。"""
    cands = [
        r"D:/AITools/TTS/emotionTTS_v4.7.0/core/bin/ffmpeg.exe",
        r"D:/软件/JianyingPro/10.5.0.13988/ffmpeg.exe",
    ]
    for c in cands:
        if os.path.isfile(c):
            return c
    from shutil import which
    return which("ffmpeg")


def health(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


def ensure_service(backend: Path, py: Path, model: Path, port: int) -> None:
    """服务不在就后台拉起，轮询 health 直到就绪。"""
    if health(port):
        log(f"[breeze] 复用已运行的服务 (port={port})")
        return

    log(f"[breeze] 启动服务（首次需加载 ~7.3GB 权重，约 2-3 分钟）...")
    cmd = [str(py), "-m", "breeze_infer.api", str(model),
           "--host", "127.0.0.1", "--port", str(port)]
    creation = 0
    if sys.platform == "win32":
        creation = subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(cmd, cwd=str(backend),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=creation)
    t0 = time.time()
    while time.time() - t0 < SERVICE_TIMEOUT:
        time.sleep(5)
        if health(port):
            log(f"[breeze] 服务就绪（{time.time()-t0:.0f}s）")
            return
    raise SystemExit(f"[breeze] 服务启动超时（>{SERVICE_TIMEOUT}s），请检查显存是否被其它引擎占用")


def to_wav(src: Path, cache_dir: Path, ffmpeg: str) -> Path:
    """非 wav 参考音 → 转 24k mono wav 并缓存（Breeze 只认 soundfile 格式）。"""
    if src.suffix.lower() in WAV_EXTS:
        return src
    cache_dir.mkdir(parents=True, exist_ok=True)
    dst = cache_dir / f"{src.stem}_{SR}.wav"
    if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
        return dst
    if not ffmpeg:
        raise SystemExit(f"[breeze] 参考音 {src.name} 非 wav 且本机无 ffmpeg，"
                         f"请先转成 wav（Breeze 不支持 m4a/mp3）")
    subprocess.run([ffmpeg, "-v", "error", "-y", "-i", str(src),
                    "-ar", str(SR), "-ac", "1", str(dst)], check=True)
    log(f"[breeze] 参考音已转 wav：{dst.name}")
    return dst


def transcribe(audio: Path, backend: Path, py: Path) -> str:
    """用内置 faster-whisper 转写参考音（GPU 缺 cublas，固定 CPU int8）。"""
    code = (
        "from faster_whisper import WhisperModel;"
        "m=WhisperModel('models/faster-whisper-small',device='cpu',compute_type='int8');"
        f"segs,_=m.transcribe(r'{audio}',language='zh',vad_filter=True);"
        "print(''.join(s.text for s in segs).strip())"
    )
    r = subprocess.run([str(py), "-c", code], cwd=str(backend),
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise SystemExit(f"[breeze] 自动转写失败：{r.stderr[-300:]}")
    return r.stdout.strip()


_CTX = {}          # main() 里填入 backend/py/model/port，供 409 自愈时重启服务
LOCK_RETRIES = 3  # 单并发冲突（HTTP 409）的重试次数


def pid_on_port(port: int):
    """找出监听该端口的进程 PID（Windows netstat）。"""
    try:
        r = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                           encoding="gbk", errors="replace", timeout=15)
        for line in (r.stdout or "").splitlines():
            if f":{port}" in line and "LISTENING" in line.upper():
                parts = line.split()
                if parts and parts[-1].isdigit():
                    return int(parts[-1])
    except Exception:
        return None
    return None


def kill_service(port: int) -> bool:
    """强杀占用端口的服务进程（用于 409 锁死后的自愈）。"""
    pid = pid_on_port(port)
    if not pid:
        return False
    log(f"[breeze] 终止锁死的服务进程 PID={pid}")
    subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                   capture_output=True, text=True, errors="replace")
    time.sleep(3)
    return True


def post_speech(port: int, fields: dict, files: dict) -> bytes:
    """POST /v1/audio/speech，带 409 自愈。

    为什么必须自愈：Breeze 服务是**单并发**（/_request_lock），一旦上一个请求没走完
    （客户端中断 / 进程被杀 / 生成器没被消费完），锁就永久不释放 —— health 仍返回 ok，
    但之后所有请求一律 409。breeze 现在是默认引擎，锁死 = 整个配音工坊瘫痪。
    策略：409 → 退避重试 → 仍 409 判定为锁死 → 杀进程重启服务 → 最后再试一次。
    """
    last = ""
    for attempt in range(LOCK_RETRIES + 1):
        try:
            return _post_once(port, fields, files)
        except urllib.error.HTTPError as e:
            if e.code != 409:
                detail = e.read().decode("utf-8", "replace")[:400]
                raise SystemExit(f"[breeze] HTTP {e.code}: {detail}")
            last = "409 服务忙（单并发）"
            if attempt < LOCK_RETRIES:
                wait = 10 * (attempt + 1)
                log(f"[breeze] {last}，{wait}s 后重试 {attempt + 1}/{LOCK_RETRIES}...")
                time.sleep(wait)
                continue
            # 重试耗尽 → 判定锁死，重启服务后做最后一次尝试
            log("[breeze] 多次 409，判定服务锁死 → 重启服务后重试")
            if kill_service(port) and _CTX.get("backend"):
                ensure_service(_CTX["backend"], _CTX["py"], _CTX["model"], port)
                return _post_once(port, fields, files)
            raise SystemExit(f"[breeze] 服务持续 409 且无法自愈：{last}")
    raise SystemExit(f"[breeze] 请求失败：{last}")


def _post_once(port: int, fields: dict, files: dict) -> bytes:
    boundary = uuid.uuid4().hex
    body = b""
    for k, v in fields.items():
        body += f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode("utf-8")
    for k, p in files.items():
        data = Path(p).read_bytes()
        fname = Path(p).name.encode("utf-8").decode("latin-1", "replace")
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; '
                 f'filename="{fname}"\r\nContent-Type: application/octet-stream\r\n\r\n').encode("utf-8")
        body += data + b"\r\n"
    body += f"--{boundary}--\r\n".encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/audio/speech", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
        return r.read()   # 必须读完：服务端在生成器 finally 里释放并发锁


def pcm_to_wav(pcm: bytes, out: Path) -> float:
    out.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm)
    return len(pcm) / 2 / SR


# ──────────────────────────────────────────────────────────────
# 多角色（与 indextts2 同思路：**逐段合成 + 拼接**，Breeze 自带的 API 把 speaker
# 写死成 [S0]，没有暴露多说话人，所以不在单次生成里做）
#   注：模型 tokenizer 里确实有 [S0]~[S9] 说话人 token（原生多角色潜力），
#       但官方 README 未文档化、自带 API 也不支持，暂不冒险走未验证路径。
# ──────────────────────────────────────────────────────────────
_MULTI_ROLE_LINE_RE = re.compile(r"^\s*\[([^\]]+)\]\s*[:：]\s*(.*)$")


def parse_multi_role_text(text: str):
    """解析剧本 → [(角色, 台词)]；不带角色标记的行续到上一条台词。"""
    segments = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _MULTI_ROLE_LINE_RE.match(line)
        if m:
            role, content = m.group(1).strip(), m.group(2).strip()
            if content:
                segments.append([role, content])
        elif segments:
            segments[-1][1] += " " + line
    return [(r, c) for r, c in segments]


def concat_wavs(paths, out_path: Path, gap_ms: int = 0) -> float:
    """按顺序拼接 wav（同采样率/位深/声道），可选角色间静音间隔。返回总时长。"""
    if not paths:
        raise ValueError("没有可拼接的片段")
    gap = b"\x00" * (int(SR * gap_ms / 1000) * 2)   # 16bit mono
    with wave.open(str(out_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        for i, p in enumerate(paths):
            if i and gap:
                w.writeframes(gap)
            with wave.open(str(p), "rb") as r:
                if (r.getframerate(), r.getsampwidth(), r.getnchannels()) != (SR, 2, 1):
                    raise SystemExit(f"[breeze] 片段参数不一致，无法拼接：{p}")
                w.writeframes(r.readframes(r.getnframes()))
    with wave.open(str(out_path), "rb") as w:
        return w.getnframes() / w.getframerate()


def split_text(text: str, budget: int):
    """按句末标点切分成 ≤budget 字的片段（单句仍超预算时再按逗号/顿号切）。

    为什么必须切：Breeze 的 max_new_tokens=1500 / max_seq_len=2048，长文本会被
    **静默截断**（实测 650 字只覆盖 89.4%，结尾直接丢）；而且长序列注意力是平方级开销，
    实测 RTF 从 1.89（30s 文本）恶化到 **7.0**（650 字，12 分 23 秒）。
    切成 ~150 字的段后：不截断 + 速度回到 RTF≈1.9。
    """
    parts = re.split(r"(?<=[。！？!?；;])", text)
    chunks, cur = [], ""
    for p in parts:
        if not p.strip():
            continue
        if len(cur) + len(p) <= budget:
            cur += p
            continue
        if cur:
            chunks.append(cur)
            cur = ""
        if len(p) <= budget:
            cur = p
            continue
        # 单句就超预算 → 退一步按逗号/顿号切
        for q in re.split(r"(?<=[，,、])", p):
            if len(cur) + len(q) <= budget:
                cur += q
            else:
                if cur:
                    chunks.append(cur)
                cur = q
    if cur:
        chunks.append(cur)
    return [c for c in chunks if c.strip()]


def resolve_ref_text(ref_wav: Path, backend: Path, py: Path,
                     manual: str = "", no_transcribe: bool = False) -> str:
    """参考音转写：手动 > 缓存 > 自动转写（结果写回缓存）。"""
    if manual:
        return manual.strip()
    cache_dir = Path(__file__).resolve().parent.parent / ".cache" / "breeze_refs"
    cache = cache_dir / f"{ref_wav.stem}.txt"
    if cache.is_file():
        t = cache.read_text(encoding="utf-8").strip()
        if t:
            return t
    if no_transcribe:
        return ""
    log(f"[breeze] 转写参考音 {ref_wav.name}（CPU whisper）...")
    t = transcribe(ref_wav, backend, py)
    if t:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache.write_text(t, encoding="utf-8")
    return t


def run_multi_role(a, backend: Path, py: Path, model: Path) -> int:
    """多角色：逐段合成（每段用自己的参考音）→ 拼接成一条对话音频。"""
    import shutil
    if not a.roles:
        print(json.dumps({"ok": False, "error": "多角色模式必须提供 --roles 角色映射JSON"}))
        return 2
    try:
        role_map = json.loads(a.roles)
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"角色映射JSON解析失败: {e}"}))
        return 2
    if not isinstance(role_map, dict) or not role_map:
        print(json.dumps({"ok": False, "error": "角色映射不能为空"}))
        return 2

    script = Path(a.multi_role).read_text(encoding="utf-8")
    segments = parse_multi_role_text(script)
    if not segments:
        print(json.dumps({"ok": False, "error": "剧本未解析到台词，请用 [角色名]: 内容 格式"}))
        return 2
    missing = sorted({r for r, _ in segments if r not in role_map})
    if missing:
        print(json.dumps({"ok": False, "error": f"以下角色未配置参考音色: {', '.join(missing)}"}))
        return 2

    ensure_service(backend, py, model, a.port)
    cache_dir = Path(__file__).resolve().parent.parent / ".cache" / "breeze_refs"
    t0 = time.time()
    seg_dir = Path(tempfile.mkdtemp(prefix="breeze_multi_"))
    seg_paths, ref_cache = [], {}
    try:
        for idx, (role, line) in enumerate(segments, 1):
            ref = Path(role_map[role])
            if not ref.exists():
                print(json.dumps({"ok": False, "error": f"角色 {role} 的参考音不存在: {ref}"}))
                return 2
            ref = to_wav(ref, cache_dir, find_ffmpeg())
            # 同一角色的转写只做一次（多角色剧本里一个角色会说很多句）
            if role not in ref_cache:
                ref_cache[role] = resolve_ref_text(ref, backend, py, "", a.no_auto_transcribe)
            if not ref_cache[role]:
                print(json.dumps({"ok": False, "error":
                    f"角色 {role} 缺少参考音转写（--no-auto-transcribe 时需手动给）"}))
                return 2
            log(f"[breeze] [{idx}/{len(segments)}] {role}（{len(line)}字）")
            safe_role = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "_", role)
            seg = seg_dir / f"{idx:03d}_{safe_role}.wav"
            pcm = post_speech(a.port, {
                "text": line,
                "instruction": a.instruction,
                "cfg_scale": str(a.cfg_scale),
                "seed": str(a.seed),
                "ref_text": ref_cache[role],
            }, {"ref_audio": str(ref)})
            pcm_to_wav(pcm, seg)
            seg_paths.append(seg)

        log("[breeze] 正在拼接音频...")
        dur = concat_wavs(seg_paths, Path(a.out), gap_ms=a.gap_ms)
        ok = Path(a.out).is_file() and Path(a.out).stat().st_size > 0
        print(json.dumps({"ok": ok, "engine": "breeze", "mode": "multi-role",
                          "roles": len(role_map), "segments": len(segments),
                          "duration": round(dur, 2), "out": a.out,
                          "cost": round(time.time() - t0, 1)}, ensure_ascii=False))
        return 0 if ok else 3
    finally:
        shutil.rmtree(seg_dir, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine-root", required=True)
    ap.add_argument("--text", default="",
                    help="要合成的文本（多角色模式下留空，由 --multi-role 提供剧本）")
    ap.add_argument("--out", required=True)
    ap.add_argument("--ref", default=None, help="参考音频（voice clone / direction）")
    ap.add_argument("--ref-text", default="", help="参考音精确转写（与 --ref 成对）")
    ap.add_argument("--ref-text-file", default=None)
    ap.add_argument("--auto-transcribe", action="store_true",
                    help="兼容旧调用：现在默认就会自动转写，此参数保留但已无必要")
    ap.add_argument("--no-auto-transcribe", action="store_true",
                    help="关闭自动转写（则必须手动给 --ref-text）")
    ap.add_argument("--multi-role", default=None,
                    help="多角色剧本文件（每行 '[角色名]: 台词'）")
    ap.add_argument("--roles", default=None,
                    help='角色映射 JSON：{"角色名":"参考音路径", ...}')
    ap.add_argument("--gap-ms", type=int, default=250,
                    help="多角色拼接时角色间静音间隔（毫秒）")
    ap.add_argument("--chunk-chars", type=int, default=DEFAULT_CHUNK_CHARS,
                    help=f"长文本自动分段的每段字数上限（默认 {DEFAULT_CHUNK_CHARS}，"
                         f"推导见 DEFAULT_CHUNK_CHARS 注释；超了才切，普通段落不切）")
    ap.add_argument("--no-chunk", action="store_true", help="关闭长文本自动分段（会截断）")
    ap.add_argument("--instruction", default="Speak clearly and naturally.",
                    help="自然语言指令（voice design / direction）")
    ap.add_argument("--cfg-scale", type=float, default=4.0)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    a = ap.parse_args()

    root = Path(a.engine_root)
    backend = find_backend(root)
    py = find_python(root)
    model = find_model(root)
    _CTX.update(backend=backend, py=py, model=model, port=a.port)

    # ── 多角色：--multi-role 剧本 + --roles 角色映射JSON ──
    if a.multi_role:
        return run_multi_role(a, backend, py, model)

    ref_text = a.ref_text
    if a.ref_text_file:
        ref_text = Path(a.ref_text_file).read_text(encoding="utf-8").strip()

    files = {}
    if a.ref:
        ref = Path(a.ref)
        if not ref.exists():
            raise SystemExit(f"[breeze] 参考音不存在：{ref}")
        # 格式适配：Breeze 不认 m4a/mp3
        ref = to_wav(ref, Path(__file__).resolve().parent.parent / ".cache" / "breeze_refs",
                     find_ffmpeg())
        if not ref_text:
            ref_text = resolve_ref_text(ref, backend, py, "", a.no_auto_transcribe)
            log(f"[breeze] ref_text：{ref_text[:60]}...")
        if not ref_text:
            raise SystemExit("[breeze] voice clone 需要 --ref-text（参考音精确转写）；"
                             "默认已自动转写（--no-auto-transcribe 可关）")
        files["ref_audio"] = str(ref)

    mode = "voice-clone" if a.ref else "voice-design"
    chunks = ([a.text] if (a.no_chunk or len(a.text) <= a.chunk_chars)
              else split_text(a.text, a.chunk_chars))
    log(f"[breeze] mode={mode}  {len(a.text)} 字 → {len(chunks)} 段"
        + ("" if len(chunks) == 1 else f"（自动分段，每段≤{a.chunk_chars}字）"))

    ensure_service(backend, py, model, a.port)
    t0 = time.time()
    if len(chunks) == 1:
        pcm = post_speech(a.port, {
            "text": a.text,
            "instruction": a.instruction,
            "cfg_scale": str(a.cfg_scale),
            "seed": str(a.seed),
            "ref_text": ref_text,
        }, files)
        dur = pcm_to_wav(pcm, Path(a.out))
    else:
        import shutil
        seg_dir = Path(tempfile.mkdtemp(prefix="breeze_chunk_"))
        try:
            segs = []
            for i, c in enumerate(chunks, 1):
                log(f"[breeze] [{i}/{len(chunks)}] {len(c)} 字")
                pcm = post_speech(a.port, {
                    "text": c,
                    "instruction": a.instruction,
                    "cfg_scale": str(a.cfg_scale),
                    "seed": str(a.seed),
                    "ref_text": ref_text,
                }, files)
                seg = seg_dir / f"{i:03d}.wav"
                pcm_to_wav(pcm, seg)
                segs.append(seg)
            log("[breeze] 正在拼接音频...")
            dur = concat_wavs(segs, Path(a.out), gap_ms=min(a.gap_ms, 200))
        finally:
            shutil.rmtree(seg_dir, ignore_errors=True)
    cost = time.time() - t0
    log(f"[breeze] ✅ {a.out}  {dur:.2f}s  耗时 {cost:.1f}s  RTF {cost/dur:.2f}")


if __name__ == "__main__":
    main()
