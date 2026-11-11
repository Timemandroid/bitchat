"""Нарезка видео на кадры через ffmpeg. Claude принимает картинки, а не видеофайлы."""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("reelbot.frames")


@dataclass
class Frame:
    path: Path
    second: float


def probe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    try:
        return max(0.0, float(out.stdout.strip()))
    except ValueError:
        return 0.0


def _scale(max_side: int) -> str:
    # Длинная сторона не больше max_side, пропорции сохраняются, маленькие кадры не увеличиваем
    return (
        f"scale='if(gt(iw,ih),min({max_side},iw),-2)':'if(gt(iw,ih),-2,min({max_side},ih))'"
    )


def extract_frames(video: Path, out_dir: Path, count: int, max_side: int) -> tuple[list[Frame], float]:
    """Берёт count кадров, равномерно по длине ролика. Возвращает кадры и длительность."""
    out_dir.mkdir(parents=True, exist_ok=True)
    duration = probe_duration(video)
    count = max(1, count)
    if duration <= 0:
        points = [0.0]
    else:
        # середины равных отрезков: без чёрного первого кадра и финальной заставки
        points = [duration * (i + 0.5) / count for i in range(count)]
    frames: list[Frame] = []
    for i, t in enumerate(points, start=1):
        dest = out_dir / f"frame_{i:02d}.jpg"
        res = subprocess.run(
            [
                "ffmpeg", "-nostdin", "-v", "error", "-y",
                "-ss", f"{t:.2f}", "-i", str(video),
                "-frames:v", "1", "-vf", _scale(max_side), "-q:v", "4",
                str(dest),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if res.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
            frames.append(Frame(dest, round(t, 1)))
        else:
            log.warning("кадр на %.1f с не получен: %s", t, res.stderr.strip()[:200])
    return frames, duration


def normalize_image(image: Path, out_dir: Path, max_side: int) -> list[Frame]:
    """Картинку из поста приводим к JPEG того же размера, что и кадры."""
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / "frame_01.jpg"
    res = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(image),
         "-frames:v", "1", "-vf", _scale(max_side), "-q:v", "4", str(dest)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if res.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
        return [Frame(dest, 0.0)]
    log.warning("картинка не обработана: %s", res.stderr.strip()[:200])
    return []
