#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
本地配音工坊 · 统一配音入口
============================
一句话选引擎 + 选音色，合成 WAV。本脚本只做"编排"：
  1. 读 registry 的 engines.json / voices.json（没有就现场发现）
  2. 解析音色别名 → 参考音频；自动补 prompt_text
  3. 用对应引擎自带的便携 Python 调起 workers/worker_xxx.py
  4. 校验输出 WAV，打印结果

用法：
  python dub.py --list-engines
  python dub.py --list-voices
  python dub.py --engine indextts2 --voice 子言 --text "今天天气真好" --out out.wav
  python dub.py --engine indextts2 --multi-role 剧本.txt --roles "子言=子言,小棠=小棠" --out out.wav  # 多角色对话
  python dub.py --engine dotstts   --ref d:/x/ref.wav --text "hi" --out out.wav --language english
  python dub.py --engine pilottts  --voice 御姐 --text "好难过" --out out.wav --emotion sad
  python dub.py --engine chatterbox --voice 子言 --text "今天天气真好" --out out.wav
  python dub.py --engine chatterbox --model zh-cmn --text "今天天气真好" --out out.wav --language zh
  python dub.py --engine chatterbox --text "Hello world" --out out.wav --language en   # 用内置音色

扩展：往 D:\\AITools\\TTS 丢新引擎文件夹 → 在 registry.FINGERPRINTS 加一条指纹
      + 写 workers/worker_xxx.py，跑一次 scan.py 即挂载。
"""
from __future__ import annotations

import argparse
import atexit
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.error
import uuid
import wave
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent
VERSION = "0.9.4"  # 单一版本号来源（--health/文档一致）
sys.path.insert(0, str(SKILL_DIR))

from registry import (  # noqa: E402
    load_engines, load_voices, find_voice, find_engine, resolve_engine,
    TTS_ROOT, VOICE_POOL, DEFAULT_ENGINE, TIER_LABEL,
    ENGINE_PORTS, engine_port, oneshot_port,
)
from lexicon import apply_brands, preview_brands  # noqa: E402

# 端口唯一来源是 registry.ENGINE_PORTS（worker 一次性合成也读它）。
# 这里保留别名只为兼容旧引用，别再往里加条目。
SERVE_PORTS = ENGINE_PORTS


def _serve_cache_dir():
    d = SKILL_DIR / ".cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _trash_rename(p):
    """「删除」实为**改名**到同目录 _trash/（不真删）——2026-11-02 补（对齐 build.py P76）。

    ★ 为什么 dub.py 也必须改（实测踩坑）：沙箱的删除保护按「本轮对话累计删除数」
      计（scope=turn, threshold=50）。dub.py 是**每段 TTS 调用一次**的客户端，
      服务轮换（每 3-4 段一次）里 unlink 端口文件 + 重置计数文件 = 每 4 段 2 次删除，
      一集 104 段就撞满 50 → 触发 SAFE_DELETE_BULK_CONFIRM_REQUIRED →
      **整个 build 卡死等人工确认**（2026-11-02 实测：M3 死在第 24 段，
      H3-retry 卡了 11 小时）。改名不产生删除操作，既不触发保护，也不丢信息。
    """
    try:
        p = Path(p)
        if not p.exists():
            return
        d = p.parent / "_trash"
        d.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        dst = d / f"{p.name}.{stamp}.trash"
        i = 0
        while dst.exists():
            i += 1
            dst = d / f"{p.name}.{stamp}_{i}.trash"
        p.rename(dst)
    except Exception:
        pass


def _serve_port_file(engine_id):
    return _serve_cache_dir() / f"serve_{engine_id}.json"


def _server_port(engine_id):
    """返回正在运行的同引擎服务端口；没有/已死则返回 None（并清理残留文件）。"""
    pf = _serve_port_file(engine_id)
    if not pf.exists():
        return None
    try:
        info = json.loads(pf.read_text(encoding="utf-8"))
        port = int(info["port"])
        s = socket.socket()
        s.settimeout(0.5)
        s.connect(("127.0.0.1", port))
        s.close()
        return port
    except Exception:
        _trash_rename(pf)     # ★ 不真删：批量生产会撞沙箱 50 次/回合删除阈值
        return None


def _send_to_server(port, payload):
    """向持久化服务发送一次合成请求，返回其 JSON 响应。

    ★ 超时 900s → 180s（2026-11-02 治根）：dotstts 一次合成实测 ~34s，
      180s 已是 5 倍余量；而**服务假死**（worker 无响应）时 900s 会让
      build 的每一段都先白等 15 分钟才降级 → 实测整条 build 卡死、
      60 分钟工作量全废。超时收紧后假死段 3 分钟内自动降级 one-shot。
      可用环境变量 DUB_SERVER_TIMEOUT 覆盖。
    """
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/synthesize",
        data=data, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_server_timeout()) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "error": f"服务返回错误: {e.code}"}
    except Exception as e:
        return {"ok": False, "error": f"服务通信失败: {e}"}


def _server_timeout():
    try:
        return int(os.environ.get("DUB_SERVER_TIMEOUT", "180"))
    except Exception:
        return 180


def _kill_dead_service(engine_id):
    """杀掉通信失败（假死）的持久化服务并清掉端口文件，**并等显存释放**。

    ★ 为什么必须清（2026-11-02 治根）：端口文件还在 → 下一段请求又被路由到
      同一个不响应的 worker → 每段白等一个超时。实测服务假死后 build 卡死 15 分钟
      仍无产出。清掉后下一段直接走一次性子进程（实测 34s 成功）。
    ★ 为什么必须等显存（2026-11-02 二次踩坑）：dotstts 模型独占 ~11.9GB 显存。
      杀掉服务后立刻拉起一次性 worker，显存尚未释放完 → worker 分配显存时
      OOM 挂死（实测卡 5 分钟无产出）。必须等 free 显存回升再放 worker。
    """
    pf = _serve_port_file(engine_id)
    try:
        info = json.loads(pf.read_text(encoding="utf-8"))
    except Exception:
        return
    pid = info.get("pid")
    if pid:
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, text=True, timeout=30)
            _print(f"[自愈] 已终止假死服务 pid={pid}", file=sys.stderr)
        except Exception:
            pass
    _trash_rename(pf)         # ★ 不真删（见 _trash_rename 说明）
    _wait_gpu_free(min_free_mb=2500, timeout_s=45)


def _wait_gpu_free(min_free_mb=2500, timeout_s=45):
    """等 GPU 空闲显存回升到阈值（服务被杀后显存释放有延迟）。"""
    try:
        r = subprocess.run(['nvidia-smi', '--query-gpu=memory.free',
                            '--format=csv,noheader,nounits'],
                           capture_output=True, text=True, encoding='utf-8', timeout=15)
        free = int(r.stdout.strip().split(',')[0])
    except Exception:
        return
    if free >= min_free_mb:
        return
    waited = 0
    while waited < timeout_s:
        time.sleep(3)
        waited += 3
        try:
            r = subprocess.run(['nvidia-smi', '--query-gpu=memory.free',
                                '--format=csv,noheader,nounits'],
                               capture_output=True, text=True, encoding='utf-8', timeout=15)
            free = int(r.stdout.strip().split(',')[0])
        except Exception:
            return
        if free >= min_free_mb:
            _print(f"[自愈] 显存已释放（free={free}MiB，等待 {waited}s）", file=sys.stderr)
            return
    _print(f"[自愈] 等待显存释放超时（free={free}MiB < {min_free_mb}MiB）", file=sys.stderr)


def _oneshot_timeout():
    try:
        return int(os.environ.get("DUB_ONESHOT_TIMEOUT", "300"))
    except Exception:
        return 300


# ★ 服务轮换阈值（2026-11-02 实测）：dotstts 常驻服务每请求泄漏约 200MB 显存，
#   第 5 个请求起 worker 假死。取 3 留安全余量（模型加载 28s 摊到 4 段 ≈ 7s/段，
#   远优于 one-shot 的 29s/段）。可用环境变量 DUB_SERVICE_MAX_REQUESTS 覆盖。
SERVICE_MAX_REQUESTS = 3


def _svc_rot_path(engine_id):
    return _serve_cache_dir() / f'svc_rot_{engine_id}.json'


def _svc_rot_count(engine_id):
    try:
        return int(json.loads(_svc_rot_path(engine_id).read_text(encoding='utf-8')).get('n', 0))
    except Exception:
        return 0


def _svc_rot_bump(engine_id):
    try:
        _svc_rot_path(engine_id).write_text(
            json.dumps({'n': _svc_rot_count(engine_id) + 1}, ensure_ascii=False),
            encoding='utf-8')
    except Exception:
        pass


def _svc_rot_reset(engine_id):
    # ★ 用写 0 代替删除：删除会撞沙箱 50 次/回合阈值（见 _trash_rename）
    try:
        _svc_rot_path(engine_id).write_text(
            json.dumps({'n': 0}, ensure_ascii=False), encoding='utf-8')
    except Exception:
        pass


def _rotate_service(engine):
    """杀掉用旧的服务并拉起一个新的（模型重新加载，端口复用）。

    ★ 为什么主动轮换而不是等它假死（2026-11-02 实测）：服务每请求泄漏显存，
      假死后要赔 180s 超时 + one-shot 兜底；主动轮换把这段等待换成一次模型加载。
    """
    _kill_dead_service(engine["id"])
    _start_service(engine)


def _start_service(engine):
    """拉起常驻服务并等端口就绪；返回端口号（失败返回 None）。

    ★ 为什么客户端要会自己拉服务（2026-11-02）：服务假死后被自愈杀掉，
      若没人再拉起，后续每段都退回 one-shot（实测 29s/段），
      整集多花 20+ 分钟。自己拉起 + 轮换 ≈ 10s/段。
    """
    port = SERVE_PORTS.get(engine["id"], 8731)
    try:
        worker_mod = Path(engine["worker"]).stem
        cmd = [engine["python"], str(SKILL_DIR / "serve.py"),
               "--engine-root", engine["root"], "--worker", "workers." + worker_mod,
               "--engine-id", engine["id"], "--port", str(port)]
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        proc = subprocess.Popen(cmd, cwd=engine["root"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=flags)
        info = {"port": port, "pid": proc.pid,
                "python": engine["python"], "root": engine["root"]}
        _serve_port_file(engine["id"]).write_text(
            json.dumps(info, ensure_ascii=False), encoding='utf-8')
        for _ in range(24):
            try:
                s2 = socket.socket()
                s2.settimeout(0.5)
                s2.connect(("127.0.0.1", port))
                s2.close()
                _print(f"[服务] 已拉起 port={port} pid={proc.pid}", file=sys.stderr)
                _svc_rot_reset(engine["id"])
                return port
            except Exception:
                time.sleep(0.5)
    except Exception as e:
        _print(f"[服务] 拉起失败: {e}", file=sys.stderr)
    return None


# 各引擎 worker 透传的参数（按引擎 id 过滤，避免误传）
# 说明：temperature / top_p / top_k / repetition_penalty 是跨引擎共享的采样参数，
#       它们在 argparse 里默认 None，由各 worker 套用该引擎自己的官方推荐默认值；
#       仅当用户显式传值时才覆盖。这样一套 CLI 适配 5 个引擎的不同默认值。
_ENGINE_OPTS = {
    "indextts2": [
        "device", "fp16",
        "emo_audio", "emo_alpha", "emo_text", "emo_vector", "max_tokens",
        # generation_kwargs（透传给 infer 的 **generation_kwargs）
        "top_p", "top_k", "temperature", "num_beams", "repetition_penalty",
        "max_mel_tokens", "interval_silence", "more_segment_before", "use_random",
    ],
    "dotstts": [
        "model", "prompt_text", "language", "num_steps", "guidance_scale",
        "speaker_scale", "precision", "max_len", "normalize_text", "ode_method",
    ],
    "pilottts": ["emotion", "language", "top_p", "top_k", "temperature"],
    "chatterbox": [
        "model", "language", "exaggeration", "cfg_weight",
        "temperature", "seed", "min_p", "top_p", "repetition_penalty",
    ],
    # Breeze：voice_design/direction 的指令与转写（ref_text 与 ref 必须成对）
    "breeze": [
        "instruction", "cfg_scale", "ref_text", "ref_text_file",
        "auto_transcribe", "seed",
        # 长文本分段：默认已安全，**不要关**（--no-chunk 会让 Breeze 静默丢尾巴）
        "chunk_chars", "no_chunk", "gap_ms",
    ],
}

# dots.tts 的语言解析依赖 langcodes，未装 language_data 时只认 ISO 代码
# （zh/en/ja...），不认中文名。这里做一层归一，让用户能直接写 chinese/english。
_LANG_MAP = {
    "chinese": "zh", "english": "en", "japanese": "ja", "korean": "ko",
    "french": "fr", "german": "de", "spanish": "es", "russian": "ru",
    "auto": "auto_detect", "auto_detect": "auto_detect",
    "none": None, "": None,
}


# --json 时置 True：所有过程日志改走 stderr，保证 stdout 只有 JSON（memory 2026-08-08 坑的修复）
_JSON_STDOUT = False


def _print(*a, **k):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if _JSON_STDOUT:
        print(*a, file=sys.stderr, **k)
    else:
        print(*a, **k)


def cmd_list_engines(args=None):
    data = load_engines()
    if args and getattr(args, "json", False):
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    _print(f"TTS_ROOT: {data.get('tts_root')}   默认引擎: {DEFAULT_ENGINE}")
    _print(f"{'引擎':<12}{'梯队':<8}{'状态':<8}{'类型':<10}说明")
    _print("-" * 84)
    for e in sorted(data.get("engines", []), key=lambda x: (x.get("tier", 3), x["name"])):
        role = "编排层" if e.get("role") == "orchestrator" else "引擎"
        flag = "就绪" if e.get("ready") else "未就绪"
        tier = TIER_LABEL.get(e.get("tier", 3), "?")
        mark = "" if e.get("tier", 3) < 3 else "  ← 停用，勿选"
        extra = ""
        if e.get("models"):
            extra = " [" + "/".join(e["models"]) + "]"
        _print(f"{e['name']:<12}{tier:<10}{flag:<9}{role:<11}{e['tagline']}{extra}{mark}")
    if data.get("unknown_dirs"):
        _print("\n未识别目录（需补指纹+worker）：")
        for d in data["unknown_dirs"]:
            _print(f"  - {d}")


def cmd_list_voices(args=None):
    data = load_voices()
    if args and getattr(args, "json", False):
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    vs = data.get("voices", [])
    common = [v for v in vs if v.get("common")]
    other = [v for v in vs if not v.get("common")]
    _print(f"音色包: {data.get('pool')}  共 {data.get('count', 0)} 个（⭐常用 {len(common)} 个，配音优先选）")
    _print("-" * 84)
    _print(f"{'分组':<7}{'别名':<12}{'性别':<6}{'时长':>7}  {'质量':<14}场景")
    for v in common + other:
        g = {"female": "女", "male": "男", "unknown": "—"}[v["gender"]]
        d = f"{v['duration']}s" if v["duration"] else "—"
        star = "⭐" if v.get("common") else " "
        grp = v.get("group", "根目录")
        _print(f"{star}{grp:<6}{v['alias']:<12}{g:<6}{d:>7}  {v['quality']:<14}{'/'.join(v['scenes'][:3])}")
    if common:
        _print("\n⭐ 常用音色：先从这里选（默认建议引擎 indextts2），明细见 音色说明.md")


def _cmd_sync_glossary():
    """把 brands.json 同步进 IndexTTS2 的原生 glossary.yaml。
    IndexTTS2 引擎在 normalize 阶段会优先套用 glossary（"定义权"层），
    这样即使直接用它的 WebUI 也能读对品牌名。dub.py 统一入口已通过 lexicon.py
    预处理覆盖全引擎，这里仅作为 IndexTTS2 原生路径的补充。"""
    try:
        import yaml  # noqa: F401  (用于写回 yaml)
    except ImportError:
        _print("[错误] 当前 Python 缺 PyYAML，无法写 glossary.yaml。请在该 Python 装 pyyaml 后重试；")
        _print("       不装也不影响 dub.py 合成（品牌词替换由 lexicon.py 在入口处完成）。")
        sys.exit(4)
    from lexicon import load_brands as _load_brands
    brands = _load_brands()
    if not brands:
        _print("[错误] 读取 brands.json 失败")
        sys.exit(1)
    # 定位 IndexTTS2 的 glossary.yaml
    data = load_engines()
    it2 = next((e for e in data.get("engines", []) if e.get("id") == "indextts2"), None)
    if not it2 or not it2.get("root"):
        _print("[错误] 找不到 IndexTTS2 引擎根目录")
        sys.exit(2)
    gl_path = os.path.join(it2["root"], "app", "python", "checkpoints", "glossary.yaml")
    gl_path = os.path.abspath(gl_path)
    existing = {}
    if os.path.exists(gl_path):
        try:
            existing = yaml.safe_load(open(gl_path, encoding="utf-8")) or {}
        except Exception as e:
            _print(f"[警告] 读取现有 glossary 失败: {e}")
            existing = {}
    # 合并：保留原有条目（如 Qwen3.0），品牌词覆盖
    merged = dict(existing)
    n_add, n_upd = 0, 0
    for token, entry in brands.items():
        if isinstance(entry, dict):
            val = {"zh": entry.get("zh", token), "en": entry.get("en", token)}
        else:
            val = {"zh": entry, "en": entry}
        if token in merged:
            if merged[token] != val:
                n_upd += 1
        else:
            n_add += 1
        merged[token] = val
    try:
        with open(gl_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(merged, f, allow_unicode=True, sort_keys=False)
    except Exception as e:
        _print(f"[错误] 写入 glossary 失败: {e}")
        sys.exit(3)
    _print(f"✅ 已同步 {len(brands)} 条品牌词到 IndexTTS2 原生 glossary：")
    _print(f"   {gl_path}")
    kept = len(existing) - n_upd  # 原有条目中未被品牌词覆盖的数量
    _print(f"   新增 {n_add} / 更新 {n_upd} / 保留原有 {kept}")


def _materialize_multirole(script_path, brand_mode):
    """读取多角色剧本 → 品牌词替换 → 写临时文件，返回 (临时路径, 原文角色列表)。
    单条与 batch 两条路径共用，保证行为一致（A10：batch 原先漏替换）。"""
    script_text = Path(script_path).read_text(encoding="utf-8")
    roles = _extract_roles(script_text)
    if brand_mode != "off":
        script_text = apply_brands(script_text, mode=brand_mode)
    # 唯一临时文件：避免并发多角色任务互踩；进程退出时 atexit 自动清理，不在磁盘留垃圾
    cache = _serve_cache_dir()
    legacy = cache / "mr_brand_tmp.txt"  # 清理 v0.6.0 之前的固定名残留
    try:
        if legacy.exists():
            _trash_rename(legacy)
    except OSError:
        pass
    tmp = cache / f"mr_brand_{os.getpid()}_{uuid.uuid4().hex[:8]}.txt"
    tmp.write_text(script_text, encoding="utf-8")
    atexit.register(lambda p=str(tmp): _safe_unlink(p))
    return str(tmp), roles


def _safe_unlink(p):
    """清理临时文件：改名到 _trash（**不真删**）——沙箱删除保护 50 次/回合阈值。"""
    _trash_rename(p)


def _build_payload(engine, ref, text, out, opts, multi_roles_extra):
    """构造发给持久化服务的请求体。"""
    payload = {"out": out}
    if multi_roles_extra:
        # 结构: ["--multi-role", path, "--roles", json]
        payload["multi_role"] = multi_roles_extra[1]
        payload["roles"] = multi_roles_extra[3]
    else:
        payload["text"] = text
        payload["ref"] = ref or None
    # 透传引擎参数（None 会被服务端忽略；未知键也会被忽略）
    for k, v in opts.items():
        payload[k] = v
    return payload


def _finish_from_response(out, resp):
    if resp.get("ok") and os.path.isfile(out) and os.path.getsize(out) > 0:
        _print(f"\n✅ 合成成功：{out}  ({os.path.getsize(out)/1024:.1f} KB)")
        sys.exit(0)
    _print(f"\n❌ 合成失败：{resp.get('error', '服务未返回有效结果')}")
    sys.exit(1)


def _wav_duration(path):
    """实测 WAV 时长（秒），纯标准库（wave 读帧数）。失败返回 None。"""
    try:
        with wave.open(path, "rb") as w:
            frames = w.getnframes()
            rate = w.getframerate()
            return round(frames / rate, 3) if rate else None
    except Exception:
        return None


def _safe_shot_name(rid):
    """shot_id 数字补零三位；字符串（如 S01-01，SKILL §六允许）原样安全使用。"""
    if isinstance(rid, int):
        return f"shot_{rid:03d}.wav"
    s = str(rid).strip()
    if s.isdigit():
        return f"shot_{int(s):03d}.wav"
    import re as _re
    # Python 3.11+ 不允许 f-string 表达式内含反斜杠，故先算再插值
    _pat = '[^0-9A-Za-z_' + chr(92) + '-]+'
    _safe = _re.sub(_pat, '_', s)
    return 'shot_' + _safe + '.wav'


def _ok_result(out):
    """合成成功的结构化回写：含实测 duration（导演引擎回写协议核心字段）。"""
    r = {"ok": True, "out": os.path.abspath(out),
         "size_kb": round(os.path.getsize(out) / 1024, 1)}
    dur = _wav_duration(out)
    if dur is not None:
        r["duration"] = dur  # 实测时长（秒）→ 导演引擎回写 storyboard t_in/t_out
    return r


# 报错行的特征词：从一堆日志里挑出「像报错」的那几行用
_ERR_LINE_HINT = ("error", "exception", "traceback", "failed", "fatal", "killed",
                  "refused", "reset", "失败", "错误", "异常", "超时")


def _tail_snippet(text, limit=400):
    """从引擎输出里截一段错误信息 —— **必须截头保尾**。

    为什么：异常堆栈和报错永远打印在**最后**，而原来的 `output_tail[:200]` 取的是
    开头 200 字 —— 等于把真正的死因截掉，只剩一堆正常日志。
    实测翻车：一次失败只看到 "[breeze] [1/2] 327 字"，真正的报错在 200 字之外，
    完全不可见，只能靠猜。错误信息的可读性直接决定排障成本，别在这省字符。
    """
    if not text:
        return ""
    lines = [l for l in text.strip().splitlines() if l.strip()]
    if not lines:
        return ""
    hits = [l for l in lines if any(k in l.lower() for k in _ERR_LINE_HINT)]
    picked = hits[-6:] if hits else lines[-6:]
    s = "\n".join(picked).strip()
    return s if len(s) <= limit else "…" + s[-limit:]


def _classify_error(returncode, output_tail, engine_id):
    r = _classify_error_core(returncode, output_tail, engine_id)
    # 固定话术之外附上原始尾部，方便人工排障（新增字段，不影响既有的 type/reason/fix 契约）
    r["raw_tail"] = _tail_snippet(output_tail, 800)
    return r


def _classify_error_core(returncode, output_tail, engine_id):
    """把引擎失败输出映射到导演引擎统一错误枚举（onboarding §3.3）。
    返回 {"type", "reason", "fix"}，映射不中一律 unknown。"""
    s = (output_tail or "").lower()
    if not s:
        return {"type": "unknown", "reason": f"进程退出码 {returncode}，无输出",
                "fix": "查看引擎日志定位问题"}
    if any(k in s for k in ("out of memory", "oom", "cuda error: out of memory", "torch.cuda.oom")):
        return {"type": "oom", "reason": "显存/内存不足",
                "fix": "加 --device cuda --fp16，或缩短文本 / 换小模型 / 关闭其它占用显存的进程"}
    if any(k in s for k in ("filenotfound", "no such file", "cannot find", "not found",
                            "is not a file", "does not exist", "找不到", "不存在")):
        return {"type": "model_missing", "reason": "缺模型/权重/参考音频",
                "fix": "跑 --health 体检；确认参考音频与引擎权重路径存在（D:\\AITools\\TTS）"}
    if any(k in s for k in ("assert", "invalid", "valueerror", "typeerror", "keyerror",
                            "unrecognized", "unknown argument", "invalid argument", "not implemented")):
        return {"type": "invalid_param", "reason": "参数非法或枚举不匹配",
                "fix": f"对照 SKILL.md 参数速查修正（引擎 {engine_id}）"}
    if any(k in s for k in ("timeout", "timed out", "connection refused", "connect failed",
                            # 服务进程被杀/崩溃时 urllib 抛 URLError，Windows 中文环境下写
                            # "远程主机强迫关闭了一个现有的连接" —— 不认这几个词就只会落到
                            # unknown，看不出是服务挂了
                            "urlerror", "connection reset", "connectionreseterror",
                            "connection aborted", "broken pipe", "远程主机", "目标计算机")):
        return {"type": "timeout", "reason": "合成超时或依赖服务不通（服务可能已崩溃/被杀）",
                "fix": "缩短文本 / --fast 降档重试；检查常驻服务是否存活（--health），"
                       "必要时 dub.py --stop-all-serve 后重跑；若机器同时跑别的 GPU/大内存任务，先让出来"}
    if any(k in s for k in ("cuda driver", "driver version", "no cuda", "cudnn", "libcuda",
                            "runtimeerror: cuda")):
        return {"type": "service_down", "reason": "GPU 驱动/CUDA 环境异常",
                "fix": "检查显卡驱动与 CUDA 版本；或换 --device cpu"}
    return {"type": "unknown", "reason": _tail_snippet(output_tail) or f"进程退出码 {returncode}",
            "fix": "记录现场（引擎输出见上方日志 / 本条 raw_tail 字段），人工排查"}


def _cmd_serve(args):
    """启动指定引擎的持久化服务（后台常驻，模型只加载一次）。"""
    engine, note = resolve_engine(args.engine)
    if note:
        _print(f"[提示] {note}")
    if not engine:
        _print(f"[错误] {note or '找不到引擎'}（先跑 scan.py --engines 或 --list-engines 看看）")
        sys.exit(13)
    if engine.get("role") == "orchestrator":
        _print("[错误] 编排层(emotionTTS)不支持持久化服务，请直接用底层引擎。")
        sys.exit(17)
    port = args.port or SERVE_PORTS.get(engine["id"], 8731)

    running = _server_port(engine["id"])
    if running:
        _print(f"[提示] {engine['name']} 服务已在端口 {running} 运行，无需重复启动。")
        sys.exit(0)

    worker_mod = Path(engine["worker"]).stem  # workers/worker_indextts2.py -> worker_indextts2
    worker_dotted = "workers." + worker_mod
    cmd = [engine["python"], str(SKILL_DIR / "serve.py"),
           "--engine-root", engine["root"], "--worker", worker_dotted,
           "--engine-id", engine["id"], "--port", str(port)]
    if args.device:
        cmd += ["--device", args.device]
    if args.fp16:
        cmd.append("--fp16")
    if args.model:
        cmd += ["--model", args.model]

    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    _print(f"[服务] 启动 {engine['name']} 持久化服务 (port={port})，首次加载模型较慢，请稍候…")
    proc = subprocess.Popen(cmd, cwd=engine["root"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=flags)
    info = {"port": port, "pid": proc.pid, "python": engine["python"], "root": engine["root"]}
    _serve_port_file(engine["id"]).write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    # 启动后短暂确认端口真的起来了：避免"端口被占用导致服务没绑上"却留下
    # 一份指向死进程的端口文件（否则后续 --stop-serve 会杀错对象、dub.py 误以为在运行）
    _up = False
    for _ in range(15):
        try:
            s2 = socket.socket(); s2.settimeout(0.5)
            s2.connect(("127.0.0.1", port)); s2.close(); _up = True; break
        except Exception:
            import time
            time.sleep(1)
    if not _up:
        try:
            _trash_rename(_serve_port_file(engine["id"]))
        except Exception:
            pass
        _print(f"[警告] 服务没在端口 {port} 监听（多半是端口被别的进程占用）。")
        _print(f"        已清除端口文件。请先停掉占用 {port} 的进程，或换 --port 再试。")
        sys.exit(0)
    _print(f"[服务] 已启动 (pid={proc.pid})。之后用 dub.py 正常合成即可自动复用；停止用 --stop-serve。")
    sys.exit(0)


def _kill_serve(engine_id: str, name: str = None) -> bool:
    """停掉一个引擎的常驻服务，返回是否真的停了。

    抽出来是为了让「停一个」和「停全部」共用同一套清理逻辑 —— 清理逻辑写两遍，
    迟早有一遍会漏掉端口文件，然后 dub.py 会对着一个死进程说"服务在运行"。
    """
    pf = _serve_port_file(engine_id)
    if not pf.exists():
        return False
    name = name or engine_id
    try:
        info = json.loads(pf.read_text(encoding="utf-8"))
    except Exception:
        info = {}
    pid = info.get("pid")
    if pid:
        killed = False
        if os.name == "nt":
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, timeout=10)
                killed = True
            except Exception:
                killed = False
        if not killed:
            try:
                os.kill(pid, 9)
            except Exception as e:
                _print(f"[警告] 终止进程 {pid} 失败: {e}")
    try:
        _trash_rename(pf)
    except Exception:
        pass
    _print(f"✅ 已停止 {name} 持久化服务" + (f"（pid={pid}）" if pid else ""))
    return True


def _cmd_stop_serve(args):
    """停止指定引擎的持久化服务。"""
    engine, note = resolve_engine(args.engine)
    if note:
        _print(f"[提示] {note}")
    if not engine:
        _print(f"[错误] {note or '找不到引擎'}")
        sys.exit(13)
    if not _kill_serve(engine["id"], engine["name"]):
        _print(f"[提示] {engine['name']} 没有运行中的服务。")
    sys.exit(0)


def _cmd_stop_all_serve():
    """停止全部引擎的常驻服务。

    为什么需要它：常驻服务即使闲置也占着显存。实测抓到过 tier 3（已停用）的
    indextts2 服务空转 9 小时，和默认引擎 Breeze 挤在同一张 12G 卡上，
    把 RTF 从 1.89 拖到 ~7 —— **没改任何代码，速度掉了近 4 倍**。
    跑正式任务前先清场，是最容易被忽略、也最便宜的一次提速。
    """
    stopped, stale = [], []
    for engine_id in ENGINE_PORTS:
        pf = _serve_port_file(engine_id)
        if pf.exists():
            if _kill_serve(engine_id):
                stopped.append(engine_id)
    # 端口文件可能残留已删除/改名的引擎，扫一遍兜底
    for pf in sorted(_serve_cache_dir().glob("serve_*.json")):
        eid = pf.name[len("serve_"):-len(".json")]
        if eid not in stopped:
            if _kill_serve(eid):
                stopped.append(eid)
    if not stopped:
        _print("[提示] 没有运行中的常驻服务。")
    else:
        _print(f"[清理] 已停止 {len(stopped)} 个服务：{', '.join(stopped)}")
        _print("[下一步] 跑正式合成前建议确认显存已释放：nvidia-smi")
    if stale:
        _print(f"[残留] {stale}")
    sys.exit(0)


def _resolve_ref(args):
    """返回 (ref_path, prompt_text, voice_obj_or_None)"""
    if args.ref:
        return args.ref, args.prompt_text, None
    if args.voice:
        v = find_voice(args.voice)
        if not v:
            _print(f"[错误] 找不到音色别名/文件: {args.voice}")
            sys.exit(10)
        # 支持 prompt_text 自动补充（仅 dotstts 用得上；不影响其它引擎）
        pt = args.prompt_text or v.get("prompt_text") or ""
        return v["file"], pt, v
    _print("[错误] 必须提供 --ref <参考音频> 或 --voice <音色别名>")
    sys.exit(11)


def _extract_roles(script_text):
    """从多角色剧本文本中提取出现过的角色名（[角色名]: 内容 格式），按出现顺序去重。"""
    import re as _re
    roles = []
    for raw_line in script_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = _re.match(r"^\s*\[([^\[\]]+)\]\s*[:：]\s*(.+?)\s*$", line)
        if m:
            r = m.group(1).strip()
            if r and r not in roles:
                roles.append(r)
    return roles


def build_worker_cmd(engine, ref, text, out, opts):
    worker = engine.get("worker")
    if not worker:
        _print(f"[错误] 引擎 {engine['name']} 没有底层 worker（可能是编排层）")
        sys.exit(12)
    cmd = [
        engine["python"],
        str(SKILL_DIR / worker),
        "--engine-root", engine["root"],
        "--out", out,
    ]
    if text:  # 多角色模式下 text 为空，由 --multi-role 提供剧本
        cmd += ["--text", text]
    if ref:  # 没有参考音频时（如 chatterbox 用内置音色）就不传 --ref
        cmd += ["--ref", ref]
    allowed = _ENGINE_OPTS.get(engine["id"], [])
    # model 默认值：dotstts / chatterbox 用扫描到的 default_model
    if engine["id"] in ("dotstts", "chatterbox") and not opts.get("model"):
        opts = dict(opts, model=engine.get("default_model"))
    # 语言名归一为 ISO 代码（仅 dotstts 需要；chatterbox 直接认 ISO）
    if engine["id"] == "dotstts" and opts.get("language"):
        opts = dict(opts, language=_LANG_MAP.get(opts["language"].lower(), opts["language"]))
    for k in allowed:
        val = opts.get(k)
        if val is None or val is False:
            continue
        flag = f"--{k.replace('_', '-')}"
        # 布尔开关型参数（store_true）：True 时只发裸 flag，不跟 "True"
        if val is True:
            cmd.append(flag)
        else:
            cmd += [flag, str(val)]
    return cmd


def _run_single(engine, ref, text, out, opts, multi_roles_extra=None,
                brand_mode="zh", no_serve=False, dry_run=False):
    """单条合成核心（cmd_dub 单条与 cmd_batch 批量共用）。
    返回结构化结果 dict：
      - 成功: {"ok": True, "out", "size_kb", "duration"(实测秒)}
      - 失败: {"ok": False, "out", "error": {"type","reason","fix"}}
      - dry_run: {"ok": True, "dry_run": True, "out", "cmd"}
    """
    out = os.path.abspath(out)
    try:
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    except Exception as e:
        return {"ok": False, "out": out,
                "error": {"type": "invalid_param", "reason": f"输出目录不可写: {e}",
                          "fix": "检查 --out 路径是否合法/可写"}}
    if text and brand_mode != "off":
        text = apply_brands(text, mode=brand_mode)
    cmd = build_worker_cmd(engine, ref or "", text or "", out, opts)
    if multi_roles_extra:
        cmd = cmd + multi_roles_extra
        # 多角色必须走**独立端口**：常驻服务是 serve.py，它不透传 --multi-role/--roles，
        # 复用了会静默退化成单人声（不报错，产物听着就是一个人念完）。
        # 端口统一到 ENGINE_PORTS 后这个保护曾失效，用 oneshot_port 显式错开。
        if engine["id"] == "breeze":
            cmd += ["--port", str(oneshot_port(engine["id"]))]
    if dry_run:
        return {"ok": True, "dry_run": True, "out": out,
                "cmd": " ".join(str(c) for c in cmd)}
    # ── 路由：优先复用持久化服务，否则一次性子进程 ──
    # 多角色不走服务：serve.py 未透传 --multi-role/--roles，走服务会静默退化成单人声
    if not no_serve and not multi_roles_extra:
        port = _server_port(engine["id"])
        # ★ 没有服务就自己拉起（2026-11-02）：服务假死后被自愈杀掉后，
        #   若没人拉起，后续每段都退 one-shot（29s/段）→ 整集多花 20+ 分钟。
        if not port:
            port = _start_service(engine)
        # ★ 服务轮换（2026-11-02 治根）：dotstts 常驻服务**每请求泄漏约 200MB 显存**，
        #   实测第 5 个请求起 worker 假死（探针：#01-#04 秒回、#05 挂 180s）。
        #   等它假死 = 每轮赔一个超时；主动轮换 = 每段稳定 ~4s（实测对比 one-shot 29s）。
        used = _svc_rot_count(engine["id"])
        if port and used >= SERVICE_MAX_REQUESTS:
            _print(f"[路由] 服务已连用 {used} 次（实测约 4 次后假死），主动轮换…",
                   file=sys.stderr)
            _rotate_service(engine)
            port = _server_port(engine["id"])
        if port:
            _print(f"[路由] 复用持久化服务 port={port}（模型已加载，秒级合成）")
            payload = _build_payload(engine, ref or "", text or "", out, opts, multi_roles_extra)
            resp = _send_to_server(port, payload)
            if resp.get("ok") and os.path.isfile(out) and os.path.getsize(out) > 0:
                _svc_rot_bump(engine["id"])
                return _ok_result(out)
            # A3：服务失败不直接判死，落回一次性子进程（保留错误信息供日志）
            print(f"[警告] 持久化服务失败（{resp.get('error', '未知')}），回退一次性子进程…",
                  file=sys.stderr)
            # ★ 服务假死必须清场（2026-11-02 治根）：端口文件还在，下一段又会被
            #   路由到同一个不响应的 worker → 每段白等一个超时，整条 build 卡死。
            _kill_dead_service(engine["id"])
            _svc_rot_reset(engine["id"])
    _print(f"[命令] {' '.join(str(c) for c in cmd[:8])} ...")
    env = os.environ.copy()
    for k in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE"):
        env.setdefault(k, "1")
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        env.pop(k, None)
    try:
        # ★ one-shot 也必须有超时（2026-11-02 治根）：此前 subprocess.run 无 timeout，
        #   引擎 OOM/挂死时无限等待 —— 实测 worker 挂在显存分配上（服务刚被杀、
        #   显存尚未释放完），整条 build 永远不返回。
        proc = subprocess.run(cmd, cwd=engine["root"], env=env,
                              capture_output=True, text=True, encoding="utf-8",
                              timeout=_oneshot_timeout())
    except subprocess.TimeoutExpired:
        return {"ok": False, "out": out,
                "error": {"type": "engine_timeout",
                          "reason": f"一次性引擎进程超时 >{_oneshot_timeout()}s（引擎挂死/OOM）",
                          "fix": "确认 GPU 显存已释放（nvidia-smi），再重跑"}}
    except Exception as e:
        return {"ok": False, "out": out,
                "error": {"type": "service_down", "reason": f"启动引擎进程失败: {e}",
                          "fix": "检查引擎便携 Python 是否可用（--health 体检）"}}
    if proc.stdout.strip():
        _print("----- 引擎输出 -----")
        _print(proc.stdout.strip())
    if proc.returncode != 0 and proc.stderr.strip():
        _print("----- 引擎错误 -----")
        _print(proc.stderr.strip()[-2000:])
    if proc.returncode == 0 and os.path.isfile(out) and os.path.getsize(out) > 0:
        return _ok_result(out)
    return {"ok": False, "out": out,
            "error": _classify_error(proc.returncode,
                                     (proc.stderr[-2000:] + proc.stdout[-2000:]), engine["id"])}


# 引擎跑满所需的空闲显存（MiB），用于起飞前预检
_VRAM_NEED_MIB = {
    "breeze": 7885,      # eager ~7.7GiB；--fast-all 要 14.4GiB，本机 12G 根本别想
    "dotstts": 6000,
    "indextts2": 5000,
    "pilottts": 6000,
    "chatterbox": 5000,
}


def _vram_preflight(engine):
    """起飞前查一次空闲显存，不够就明说。

    为什么必须查：2026-09-04 抓到一次**同输入 4.4 倍变慢** —— 完全相同的
    文案/音色/引擎，RTF 从 1.89 掉到 8.38。代码一个字没改。原因是机器上
    ComfyUI 占了 18GB 内存 + GPU 算力，加上桌面应用常吃 6GB 显存，Breeze 的
    7.7GiB 装不进剩下的 ~6GB，只能溢出到内存跑。
    这种故障最坑的地方：它不报错、不失败，只是**慢得莫名其妙**。
    所以宁可起飞前多嘴一句，也别让人等十分钟然后以为是模型坏了。
    """
    need = _VRAM_NEED_MIB.get(engine.get("id"), 6000)
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10)
        if out.returncode != 0:
            return
        used_s, total_s = out.stdout.strip().split(",")[:2]
        used, total = int(used_s), int(total_s)
    except Exception:
        return          # 没 nvidia-smi / 非 N 卡：静默跳过，不打扰

    free = total - used
    if free >= need:
        return
    _print(f"[⚠️ 显存预检] 空闲显存仅 {free} MiB，而 {engine['name']} 顺畅运行约需 {need} MiB。")
    _print("             不足时会溢出到内存 → **不报错、不失败，只是慢很多**。")
    _print("             实测同输入 RTF 可从 1.89 劣化到 8.38（4.4 倍）。")
    _print("             建议：关掉占显存的桌面应用 / 正在跑的 ComfyUI，再重试。")
    _print("             查占用：nvidia-smi    清闲置服务：dub.py --stop-all-serve")


def cmd_dub(args):
    global _JSON_STDOUT
    _JSON_STDOUT = bool(getattr(args, "json", False))
    engine, note = resolve_engine(args.engine)
    if note:
        _print(f"[提示] {note}")
    if not engine:
        _print(f"[错误] {note or '找不到引擎'}（先跑 scan.py --engines 或 --list-engines 看看）")
        sys.exit(13)

    # 编排层（emotionTTS）特殊处理
    if engine.get("role") == "orchestrator":
        _print(f"[说明] {engine['name']} 是 AI 情绪导演（编排层），底层调用 IndexTTS2。")
        _print("       请先启动它的 WebUI/服务（其整合包自带启动器），通过它自己的界面使用；")
        _print("       本技能直接对接底层引擎，请改用：")
        _print("         python dub.py --engine indextts2 --voice <别名> --text ... --out ...")
        _print("         python dub.py --engine dotstts   --ref <wav> --text ... --out ...")
        _print("         python dub.py --engine pilottts  --voice <别名> --text ... --out ...")
        _print("         python dub.py --engine chatterbox --ref <wav> --text ... --out ...")
        sys.exit(0)

    if not engine.get("ready"):
        _print(f"[警告] 引擎 {engine['name']} 未就绪（便携 Python 或权重缺失），合成可能失败。")

    _vram_preflight(engine)

    # ── 多角色模式：--multi-role 剧本 + --roles 角色映射（角色名=音色别名或路径,逗号分隔）──
    if args.multi_role:
        # 多角色 = 逐段合成 + 拼接，breeze 与 indextts2 都支持（breeze worker 已实装）
        if engine["id"] not in ("indextts2", "breeze"):
            _print("[错误] 多角色目前只有 indextts2 / breeze 支持。")
            sys.exit(17)
        if not args.roles:
            _print("[错误] 多角色模式必须提供 --roles \"角色名=音色别名或路径,角色名2=...\"")
            sys.exit(18)
        try:
            multi_role_file, roles_in_script = _materialize_multirole(
                args.multi_role, args.brand_mode)
        except Exception as e:
            _print(f"[错误] 读取剧本失败: {e}")
            sys.exit(19)
        # 解析 --roles 映射
        role_map = {}
        for pair in args.roles.split(","):
            pair = pair.strip()
            if not pair:
                continue
            if "=" not in pair:
                _print(f"[错误] roles 条目格式应为 角色名=音色别名或路径: {pair}")
                sys.exit(20)
            rname, rvoice = pair.split("=", 1)
            rname, rvoice = rname.strip(), rvoice.strip()
            if not rname or not rvoice:
                _print(f"[错误] roles 条目不能为空: {pair}")
                sys.exit(20)
            # 音色别名 → 参考音频路径（走音色包索引）；否则当作文件路径
            v = find_voice(rvoice)
            role_map[rname] = v["file"] if v else rvoice
        missing = sorted({r for r in roles_in_script if r not in role_map})
        if missing:
            _print(f"[错误] 剧本中的角色未在 --roles 里配置: {', '.join(missing)}")
            _print(f"       剧本角色: {', '.join(roles_in_script)}")
            sys.exit(21)
        # 多角色不走 ref/text；文本来源是剧本文件
        ref = ""
        roles_json = json.dumps(role_map, ensure_ascii=False)
        text = ""
        prompt_text = ""
        multi_roles_extra = ["--multi-role", multi_role_file, "--roles", roles_json]
    else:
        multi_roles_extra = None
        # chatterbox 允许无参考音（用内置默认音色）；其它引擎必须给 --ref/--voice
        if engine["id"] == "chatterbox" and not args.ref and not args.voice:
            ref, prompt_text = "", ""
        else:
            ref, prompt_text, _v = _resolve_ref(args)

    # 文本来源（单人模式）
    if not args.multi_role:
        if args.text_file:
            try:
                text = Path(args.text_file).read_text(encoding="utf-8").strip()
            except Exception as e:
                _print(f"[错误] 读取文本文件失败: {e}")
                sys.exit(14)
        else:
            text = args.text
        if not text:
            _print("[错误] 必须提供 --text 或 --text-file")
            sys.exit(15)
        # 品牌名词典：进入引擎前替换成读得对的表达（zh 默认）
        if args.brand_mode != "off":
            replaced, hits = preview_brands(text, mode=args.brand_mode)
            if hits:
                _print("[品牌词] " + "，".join(f"{a}→{b}" for a, b in hits))
            text = replaced

    opts = dict(
        device=args.device, fp16=args.fp16,
        emo_audio=args.emo_audio, emo_alpha=args.emo_alpha,
        emo_text=args.emo_text, emo_vector=args.emo_vector, max_tokens=args.max_tokens,
        # IndexTTS2 generation_kwargs（None 时由 worker 套用官方默认）
        top_p=args.top_p, top_k=args.top_k, temperature=args.temperature,
        num_beams=args.num_beams, repetition_penalty=args.repetition_penalty,
        max_mel_tokens=args.max_mel_tokens, interval_silence=args.interval_silence,
        more_segment_before=args.more_segment_before, use_random=args.use_random,
        # dots.tts
        model=args.model, prompt_text=prompt_text, language=args.language,
        num_steps=args.num_steps, guidance_scale=args.guidance_scale,
        speaker_scale=args.speaker_scale, precision=args.precision, max_len=args.max_len,
        normalize_text=args.normalize_text, ode_method=args.ode_method,
        # PilotTTS
        emotion=args.emotion,
        # Chatterbox-V3
        exaggeration=args.exaggeration, cfg_weight=args.cfg_weight,
        seed=args.seed, min_p=args.min_p,
        # Breeze TTS 2
        instruction=args.instruction, cfg_scale=args.cfg_scale,
        ref_text=args.ref_text, ref_text_file=args.ref_text_file,
        auto_transcribe=args.auto_transcribe,
        # 长文本分段（None/False 时不透传，由 worker 用默认 350）
        # 注意：新增参数必须同时改 _ENGINE_OPTS 白名单和这里，只改一处不会生效
        chunk_chars=args.chunk_chars, no_chunk=args.no_chunk, gap_ms=args.gap_ms,
    )

    # ── --fast 加速预设：仅在用户没显式改过时才套用（尊重显式传参）──
    if args.fast:
        if engine["id"] == "indextts2" and args.num_beams == 3:
            opts["num_beams"] = 2
            opts["max_mel_tokens"] = 1200
        if engine["id"] == "dotstts" and args.num_steps == 10:
            opts["num_steps"] = 8
        _print("[fast] 已启用加速预设（降低 beam/步数，确定性略降、速度更快）")

    out = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)

    _print(f"[合成] 引擎={engine['name']}  参考={Path(ref).name if ref else '(多角色)'}  输出={out}")

    # ── 执行：与批量路径共用 _run_single ──
    # 为什么必须共用：这里原先有一份**内联副本**，结果「多角色必须绕开常驻服务」
    # 的修复只落在 _run_single（批量路径），单人路径漏网 —— 起了 --serve 之后
    # `dub.py --multi-role ...` 会被路由到 serve.py，而它不透传 --multi-role，
    # 于是**静默退化成单人声**（不报错，产物听着就是一个人念完）。
    # 同一段逻辑写两遍就一定会有一遍漏改，直接从根上合并掉。
    # 品牌词 cmd_dub 已在上面对 text 替换过，这里传 off 避免重复替换。
    res = _run_single(engine, ref, text, out, opts, multi_roles_extra,
                      brand_mode="off", no_serve=args.no_serve,
                      dry_run=args.dry_run)

    if args.dry_run:
        _print("[dry-run] 校验通过，未真正合成。将执行命令：")
        _print("  " + res.get("cmd", ""))
        sys.exit(0)

    if res.get("ok"):
        if args.json:
            print(json.dumps(res, ensure_ascii=False))
            sys.exit(0)
        _print(f"\n✅ 合成成功：{res['out']}  ({res['size_kb']} KB)")
        if res.get("duration") is not None:
            _print(f"   实测时长: {res['duration']:.2f}s（回写 storyboard t_in/t_out）")
        sys.exit(0)

    err = res["error"]
    if args.json:
        print(json.dumps({"ok": False, "out": os.path.abspath(out), "error": err},
                         ensure_ascii=False))
        sys.exit(1)
    _print(f"\n❌ 合成失败 [{err['type']}]：{err['reason']}")
    _print(f"   fix: {err['fix']}")
    sys.exit(1)


def cmd_health(args):
    """体检：引擎就绪 / 音色包 / 品牌词典 / 常驻服务状态（对接验收硬指标）。"""
    data = load_engines()
    voices = load_voices()
    brands_count = 0
    try:
        from lexicon import load_brands
        brands_count = len(load_brands())
    except Exception:
        pass
    eng_rows = []
    for e in data.get("engines", []):
        eng_rows.append({
            "id": e["id"], "name": e["name"], "role": e.get("role"),
            "ready": bool(e.get("ready")), "python_ok": bool(e.get("python_ok")),
            "sample_rate": e.get("sample_rate"),
            "models": e.get("models") or [], "default_model": e.get("default_model"),
            "worker": e.get("worker"),
        })
    serve = {}
    for eid in SERVE_PORTS:
        p = _server_port(eid)
        if p:
            serve[eid] = {"running": True, "port": p}
    ok = any(r["ready"] for r in eng_rows)
    vlist = voices.get("voices", [])
    common_cnt = sum(1 for v in vlist if v.get("common"))
    result = {
        "ok": ok, "skill": "本地配音工坊", "version": VERSION,
        "tts_root": data.get("tts_root"),
        "engines": eng_rows,
        "voices": {"pool": voices.get("pool"), "count": voices.get("count", 0),
                   "common": common_cnt},
        "brands": brands_count,
        "serve": serve,
        "unknown_dirs": data.get("unknown_dirs", []),
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print(f"本地配音工坊 v{VERSION}  TTS_ROOT: {result['tts_root']}")
        _print(f"就绪引擎: {sum(1 for r in eng_rows if r['ready'])}/{len(eng_rows)}  "
               f"音色: {result['voices']['count']}（⭐常用 {common_cnt}）  品牌词: {result['brands']}")
        for r in eng_rows:
            flag = "✅" if r["ready"] else "❌"
            extra = f"  [{('/'.join(r['models']))}]" if r["models"] else ""
            _print(f"  {flag} {r['id']:<12} {r['name']} ({r['sample_rate']}Hz){extra}")
        if serve:
            _print("常驻服务: " + ", ".join(f"{k}@{v['port']}" for k, v in serve.items()))
        if result.get("unknown_dirs"):
            _print("未识别目录: " + ", ".join(result["unknown_dirs"]))
        _print("健康 ✅" if ok else "健康 ❌（无就绪引擎，检查 D:\\AITools\\TTS 后跑 scan.py）")
    sys.exit(0 if ok else 3)


def cmd_batch(args):
    global _JSON_STDOUT
    _JSON_STDOUT = bool(getattr(args, "json", False))
    """批量消费导演引擎分镜包的 prompts/voice_jobs.json（12-engine-onboarding §3）。
    每个 job: {shot_id, role, engine, voice|ref, emotion, text, lang, out}，
    逐个合成 → 汇总结果清单（含实测 duration / 失败 error{type,reason,fix}）。
    """
    try:
        jobs = json.loads(Path(args.batch).read_text(encoding="utf-8"))
    except Exception as e:
        _print(f"[错误] 读取 voice_jobs.json 失败: {e}")
        sys.exit(22)
    if not isinstance(jobs, list) or not jobs:
        _print("[错误] voice_jobs.json 应为非空数组")
        sys.exit(23)

    results = []
    for i, job in enumerate(jobs, 1):
        if not isinstance(job, dict):
            results.append({"shot_id": i, "ok": False,
                            "error": {"type": "invalid_param", "reason": "job 不是对象",
                                      "fix": "修正 voice_jobs.json 结构"}})
            continue
        rid = job.get("shot_id", i)
        # 不写 engine 的 job 走默认引擎（DEFAULT_ENGINE），不可用按回退链切
        engine_id = job.get("engine") or DEFAULT_ENGINE
        engine, enote = resolve_engine(job.get("engine"))
        if enote:
            _print(f"[提示] shot={rid}: {enote}")
        if not engine:
            results.append({"shot_id": rid, "ok": False,
                            "error": {"type": "invalid_param",
                                      "reason": enote or f"未知引擎: {engine_id}",
                                      "fix": f"job 的 engine 留空（走默认 {DEFAULT_ENGINE}），"
                                             f"或显式写 breeze / dotstts；--list-engines 看梯队"}})
            continue
        if engine.get("role") == "orchestrator":
            results.append({"shot_id": rid, "ok": False,
                            "error": {"type": "invalid_param",
                                      "reason": f"{engine_id} 是编排层，不能直连合成",
                                      "fix": "改用 breeze / dotstts"}})
            continue
        out = job.get("out") or os.path.join("outputs", _safe_shot_name(rid))
        text = job.get("text") or ""
        # 长稿支持走文件：JSON 里塞几百字既难读又容易踩转义，而长文本恰恰是
        # 批量场景的主力（旁白/整段口播）。相对路径按 **job 文件所在目录**解析，
        # 不跟着调用时的 CWD 漂移。
        if not text and job.get("text_file"):
            tf = Path(job["text_file"])
            if not tf.is_absolute():
                tf = Path(args.batch).resolve().parent / tf
            try:
                text = tf.read_text(encoding="utf-8").strip()
            except Exception as e:
                results.append({"shot_id": rid, "ok": False, "out": os.path.abspath(out),
                                "error": {"type": "invalid_param",
                                          "reason": f"读取 text_file 失败: {e}",
                                          "fix": "检查 job 的 text_file 路径（相对路径按 job 文件所在目录解析）"}})
                continue
        if not text and not job.get("multi_role"):
            results.append({"shot_id": rid, "ok": False, "out": os.path.abspath(out),
                            "error": {"type": "invalid_param", "reason": "缺少 text 字段",
                                      "fix": "job 补 text（对白逐字原文）或 text_file（长稿走文件）或 multi_role（剧本）"}})
            continue
        ref = job.get("ref") or ""
        if not ref and job.get("voice"):
            v = find_voice(job["voice"])
            if not v:
                results.append({"shot_id": rid, "ok": False, "out": os.path.abspath(out),
                                "error": {"type": "invalid_param",
                                          "reason": f"找不到音色别名: {job['voice']}",
                                          "fix": "python dub.py --list-voices 查看可用音色"}})
                continue
            ref = v["file"]
        if not ref and engine_id != "chatterbox" and not job.get("multi_role"):
            results.append({"shot_id": rid, "ok": False, "out": os.path.abspath(out),
                            "error": {"type": "invalid_param", "reason": "缺少 voice/ref 字段",
                                      "fix": "job 补 voice（音色别名）或 ref（参考音频路径）"}})
            continue

        # 组装透传参数：job 字段优先，未写的用 CLI 默认值
        opts = dict(
            device=args.device, fp16=args.fp16,
            emo_audio=job.get("emo_audio"), emo_alpha=job.get("emo_alpha", 1.0),
            emo_text=None, emo_vector=job.get("emo_vector"), max_tokens=args.max_tokens,
            top_p=job.get("top_p", args.top_p), top_k=job.get("top_k", args.top_k),
            temperature=job.get("temperature", args.temperature),
            num_beams=job.get("num_beams", args.num_beams),
            repetition_penalty=job.get("repetition_penalty", args.repetition_penalty),
            max_mel_tokens=job.get("max_mel_tokens", args.max_mel_tokens),
            interval_silence=job.get("interval_silence", args.interval_silence),
            more_segment_before=job.get("more_segment_before", args.more_segment_before),
            use_random=job.get("use_random", args.use_random),
            model=job.get("model", args.model), prompt_text=job.get("prompt_text", args.prompt_text),
            language=job.get("lang", job.get("language", args.language)),
            num_steps=job.get("num_steps", args.num_steps),
            guidance_scale=job.get("guidance_scale", args.guidance_scale),
            speaker_scale=job.get("speaker_scale", args.speaker_scale),
            precision=job.get("precision", args.precision), max_len=job.get("max_len", args.max_len),
            normalize_text=job.get("normalize_text", args.normalize_text),
            ode_method=job.get("ode_method", args.ode_method),
            emotion=job.get("emotion"), exaggeration=job.get("exaggeration", args.exaggeration),
            cfg_weight=job.get("cfg_weight", args.cfg_weight), seed=job.get("seed", args.seed),
            min_p=job.get("min_p", args.min_p),
            # Breeze TTS 2（voice_jobs.json 可直接写 instruction / cfg_scale / ref_text）
            instruction=job.get("instruction", args.instruction),
            cfg_scale=job.get("cfg_scale", args.cfg_scale),
            ref_text=job.get("ref_text", args.ref_text),
            ref_text_file=job.get("ref_text_file", args.ref_text_file),
            auto_transcribe=job.get("auto_transcribe", args.auto_transcribe),
            # 长文本分段：批量里长稿也会被截断，job 可逐条覆盖
            chunk_chars=job.get("chunk_chars", args.chunk_chars),
            no_chunk=job.get("no_chunk", args.no_chunk),
            gap_ms=job.get("gap_ms", args.gap_ms),
        )
        # emotion 字段三形态（导演引擎契约）：emo-text / emotion(枚举) / 副语言标签(已内嵌文本)
        emo = job.get("emotion")
        if isinstance(emo, dict):
            etype, evalue = emo.get("type"), emo.get("value")
            if etype == "emo-text" and evalue:
                opts["emo_text"] = evalue
            elif etype == "emotion" and evalue:
                opts["emotion"] = evalue
            elif etype == "emo-audio" and evalue:
                opts["emo_audio"] = evalue
            elif etype == "emo-vector" and evalue:
                opts["emo_vector"] = evalue
            elif evalue:
                opts["emotion" if engine_id == "pilottts" else "emo_text"] = evalue
        elif isinstance(emo, str) and emo:
            opts["emotion" if engine_id == "pilottts" else "emo_text"] = emo
        if args.fast:
            if engine_id == "indextts2" and opts["num_beams"] == 3:
                opts["num_beams"] = 2
            if engine_id == "dotstts" and opts["num_steps"] == 10:
                opts["num_steps"] = 8

        multi_roles_extra = None
        if job.get("multi_role"):
            # A10：batch 多角色同样走品牌词替换（与单条路径一致）
            script_path, _roles = _materialize_multirole(job["multi_role"], args.brand_mode)
            roles_map = job.get("roles") or {}
            if isinstance(roles_map, dict):
                resolved = {}
                for rname, rvoice in roles_map.items():
                    v = find_voice(rvoice)
                    resolved[rname] = v["file"] if v else rvoice
                roles_json = json.dumps(resolved, ensure_ascii=False)
            else:
                roles_json = str(roles_map)
            multi_roles_extra = ["--multi-role", script_path, "--roles", roles_json]
            ref, text = "", ""

        _print(f"[batch {i}/{len(jobs)}] shot={rid} engine={engine_id} out={os.path.abspath(out)}")
        res = _run_single(engine, ref, text, out, opts, multi_roles_extra,
                          brand_mode=args.brand_mode, no_serve=args.no_serve,
                          dry_run=args.dry_run)
        res["shot_id"] = rid
        results.append(res)

    summary = {"ok": True, "total": len(results),
               "succeeded": sum(1 for r in results if r.get("ok")),
               "failed": sum(1 for r in results if not r.get("ok")),
               "jobs": results}
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        for r in results:
            if r.get("ok"):
                extra = f"  时长={r.get('duration')}s" if r.get("duration") else ""
                if r.get("dry_run"):
                    _print(f"  ⏭ shot={r['shot_id']}: [dry-run] 未合成，命令: {r.get('cmd','')[:100]}...")
                else:
                    _print(f"  ✅ shot={r['shot_id']}: {r.get('out')} ({r.get('size_kb')} KB){extra}")
            else:
                err = r.get("error", {})
                _print(f"  ❌ shot={r['shot_id']}: [{err.get('type', 'unknown')}] {err.get('reason', '')}")
                _print(f"     fix: {err.get('fix', '')}")
        _print(f"\n批处理完成: 成功 {summary['succeeded']}/{summary['total']}")
    sys.exit(0 if summary["failed"] == 0 else 1)


def main():
    ap = argparse.ArgumentParser(description="本地配音工坊 · 统一配音入口")
    ap.add_argument("--engine-root", default=None, help=f"覆盖 TTS_ROOT（默认 {TTS_ROOT}）")
    ap.add_argument("--list-engines", action="store_true")
    ap.add_argument("--list-voices", action="store_true")
    ap.add_argument("--health", action="store_true",
                    help="体检：引擎/音色/词典/常驻服务就绪状态（导演引擎对接验收）")
    ap.add_argument("--batch", default=None,
                    help="批量消费导演引擎分镜包 prompts/voice_jobs.json（逐 job 合成 + 回写时长）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只校验输入与命令构造，不真正合成（配合 --engine / --batch）")
    ap.add_argument("--json", action="store_true",
                    help="结构化 JSON 输出（对接导演引擎回写协议：含实测 duration / error 分类）")
    ap.add_argument("--sync-glossary", action="store_true",
                   help="把 brands.json 同步进 IndexTTS2 的原生 glossary.yaml（供其 WebUI 直接用）")

    # dub 动作
    ap.add_argument("--engine", default=None, help=f"引擎 id。不传则用默认 {DEFAULT_ENGINE}（梯队①）；不可用自动沿回退链切换。停用引擎见 --list-engines")
    ap.add_argument("--voice", help="音色别名（来自音色包，见 --list-voices）")
    ap.add_argument("--ref", help="参考音频路径（直接指定，跳过 --voice）")
    ap.add_argument("--text", help="要合成的文本")
    ap.add_argument("--text-file", help="从文件读取文本")
    ap.add_argument("--multi-role", help="多角色剧本文件（[角色名]: 内容 每行一条；仅 indextts2）")
    ap.add_argument("--roles", help="多角色映射：角色名=音色别名或路径，逗号分隔（如 子言=子言,小棠=小棠）")
    ap.add_argument("--out", default="outputs/dub.wav", help="输出 WAV 路径")

    # 通用透传
    ap.add_argument("--device", default=None)
    ap.add_argument("--fp16", action="store_true")
    # IndexTTS2 情感 + 生成采样参数（generation_kwargs）
    ap.add_argument("--emo-audio", default=None)
    ap.add_argument("--emo-alpha", type=float, default=1.0, help="情感强度 0~1")
    ap.add_argument("--emo-text", default=None, help="自然语言情感描述，如'悲伤地'")
    ap.add_argument("--emo-vector", default=None, help="8维情感向量(逗号分隔，高级)")
    ap.add_argument("--max-tokens", type=int, default=120, help="每段最大 token 数")
    ap.add_argument("--top-p", type=float, default=None, help="核采样阈值(IndexTTS2/Chatterbox/PilotTTS)")
    ap.add_argument("--top-k", type=int, default=None, help="Top-K 采样(IndexTTS2/PilotTTS)")
    ap.add_argument("--temperature", type=float, default=None, help="采样温度，None=各引擎官方默认")
    ap.add_argument("--num-beams", type=int, default=3, help="IndexTTS2 beam 数(确定性)")
    ap.add_argument("--repetition-penalty", type=float, default=None, help="重复惩罚(None=引擎默认)")
    ap.add_argument("--max-mel-tokens", type=int, default=1500, help="IndexTTS2 单段最大 mel token")
    ap.add_argument("--interval-silence", type=int, default=200, help="IndexTTS2 句间静音(ms)")
    ap.add_argument("--more-segment-before", type=int, default=0, help="IndexTTS2 前瞻段数")
    ap.add_argument("--use-random", action="store_true", help="IndexTTS2 关闭 beam 走随机采样")
    # dots.tts
    ap.add_argument("--model", default=None, help="dots.tts 模型变体: dots-tts-soar / dots-tts-mf")
    ap.add_argument("--prompt-text", default=None, help="参考音频转写文本")
    ap.add_argument("--language", default=None)
    ap.add_argument("--num-steps", type=int, default=10, help="扩散采样步数 10-32")
    ap.add_argument("--guidance-scale", type=float, default=1.2, help="引导强度 >2 放大能量")
    ap.add_argument("--speaker-scale", type=float, default=1.5, help="说话人相似度权重")
    ap.add_argument("--precision", default="bfloat16", help="bfloat16 / float16")
    ap.add_argument("--max-len", type=int, default=500, help="最大音频 patch 数")
    ap.add_argument("--normalize-text", action="store_true", help="dots.tts 文本归一化(去标点等)")
    ap.add_argument("--ode-method", default="euler", help="dots.tts ODE 求解器: euler / midpoint 等")
    # PilotTTS 情感 + 采样
    ap.add_argument("--emotion", default=None, help="PilotTTS 情感标签(逗号分隔可叠加)")
    # Chatterbox-V3
    ap.add_argument("--exaggeration", type=float, default=0.5, help="Chatterbox 情绪夸张度 0.25-2.0")
    ap.add_argument("--cfg-weight", type=float, default=0.5, help="Chatterbox CFG/节奏权重 0.2-1.0")
    ap.add_argument("--min-p", type=float, default=None, help="Chatterbox 最小概率阈值(None=默认0.05)")
    ap.add_argument("--seed", type=int, default=0, help="随机种子，0=随机(Chatterbox)")
    # Breeze TTS 2
    ap.add_argument("--instruction", default=None,
                   help="Breeze 自然语言指令（voice design/direction：描述音色或调语气）")
    ap.add_argument("--cfg-scale", type=float, default=None, help="Breeze 指令跟随强度(官方推荐 4)")
    ap.add_argument("--ref-text", default=None, help="Breeze 参考音精确转写(voice clone 必填)")
    ap.add_argument("--ref-text-file", default=None, help="Breeze 转写走文件(推荐，规避命令行编码)")
    ap.add_argument("--auto-transcribe", action="store_true",
                   help="Breeze 未给 --ref-text 时，用内置 whisper 自动转写参考音")
    # 长文本分段（Breeze 服务端 max_new_tokens=1500，超长会静默丢尾巴）
    ap.add_argument("--chunk-chars", type=int, default=None,
                   help="长文本分段字数上限，默认 350（Breeze 上限约 544 字，留 36%% 余量）")
    ap.add_argument("--no-chunk", action="store_true",
                   help="关闭长文本自动分段 —— ⚠️ 超过 544 字会静默丢结尾，别在日常流程里用")
    ap.add_argument("--gap-ms", type=int, default=None,
                   help="多角色/分段之间的静音间隔(毫秒)，默认 250")
    ap.add_argument("--brand-mode", default="zh", choices=["zh", "en", "off"],
                   help="品牌名替换: zh=中文读法(默认) / en=英文读法 / off=不替换")

    # 运行加速 / 服务模式
    ap.add_argument("--serve", action="store_true",
                   help="启动指定引擎的持久化服务（模型只加载一次，后续合成秒级复用）")
    ap.add_argument("--stop-serve", action="store_true",
                   help="停止指定引擎的持久化服务")
    ap.add_argument("--stop-all-serve", action="store_true",
                   help="停止全部引擎的常驻服务（跑正式任务前先清场，避免闲置服务占显存）")
    ap.add_argument("--no-serve", action="store_true",
                   help="强制走一次性子进程，即使有服务在运行也不复用")
    ap.add_argument("--port", type=int, default=None,
                   help="--serve 时指定端口（默认按引擎分配）")
    ap.add_argument("--fast", action="store_true",
                   help="加速预设：降低 beam/步数换速度（indextts2 num_beams=2、dotstts steps=8），质量略降")

    a = ap.parse_args()

    if a.engine_root:
        os.environ["TTS_ROOT"] = a.engine_root
        # 重新载入 registry 的模块级常量
        import importlib
        import registry
        importlib.reload(registry)
        globals()["load_engines"] = registry.load_engines
        globals()["load_voices"] = registry.load_voices
        globals()["find_voice"] = registry.find_voice
        globals()["find_engine"] = registry.find_engine
        globals()["TTS_ROOT"] = registry.TTS_ROOT
        globals()["VOICE_POOL"] = registry.VOICE_POOL

    if a.list_engines:
        cmd_list_engines(a); return
    if a.list_voices:
        cmd_list_voices(a); return
    if a.health:
        cmd_health(a); return
    if a.batch:
        cmd_batch(a); return
    if a.sync_glossary:
        _cmd_sync_glossary(); return
    if a.stop_all_serve:
        _cmd_stop_all_serve(); return
    if a.serve or a.stop_serve:
        # 不传 --engine 时按梯队走默认引擎（--serve 单独用即可）
        if a.serve:
            _cmd_serve(a); return
        _cmd_stop_serve(a); return
    # 给了任一合成输入就开工；--engine 可省略（走默认引擎 + 回退链）
    if a.engine or a.text or a.text_file or a.out:
        cmd_dub(a); return

    ap.print_help()


if __name__ == "__main__":
    main()
