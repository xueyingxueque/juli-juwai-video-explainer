# -*- coding: utf-8 -*-
"""Breeze TTS 2 调用脚本（本地配音工坊 breeze 引擎 worker 雏形）

走内置 breeze_infer.api 的 FastAPI 服务（需先起服务），返回 24kHz mono s16le PCM，本脚本转 WAV。

用法：
  # ① Voice Design（无参考音，自然语言描述音色）
  python breeze_test.py --text-file 文案.txt --instruction "一位温柔自信的年轻女性..." --out out.wav
  # ② Voice Clone（参考音 + 精确转写，两者必须成对）
  python breeze_test.py --text-file 文案.txt --ref-audio 小欧.wav --ref-text "参考音的准确文字稿" --out out.wav
  # ③ Voice Direction（克隆 + 指令调节语气/情感/节奏）
  python breeze_test.py --text-file 文案.txt --ref-audio 小欧.wav --ref-text "..." --instruction "语速放慢，语气严肃" --out out.wav
"""
import argparse
import time
import urllib.error
import urllib.request
import uuid
import wave
from pathlib import Path

HOST = "http://127.0.0.1:7860"
SR = 24000  # Breeze TTS 2 输出采样率（mono, s16le）


def post_speech(fields: dict, files: dict | None = None, timeout: int = 900) -> bytes:
    """POST /v1/audio/speech，返回裸 PCM 字节。multipart 手工构造，零第三方依赖。"""
    boundary = uuid.uuid4().hex
    body = b""
    for k, v in fields.items():
        body += (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'
        ).encode("utf-8")
    for k, path in (files or {}).items():
        data = Path(path).read_bytes()
        fname = Path(path).name.encode("utf-8").decode("latin-1", "replace")
        body += (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; '
            f'filename="{fname}"\r\nContent-Type: application/octet-stream\r\n\r\n'
        ).encode("utf-8") + data + b"\r\n"
    body += f"--{boundary}--\r\n".encode("utf-8")

    req = urllib.request.Request(
        HOST + "/v1/audio/speech",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        raise SystemExit(f"❌ HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:500]}")


def pcm_to_wav(pcm: bytes, out: str, sr: int = SR) -> float:
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)
    return len(pcm) / 2 / sr


def health() -> dict:
    with urllib.request.urlopen(HOST + "/health", timeout=10) as r:
        import json
        return json.loads(r.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser(description="Breeze TTS 2 推理调用")
    ap.add_argument("--text", help="直接给文本")
    ap.add_argument("--text-file", help="长文本走文件")
    ap.add_argument("--instruction", default="Speak clearly and naturally.",
                    help="自然语言指令（voice design / direction）")
    ap.add_argument("--ref-audio", help="参考音频（voice clone / direction）")
    ap.add_argument("--ref-text", default="", help="参考音频精确转写（与 ref-audio 成对）")
    ap.add_argument("--ref-text-file", help="转写走文件（推荐，避免命令行中文编码问题）")
    ap.add_argument("--cfg-scale", type=float, default=4.0, help="指令跟随强度，官方推荐 4")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    if a.text_file:
        text = Path(a.text_file).read_text(encoding="utf-8").strip()
    elif a.text:
        text = a.text
    else:
        raise SystemExit("❌ 需要 --text 或 --text-file")

    ref_text = a.ref_text
    if a.ref_text_file:
        ref_text = Path(a.ref_text_file).read_text(encoding="utf-8").strip()

    fields = {
        "text": text,
        "instruction": a.instruction,
        "cfg_scale": str(a.cfg_scale),
        "seed": str(a.seed),
        "ref_text": ref_text,
    }
    files = {}
    if a.ref_audio:
        files["ref_audio"] = a.ref_audio
        if not ref_text:
            raise SystemExit("❌ voice clone 模式必须同时给 --ref-text / --ref-text-file")

    mode = "voice-clone" if a.ref_audio else "voice-design"
    print(f"[Breeze] mode={mode}  文本 {len(text)} 字  指令={a.instruction[:40]}...")
    t0 = time.time()
    pcm = post_speech(fields, files)
    dur = pcm_to_wav(pcm, a.out)
    print(f"✅ 合成成功：{a.out}  {dur:.2f}s  耗时 {time.time()-t0:.1f}s  "
          f"(RTF {(time.time()-t0)/dur:.2f})")


if __name__ == "__main__":
    main()
