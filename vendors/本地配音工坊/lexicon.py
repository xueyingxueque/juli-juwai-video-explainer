# -*- coding: utf-8 -*-
"""
品牌名 / 专有名词读音词典中间件
================================
TTS 引擎对 "DeepSeek / ChatGPT / RTX / B站" 这类品牌名几乎必读错（见行业测试：
5 款主流 TTS 专有名词错误率 29%~57%）。根因在文本正则化(TN)阶段——引擎不知道
品牌方定义的读法。本模块在文本进入引擎之前，把品牌 token 替换成"读得对的表达"
（官方中文名 / 同音替换 Trick），是业界公认的"定义权"注入层，不侵入任何引擎。

用法：
  from lexicon import apply_brands
  text = apply_brands("用 DeepSeek 做个 demo", mode="zh")   # -> 用 深度求索 做个 demo

- 词典来源：技能目录下的 brands.json（用户可自由增删）。
- mode:
    "zh"  用中文读法（默认，适合中文配音，官方名优先，否则音译）
    "en"  用英文读法（逐字母/英文读法，适合英文配音）
    "off" 不做替换（原样输出）
- 匹配：ASCII 品牌大小写不敏感、按词边界匹配（不会把 "A" 嵌在单词里误替）；
        中文/混合品牌按字面精确匹配。长词条优先（先替 "GPT-4o" 再替 "GPT"）。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent
BRANDS_JSON = SKILL_DIR / "brands.json"

# 模块级缓存：避免每次合成都读盘
_cache = {"mtime": 0.0, "data": None}


def load_brands(force=False):
    """读取 brands.json。带 mtime 缓存；文件被改后自动重载。"""
    global _cache
    try:
        mtime = os.path.getmtime(BRANDS_JSON)
    except OSError:
        return {}
    if _cache["data"] is not None and not force and _cache["mtime"] == mtime:
        return _cache["data"]
    try:
        data = json.loads(BRANDS_JSON.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    # 过滤掉 "_说明" 这类注释键
    data = {k: v for k, v in data.items() if not k.startswith("_")}
    _cache = {"mtime": mtime, "data": data}
    return data


def _build_patterns(brands):
    """把品牌词典编译成 (regex, replacement_getter) 列表，长词条优先。"""
    items = []
    for token, entry in brands.items():
        if isinstance(entry, dict):
            replacement = entry
        else:  # 兼容 "GPT": "吉皮提" 简写
            replacement = {"zh": entry, "en": entry}
        # ASCII 词边界；非 ASCII 用字面（前后非字母数字）边界
        if re.fullmatch(r"[A-Za-z0-9.\-+# ]+", token):
            pat = r"(?<![A-Za-z0-9])" + re.escape(token) + r"(?![A-Za-z0-9])"
            flags = re.IGNORECASE
        else:
            pat = r"(?<![A-Za-z0-9])" + re.escape(token) + r"(?![A-Za-z0-9])"
            flags = 0
        items.append((re.compile(pat, flags), replacement, token))
    # 长词条优先：避免 "GPT" 抢在 "GPT-4o" 前面
    items.sort(key=lambda x: len(x[2]), reverse=True)
    return items


def apply_brands(text, mode="zh"):
    """把 text 里的品牌 token 按 mode 替换成读得对的表达。"""
    if not text or mode == "off":
        return text
    brands = load_brands()
    if not brands:
        return text
    patterns = _build_patterns(brands)
    out = text
    for pat, repl, orig in patterns:
        out = pat.sub(lambda m: (repl.get(mode) or repl.get("zh") or m.group(0)), out)
    return out


def preview_brands(text, mode="zh"):
    """返回 (替换后文本, [(原词, 替换词), ...])，用于日志/调试。"""
    if not text or mode == "off":
        return text, []
    brands = load_brands()
    if not brands:
        return text, []
    patterns = _build_patterns(brands)
    hits = []
    out = text
    for pat, repl, orig in patterns:
        def _make(o=orig, r=repl):
            def _sub(m):
                val = r.get(mode) or r.get("zh") or m.group(0)
                hits.append((o, val))
                return val
            return _sub
        out = pat.sub(_make(), out)
    return out, hits


if __name__ == "__main__":
    sample = "用 DeepSeek 跑个 demo，再调 ChatGPT 和 GPT-4o，最后用 RTX 渲染。"
    new, log = preview_brands(sample, "zh")
    print("原:", sample)
    print("替:", new)
    for a, b in log:
        print(f"  {a} -> {b}")
