"""VLM 调用层 —— 本地回环，统一封装。

边界（不可放宽）：
- 只连 127.0.0.1 回环，不出网。
- 帧与文本只进本地 VLM，不经聊天模型、不上传。
- 生产时禁止重定向与代理。
"""
import base64
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_cfg() -> dict:
    return json.loads((ROOT / 'config' / 'engine.json').read_text(encoding='utf-8'))


def endpoint() -> str:
    return load_cfg()['runtime']['vlm_endpoint'].rstrip('/')


def health(timeout: int = 8) -> bool:
    try:
        with urllib.request.urlopen(f'{endpoint()}/health', timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _post(payload: dict, timeout: int, endpoint_path: str = '/chat/completions') -> dict:
    body = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        f'{endpoint()}{endpoint_path}', data=body,
        headers={'Content-Type': 'application/json'})
    # 显式禁用代理，保证只走回环
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def ask(prompt: str, images: list | None = None, max_tokens: int = 400,
        temperature: float = 0.3, retries: int = 2, timeout: int = 300,
        frequency_penalty: float = 0.0, presence_penalty: float = 0.0) -> str:
    """向本地 VLM 提问。images 为帧路径列表。

    失败时最多重试 `retries` 次（模型请求/JSON 解析类错误），仍失败则抛异常交编排层处置。

    ★ frequency_penalty / presence_penalty（2026-10-20 实测需要）：
      llama-server 默认 repeat_penalty=1.0，等价于"不惩罚重复"。8B 模型在
      **长规则提示词 + 要输出 20 句结构化长文**时会出现 repetition collapse ——
      实测 21 句稿里有 11 句是逐字重复（同一句"画面字幕写着…"贴了 4 遍）。
      长稿生成需要显式给惩罚；短任务（如单帧画面描述）保持 0，避免影响描述准确性。
    """
    content = []
    for img in (images or []):
        b = base64.b64encode(Path(img).read_bytes()).decode()
        content.append({'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{b}'}})
    content.append({'type': 'text', 'text': prompt})
    payload = {'model': 'local', 'messages': [{'role': 'user', 'content': content}],
               'max_tokens': max_tokens, 'temperature': temperature}
    if frequency_penalty:
        payload['frequency_penalty'] = frequency_penalty
    if presence_penalty:
        payload['presence_penalty'] = presence_penalty

    last = None
    for attempt in range(retries + 1):
        try:
            t0 = time.time()
            d = _post(payload, timeout)
            txt = d['choices'][0]['message']['content']
            return {'text': txt, 'infer_sec': round(time.time() - t0, 2),
                    'attempt': attempt + 1, 'engine': 'qwen3-vl-8b'}.get('text') and txt
        except Exception as e:
            last = f'{type(e).__name__}: {e}'
            if attempt < retries:
                time.sleep(2)
    raise RuntimeError(f'VLM 请求失败（重试 {retries} 次）: {last}')


def ask_json(prompt: str, images: list | None = None, default=None, **kw):
    """要求 VLM 返回 JSON；解析失败重试，最终失败返回 default 并由上层记风险。"""
    for attempt in range(3):
        raw = ask(prompt + '\n只输出合法 JSON，不要任何解释文字。', images=images, **kw)
        m = raw.strip()
        if m.startswith('```'):
            m = m.split('```')[1]
            if m.startswith('json'):
                m = m[4:]
        m = m.strip().rstrip('，。')
        start, end = m.find('{'), m.rfind('}')
        if start >= 0 and end > start:
            try:
                return json.loads(m[start:end + 1])
            except Exception:
                pass
    return default


if __name__ == '__main__':
    cfg = load_cfg()
    ok = health()
    print(json.dumps({'endpoint': endpoint(), 'healthy': ok,
                      'model': cfg['runtime']['vlm_model'],
                      'mmproj_exists': Path(cfg['runtime']['mmproj']).exists()},
                     ensure_ascii=False, indent=2))
