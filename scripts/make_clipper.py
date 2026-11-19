# -*- coding: utf-8 -*-
r"""make_clipper.py —— 生成「边播边剪」HTML 镜头工作台（v3.6.2 新增工具层）。

用途：替代手工看视频记时间点——本地 HTML 里放播放器+抽帧网格+记镜头面板，
点「记下当前点」自动抓 video.currentTime，导出直接入 shot_evidence.py 的
spec JSON（shots: [{ep, t, raw, text, block}]）。

用法：
    python make_clipper.py <剧名> <集号> <原片.mp4> <_s1/epN 目录> [--out x.html]
数据全部内联（file:// 下 fetch 本地 json 被 CORS 拦，必须内联）；
视频/帧用 file:/// 绝对路径直接引用（video/img src 不受 file:// CORS 限制）。
"""
import json
import sys
from pathlib import Path

BLOCKS = ['hook', 'setup', 'rising', 'core', 'twist', 'climax', 'ending', 'cta']

HTML = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>__TITLE__ · 镜头工作台</title>
<style>
  :root { --bg:#141618; --panel:#1d2024; --line:#2c3036; --txt:#e8e6e1; --dim:#9a968f;
          --acc:#c0392b; --acc2:#b33a2e; --ok:#4a8; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:var(--bg); color:var(--txt); font:14px/1.6 "Microsoft YaHei",sans-serif;
         display:flex; flex-direction:column; height:100vh; }
  header { padding:10px 16px; background:var(--panel); border-bottom:1px solid var(--line);
           display:flex; align-items:center; gap:14px; }
  header h1 { font-size:16px; font-weight:600; }
  header .meta { color:var(--dim); font-size:12px; }
  main { flex:1; display:flex; min-height:0; }
  /* 左：播放器 */
  #left { flex:1.25; display:flex; flex-direction:column; border-right:1px solid var(--line); min-width:0; }
  video { width:100%; flex:1; background:#000; object-fit:contain; min-height:0; }
  #ctrl { padding:8px 12px; background:var(--panel); display:flex; flex-wrap:wrap; gap:8px; align-items:center; }
  button { background:#2a2e34; color:var(--txt); border:1px solid var(--line); border-radius:6px;
           padding:5px 12px; cursor:pointer; font-size:13px; }
  button:hover { background:#34383f; }
  button.primary { background:var(--acc); border-color:var(--acc); font-weight:600; }
  button.primary:hover { background:var(--acc2); }
  #tcode { font:600 22px/1 Consolas,monospace; color:#fff; background:#000;
           padding:4px 12px; border-radius:6px; min-width:110px; text-align:center; }
  .hint { color:var(--dim); font-size:11px; }
  /* 右栏 */
  #right { flex:1; display:flex; flex-direction:column; min-width:360px; }
  #rec { padding:12px; border-bottom:1px solid var(--line); background:var(--panel); }
  #rec h2 { font-size:13px; color:var(--dim); margin-bottom:8px; font-weight:600; }
  .row { display:flex; gap:8px; margin-bottom:8px; align-items:center; }
  .row label { color:var(--dim); font-size:12px; width:34px; }
  input[type=number], input[type=text], select { background:#141618; color:var(--txt);
      border:1px solid var(--line); border-radius:5px; padding:5px 8px; font-size:13px; }
  input[type=number] { width:90px; } input[type=text] { flex:1; }
  #shotlist { flex:1; overflow-y:auto; padding:8px 12px; }
  .shot { background:var(--panel); border:1px solid var(--line); border-radius:8px;
          padding:8px 10px; margin-bottom:8px; }
  .shot .st { display:flex; gap:8px; align-items:baseline; }
  .shot .tt { font:600 15px/1 Consolas,monospace; color:#fff; }
  .shot .blk { font-size:11px; color:var(--bg); background:var(--dim); border-radius:4px; padding:1px 6px; }
  .shot .blk.raw { background:#7a6; } .shot .blk.hook { background:#e6a23c; }
  .shot .blk.climax { background:var(--acc); } .shot .blk.cta { background:#8e6; }
  .shot .tx { margin-top:4px; color:var(--txt); }
  .shot .note { color:var(--dim); font-size:12px; }
  .shot .ops { margin-top:6px; display:flex; gap:6px; }
  .shot .ops button { font-size:11px; padding:2px 8px; }
  #frames { height:238px; overflow-y:auto; border-top:1px solid var(--line);
            display:grid; grid-template-columns:repeat(auto-fill,minmax(120px,1fr)); gap:6px; padding:8px; }
  #frames img { width:100%; border:2px solid transparent; border-radius:5px; cursor:pointer; display:block; }
  #frames img:hover { border-color:var(--acc); }
  #frames .cap { font-size:10px; color:var(--dim); text-align:center; margin-top:-4px; }
  #subs { max-height:150px; overflow-y:auto; padding:10px 16px; background:#101214;
          border-top:1px solid var(--line); color:#cfcbc4; font-size:12.5px; white-space:pre-wrap; }
  #subs summary { cursor:pointer; color:var(--dim); }
  #toast { position:fixed; left:50%; bottom:26px; transform:translateX(-50%); background:#000d;
           padding:8px 18px; border-radius:8px; display:none; pointer-events:none; }
</style>
</head>
<body>
<header>
  <h1>__TITLE__</h1>
  <span class="meta">ep__EP__ · 镜头工作台 · 空格播放/暂停 · ←→±1s · Shift+←→±5s · Enter 记点</span>
  <span style="flex:1"></span>
  <button class="primary" onclick="exportJson()">导出 spec JSON</button>
  <button onclick="copyJson()">复制</button>
</header>
<main>
  <div id="left">
    <video id="v" src="__VIDEO__" controlslist="nodownload"></video>
    <div id="ctrl">
      <button onclick="jog(-5)">⏮ -5s</button>
      <button onclick="jog(-1)">-1s</button>
      <span id="tcode">0.00</span>
      <button onclick="jog(1)">+1s</button>
      <button onclick="jog(5)">+5s</button>
      <select id="rate" onchange="v.playbackRate=parseFloat(this.value)">
        <option value="0.5">0.5x</option><option value="1" selected>1x</option>
        <option value="1.5">1.5x</option><option value="2">2x</option>
      </select>
      <button onclick="v.muted=!v.muted">静音/开声</button>
      <span class="hint" id="dur"></span>
    </div>
  </div>
  <div id="right">
    <div id="rec">
      <h2>记镜头（t 自动取当前播放位置）</h2>
      <div class="row"><label>t</label>
        <input type="number" id="f_t" step="0.01" min="0">
        <label style="width:52px">block</label>
        <select id="f_blk">__BLOCK_OPTS__</select>
      </div>
      <div class="row"><label>解说</label>
        <input type="text" id="f_tx" placeholder="这句解说词（原声段留空）">
      </div>
      <div class="row"><label>备注</label>
        <input type="text" id="f_note" placeholder="画面要点/情绪（可选）">
        <label style="width:34px"><input type="checkbox" id="f_raw"> 原声</label>
      </div>
      <div class="row">
        <button class="primary" onclick="addShot()">＋ 记下这个点 (Enter)</button>
        <span class="hint" id="cnt">已记 0 个镜头</span>
      </div>
    </div>
    <div id="shotlist"></div>
    <div id="frames"></div>
  </div>
</main>
<details id="subsWrap" open><summary>本集全文字幕（ASR，供对剧情用）</summary>
<div id="subs">__SUBS__</div></details>
<div id="toast"></div>
<script>
const EP = __EP__;
const v = document.getElementById('v');
let shots = [];
const BLK = __BLOCKS_JSON__;
v.addEventListener('timeupdate', () => {
  document.getElementById('tcode').textContent = v.currentTime.toFixed(2);
});
v.addEventListener('loadedmetadata', () => {
  document.getElementById('dur').textContent = '总长 ' + v.duration.toFixed(1) + 's';
});
function jog(s) { v.currentTime = Math.max(0, Math.min(v.duration || 1e9, v.currentTime + s)); }
document.addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
  if (e.code === 'Space') { e.preventDefault(); v.paused ? v.play() : v.pause(); }
  else if (e.code === 'ArrowLeft') { e.preventDefault(); jog(e.shiftKey ? -5 : -1); }
  else if (e.code === 'ArrowRight') { e.preventDefault(); jog(e.shiftKey ? 5 : 1); }
  else if (e.code === 'Enter') { e.preventDefault(); addShot(); }
});
function addShot() {
  const t = parseFloat(document.getElementById('f_t').value);
  if (!(t >= 0)) { toast('先填时间 t（或直接按 Enter 取当前播放位置）'); return; }
  shots.push({ ep: EP, t: +t.toFixed(2),
               raw: document.getElementById('f_raw').checked ? 1 : 0,
               text: document.getElementById('f_tx').value.trim(),
               block: document.getElementById('f_blk').value,
               note: document.getElementById('f_note').value.trim() });
  document.getElementById('f_tx').value = ''; document.getElementById('f_note').value = '';
  document.getElementById('f_raw').checked = false;
  render(); toast('已记 ' + t.toFixed(2) + 's');
}
function del(i) { shots.splice(i, 1); render(); }
function move(i, d) { const j = i + d; if (j < 0 || j >= shots.length) return;
  [shots[i], shots[j]] = [shots[j], shots[i]]; render(); }
function seekTo(t) { v.currentTime = t; }
function render() {
  const box = document.getElementById('shotlist');
  box.innerHTML = shots.map((s, i) =>
    `<div class="shot"><div class="st">
       <span class="tt" onclick="seekTo(${s.t})" style="cursor:pointer" title="跳转到此">${s.t.toFixed(2)}s</span>
       <span class="blk ${s.block}">${s.block}</span>
       ${s.raw ? '<span class="blk raw">原声</span>' : ''}</div>
     <div class="tx">${s.text ? s.text : '<span style="color:var(--dim)">（原声段，无解说）</span>'}</div>
     ${s.note ? `<div class="note">${s.note}</div>` : ''}
     <div class="ops">
       <button onclick="move(${i},-1)">上移</button><button onclick="move(${i},1)">下移</button>
       <button onclick="del(${i})">删除</button></div></div>`).join('');
  document.getElementById('cnt').textContent = `已记 ${shots.length} 个镜头`;
}
function specJson() {
  return JSON.stringify({ shots: shots }, null, 1);
}
function exportJson() {
  if (!shots.length) { toast('还没记任何镜头'); return; }
  const blob = new Blob([specJson()], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `spec-ep${EP}.json`;
  a.click(); URL.revokeObjectURL(a.href);
  toast('已导出 spec-ep' + EP + '.json（' + shots.length + ' 镜头）');
}
function copyJson() {
  navigator.clipboard.writeText(specJson()).then(() => toast('JSON 已复制到剪贴板'));
}
let tt;
function toast(m) { const t = document.getElementById('toast');
  t.textContent = m; t.style.display = 'block';
  clearTimeout(tt); tt = setTimeout(() => t.style.display = 'none', 1800); }
v.addEventListener('timeupdate', () => { document.getElementById('f_t').value = v.currentTime.toFixed(2); });
// 抽帧网格（点击跳转）
const FR = __FRAMES_JSON__;
const fbox = document.getElementById('frames');
FR.forEach(f => {
  const img = document.createElement('img');
  img.src = f.path;   // python 侧已 as_uri() 预编码（含 file:/// 前缀）
  img.title = f.t.toFixed(1) + 's';
  img.onclick = () => { seekTo(f.t); };
  const cap = document.createElement('div'); cap.className = 'cap'; cap.textContent = f.t.toFixed(0) + 's';
  const wrap = document.createElement('div');
  wrap.appendChild(img); wrap.appendChild(cap); fbox.appendChild(wrap);
});
</script>
</body>
</html>
'''


def main() -> int:
    argv = [a for a in sys.argv[1:]]
    out = None
    if '--out' in argv:
        i = argv.index('--out')
        out = Path(argv[i + 1])
        del argv[i:i + 2]
    if len(argv) < 4:
        print(__doc__)
        return 1
    series, ep, video, s1dir = argv[0], int(argv[1]), argv[2], argv[3]
    video_p, s1 = Path(video), Path(s1dir)
    out = out or Path(f'clipper_ep{ep}.html')
    asr = json.loads((s1 / 'asr_full.json').read_text(encoding='utf-8'))
    ftimes = json.loads((s1 / 'frame_times.json').read_text(encoding='utf-8'))
    if isinstance(ftimes, dict):
        ftimes = ftimes.get('frames') or ftimes.get('times') or []

    frames = []
    fdir = s1 / 'frames'
    for f in ftimes:
        if isinstance(f, dict):
            fname, t = f.get('frame'), float(f.get('t', 0))
        else:
            fname, t = None, float(f)
        cand = (fdir / fname) if fname else None
        if cand is None or not cand.is_file():
            # 兜底：按序号式命名 f%04d.jpg 找最近帧
            idx = len(frames)
            for k in range(idx, -1, -1):
                c2 = fdir / f'f{k:04d}.jpg'
                if c2.is_file():
                    cand = c2
                    break
        if cand is not None and cand.is_file():
            # ★ as_uri() 预编码中文路径（JS 手拼 file:/// + 未编码中文在浏览器里
            #   加载失败；视频侧同理，统一 python 侧编码）
            frames.append({'t': t, 'path': cand.as_uri()})

    html = (HTML
            .replace('__TITLE__', f'{series} · 第{ep}集')
            .replace('__EP__', str(ep))
            .replace('__VIDEO__', video_p.as_uri())
            .replace('__SUBS__', asr.get('text', '').replace('<', '&lt;'))
            .replace('__BLOCK_OPTS__', ''.join(
                f'<option value="{b}"{" selected" if b == "core" else ""}>{b}</option>'
                for b in BLOCKS))
            .replace('__BLOCKS_JSON__', json.dumps(BLOCKS))
            .replace('__FRAMES_JSON__', json.dumps(frames)))
    out.write_text(html, encoding='utf-8')
    print(f'工作台 → {out}')
    print(f'  视频 {video_p.name}（{asr.get("duration_sec", 0):.0f}s）· '
          f'抽帧 {len(frames)} 张 · 字幕 {len(asr.get("text", ""))} 字')
    print(f'  浏览器打开后：播放→Enter 记点→右上「导出 spec JSON」→ '
          f'shot_evidence.py spec-ep{ep}.json')
    return 0


if __name__ == '__main__':
    sys.exit(main())
