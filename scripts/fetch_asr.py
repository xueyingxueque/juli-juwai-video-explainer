"""断点续传 ASR 模型：验证完整性后才落 model.bin，禁止把不完整下载称为就绪。"""
import hashlib, json, os, sys, time
from pathlib import Path
import requests

name = sys.argv[1] if len(sys.argv) > 1 else 'faster-whisper-medium'
root = Path('D:/AITools/models') / name
root.mkdir(parents=True, exist_ok=True)
target = root / 'model.bin'

small = ['config.json', 'tokenizer.json', 'vocabulary.txt']
# ★ 端点可用 HF_ENDPOINT 覆盖（国内直连 HF 不稳，实测 hf-mirror.com 可达）
ENDPOINT = os.environ.get('HF_ENDPOINT', 'https://huggingface.co').rstrip('/')
print('[meta]', name, '| endpoint:', ENDPOINT, flush=True)
meta = requests.get(f'{ENDPOINT}/api/models/Systran/{name}/tree/main', timeout=30)
meta.raise_for_status()
entry = next(x for x in meta.json() if x['path'] == 'model.bin')
expected, oid = entry['size'], entry['lfs']['oid']
print(f'[expect] {expected} bytes sha256={oid}', flush=True)

for f in small:
    p = root / f
    if not p.exists():
        r = requests.get(f'{ENDPOINT}/Systran/{name}/resolve/main/{f}', timeout=60)
        r.raise_for_status()
        p.write_bytes(r.content)
        print(f'[small] {f}', flush=True)

part = root / 'model.bin.part'
# ★ 端点可用环境变量覆盖（国内直连 HF 不稳，实测 hf-mirror.com 可达）
ENDPOINT = os.environ.get('HF_ENDPOINT', 'https://huggingface.co').rstrip('/')
url = f'{ENDPOINT}/Systran/{name}/resolve/main/model.bin?download=true'
print(f'[endpoint] {ENDPOINT}', flush=True)

# ★★ 真正的断点续传（2026-10-16 重写）
#   旧实现用 `mode='ab'` 追加，但 requests 断线重连后**没有发 Range 头**，
#   服务端从头开始发，客户端继续追加 → 重复写入 58MB，sha256 校验失败。
#   正解：先看本地已有多少字节，显式发 `Range: bytes=<已有>-`，
#   并用 `wb` + `seek()` 定位写入；服务端返回 206 才算续传成功，
#   若返回 200（不支持 Range）则截断重下。
CHUNK = 1024 * 1024
RETRY = 6
done = False
for attempt in range(1, RETRY + 1):
    have = part.stat().st_size if part.exists() else 0
    if have > expected:          # 上次续传写坏了，截断
        print(f'[repair] 本地 {have} > 预期 {expected}，截断重来', flush=True)
        with part.open('r+b') as f:
            f.truncate(0)
        have = 0
    headers = {'Range': f'bytes={have}-'} if have else {}
    try:
        with requests.get(url, headers=headers, stream=True, timeout=(20, 120)) as r:
            r.raise_for_status()
            if have and r.status_code == 206:
                print(f'[resume] 服务端支持 Range，从 {have} 继续', flush=True)
                mode = 'ab'
            elif have:
                print(f'[restart] 服务端不支持 Range（HTTP {r.status_code}），从头重下', flush=True)
                mode = 'wb'
            else:
                mode = 'wb'
            got = have
            last = time.monotonic()
            with part.open(mode) as f:
                for chunk in r.iter_content(CHUNK):
                    if not chunk:
                        continue
                    f.write(chunk)
                    got += len(chunk)
                    if time.monotonic() - last > 10:
                        pct = got * 100 // expected
                        speed = (got - have) / max(time.monotonic() - last, 1) / 1024
                        print(f'[progress] {got}/{expected} ({pct}%) '
                              f'{speed:.0f} KB/s', flush=True)
                        last = time.monotonic()
        if part.stat().st_size == expected:
            done = True
            break
        print(f'[retry] 第 {attempt} 次中断，当前 {part.stat().st_size}/{expected}',
              flush=True)
    except Exception as e:
        print(f'[retry] 第 {attempt} 次异常：{type(e).__name__}: {str(e)[:90]}',
              flush=True)
    time.sleep(min(5 * attempt, 20))

size = part.stat().st_size if part.exists() else 0
if not done or size != expected:
    print(f'[INCOMPLETE] {size}/{expected}，保留 .part 供续传', flush=True)
    sys.exit(2)
print('[verify] 计算 sha256（大文件，约需 1 分钟）...', flush=True)
hh = hashlib.sha256()
with part.open('rb') as f:
    for b in iter(lambda: f.read(4 * 1024 * 1024), b''):
        hh.update(b)
if hh.hexdigest() != oid:
    print('[MISMATCH] 校验失败，删除 .part', flush=True)
    part.unlink()
    sys.exit(3)
part.replace(target)
(root / 'download-verification.json').write_text(json.dumps(
    {'bytes': size, 'sha256': hh.hexdigest(), 'official_lfs_oid': oid, 'verified': True}, indent=2), encoding='utf-8')
print('[OK] MODEL_VERIFIED', size, flush=True)
