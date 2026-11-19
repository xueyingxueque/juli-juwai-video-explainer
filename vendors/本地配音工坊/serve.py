#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
本地配音工坊 · 持久化 TTS 服务（运行加速核心）
============================================
痛点：dub.py 每合成一次就起一个子进程，重新 import torch + 重新加载模型
（IndexTTS2 约 90s），绝大多数时间花在"加载"而非"合成"。

本服务只加载一次模型，之后在同一个进程内复用，处理多条合成请求：
  - 由 dub.py 用引擎自带 Python 拉起（必须，因为 torch/cuda 在引擎绿色包里）
  - 监听 127.0.0.1:<port> 的 HTTP POST /synthesize
  - 请求体(JSON)：{ "text", "ref", "out", <引擎参数...> }
  - 按"加载期参数"(device/fp16/model/precision/emotion…)做模型缓存，
    不同加载参数自动加载不同模型并复用（如 PilotTTS 情感/非情感两套权重）

用法（由 dub.py 自动管理，一般无需手动）：
  <engine_python> serve.py --engine-root <root> --worker workers.worker_indextts2 \
      --engine-id indextts2 --port 8731 [--device cuda] [--fp16] ...
"""
from __future__ import annotations

import argparse
import copy
import importlib
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
if SKILL_DIR not in sys.path:
    sys.path.insert(0, SKILL_DIR)

# ── 离线 + 清代理 + 显存碎片优化（减少 OOM / 提升稳定性）──
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "max_split_size_mb:32")
for k in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE"):
    os.environ.setdefault(k, "1")
for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(k, None)

# 每个引擎的"加载期参数"：这些参数变化时需要重新加载模型。
# 其余参数（text/ref/out/temperature/num_steps…）是每次请求级别的，不触发重载。
LOAD_TIME_KEYS = {
    "indextts2": ["device", "fp16", "engine_root"],
    "chatterbox": ["device", "engine_root", "model"],
    "dotstts": ["device", "engine_root", "model", "precision", "max_len"],
    "pilottts": ["device", "engine_root", "emotion"],
}
# 永远固定、不允许请求覆盖的键（避免把模型加载到错误设备/目录）
FIXED_KEYS = {"engine_root", "device"}
# A13：模型缓存上限。12G 显存上 pilottts 双权重 / dotstts 切 soar,mf 会堆积，
# 超过上限挤出最久未用的模型并 best-effort 清显存。
MAX_CACHED_MODELS = 2


def _server_argparser():
    ap = argparse.ArgumentParser(description="TTS 持久化服务")
    ap.add_argument("--engine-root", required=True)
    ap.add_argument("--worker", required=True, help="worker 模块，如 workers.worker_indextts2")
    ap.add_argument("--engine-id", required=True, help="indextts2/dotstts/pilottts/chatterbox")
    ap.add_argument("--port", type=int, required=True)
    # 加载期参数（可选，用于预加载）
    ap.add_argument("--device", default=None)
    ap.add_argument("--fp16", action="store_true")
    ap.add_argument("--model", default=None)
    ap.add_argument("--precision", default="bfloat16")
    ap.add_argument("--max-len", type=int, default=500)
    ap.add_argument("--emotion", default=None)
    return ap


def make_base_args(wmod, server_args):
    """用 worker 自带 parser 构造一份"默认参数 + 服务启动时的加载期覆盖"。

    注意：各 worker 的 build_argparser 把若干参数标为 required
    （--engine-root/--out，dotstts/pilottts 还有 --ref/--text），
    这里用最小占位的 argv 取出官方默认值；真实值由每次请求的 payload 覆盖，
    因此占位值不会被实际合成用到。"""
    parser = wmod.build_argparser()
    cache = os.path.join(SKILL_DIR, ".cache")
    os.makedirs(cache, exist_ok=True)
    placeholder = os.path.join(cache, "serve_base_placeholder.wav")
    argv = [
        "--engine-root", server_args.engine_root,
        "--out", placeholder,
        "--ref", placeholder,
        "--text", " ",
    ]
    base = parser.parse_args(argv)
    # 把服务启动参数写进去（engine_root 必填，其余仅覆盖非 None）
    base.engine_root = server_args.engine_root
    for k in ("device", "fp16", "model", "precision", "max_len", "emotion"):
        v = getattr(server_args, k, None)
        if v is not None:
            setattr(base, k, v)
    return base


class TTSHandler(BaseHTTPRequestHandler):
    wmod = None
    base = None
    engine_id = None
    model_cache = {}  # load_time_tuple -> model_context
    cache_lock = None

    def _log(self, msg):
        sys.stdout.write(msg + "\n")
        sys.stdout.flush()

    def do_POST(self):
        if self.path != "/synthesize":
            self._send(404, {"ok": False, "error": f"未知路径: {self.path}"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw.decode("utf-8") or "{}")
        except Exception as e:
            self._send(400, {"ok": False, "error": f"请求体解析失败: {e}"})
            return

        try:
            result = self._handle(payload)
            self._send(200, result)
        except BaseException as e:  # 捕获 SystemExit / 异常，保证服务不崩
            self._send(200, {"ok": False, "error": f"合成失败: {e}"})

    def _handle(self, payload):
        # 构造本次请求的参数命名空间：拷贝 base，套用请求里的非 None 覆盖
        req = copy.copy(self.base)
        for k, v in payload.items():
            if v is None:
                continue
            if k in FIXED_KEYS:
                continue  # engine_root/device 永远用 base 的
            if not hasattr(req, k):
                # 未知键（如 engine 派生字段）忽略，避免 argparse 缺属性报错
                continue
            setattr(req, k, v)

        # 计算加载期参数元组 → 命中模型缓存则复用，否则加载并缓存
        lt_keys = LOAD_TIME_KEYS.get(self.engine_id, ["engine_root"])
        lt_tuple = tuple(getattr(req, k, None) for k in lt_keys)
        with self.cache_lock:
            ctx = self.model_cache.get(lt_tuple)
            if ctx is None:
                self._log(f"[serve] 加载模型（缓存键={lt_tuple}）…")
                ctx = self.wmod.load_model(req)
                self.model_cache[lt_tuple] = ctx
                self._log(f"[serve] 模型已加载，当前缓存 {len(self.model_cache)} 个")
                self._evict_if_needed()

            ok = self.wmod.synthesize(ctx, req)
            out = payload.get("out")
            if ok and out and os.path.isfile(out) and os.path.getsize(out) > 0:
                return {"ok": True, "out": os.path.abspath(out),
                        "size_kb": round(os.path.getsize(out) / 1024, 1)}
            return {"ok": False, "error": "合成未产出有效音频", "out": out}

    def _evict_if_needed(self):
        """LRU 淘汰：model_cache 按插入顺序保序，弹出最旧的并尝试清显存。"""
        while len(self.model_cache) > MAX_CACHED_MODELS:
            old_key = next(iter(self.model_cache))
            old_ctx = self.model_cache.pop(old_key)
            del old_ctx
            import gc
            gc.collect()
            try:
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass
            self._log(f"[serve] 缓存超上限，淘汰旧模型 {old_key}（LRU={MAX_CACHED_MODELS}）")

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass  # 静默默认访问日志


def main():
    args = _server_argparser().parse_args()
    wmod = importlib.import_module(args.worker)

    TTSHandler.wmod = wmod
    TTSHandler.base = make_base_args(wmod, args)
    TTSHandler.engine_id = args.engine_id
    TTSHandler.model_cache = {}
    import threading
    TTSHandler.cache_lock = threading.Lock()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), TTSHandler)
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps({"ok": True, "event": "server_started",
                      "engine": args.engine_id, "port": args.port}))
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
