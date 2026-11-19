#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地配音工坊冒烟测试（v0.6.0 重写，替代旧 _verify/test_serve.py）

默认只跑"不加载模型"的快测试（秒级）：
  python tests/test_smoke.py
端到端（真加载模型、真合成，分钟级）需显式：
  python tests/test_smoke.py --e2e --engine indextts2 --voice 谭湘文徐浩伦

与旧版区别：不再硬编码 .workbuddy 的 Python 版本路径，解释器一律用 sys.executable；
技能根按本文件位置自动定位，换目录/换机器都能跑。
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
DUB = SKILL_ROOT / "dub.py"
SERVE = SKILL_ROOT / "serve.py"
passed, failed = [], []


def run(cmd, expect_rc=0, timeout=120):
    p = subprocess.run([sys.executable, "-X", "utf8"] + cmd,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=str(SKILL_ROOT), timeout=timeout)
    return p


def check(name, cond, detail=""):
    (passed if cond else failed).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--e2e", action="store_true")
    ap.add_argument("--engine", default="indextts2")
    ap.add_argument("--voice", default="谭湘文徐浩伦")
    args = ap.parse_args()

    # 1. --help 可解析
    p = run([str(DUB), "--help"])
    check("dub --help rc=0", p.returncode == 0)

    # 2. 引擎清单 JSON 合法且四引擎齐
    p = run([str(DUB), "--list-engines", "--json"])
    ok = False
    try:
        data = json.loads(p.stdout)
        ids = {e["id"] for e in data["engines"]}
        ok = {"indextts2", "dotstts", "pilottts", "chatterbox"} <= ids
    except Exception:
        pass
    check("list-engines 四引擎齐", ok, "" if ok else (p.stdout[-200:] + p.stderr[-200:]))

    # 3. 音色清单 JSON 合法
    p = run([str(DUB), "--list-voices", "--json"])
    ok = False
    try:
        v = json.loads(p.stdout)
        ok = len(v.get("voices", [])) >= 20
    except Exception:
        pass
    check("list-voices ≥20", ok)

    # 4. --health JSON：版本号必须等于 dub.VERSION（A1 回归）
    p = run([str(DUB), "--health", "--json"])
    ok = False
    ver = None
    try:
        h = json.loads(p.stdout)
        ver = h.get("version")
        ns = {}
        src = (SKILL_ROOT / "dub.py").read_text(encoding="utf-8")
        for line in src.splitlines():
            if line.startswith("VERSION ="):
                exec(line, ns)
        ok = ver == ns.get("VERSION")
    except Exception:
        pass
    check("health.version == dub.VERSION", ok, f"(health={ver})")

    # 5. serve.py 语法可导入（不真起服务）
    p = run(["-c", "import py_compile,sys; py_compile.compile(r'%s', doraise=True)" % SERVE])
    check("serve.py 可编译", p.returncode == 0, p.stderr[-200:])

    # 6. 品牌词替换：GPT-4o → 中文读法
    p = run(["-c",
             "import sys; sys.path.insert(0, r'%s'); from lexicon import apply_brands; "
             "print(apply_brands('我用GPT-4o做演示'))" % str(SKILL_ROOT)])
    check("品牌词 GPT-4o 替换", "吉皮提" in p.stdout, p.stdout.strip())

    if args.e2e:
        print("\n--- 端到端（真合成，可能数分钟）---")
        out = SKILL_ROOT / "tests" / "_e2e_out.wav"
        p = run([str(DUB), args.engine, "--voice", args.voice,
                 "--text", "冒烟测试，一二三四五。", "--out", str(out), "--fast"], timeout=600)
        check("e2e 合成出文件", p.returncode == 0 and out.exists() and out.stat().st_size > 1000,
              p.stderr[-300:])
        out.unlink(missing_ok=True)

    print(f"\n结果：{len(passed)} 通过 / {len(failed)} 失败")
    if failed:
        print("失败项：", failed)
        sys.exit(1)


if __name__ == "__main__":
    main()
