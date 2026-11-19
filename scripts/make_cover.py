# -*- coding: utf-8 -*-
r"""封面排版 + 烧录（技能级，2026-10-22 从 yjfxx/sjl 两个任务副本提升）

规则（用户 2026-10-21 定，2026-10-22 固化为技能能力）：
  1. 底图优先级：素材文件夹里的官方海报 > 原片视频帧（文件夹没有图片时才抽帧）；
  2. 右上角：解说者头像（圆形）+ 昵称条；
  3. 烧录：封面压成片前 1.5 秒（默认），编码与成片一致（h264 High/yuv420p/30fps/aac）。

用法：
  # 生成封面（海报底）
  python make_cover.py make --base-image 海报.jpg --avatar avatar.png --nick 囡囡解说 \
      --main 白捡了个福星闺女 --sub 被全村嘲笑命硬克子的渔夫，这一局翻了身 \
      --lead 命硬克子的渔夫 --badge 第1集 --out cover_D1.jpg
  # 生成封面（视频帧底，文件夹无图时）
  python make_cover.py make --base-video 原片.mp4 --frame-t 61.16 ...同上...
  # 烧录进成片
  python make_cover.py burn --video D1.mp4 --cover cover_D1.jpg --out D1_封面版.mp4

依赖：Pillow + ffmpeg（config/engine.json 的 runtime.ffmpeg）。
注意：本文件 docstring 以 r 开头——Windows 路径含反斜杠时必须 raw string（2026-08-12 踩坑教训）。
"""
import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / 'scripts'))
import size_budget  # noqa: E402  体积预算唯一真相源（与 build.py 共用）
CFG = json.loads((SKILL / 'config' / 'engine.json').read_text(encoding='utf-8'))
FF = CFG['runtime']['ffmpeg']
FFPROBE = CFG['runtime']['ffprobe']

W, H = 1080, 1920
FBD = 'C:/Windows/Fonts/msyhbd.ttc'
FRG = 'C:/Windows/Fonts/msyh.ttc'
GOLD = (232, 196, 128)
WARM = (240, 235, 225)
CINNABAR = (178, 58, 46)


def ft(path, sz):
    return ImageFont.truetype(path, sz)


def _cover_crop(im: Image.Image) -> Image.Image:
    """任意尺寸图 → cover 模式裁满 1080x1920（居中裁剪）+ 锐化"""
    sw, sh = im.size
    scale = max(W / sw, H / sh)
    im = im.resize((int(sw * scale) + 1, int(sh * scale) + 1), Image.LANCZOS)
    x = (im.width - W) // 2
    y = (im.height - H) // 2
    return im.crop((x, y, x + W, y + H)).filter(
        ImageFilter.UnsharpMask(radius=2, percent=60, threshold=3))


def _top_shade(im: Image.Image, band: int = 640, alpha: int = 205) -> Image.Image:
    """顶部渐变暗带，给文字让位"""
    strip = Image.new('RGBA', (1, H))
    sd = ImageDraw.Draw(strip)
    for yy in range(H):
        a = int(alpha * (1 - yy / band) ** 1.3) if yy < band else 0
        sd.point((0, yy), fill=(0, 0, 0, a))
    return Image.alpha_composite(im.convert('RGBA'), strip.resize((W, H))).convert('RGB')


def _fit_font(draw: ImageDraw.ImageDraw, text: str, path: str, size: int,
              max_w: int, min_size: int = 56) -> ImageFont.FreeTypeFont:
    """主标自适应：超宽就缩字号（一次做对，避免长剧名溢出画面）"""
    f = ft(path, size)
    while draw.textlength(text, font=f) > max_w and f.size > min_size:
        f = ft(path, f.size - 4)
    return f


def grab_frame(video: str, t: float) -> Path:
    tmp = Path(tempfile.gettempdir()) / f'coverframe_{Path(video).stem[:16]}_{t:.2f}.jpg'
    subprocess.run([FF, '-v', 'error', '-y', '-ss', f'{t:.2f}', '-i', video,
                    '-frames:v', '1', '-q:v', '3', str(tmp)],
                   capture_output=True, text=True, encoding='utf-8',
                   errors='replace', timeout=180)
    if not tmp.is_file():
        raise SystemExit(f'抽帧失败: {video} @ {t}s')
    return tmp


def cmd_make(a) -> int:
    if a.base_image:
        base = Image.open(a.base_image).convert('RGB')
    elif a.base_video:
        base = Image.open(grab_frame(a.base_video, a.frame_t)).convert('RGB')
    else:
        raise SystemExit('必须提供 --base-image 或 --base-video + --frame-t')
    im = _top_shade(_cover_crop(base))
    # 底部版式：文字块下方先压渐变暗带（必须在任何文字之前画，否则压暗文字）
    if a.title_y > H // 2:
        strip = Image.new('RGBA', (1, H))
        sd = ImageDraw.Draw(strip)
        band_start = a.title_y - 160
        for yy in range(H):
            frac = max(0.0, (yy - band_start) / (H - band_start))
            sd.point((0, yy), fill=(0, 0, 0, int(190 * frac ** 1.2)))
        im = Image.alpha_composite(im.convert('RGBA'), strip.resize((W, H))).convert('RGB')
    d = ImageDraw.Draw(im, 'RGBA')

    # 左上：剧名 + 类型行（可选；海报顶部已有标题艺术字时留空）
    if a.series:
        d.text((56, 92), a.series, font=ft(FBD, 36), fill=GOLD)
        d.text((60, 146), a.tag or '系列解说', font=ft(FRG, 25), fill=(200, 205, 210))

    # 右上/右下：解说者头像 + 昵称条（可选）
    if a.avatar:
        av = Image.open(a.avatar).convert('RGBA').resize((170, 170))
        if a.avatar_pos == 'bottom-right':
            ax, ay = W - 240, H - 310          # 底图标题艺术字在顶部时锚底
        else:
            ax, ay = W - 240, 84
        d.ellipse([ax - 5, ay - 5, ax + 175, ay + 175], outline=(255, 252, 246), width=5)
        im.paste(av, (ax, ay), av)
        d.rounded_rectangle([ax - 8, ay + 190, ax + 178, ay + 244], radius=8,
                            fill=(20, 24, 30))
        d.text((ax + 14, ay + 202), a.nick or '解说', font=ft(FBD, 28), fill=GOLD)

    # 集数角标 + 引导语（title_y 为文字块基准，bottom 版式整体下移）
    bx, by = 56, a.title_y - 110
    d.rounded_rectangle([bx, by, bx + 168, by + 62], radius=10, fill=CINNABAR)
    d.text((bx + 24, by + 12), a.badge, font=ft(FBD, 34), fill=(255, 250, 245))
    d.text((bx + 196, by + 14), a.lead, font=ft(FRG, 32), fill=GOLD)

    # 主标（自适应）+ 副标 + 金色分隔条
    main_f = _fit_font(d, a.main, FBD, 88, W - 108)
    d.text((54, a.title_y), a.main, font=main_f, fill=(255, 253, 250),
           stroke_width=7, stroke_fill=(12, 14, 18))
    d.text((58, a.title_y + 140), a.sub, font=ft(FRG, 37), fill=WARM,
           stroke_width=4, stroke_fill=(20, 22, 26))
    d.rectangle([58, a.title_y + 228, 58 + 300, a.title_y + 234], fill=GOLD)
    im.save(a.out, quality=95)
    print('封面 ->', a.out)
    return 0


def probe_duration(video: str) -> float:
    out = subprocess.run([FFPROBE, '-v', 'error', '-show_entries', 'format=duration',
                          '-of', 'default=noprint_wrappers=1:nokey=1', video],
                         capture_output=True, text=True, encoding='utf-8',
                         errors='replace', timeout=60)
    return float(out.stdout.strip())




def cover_budget(total_sec: float, platform: str = 'douyin'):
    """按「平台体积上限 + 时长」反算码率上限（长片防超 128MB）。

    ★ 2026-11-03 重写：逻辑已抽到 scripts/size_budget.py，与 build.py **共用同一份**
      （此前这里是独立副本 → 同一处缺陷在两个文件各错一次 → H3/M3 连撞两集）。
      关键修正：老实现同时给 `-crf 20` 和 `-b:v` → **-b:v 被无视**
      （实测 -crf22 + -b:v 500000 仍出 1347975bps），上限完全没生效。
    """
    return size_budget.plan(total_sec, platform, crf=20, preset='medium')

def cmd_burn(a) -> int:
    """封面压成片前 N 秒；压完必须核对「音画时长 == 原片」（防截断，2026-08 教训）"""
    dur = a.dur
    # scale2ref 保证封面跟随成片分辨率；enable 窗口外不渲染
    vf = (f"[1:v]scale={W}:{H}[cv];"
          f"[0:v][cv]overlay=0:0:enable='lt(t,{dur})'[v]")
    src_dur = probe_duration(a.video)
    # ★ 体积预算（2026-11-03 重写）：与 build.py 共用 size_budget.plan
    #   老实现给 `-crf 20` + `-b:v` → -b:v 被无视 → 封面版照样超 128MB。
    #   注意音频：封面层用 `-c:a aac -b:a 128k` 重编码 → 音频码率是 128k。
    info = cover_budget(src_dur, 'douyin')
    rate_args = info['make_args']()
    if info.get('applied'):
        print(size_budget.describe(info))
    cmd = [FF, '-v', 'error', '-y', '-i', a.video, '-loop', '1', '-i', a.cover,
           '-filter_complex', vf, '-map', '[v]', '-map', '0:a?'] + rate_args + [
           '-pix_fmt', 'yuv420p', '-r', '30', '-profile:v', 'high',
           '-c:a', 'aac', '-b:a', '128k', '-movflags', '+faststart',
           '-t', str(src_dur), a.out]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8',
                       errors='replace', timeout=600)
    if r.returncode != 0:
        print(r.stderr[-2000:])
        raise SystemExit(f'烧封面失败 exit={r.returncode}')
    # ★ 体积硬闸：封面版是**最终交付物**，超限就是废片 → 宁可降码率重编
    size_budget.enforce_cap(a.out, 'douyin')
    d0, d1 = probe_duration(a.video), probe_duration(a.out)
    if abs(d1 - d0) > 0.5:
        raise SystemExit(f'烧封面后时长不符: 原片 {d0:.1f}s vs 成品 {d1:.1f}s —— 拒绝交付')
    print(f'烧封面 -> {a.out}（前 {dur}s，时长 {d1:.1f}s ✓ 与原片一致）')
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description='封面生成与烧录（技能级）')
    sub = ap.add_subparsers(dest='cmd', required=True)

    m = sub.add_parser('make', help='生成封面 jpg')
    m.add_argument('--base-image', help='底图：文件夹官方海报（优先）')
    m.add_argument('--base-video', help='底图：原片视频（无文件夹图片时）')
    m.add_argument('--frame-t', type=float, default=0.0, help='抽帧时间点（秒）')
    m.add_argument('--series', default='', help='左上剧名（可空）')
    m.add_argument('--tag', default='', help='类型行（可空）')
    m.add_argument('--avatar', help='解说者头像 png（可空）')
    m.add_argument('--nick', default='', help='头像下昵称')
    m.add_argument('--main', required=True, help='主标')
    m.add_argument('--sub', required=True, help='副标')
    m.add_argument('--lead', required=True, help='引导语')
    m.add_argument('--badge', required=True, help='集数角标，如「第 1 集」')
    m.add_argument('--out', required=True, help='输出 jpg')
    m.add_argument('--title-y', type=int, default=360,
                   help='主标基准 y（默认 360 顶部版式；海报顶部有标题艺术字时用 1300 底部版式）')
    m.add_argument('--avatar-pos', choices=['top-right', 'bottom-right'],
                   default='top-right', help='头像位置（底部版式配 bottom-right）')
    m.set_defaults(fn=cmd_make)

    b = sub.add_parser('burn', help='封面烧进成片前 N 秒')
    b.add_argument('--video', required=True)
    b.add_argument('--cover', required=True)
    b.add_argument('--out', required=True)
    b.add_argument('--dur', type=float, default=1.5)
    b.set_defaults(fn=cmd_burn)

    a = ap.parse_args()
    return a.fn(a)


if __name__ == '__main__':
    sys.exit(main())
