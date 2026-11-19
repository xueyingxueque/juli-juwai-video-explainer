"""生成 ASS 并烧字幕 —— 独立验证脚本（诊断用）。

★ 为什么用 ASS 而不是 SRT + force_style：
  SRT 没有 PlayResX/PlayResY，`BorderStyle=3`（整行底栏）
  在 libass 下渲染不出来 —— ffmpeg 不报错，但画面上一点字幕都没有。
  ASS 可以显式声明画布尺寸，底栏样式才生效。
"""
import subprocess
import sys
import tempfile
from pathlib import Path

FF = r'D:\AITools\TTS\Dots.tts\wzf\ffmpeg\bin\ffmpeg.exe'


def parse_srt(txt):
    """解析 SRT → [(start, end, text)]"""
    items = []
    cur = None
    for ln in txt.splitlines():
        s = ln.strip()
        if not s:
            continue
        if '-->' in s:
            if cur:
                items.append(cur)
            a, b = [x.strip() for x in s.split('-->')]
            cur = [a, b, []]
        elif cur is not None and not s.isdigit():
            cur[2].append(s)
    if cur:
        items.append(cur)
    return items


def to_ass_time(t):
    """00:00:02,770 → 0:00:02.77"""
    h, m, rest = t.split(':')
    s, ms = rest.split(',')
    return '{}:{}:{:02d}.{:02d}'.format(int(h), int(m), int(s), int(ms) // 10)


HEAD = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Nar,Microsoft YaHei,52,&H00FFFFFF,&H00FFFFFF,&H00000000,&HC0000000,-1,0,0,0,100,100,1,0,3,2,0,2,60,60,300,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

TMPL = "Dialogue: 0,{a},{b},Nar,,0,0,0,,{tx}\n"


def build_ass(srt_path: Path) -> Path:
    raw = srt_path.read_bytes()
    if raw[:3] == bytes([0xEF, 0xBB, 0xBF]):
        raw = raw[3:]
    items = parse_srt(raw.decode('utf-8'))
    body = ''.join(TMPL.format(a=to_ass_time(a), b=to_ass_time(b),
                               tx=' '.join(tx)) for a, b, tx in items)
    out = Path(tempfile.gettempdir()) / 'wb.ass'
    out.write_text(HEAD + body, encoding='utf-8')
    print('ASS 已生成:', out, '| 字幕条数:', len(items))
    return out


def burn(video: Path, ass: Path, out: Path):
    sp = str(ass).replace(chr(92), '/').replace(':', chr(92) + ':')
    vf = "ass=filename='" + sp + "'"
    r = subprocess.run(
        [FF, '-v', 'error', '-y', '-i', str(video), '-vf', vf,
         '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '22',
         '-c:a', 'copy', '-movflags', '+faststart', str(out)],
        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=1800)
    if r.returncode != 0:
        print('烧录失败:', r.stderr[-300:])
        return False
    print('烧录完成:', out, f'{out.stat().st_size // 1024}KB')
    return True


if __name__ == '__main__':
    src = Path(sys.argv[1])
    srt = Path(sys.argv[2])
    out = Path(sys.argv[3])
    if not burn(src, build_ass(srt), out):
        sys.exit(1)
