"""Render the deterministic slide image used by the `slide` case.

The picture is drawn from code, so the case needs no downloaded asset and
contains no customer, machine or user data. Regenerate it with:

    python tools/bench/make_slide.py --out tools/bench/assets/slide-1280x720.png
"""
import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 1280, 720
TITLE = 'Ascend 950DT 推理性能优化路线'
KICKER = 'AI 基础设施季度评审 · 2026 Q4'
BULLETS = [
    ('计算效率', 'EP8 专家并行 + FP8 权重，单卡算力利用率 41% → 58%'),
    ('显存效率', 'KV 分块复用与滑动窗口重放，长上下文占用下降 37%'),
    ('调度效率', '连续批处理 + chunked prefill，首 token 延迟下降 44%'),
    ('通信效率', 'UB 平面直连集合通信，AllReduce 耗时下降 29%'),
]
METRICS = [('吞吐', '2.7x'), ('首字延迟', '-44%'), ('单卡 TCO', '-21%')]
FOOTNOTE = '注：数据来自 8 卡单机实测，含预热；不作为对外承诺。'

# A second, visually similar deck page so multi-image cases stay deterministic.
ALT_TITLE = '长上下文服务的容量规划'
ALT_KICKER = 'AI 基础设施季度评审 · 2026 Q4'
ALT_BULLETS = [
    ('窗口', '单请求上限 1,048,576 token，输入与输出合计计入'),
    ('显存', '每 DP rank 预留 35.7 GiB KV，1M 并发约 9.7 路'),
    ('调度', 'max_num_seqs 与 max_num_batched_tokens 共同决定排队'),
    ('风险', '长请求会挤压短请求；需按业务 SLA 设置准入'),
]
ALT_METRICS = [('KV 余量', '9.7x'), ('排队上限', '32'), ('降级阈值', '-20%')]
ALT_FOOTNOTE = '注：容量数字为配置推算，不代表已压满实测。'


def font(size, bold=False):
    """Pick any installed CJK font, else fall back to Pillow's default."""
    candidates = [
        r'C:\Windows\Fonts\msyhbd.ttc' if bold else r'C:\Windows\Fonts\msyh.ttc',
        r'C:\Windows\Fonts\simhei.ttf',
        '/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc' if bold
        else '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf' if bold
        else '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def render(variant='main'):
    title, kicker = (TITLE, KICKER) if variant == 'main' else (ALT_TITLE, ALT_KICKER)
    bullets = BULLETS if variant == 'main' else ALT_BULLETS
    metrics = METRICS if variant == 'main' else ALT_METRICS
    footnote = FOOTNOTE if variant == 'main' else ALT_FOOTNOTE
    image = Image.new('RGB', (WIDTH, HEIGHT), (250, 251, 253))
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, WIDTH, 132], fill=(23, 43, 77))
    draw.text((64, 30), kicker, font=font(24), fill=(150, 185, 230))
    draw.text((64, 66), title, font=font(42, bold=True), fill=(255, 255, 255))
    y = 176
    for label, text in bullets:
        draw.rounded_rectangle([64, y, 68 + 150, y + 34], radius=6, fill=(232, 238, 248))
        draw.text((80, y + 6), label, font=font(21, bold=True), fill=(23, 43, 77))
        draw.text((236, y + 6), text, font=font(21), fill=(45, 55, 72))
        y += 58
    x = 64
    for name, value in metrics:
        draw.rounded_rectangle([x, 470, x + 360, 606], radius=10,
                               fill=(255, 255, 255), outline=(205, 214, 228), width=2)
        draw.text((x + 28, 492), name, font=font(22), fill=(96, 110, 130))
        draw.text((x + 28, 528), value, font=font(52, bold=True), fill=(20, 120, 90))
        x += 392
    draw.text((64, 638), footnote, font=font(19), fill=(120, 130, 145))
    return image


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--variant', choices=['main', 'alt'], default='main')
    parser.add_argument('--size', default='1280x720', help='Output size, e.g. 1280x720 or 512x288')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    width, height = (int(v) for v in args.size.lower().split('x'))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    image = render(args.variant)
    if (width, height) != (WIDTH, HEIGHT):
        image = image.resize((width, height), Image.LANCZOS)
    image.save(args.out, format='PNG', optimize=True)
    print(f'{args.out} {image.size[0]}x{image.size[1]} {args.out.stat().st_size} bytes')


if __name__ == '__main__':
    main()
