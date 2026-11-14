"""演示视频录制：浏览器逐帧截图 → ffmpeg 合成 MP4。

为什么不用浏览器自带录制：当前 IAB 后端不提供 recording 能力（`tab.recording`
在 capability 列表里是空的），所以改成「按节奏截图 + 事后合成」。
代价是帧率不连续（不是 60fps 录屏），但演示够用，而且每一步都能精确控制。

用法：由调用方（agent）在 Node REPL 里逐帧驱动，这里只做合成。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


def build(frames: Path, out: Path, *, fps: float = 2.0, size: str = "",
          out_fps: int = 30) -> int:
    images = sorted(frames.glob("*.png"))
    if not images:
        print(f"没有帧：{frames}", file=sys.stderr)
        return 1

    try:
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        ffmpeg = shutil.which("ffmpeg") or ""
    if not ffmpeg:
        print("找不到 ffmpeg：pip install imageio-ffmpeg", file=sys.stderr)
        return 1

    # 序号必须连续，否则 -start_number 对不上；用固定宽度命名保证字典序 = 时间序
    expected = [f"{i:05d}.png" for i in range(1, len(images) + 1)]
    actual = [p.name for p in images]
    if actual != expected:
        print(f"帧序号不连续：{actual[:3]} … {actual[-3:]}（共 {len(actual)}）", file=sys.stderr)
        return 1

    # 截图是稀疏的（每 ~1.2 秒一帧）。直接输出 2fps 视频播放会一顿一顿，
    # 所以按 30fps 编码、每帧复制成多帧：时长不变，播放器不再卡。
    filters = [f"fps={out_fps}"]
    if size:
        filters.insert(0, f"scale={size}:flags=lanczos")
    filters.append("format=yuv420p")
    command = [
        ffmpeg, "-y",
        "-framerate", str(fps),
        "-i", str(frames / "%05d.png"),
        "-vf", ",".join(filters),
        "-r", str(out_fps),
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "20",
        "-movflags", "+faststart",
        str(out),
    ]
    print(" ".join(command))
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stderr[-1500:], file=sys.stderr)
        return result.returncode
    size_mb = out.stat().st_size / 1_048_576
    print(f"完成：{out}（{len(images)} 帧 · {size_mb:.1f} MB · 约 {len(images) / fps:.1f} 秒）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--frames", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--fps", type=float, default=2.0, help="截图节奏（每帧代表的时长）")
    parser.add_argument("--out-fps", type=int, default=30, help="成片帧率（复制帧填充）")
    parser.add_argument("--size", default="", help="如 1440:-2")
    args = parser.parse_args()
    return build(Path(args.frames), Path(args.out), fps=args.fps,
                 size=args.size, out_fps=args.out_fps)


if __name__ == "__main__":
    sys.exit(main())
