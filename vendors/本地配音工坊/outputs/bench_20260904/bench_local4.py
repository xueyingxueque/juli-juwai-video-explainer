# -*- coding: utf-8 -*-
"""本地配音工坊 · 五引擎横评（同一文案 + 同一常用音色）

口径：每个引擎跑同一份 30s 文案，用同一参考音色，记录「真实耗时 / 音频时长 / RTF / 体积」。
RTF < 1 = 比实时快；RTF 越小越快。
"""
import json
import subprocess
import sys
import time
import wave
from pathlib import Path

SKILL = Path(r"C:/Users/Administrator/.workbuddy/skills/本地配音工坊")
PY = r"C:/Users/Administrator/.workbuddy/binaries/python/versions/3.13.12/python.exe"
HERE = Path(__file__).resolve().parent
TEXT = HERE / "文案_30s.txt"
VOICE = "小欧"

# 引擎 → 额外参数（按各引擎能力给到对齐的合理设置）
ENGINES = [
    ("indextts2", []),
    ("dotstts", []),
    ("pilottts", []),
    ("chatterbox", []),
]


def probe(path: Path):
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / w.getframerate(), w.getframerate(), w.getnchannels()
    except Exception:
        return None, None, None


def run(engine: str, extra: list):
    out = HERE / f"{engine}_30s.wav"
    cmd = [PY, "dub.py", "--engine", engine, "--voice", VOICE,
           "--text-file", str(TEXT), "--out", str(out), "--json", *extra]
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=SKILL, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    cost = time.time() - t0
    dur, sr, ch = probe(out)
    size_kb = out.stat().st_size / 1024 if out.exists() else 0
    # --json 时 stdout 只有一份 JSON
    payload = None
    try:
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception:
        pass
    return {
        "engine": engine, "ok": out.exists() and dur,
        "cost_s": round(cost, 1), "dur_s": round(dur, 2) if dur else None,
        "rtf": round(cost / dur, 2) if dur else None,
        "sr": sr, "ch": ch, "size_kb": round(size_kb, 1),
        "json_ok": (payload or {}).get("ok"),
        "err": None if out.exists() else (proc.stderr or proc.stdout)[-300:],
    }


def main():
    rows = []
    for engine, extra in ENGINES:
        print(f"▶ 测试 {engine} ...", flush=True)
        r = run(engine, extra)
        rows.append(r)
        flag = "✅" if r["ok"] else "❌"
        print(f"  {flag} 耗时 {r['cost_s']}s  音频 {r['dur_s']}s  RTF {r['rtf']}  "
              f"{r['sr']}Hz  {r['size_kb']}KB", flush=True)
        if not r["ok"]:
            print(f"     err: {r['err']}", flush=True)
    (HERE / "bench_local4.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n===== 汇总 =====")
    print(f"{'引擎':<12}{'耗时s':>8}{'音频s':>8}{'RTF':>7}{'采样':>8}{'大小KB':>9}")
    for r in rows:
        print(f"{r['engine']:<12}{r['cost_s']:>8}{r['dur_s'] or 0:>8}"
              f"{r['rtf'] or 0:>7}{r['sr'] or 0:>8}{r['size_kb']:>9}")


if __name__ == "__main__":
    main()
