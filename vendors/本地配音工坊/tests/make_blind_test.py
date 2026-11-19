# -*- coding: utf-8 -*-
"""
本地配音工坊 · 生成「人耳盲测打分」HTML
=====================================
客观指标（CER/音色相似度/带宽/基频）机器能算，但**自然度、机械感、好不好听**只能人耳听。
本脚本把 score.json + 音频目录打成一个双击即开的盲测页：
  - 随机编号 A/B/C…，隐藏引擎名（避免品牌偏见），可反复洗牌
  - 参考音固定置顶且标注为非盲（用于「像不像」判断）
  - 四维 1~5 分 + 采用意愿 + 备注
  - 「揭晓」后显示引擎名与机器客观分对照
  - 「导出 JSON」下载结果，交给 Agent 回填到 音色说明.md

用法：
  python tests/make_blind_test.py --dir outputs/bench_20260904 --ref refs/小欧_24k.wav
产出：<dir>/盲测打分.html（自包含，无外部依赖，双击即开）
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

TPL = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>TTS 人耳盲测打分</title>
<style>
  :root{
    --bg:#f6f7f9; --card:#fff; --line:#e3e6ea; --txt:#1f2328; --dim:#6b7280;
    --accent:#2563eb; --accent-d:#1d4ed8; --ok:#16a34a; --warn:#d97706; --bad:#dc2626;
  }
  *{box-sizing:border-box}
  body{margin:0;padding:28px 20px 60px;background:var(--bg);color:var(--txt);
       font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;}
  .wrap{max-width:1080px;margin:0 auto}
  h1{font-size:20px;margin:0 0 4px}
  .sub{color:var(--dim);font-size:13px;margin-bottom:20px}
  .bar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:16px 0}
  button{background:var(--accent);color:#fff;border:0;border-radius:6px;padding:8px 14px;
         font-size:13px;cursor:pointer}
  button:hover{background:var(--accent-d)}
  button.ghost{background:#fff;color:var(--txt);border:1px solid var(--line)}
  button.ghost:hover{background:#f0f2f5}
  .ref{background:#fffbea;border:1px solid #fde68a;border-radius:8px;padding:12px 14px;margin-bottom:18px}
  .ref b{color:#92400e}
  .card{background:var(--card);border:1px solid var(--line);border-radius:10px;
        padding:14px 16px;margin-bottom:12px}
  .card.done{border-color:#86efac;box-shadow:0 0 0 2px #dcfce7 inset}
  .head{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
  .lab{font-size:22px;font-weight:700;width:34px;height:34px;line-height:34px;text-align:center;
       border-radius:8px;background:#eef2ff;color:var(--accent);flex:none}
  .name{font-weight:600}
  .revealed{color:var(--dim);font-size:12px;margin-left:auto}
  audio{width:100%;margin:10px 0 2px}
  .dims{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:10px;margin-top:10px}
  .dim{border:1px solid var(--line);border-radius:8px;padding:8px 10px}
  .dim .t{font-size:12px;color:var(--dim);margin-bottom:4px}
  .stars{display:flex;gap:4px}
  .stars button{background:#f3f4f6;color:#374151;border:1px solid var(--line);
                width:30px;height:28px;padding:0;border-radius:5px;font-size:13px}
  .stars button.on{background:var(--accent);color:#fff;border-color:var(--accent)}
  .foot{display:flex;align-items:center;gap:14px;margin-top:10px;flex-wrap:wrap}
  .foot label{font-size:13px;display:flex;align-items:center;gap:5px;cursor:pointer}
  input[type=text]{flex:1;min-width:180px;border:1px solid var(--line);border-radius:6px;
                   padding:6px 9px;font-size:13px}
  table{width:100%;border-collapse:collapse;background:#fff;border:1px solid var(--line);
        border-radius:8px;overflow:hidden;margin-top:10px;font-size:13px}
  th,td{padding:7px 9px;border-bottom:1px solid var(--line);text-align:center;white-space:nowrap}
  th{background:#f9fafb;font-weight:600}
  td.l{text-align:left}
  .hide{display:none}
  .tip{font-size:12px;color:var(--dim);margin-top:6px}
  .score{font-weight:700}
  .s-good{color:var(--ok)} .s-mid{color:var(--warn)} .s-bad{color:var(--bad)}
</style>
</head>
<body><div class="wrap">
<h1>TTS 人耳盲测打分</h1>
<div class="sub">随机编号 · 隐藏引擎名 · 听完打分 → 导出 JSON 回填技能文档。同文案 __NCHARS__ 字，建议戴耳机。</div>

<div class="ref">
  <b>参考音（非盲，用于判断「像不像」）：</b> __REFNAME__
  <audio controls src="__REF__"></audio>
</div>

<div class="bar">
  <button onclick="shuffle()">重新洗牌</button>
  <button class="ghost" onclick="reveal()">揭晓引擎名与机器分</button>
  <button onclick="exportJson()">导出结果 JSON</button>
  <button class="ghost" onclick="clearAll()">清空评分</button>
  <span class="tip" id="prog"></span>
</div>

<div id="list"></div>

<div id="revealBox" class="hide">
  <h2 style="font-size:16px;margin:22px 0 6px">机器客观分对照（客观指标，非主观音质）</h2>
  <div class="tip" style="margin-bottom:6px">
    CER = ASR 回听字错率（越低越咬字清楚）· sim = ECAPA 音色相似度余弦（越接近 1 越像参考音）·
    bw = 频谱 -60dB 截止频率 · f0σ = 基频起伏（半音标准差，越小越念稿）
  </div>
  <table id="tbl"></table>
</div>

<script>
const DATA = __DATA__;
const DIMS = [
  ["natural","整体自然度（1 机械 → 5 真人）"],
  ["sim","像不像参考音色（1 完全不像 → 5 一模一样）"],
  ["clear","咬字清晰度（1 糊 → 5 字字清楚）"],
  ["artifact","杂音/电音/破音（1 很多 → 5 完全没有）"]
];
let order = DATA.map((_,i)=>i);
let ans = {};

function shuffle(){
  for(let i=order.length-1;i>0;i--){const j=Math.floor(Math.random()*(i+1));[order[i],order[j]]=[order[j],order[i]];}
  render();
}
function render(){
  const box=document.getElementById("list"); box.innerHTML="";
  order.forEach((idx,pos)=>{
    const d=DATA[idx], lab=String.fromCharCode(65+pos), a=ans[idx]||{};
    const el=document.createElement("div");
    el.className="card"+(DIMS.every(x=>a[x[0]])?" done":"");
    el.innerHTML=`
      <div class="head">
        <div class="lab">${lab}</div>
        <div class="name">${d.file}</div>
        <div class="revealed hide" data-rv="${idx}"></div>
      </div>
      <audio controls preload="none" src="${d.file}" onplay="stopOthers(this)"></audio>
      <div class="tip">时长 ${d.duration}s · ${d.sr}Hz</div>
      <div class="dims">${DIMS.map(([k,t])=>`
        <div class="dim"><div class="t">${t}</div><div class="stars" data-k="${k}" data-i="${idx}">
          ${[1,2,3,4,5].map(v=>`<button class="${a[k]==v?'on':''}" onclick="rate(${idx},'${k}',${v})">${v}</button>`).join("")}
        </div></div>`).join("")}
      </div>
      <div class="foot">
        <label><input type="checkbox" ${a.use?"checked":""} onchange="setUse(${idx},this.checked)"> 愿意实际采用</label>
        <input type="text" placeholder="备注（如：尾音发飘、断句奇怪）" value="${a.note||""}" oninput="setNote(${idx},this.value)">
      </div>`;
    box.appendChild(el);
  });
  updateProg(); document.querySelectorAll("[data-rv]").forEach(x=>{ if(revealed) fillRv(x); });
}
function stopOthers(cur){ document.querySelectorAll("audio").forEach(a=>{ if(a!==cur) a.pause(); }); }
function rate(i,k,v){ ans[i]=ans[i]||{}; ans[i][k]=v; render(); }
function setUse(i,v){ ans[i]=ans[i]||{}; ans[i].use=v; render(); }
function setNote(i,v){ ans[i]=ans[i]||{}; ans[i].note=v; }
function updateProg(){
  const n=Object.values(ans).filter(a=>a&&DIMS.every(d=>a[d[0]])).length;
  document.getElementById("prog").textContent=`已评 ${n}/${DATA.length}`;
}
let revealed=false;
function reveal(){
  revealed=true; document.getElementById("revealBox").classList.remove("hide");
  document.querySelectorAll("[data-rv]").forEach(fillRv);
  const rows=DATA.map((d,i)=>{const a=ans[i]||{};
    const avg=DIMS.filter(x=>a[x[0]]).length? (DIMS.reduce((s,x)=>s+(a[x[0]]||0),0)/DIMS.filter(x=>a[x[0]]).length).toFixed(2):"-";
    const cls=d.score>=80?"s-good":(d.score>=65?"s-mid":"s-bad");
    return `<tr><td class="l">${d.file}</td><td>${d.engine||"-"}</td>
      <td class="score ${cls}">${d.score}</td><td>${d.cer}</td><td>${d.sim}</td>
      <td>${d.f_cut_60db}</td><td>${d.f0_std_semitone}</td>
      <td>${(d.kw_miss||[]).join("、")||"-"}</td><td>${avg}</td></tr>`;}).join("");
  document.getElementById("tbl").innerHTML=
    `<tr><th class="l">文件</th><th>引擎</th><th>机器分</th><th>CER↓</th><th>sim↑</th>
     <th>bw Hz↑</th><th>f0σ</th><th>读错的关键词</th><th>你的均分</th></tr>`+rows;
}
function fillRv(el){
  const d=DATA[+el.dataset.rv];
  el.classList.remove("hide");
  el.textContent=`引擎 ${d.engine||"-"} · 机器分 ${d.score}`;
}
function exportJson(){
  const out=DATA.map((d,i)=>{const a=ans[i]||{};return {
    file:d.file, engine:d.engine, machine_score:d.score, cer:d.cer, sim:d.sim,
    human:{natural:a.natural||null, sim:a.sim||null, clear:a.clear||null, artifact:a.artifact||null},
    adopt:!!a.use, note:a.note||""};});
  const blob=new Blob([JSON.stringify({ref:"__REFNAME__",rows:out},null,2)],{type:"application/json"});
  const a=document.createElement("a");
  a.href=URL.createObjectURL(blob);
  a.download="盲测结果_"+new Date().toISOString().slice(0,10).replace(/-/g,"")+".json";
  a.click();
}
function clearAll(){ if(confirm("清空所有评分？")){ans={};render();} }
shuffle();
</script>
</div></body></html>
"""


def guess_engine(fname: str) -> str:
    for k in ("breeze", "chatterbox", "dotstts", "indextts2", "pilottts", "emotiontts"):
        if fname.lower().startswith(k):
            return {"breeze": "Breeze TTS 2", "chatterbox": "Chatterbox-V3",
                    "dotstts": "dots.tts", "indextts2": "IndexTTS2",
                    "pilottts": "PilotTTS", "emotiontts": "EmotionTTS"}[k]
    return "-"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--ref", required=True, help="参考音相对 --dir 的路径")
    ap.add_argument("--score", default=None, help="score.json 路径，默认 <dir>/score.json")
    ap.add_argument("--out", default=None, help="输出 html，默认 <dir>/盲测打分.html")
    args = ap.parse_args()

    d = Path(args.dir)
    sp = Path(args.score) if args.score else d / "score.json"
    data = json.loads(sp.read_text(encoding="utf-8"))
    rows = [r for r in data["rows"] if not r["file"].startswith("smoke")]
    for r in rows:
        r["engine"] = guess_engine(r["file"])

    nchars = len([c for c in data.get("text_gt", "") if "\u4e00" <= c <= "\u9fff"])
    html = (TPL.replace("__DATA__", json.dumps(rows, ensure_ascii=False))
               .replace("__REF__", args.ref)
               .replace("__REFNAME__", Path(args.ref).name)
               .replace("__NCHARS__", str(nchars)))
    out = Path(args.out) if args.out else d / "盲测打分.html"
    out.write_text(html, encoding="utf-8")
    print(f"已生成 {out}（{len(rows)} 个样本，参考音 {args.ref}）")


if __name__ == "__main__":
    main()
