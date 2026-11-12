"""Image and audio conversion with an on-disk cache.

Images become WebP (Pillow), audio becomes AAC in an .m4a container (ffmpeg),
because Safari on iPad does not play Ogg Vorbis reliably. Converted files are
cached under the work folder, keyed by source file and settings, so re-running
the converter only redoes what changed.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PIL import Image

Image.MAX_IMAGE_PIXELS = None


def find_tool(name: str) -> str:
    """Locate ffmpeg/ffprobe: PATH first, then the winget package folder."""
    found = shutil.which(name)
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA", "")
    pattern = os.path.join(local, "Microsoft", "WinGet", "Packages", "Gyan.FFmpeg*", "**", "bin", name + ".exe")
    hits = glob.glob(pattern, recursive=True)
    if hits:
        return hits[0]
    raise FileNotFoundError(
        f"{name} was not found. Install it with:  winget install --id Gyan.FFmpeg -e --source winget"
    )


@dataclass
class Job:
    kind: str            # image | audio | video | copy
    source: Path
    out_name: str        # path inside the bundle
    params: dict
    loader: Callable[[], Image.Image] | None = None   # for images built in memory


class MediaConverter:
    def __init__(self, cache_dir: Path, workers: int | None = None):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.workers = workers or max(2, (os.cpu_count() or 4) - 1)
        self.jobs: dict[str, Job] = {}
        self._ffmpeg: str | None = None
        self._ffprobe: str | None = None
        self._lock = threading.Lock()

    # -- tools
    @property
    def ffmpeg(self) -> str:
        if not self._ffmpeg:
            self._ffmpeg = find_tool("ffmpeg")
        return self._ffmpeg

    @property
    def ffprobe(self) -> str:
        if not self._ffprobe:
            self._ffprobe = find_tool("ffprobe")
        return self._ffprobe

    def duration_ms(self, source: Path) -> int:
        out = subprocess.run(
            [self.ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(source)],
            capture_output=True, text=True, check=True,
        ).stdout
        return round(float(json.loads(out)["format"]["duration"]) * 1000)

    # -- scheduling
    def add_image(self, source: Path, out_name: str, *, quality: int = 82, lossless: bool = False,
                  max_size: tuple[int, int] | None = None, loader=None, tag: str = "") -> str:
        params = {"q": quality, "lossless": lossless, "max": max_size, "tag": tag}
        self.jobs.setdefault(out_name, Job("image", Path(source), out_name, params, loader))
        return out_name

    def add_audio(self, source: Path, out_name: str, *, bitrate: str, channels: int | None = None,
                  rate: int = 44100, loader: Callable[[], bytes] | None = None, tag: str = "") -> str:
        """`loader` supplies the encoded audio bytes when the sound sits inside an archive;
        `tag` must then identify the entry so the cache key stays unique."""
        params = {"b": bitrate, "ac": channels, "ar": rate, "tag": tag}
        self.jobs.setdefault(out_name, Job("audio", Path(source), out_name, params, loader))
        return out_name

    def add_video(self, source: Path, out_name: str, *, height: int = 720, crf: int = 26,
                  audio_bitrate: str = "128k") -> str:
        params = {"h": height, "crf": crf, "ab": audio_bitrate}
        self.jobs.setdefault(out_name, Job("video", Path(source), out_name, params))
        return out_name

    # -- execution
    def _cache_path(self, job: Job) -> Path:
        try:
            stat = job.source.stat()
            stamp = f"{stat.st_size}:{int(stat.st_mtime)}"
        except OSError:
            stamp = "mem"
        key = json.dumps([job.kind, str(job.source), stamp, job.params], sort_keys=True, default=str)
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
        ext = os.path.splitext(job.out_name)[1]
        return self.cache_dir / digest[:2] / (digest + ext)

    def _convert(self, job: Job, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.stem + ".part" + target.suffix)
        if job.kind == "image":
            image = job.loader() if job.loader else Image.open(job.source)
            if image.mode not in ("RGB", "RGBA"):
                has_alpha = image.mode in ("LA", "PA") or "transparency" in image.info
                image = image.convert("RGBA" if has_alpha else "RGB")
            if image.mode == "RGBA" and image.getextrema()[3][0] == 255:
                image = image.convert("RGB")
            limit = job.params["max"]
            if limit and (image.width > limit[0] or image.height > limit[1]):
                image.thumbnail(limit, Image.LANCZOS)
            if job.params["lossless"]:
                image.save(tmp, "WEBP", lossless=True, quality=90, method=6)
            else:
                image.save(tmp, "WEBP", quality=job.params["q"], alpha_quality=92, method=6)
            image.close()
        elif job.kind == "audio":
            payload = job.loader() if job.loader else None
            cmd = [self.ffmpeg, "-v", "error", "-y", "-i", "pipe:0" if payload is not None else str(job.source)]
            cmd += ["-vn", "-map_metadata", "-1", "-c:a", "aac", "-b:a", job.params["b"]]
            if job.params["ac"]:
                cmd += ["-ac", str(job.params["ac"])]
            cmd += ["-ar", str(job.params["ar"]), "-movflags", "+faststart", "-f", "ipod", str(tmp)]
            done = subprocess.run(cmd, input=payload, capture_output=True)
            if done.returncode != 0:
                message = done.stderr.decode("utf-8", "replace").strip()[:300]
                raise RuntimeError(f"ffmpeg failed for {job.out_name}: {message}")
        elif job.kind == "video":
            cmd = [self.ffmpeg, "-v", "error", "-y", "-i", str(job.source),
                   "-vf", f"scale=-2:{job.params['h']}", "-c:v", "libx264", "-preset", "slow",
                   "-crf", str(job.params["crf"]), "-pix_fmt", "yuv420p", "-profile:v", "high",
                   "-c:a", "aac", "-b:a", job.params["ab"], "-ar", "44100",
                   "-movflags", "+faststart", "-f", "mp4", str(tmp)]
            done = subprocess.run(cmd, capture_output=True, text=True)
            if done.returncode != 0:
                raise RuntimeError(f"ffmpeg failed for {job.source}: {done.stderr.strip()[:300]}")
        else:
            shutil.copyfile(job.source, tmp)
        os.replace(tmp, target)

    def run(self, progress: Callable[[int, int], None] | None = None) -> tuple[dict[str, Path], list[str]]:
        """Run every scheduled job. Returns ({bundle name: converted file}, [error messages])."""
        results: dict[str, Path] = {}
        errors: list[str] = []
        todo: list[tuple[Job, Path]] = []
        for job in self.jobs.values():
            target = self._cache_path(job)
            if target.exists() and target.stat().st_size > 0:
                results[job.out_name] = target
            else:
                todo.append((job, target))
        total, done = len(self.jobs), len(results)
        if progress:
            progress(done, total)
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {pool.submit(self._convert, job, target): (job, target) for job, target in todo}
            for future in as_completed(futures):
                job, target = futures[future]
                try:
                    future.result()
                    results[job.out_name] = target
                except Exception as exc:  # keep going; report at the end
                    errors.append(f"{job.out_name}: {exc}")
                done += 1
                if progress:
                    progress(done, total)
        return results, errors
