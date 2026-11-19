# -*- coding: utf-8 -*-
"""
本地配音工坊 · 引擎与音色注册层
================================
职责：
  1. 扫描 TTS_ROOT（默认 D:\\AITools\\TTS），按指纹自动识别已安装的配音引擎
  2. 扫描 TTS_ROOT/音色包，建立统一音色索引（含时长、参考文本、场景标签）
  3. 输出 engines.json / voices.json 供 dub.py 使用

设计原则：**加引擎只需往 D:\\AITools\\TTS 丢文件夹**，
若是已知引擎（指纹命中）自动挂载；若未知，scan 会提示需要补一个 adapter。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import wave
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent
TTS_ROOT = Path(os.environ.get("TTS_ROOT", r"D:\AITools\TTS"))
VOICE_POOL = TTS_ROOT / "音色包"
ENGINES_JSON = SKILL_DIR / "engines.json"
VOICES_JSON = SKILL_DIR / "voices.json"
VOICE_TAGS = SKILL_DIR / "voice_tags.json"

AUDIO_EXT = {".wav", ".mp3", ".flac", ".m4a", ".ogg"}

# 明确不是引擎的工作目录（不参与指纹扫描，避免 --list-engines 出现噪声）
#   - 音色包：全引擎共用的参考音池
#   - 口播稿 / outputs / 输出：文案与产物存放处，用户可自行增删
NON_ENGINE_DIRS = {"音色包", "口播稿", "outputs", "输出", "output", "成品"}

# ──────────────────────────────────────────────────────────────
# 引擎梯队（2026-09-04 用户试听拍板，Agent 必须遵守）
#   1 = 默认首选（不传 --engine 就用它）
#   2 = 第二梯队（高保真/成品场景）
#   3 = 停用：代码与指纹保留，但文档不引导、不进默认、不进回退链
#        想恢复只需把 tier 改回去 —— 所以不做物理删除
# ──────────────────────────────────────────────────────────────
TIER_LABEL = {1: "①默认", 2: "②备选", 3: "停用"}
DEFAULT_ENGINE = "breeze"
# 默认引擎不可用（没装 / 未就绪）时的自动回退顺序，只含在用梯队
FALLBACK_CHAIN = ["breeze", "dotstts"]

# ──────────────────────────────────────────────────────────────
# 常驻服务端口表（**唯一来源**，dub.py 与 workers 都从这里取）
#
# 为什么必须唯一：worker 一次性合成时自己拉起服务，dub.py --serve 时拉起常驻服务。
# 两边端口不一致就会同时存在两个同类引擎实例 —— Breeze 每个实例约 7.7GiB 显存，
# 12G 卡上两个必 OOM，而且残留的那个没人回收（查出来时已经白占 28 分钟）。
# 教训：端口这类"连接两侧都要知道"的配置，写两遍就一定会漂。
# ──────────────────────────────────────────────────────────────
ENGINE_PORTS = {
    "indextts2": 8731,
    "dotstts": 8732,
    "pilottts": 8733,
    "chatterbox": 8734,
    "breeze": 8735,
}


def engine_port(engine_id: str, fallback: int = 8731) -> int:
    """取引擎端口；未登记的统一走 fallback，避免各处硬编码散落"""
    return ENGINE_PORTS.get(engine_id, fallback)


# 一次性子进程专用端口 = 常驻服务端口 + 100
#
# 为什么必须错开（这是踩过的坑）：`--serve` 起的是**本技能自己的 serve.py**，
# 它不透传 `--multi-role/--roles`。worker 一次性跑时若复用了它，多角色请求会
# **静默退化成单人声** —— 不报错、不失败，产物听着就是一个人念完全场。
#
# 早期 worker 默认端口(7860) 与 --serve(8735) 天然不同，歪打正着躲过了；
# 后来为修「两个实例挤爆显存」把端口统一，这个保护就失效了。
# 所以：凡是需要**绕开常驻服务**的场景（目前是多角色），必须走另一个端口。
ONESHOT_PORT_OFFSET = 100


def oneshot_port(engine_id: str, fallback: int = 8731) -> int:
    """一次性子进程应使用的端口（与常驻服务错开，避免误复用 serve.py）"""
    return ENGINE_PORTS.get(engine_id, fallback) + ONESHOT_PORT_OFFSET

# ──────────────────────────────────────────────────────────────
# 引擎指纹表：新增引擎在这里加一条即可
#   probe   : 相对引擎根目录的特征文件，全部存在才算命中
#   python  : 引擎自带的便携 Python
#   worker  : 本技能里对应的 worker 脚本
# ──────────────────────────────────────────────────────────────
FINGERPRINTS = [
    {
        "id": "indextts2",
        "name": "IndexTTS2",
        "vendor": "B站 Index 团队",
        "probe": ["app/python/indextts/infer_v2.py", "app/python/checkpoints/config.yaml"],
        "python": "app/python/build_venv/python.exe",
        "worker": "workers/worker_indextts2.py",
        "cwd": "app/python",
        "sample_rate": 22050,
        "tier": 3,
        "features": ["voice_clone", "emotion_vector", "emotion_audio", "emotion_text", "duration_control"],
        "tagline": "情感/音色解耦 + 时长精确可控，中文综合最稳",
    },
    {
        "id": "dotstts",
        "name": "dots.tts",
        "vendor": "小红书 hilab",
        "probe": ["src/dots_tts/runtime.py", "pretrained_models"],
        "python": "wzf/python.exe",
        "worker": "workers/worker_dotstts.py",
        "cwd": ".",
        "sample_rate": 48000,
        "tier": 2,
        "features": ["voice_clone", "prompt_text", "multilingual", "48k"],
        "tagline": "48kHz 高保真 + 音色相似度 SOTA，多语种最强",
    },
    {
        "id": "pilottts",
        "name": "PilotTTS",
        "vendor": "PilotTTS 团队",
        "probe": ["pilot_voice/engine.py", "model_assets/pilot_tts.pt"],
        "python": "python/python.exe",
        "worker": "workers/worker_pilottts.py",
        "cwd": ".",
        "sample_rate": 24000,
        "tier": 3,
        "features": ["voice_clone", "emotion_tag", "paralinguistic", "dialect"],
        "tagline": "说话人相似度最高 + 笑/哭/叹气副语言标签，方言强",
    },
    {
        "id": "emotiontts",
        "name": "EmotionTTS",
        "vendor": "EmotionTTS 整合包",
        "probe": ["core/lite_server.py", "core/routers/tts_router.py"],
        "python": "core/pyenv/python.exe",
        "worker": None,  # 编排层，非底层引擎
        "cwd": ".",
        "sample_rate": 0,
        "tier": 3,
        "features": ["orchestrator", "llm_emotion_match", "asr_tagging"],
        "tagline": "AI 情绪导演（编排层）：LLM 自动选情绪+参考音，底层仍调 IndexTTS2",
        "role": "orchestrator",
    },
    {
        "id": "chatterbox",
        "name": "Chatterbox-V3",
        "vendor": "Resemble AI",
        "probe": ["src/chatterbox/__init__.py", "checkpoints/s3gen.pt", "multilingual_app.py"],
        "python": "wzf/python.exe",
        "worker": "workers/worker_chatterbox.py",
        "cwd": ".",
        "sample_rate": 24000,
        "tier": 3,
        "features": ["voice_clone", "multilingual", "exaggeration", "cfg_weight",
                     "temperature", "builtin_voice", "watermark", "24k"],
        "tagline": "23 语种多语 TTS + 中文精调 zh-cmn，零样本克隆 + 夸张度/节奏可控，自带水印",
    },
    {
        "id": "breeze",
        "name": "Breeze TTS 2",
        "vendor": "BreezeBlue / RESONIA",
        # 模型目录与 GUI 版本号无关，用 models/breeze_tts 做指纹最稳
        "probe": ["models/breeze_tts"],
        "python": "T8star-Aix-Voice-Studio-v0.2.9-win32-x64/resources/python/python.exe",
        "python_glob": "T8star-Aix-Voice-Studio-*/resources/python/python.exe",
        "worker": "workers/worker_breeze.py",
        "cwd": ".",
        "sample_rate": 24000,
        "tier": 1,
        "features": ["voice_clone", "voice_design", "voice_direction",
                     "vocal_events", "bilingual", "service_mode", "24k"],
        "tagline": "开源榜首：Voice Design 无参考音造音色 + 中英双语 + 中文[笑]事件标签",
    },
]

# ──────────────────────────────────────────────────────────────
# 音色场景规则：按文件名关键词猜测场景/性别，scan 时作为初始标注
# 人工精修结果放 voice_tags.json，优先级更高
# ──────────────────────────────────────────────────────────────
SCENE_RULES = [
    (r"东北话|四川话|陕西话|北京|广东|粤|靓仔", ["方言短视频", "喜剧", "地域号"]),
    (r"新闻|播报|气象|记者|垃圾分类|消息", ["新闻播报", "资讯口播"]),
    (r"直播间|主播|优惠|抢购|益生菌|零食|带货", ["直播带货", "电商口播"]),
    (r"演讲|交流|创业|新东方|教书", ["知识分享", "演讲", "职场"]),
    (r"实验|化学|原理|高原|海拔|老街|盐马古道", ["科普解说", "纪录片"]),
    (r"听众|音乐|电台|晚上好|温馨|夜晚", ["情感电台", "深夜播客"]),
    (r"女友|男友|宝宝|恋爱|睡觉|喜欢|勾引|吊我|中意", ["情感陪伴", "ASMR", "虚拟角色"]),
    (r"萝莉|御姐|温柔|知性|瑶瑶|珊珊", ["虚拟角色", "游戏配音", "情感陪伴"]),
    (r"笑声|笑|相声|徒弟|功夫|开玩笑", ["喜剧", "综艺", "情绪素材"]),
    (r"毕业|时光|回忆|友情|傍晚|茉莉|温暖|安心", ["情感短片", "抒情文案", "Vlog"]),
    (r"依恋|心理|感情|茶|挫折|希望", ["心理疗愈", "情感电台"]),
    (r"管理员|登录|改动", ["系统提示", "教程演示"]),
    (r"原唱|樱桃|素材", ["音频素材", "测试样本"]),
]

GENDER_RULES = [
    (r"女友|女孩|萝莉|御姐|温柔|知性|珊珊|瑶瑶|思思|小棠|小雨|女播音|嫁到何家|宝宝", "female"),
    (r"表哥|靓仔|小昊|明宇|子言|郭德纲|老师|徒弟|演讲|创业", "male"),
]

# 精品音色（带规范命名 名字_风格 的那批）
CURATED_RE = re.compile(r"^([\u4e00-\u9fa5A-Za-z]{1,4})_(.+)$")

# ──────────────────────────────────────────────────────────────
# 音色分组：音色包下的一层子目录 = 一个分组
#   - 「常用」分组：配音优先选（--list-voices 置顶标 ⭐）
#   - 备份/存档类目录不扫描（不进音色索引）
# ──────────────────────────────────────────────────────────────
COMMON_GROUP = "常用"
SKIP_GROUPS = {"备份", "backup", "存档", "归档", "old", "_old", "废弃"}


def _iter_voice_files(pool: Path):
    """遍历音色文件：顶层文件 group="根目录"，一层子目录 group=目录名。
    常用组优先输出（保证 alias 优先分配给常用音色）。"""
    dirs = []
    for d in sorted(pool.iterdir()):
        if d.is_dir() and d.name not in SKIP_GROUPS and not d.name.startswith("."):
            dirs.append(d)
    dirs.sort(key=lambda d: (d.name != COMMON_GROUP, d.name))  # 常用组排最前
    for d in dirs:
        for f in sorted(d.iterdir()):
            if f.is_file() and f.suffix.lower() in AUDIO_EXT:
                yield f, d.name
    for f in sorted(pool.iterdir()):
        if f.is_file() and f.suffix.lower() in AUDIO_EXT:
            yield f, "根目录"


def _probe_ok(root: Path, probes) -> bool:
    return all((root / p).exists() for p in probes)


def discover_engines(tts_root: Path = TTS_ROOT) -> dict:
    """扫描 TTS_ROOT 下所有子目录，按指纹识别引擎"""
    found, unknown = [], []
    if not tts_root.is_dir():
        return {"tts_root": str(tts_root), "engines": [], "unknown_dirs": [], "error": "TTS_ROOT 不存在"}

    for child in sorted(tts_root.iterdir()):
        if not child.is_dir() or child.name in NON_ENGINE_DIRS or child.name.startswith("."):
            continue
        hit = None
        for fp in FINGERPRINTS:
            if _probe_ok(child, fp["probe"]):
                hit = fp
                break
        if hit is None:
            unknown.append(child.name)
            continue

        py = child / hit["python"]
        # GUI 版本号会升级（v0.2.9 → v0.3.x），静态路径失效时用通配兜底
        if not py.is_file() and hit.get("python_glob"):
            import glob as _glob
            _hits = sorted(_glob.glob(str(child / hit["python_glob"])))
            if _hits:
                py = Path(_hits[-1])
        entry = {
            "id": hit["id"],
            "name": hit["name"],
            "vendor": hit["vendor"],
            "root": str(child),
            "python": str(py),
            "python_ok": py.is_file(),
            "worker": hit["worker"],
            "cwd": str((child / hit["cwd"]).resolve()),
            "sample_rate": hit["sample_rate"],
            "features": hit["features"],
            "tagline": hit["tagline"],
            "role": hit.get("role", "engine"),
            "tier": hit.get("tier", 3),
            "ready": py.is_file(),
        }
        # dots.tts：列出可用模型变体
        if hit["id"] == "dotstts":
            models = []
            pm = child / "pretrained_models"
            if pm.is_dir():
                for m in sorted(pm.iterdir()):
                    if m.is_dir() and (m / "config.json").is_file():
                        models.append(m.name)
            entry["models"] = models
            entry["default_model"] = "dots-tts-soar" if "dots-tts-soar" in models else (models[0] if models else None)
            entry["ready"] = entry["python_ok"] and bool(models)
        # PilotTTS：base / instruct 两套权重
        if hit["id"] == "pilottts":
            entry["models"] = [
                m for m in ("pilot_tts", "pilot_tts_instruct")
                if (child / "model_assets" / f"{m}.pt").is_file()
            ]
            entry["default_model"] = "pilot_tts"
            entry["ready"] = entry["python_ok"] and bool(entry["models"])
        if hit["id"] == "indextts2":
            ck = child / "app/python/checkpoints"
            entry["ready"] = entry["python_ok"] and (ck / "gpt.pth").is_file()
        # Chatterbox-V3：v3(通用多语) / zh-cmn(中文精调) 两套 T3 权重
        if hit["id"] == "chatterbox":
            ck = child / "checkpoints"
            entry["models"] = [
                m for m in ("v3", "zh-cmn")
                if (ck / {"v3": "t3_mtl23ls_v3.safetensors",
                          "zh-cmn": "t3_zh_cmn.safetensors"}[m]).is_file()
            ]
            entry["default_model"] = "v3" if "v3" in entry["models"] else None
            entry["ready"] = entry["python_ok"] and bool(entry["models"]) \
                and (ck / "s3gen.pt").is_file() and (ck / "ve.pt").is_file()

        found.append(entry)

    return {"tts_root": str(tts_root), "engines": found, "unknown_dirs": unknown}


def _wav_duration(p: Path):
    try:
        with wave.open(str(p), "rb") as w:
            return round(w.getnframes() / float(w.getframerate()), 2)
    except Exception:
        return None


def _ffprobe_duration(p: Path, ffprobe: str | None):
    if not ffprobe:
        return None
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(p)],
            capture_output=True, text=True, timeout=20,
        )
        return round(float(out.stdout.strip()), 2)
    except Exception:
        return None


def _find_ffprobe(tts_root: Path):
    # A12：不硬编码 emotionTTS 版本号，glob 匹配任意版本整合包自带的 ffprobe
    candidates = sorted(tts_root.glob("emotionTTS*/core/bin/ffprobe.exe"))
    if candidates:
        return str(candidates[0])
    # 兜底：TTS_ROOT 下任意 ffprobe.exe
    extras = sorted(tts_root.glob("**/ffprobe.exe"))
    return str(extras[0]) if extras else None


def _guess_gender(name: str):
    for pat, g in GENDER_RULES:
        if re.search(pat, name):
            return g
    return "unknown"


def _guess_scenes(name: str):
    scenes = []
    for pat, tags in SCENE_RULES:
        if re.search(pat, name):
            scenes.extend(tags)
    return sorted(set(scenes)) or ["通用"]


def _make_alias(stem: str, used: set):
    """长句文件名 → 短别名；规范命名取名字部分"""
    m = CURATED_RE.match(stem)
    if m and len(m.group(1)) <= 4:
        base = m.group(1)
    elif len(stem) <= 8:
        base = stem
    else:
        base = re.split(r"[，,。！!？?、\s]", stem)[0][:8] or stem[:8]
    alias, i = base, 2
    while alias in used:
        alias = f"{base}{i}"
        i += 1
    used.add(alias)
    return alias


def discover_voices(pool: Path = VOICE_POOL) -> dict:
    """扫描音色包目录，建立统一音色索引"""
    if not pool.is_dir():
        return {"pool": str(pool), "voices": [], "error": "音色包目录不存在"}

    ffprobe = _find_ffprobe(pool.parent)
    manual = {}
    if VOICE_TAGS.is_file():
        try:
            manual = json.loads(VOICE_TAGS.read_text(encoding="utf-8"))
        except Exception:
            manual = {}

    voices, used = [], set()
    for f, group in _iter_voice_files(pool):
        stem = f.stem
        dur = _wav_duration(f) if f.suffix.lower() == ".wav" else None
        if dur is None:
            dur = _ffprobe_duration(f, ffprobe)

        alias = _make_alias(stem, used)
        m = CURATED_RE.match(stem)
        # 文件名若是整句话 → 它就是这段参考音频的转写文本，可直接喂给支持 prompt_text 的引擎
        prompt_text = stem if len(stem) >= 12 and not m else ""

        item = {
            "alias": alias,
            "file": str(f),
            "filename": f.name,
            "group": group,
            "common": group == COMMON_GROUP,
            "style": m.group(2) if m else "",
            "duration": dur,
            "gender": _guess_gender(stem),
            "scenes": _guess_scenes(stem),
            "prompt_text": prompt_text,
            "curated": bool(m),
            "quality": _quality_hint(dur),
        }
        item.update(manual.get(alias, {}))
        voices.append(item)

    # 常用组优先排序（--list-voices 置顶）
    voices.sort(key=lambda v: (not v["common"], v["alias"]))
    return {"pool": str(pool), "count": len(voices), "voices": voices}


def _quality_hint(dur):
    """参考音频时长评级：克隆最佳区间 5-15s"""
    if dur is None:
        return "unknown"
    if dur < 3:
        return "偏短(可能不稳)"
    if dur <= 15:
        return "最佳"
    if dur <= 30:
        return "可用(建议裁到15s内)"
    return "过长(务必裁剪)"


def load_engines():
    if ENGINES_JSON.is_file():
        return json.loads(ENGINES_JSON.read_text(encoding="utf-8"))
    return discover_engines()


def load_voices():
    if VOICES_JSON.is_file():
        return json.loads(VOICES_JSON.read_text(encoding="utf-8"))
    return discover_voices()


def find_voice(key: str):
    """按别名 / 文件名 / 模糊匹配找音色"""
    data = load_voices()
    vs = data.get("voices", [])
    if os.path.isfile(key):
        return {"alias": Path(key).stem, "file": key, "prompt_text": "", "scenes": [], "duration": None}
    for v in vs:
        if v["alias"] == key or v["filename"] == key:
            return v
    low = key.lower()
    hits = [v for v in vs if low in v["alias"].lower() or low in v["filename"].lower()]
    return hits[0] if hits else None


def find_engine(key: str):
    data = load_engines()
    for e in data.get("engines", []):
        if e["id"] == key or e["name"].lower() == key.lower():
            return e
    return None


def resolve_engine(key: str = None, verbose=None):
    """按梯队解析引擎（Agent 统一走这里，别自己挑）。

    - 不传 key → 用 DEFAULT_ENGINE（当前 breeze）
    - 指定的/默认的不可用（没装或未就绪）→ 沿 FALLBACK_CHAIN 找第一个 ready 的
    - 全链都不可用 → 退回「第一个 ready 的引擎」并提示（保底不中断任务）
    返回 (engine, note)；都找不到时 engine 为 None。
    """
    data = load_engines()
    engines = data.get("engines", [])
    if not engines:
        return None, "engines.json 为空，先跑 scan.py --engines"

    def _pick(k):
        if not k:
            return None
        e = find_engine(k)
        return e if e and e.get("ready") else None

    e = _pick(key) if key else _pick(DEFAULT_ENGINE)
    if e:
        notes = []
        if key and e["id"] != key:
            notes.append(f"引擎 {key} 未就绪，已回退到 {e['name']}")
        if not key and e["id"] != DEFAULT_ENGINE:
            notes.append(f"默认引擎 {DEFAULT_ENGINE} 未就绪，已回退到 {e['name']}")
        # 显式点名停用引擎：放行但报警（真有方言/副语言需求时能救急，但不能悄悄用）
        if e.get("tier", 3) >= 3:
            notes.append(f"⚠️ {e['name']} 已停用（tier 3），非必要勿用")
        return e, "；".join(notes) or None

    # 沿回退链找（只含在用梯队）
    for k in FALLBACK_CHAIN:
        e = _pick(k)
        if e:
            return e, f"{key or DEFAULT_ENGINE} 不可用，已沿回退链切换到 {e['name']}"

    # 保底：任意一个 ready 的
    for e in engines:
        if e.get("ready") and e.get("role") != "orchestrator":
            return e, f"回退链全不可用，临时改用 {e['name']}（梯队外，请检查环境）"
    return None, "没有任何就绪引擎，先跑 scan.py --engines 与 --health"


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(discover_engines(), ensure_ascii=False, indent=2))
