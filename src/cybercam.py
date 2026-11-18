#!/usr/bin/env python3
"""CyberCam 0.5.2 by Cybern0mads for M5Stack Cardputer Zero.

RGB565 framebuffer + evdev user interface; rpicam still/video/MJPEG backend.
No modification to AuraCam or the stock CameraApp is required.
"""
from __future__ import annotations

# Copyright (c) 2026 Cybern0mads. MIT licensed; see LICENSE.

import argparse
import html
import hmac
import mimetypes
import secrets
import tempfile
import urllib.parse
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from functools import lru_cache
import array
import fcntl
import io
import json
import mmap
import os
from pathlib import Path
import select
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
from datetime import datetime
from collections import deque

from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps

try:
    from evdev import InputDevice, ecodes, list_devices
except ImportError:
    InputDevice = None
    ecodes = None
    list_devices = None

VERSION = "0.5.2"
HOME = Path.home()
ROOT = HOME / "Pictures" / "CyberCam"
CONFIG = HOME / ".config" / "cybercam" / "settings.json"
LOG = HOME / ".local" / "state" / "cybercam" / "camera.log"
SHUTTER = Path(__file__).resolve().parent / "shutter.wav"
RESOLUTIONS = [(3280, 2464), (1920, 1080), (1640, 1232), (640, 480)]
# 1640x1232 uses the entire IMX219 sensor (2x2 binned), unlike the
# high-FPS 640x480 sensor mode which has a substantial center crop.
# Keep widescreen output optional: 16:9 loses top/bottom field of view.
FULL_FOV_MODE = "1640:1232:10:P"
FULL_STILL_MODE = "3280:2464:10:P"
VIDEOS = [
    (1280, 960, 30, 8_000_000),   # 4:3 WIDE, default
    (1440, 1080, 30, 12_000_000), # 4:3 WIDE high detail
    (1280, 720, 30, 8_000_000),   # 16:9 (vertical crop)
    (640, 480, 30, 4_000_000),    # low bandwidth, full FoV
]
VIDEO_LABELS = ("WIDE 960P", "WIDE 1080P", "16:9 720P", "WIDE 480P")
FILTERS = ["NORMAL", "MONO", "GBOY", "GREEN", "4TONE", "PIXEL", "HICON",
           "GAMEBOY+", "VHS1998", "CYBERVISION", "FILM1999", "VIRTUALRED",
           "C64RETRO", "NEONEDGE", "PIXELDREAM", "CYBERGLITCH"]
EXTRA_MODES = ("TIMELAPSE", "STOPMOTION", "BOOMERANG")
MODES = ("PHOTO", "GIF", "VIDEO", "TIMELAPSE", "STOPMOTION", "BOOMERANG")
MODE_BUTTONS = {"PHOTO": "1", "GIF": "2", "VIDEO": "3", "TIMELAPSE": "4"}
MENU_CHOICES = MODES + ("GALLERY",)
TIMELAPSE_INTERVALS = (2, 5, 10, 30, 60, 300, 1800)
# Keep original media; only generated animation/video frames are downscaled for
# a practical FFmpeg memory footprint on this 512 MB RAM handheld.
MOVIE_SIZE = (640, 480)
MAGENTA = (255, 68, 145)
MINT = (111, 240, 205)
PAPER = (230, 241, 242)
# Optional acceleration; package declares python3-numpy on device.
try:
    import numpy as np
except ImportError:
    np = None



def camera_log():
    LOG.parent.mkdir(parents=True, exist_ok=True)
    return open(LOG, "ab", buffering=0)


def timestamp():
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]


def load_settings():
    defaults = {"sound": True, "filter": "NORMAL", "resolution": 0, "video": 0, "rotation": 180, "preview_fps": 15, "timelapse_interval": 1}
    try:
        values = json.loads(CONFIG.read_text())
        defaults.update({k: values[k] for k in defaults if k in values})
    except (OSError, ValueError, TypeError):
        pass
    if defaults["filter"] not in FILTERS:
        defaults["filter"] = "NORMAL"
    for key, count in (("resolution", len(RESOLUTIONS)), ("video", len(VIDEOS))):
        if not isinstance(defaults[key], int) or not (0 <= defaults[key] < count):
            defaults[key] = 0
    if defaults["rotation"] not in (0, 180):
        defaults["rotation"] = 180
    if defaults["preview_fps"] not in (12, 15, 20, 24, 30):
        defaults["preview_fps"] = 15
    if not isinstance(defaults["timelapse_interval"], int) or not (0 <= defaults["timelapse_interval"] < len(TIMELAPSE_INTERVALS)):
        defaults["timelapse_interval"] = 1
    return defaults


def save_settings(values):
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(values, indent=2) + "\n")


def click(sound=True):
    if sound and SHUTTER.exists() and shutil.which("aplay"):
        try:
            subprocess.Popen(["aplay", "-q", str(SHUTTER)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            pass


def run_command(command, timeout=45):
    with camera_log() as log:
        result = subprocess.run(command, stdout=log, stderr=log, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"Camera command failed (exit {result.returncode}); see {LOG}")


def reserve_path(subfolder, kind, extension):
    directory = ROOT / subfolder
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / f"cyber_{timestamp()}_{kind}.{extension}"
    i = 1
    while p.exists():
        p = directory / f"cyber_{timestamp()}_{kind}_{i}.{extension}"
        i += 1
    return p


def preview_size(img, size):
    return ImageOps.pad(img.convert("RGB"), size, method=Image.Resampling.BILINEAR,
                        color=(14, 20, 31))


def _palette(colors):
    palette = Image.new("P", (1, 1))
    values = [c for color in colors for c in color]
    palette.putpalette(values + [0] * (768 - len(values)))
    return palette


GREEN_COLORS = ((22, 36, 32), (83, 117, 101), (164, 190, 155), (227, 242, 210))
GRAY_COLORS = ((20, 24, 32), (89, 99, 112), (173, 184, 190), (243, 246, 241))
GREEN_PALETTE = _palette(GREEN_COLORS)
GRAY_PALETTE = _palette(GRAY_COLORS)


def filter_image(source, mode, animated=False, preview=False):
    """Efficient retro effects. Still photos preserve original dimensions/ratio.

    Pixel/detail deliberately reduced for vintage look; resizing doesn't add detail.
    """
    if mode == "NORMAL":
        if animated:
            return preview_size(source, (240, 176))
        return source.convert("RGB").copy()
    if mode in ("MONO", "HICON"):
        working = preview_size(source, (240,176)) if animated else source.convert("RGB")
        gray = ImageOps.autocontrast(working.convert("L"))
        if mode == "HICON":
            gray = ImageEnhance.Contrast(gray).enhance(2.0)
        return gray.convert("RGB")
    if mode in ("GAMEBOY+", "VHS1998", "CYBERVISION", "FILM1999", "VIRTUALRED",
                "C64RETRO", "NEONEDGE", "PIXELDREAM", "CYBERGLITCH"):
        # Bound working size before running effects to avoid 8MP RGB tripling
        # memory consumption. The resulting still PNG retains original dimensions.
        original_size = source.size
        if animated:
            base = preview_size(source, (240, 176))
        elif preview:
            base = source.convert("RGB")
        else:
            scale = min(1.0, 820/source.width, 616/source.height)
            base = source.convert("RGB").resize((max(1, int(source.width*scale)),
                max(1, int(source.height*scale))), Image.Resampling.BOX)
        out = creative_filter(base, mode)
        if not animated and not preview and out.size != original_size:
            out = out.resize(original_size, Image.Resampling.NEAREST)
        return out
    if mode not in ("GBOY", "GREEN", "4TONE", "PIXEL"):
        raise ValueError(f"Unknown filter {mode}")

    original_size = source.size
    if animated:
        target = (240, 176)
        base = preview_size(source, target)
    elif preview:
        target = source.size
        base = source.convert("RGB")
    else:
        # 4x block-dither on an 8 MP JPEG (820x616) is practical on 512 MB RAM.
        target = (min(source.width, 820), min(source.height, 616))
        base = source.resize(target, Image.Resampling.BOX).convert("RGB")

    if mode == "PIXEL":
        thumb = base.resize((max(1, target[0] // 4), max(1, target[1] // 4)), Image.Resampling.BOX)
        out = thumb.quantize(colors=16, dither=Image.Dither.NONE).convert("RGB").resize(
            target, Image.Resampling.NEAREST)
    else:
        gray = ImageOps.autocontrast(base.convert("L"))
        if mode == "GBOY":
            out = ImageEnhance.Contrast(gray).enhance(1.35).convert(
                "1", dither=Image.Dither.FLOYDSTEINBERG).convert("RGB")
        else:
            out = gray.convert("RGB").quantize(
                palette=GREEN_PALETTE if mode == "GREEN" else GRAY_PALETTE,
                dither=Image.Dither.FLOYDSTEINBERG).convert("RGB")
    if not animated and not preview and out.size != original_size:
        out = out.resize(original_size, Image.Resampling.NEAREST)
    return out



@lru_cache(maxsize=4)
def film_vignette(w,h):
    mask = Image.new("L",(w,h),0)
    px = mask.load()
    for y in range(h):
        yy = ((y-h/2)/(h/2))**2
        for x in range(w):
            v = ((x-w/2)/(w/2))**2 + yy
            px[x,y] = max(100, min(255, int(255-75*v)))
    return mask


def creative_filter(base, mode):
    """Retro/cyber looks processed at bounded sizes; no dependence on OpenCV."""
    from PIL import ImageFilter, ImageChops
    rgb = base.convert("RGB")
    w, h = rgb.size
    gray = ImageOps.autocontrast(rgb.convert("L"))
    if mode == "GAMEBOY+":
        boosted = ImageEnhance.Contrast(gray).enhance(1.7)
        pal = ((12, 31, 27), (56, 96, 66), (142, 177, 107), (220, 242, 174))
        quant = boosted.convert("RGB").quantize(palette=_palette(pal),
                            dither=Image.Dither.FLOYDSTEINBERG).convert("RGB")
        return quant
    if mode == "VIRTUALRED":
        levels = gray.point(lambda n: (n//64)*85)
        return Image.merge("RGB", (levels, levels.point(lambda n:n//12), levels.point(lambda n:n//18)))
    if mode == "C64RETRO":
        colors = [(0,0,0), (255,255,255), (136,0,0), (170,255,238),
                  (204,68,204), (0,204,85), (0,0,170), (238,238,119),
                  (221,136,85), (102,68,0), (255,119,119), (51,51,51),
                  (119,119,119), (170,255,102), (0,136,255), (187,187,187)]
        small = rgb.resize((max(1,w//3), max(1,h//3)), Image.Resampling.BOX)
        return small.quantize(palette=_palette(colors), dither=Image.Dither.FLOYDSTEINBERG).convert("RGB").resize((w,h), Image.Resampling.NEAREST)
    if mode == "PIXELDREAM":
        small = rgb.resize((max(1,w//5),max(1,h//5)), Image.Resampling.BOX)
        small = ImageEnhance.Color(small).enhance(1.8)
        return small.quantize(colors=24, dither=Image.Dither.NONE).convert("RGB").resize((w,h), Image.Resampling.NEAREST)
    if mode == "CYBERVISION":
        # Stylized false color, not thermal or infrared sensor data.
        ramp = [(5,12,27), (0,65,100), (0,215,230), (112,62,217), (243,74,184), (248,232,150)]
        lut = [ramp[min(5,i//43)] for i in range(256)]
        return Image.merge("RGB", tuple(gray.point([c[k] for c in lut]) for k in range(3)))
    if mode == "NEONEDGE":
        edges = ImageOps.autocontrast(gray.filter(ImageFilter.FIND_EDGES))
        edge = ImageEnhance.Contrast(edges).enhance(2.5)
        return Image.merge("RGB", (edge.point(lambda n: n//3), edge,
                     edge.point(lambda n: min(255, 65+n//2))))
    if mode == "FILM1999":
        # Gentle warming; vignette mask cached so it isn't regenerated at 15 FPS.
        from PIL import ImageChops
        r,g,b = rgb.split()
        warm = Image.merge("RGB", (r.point(lambda n:min(255,int(n*1.06+5))),
                       g.point(lambda n:min(255,int(n*1.02))), b.point(lambda n:int(n*.85))))
        tint = film_vignette(w,h)
        return Image.merge("RGB", [ImageChops.multiply(ch,tint) for ch in warm.split()])
    if mode == "VHS1998":
        out = rgb.copy()
        r,g,b = out.split()
        # chromatic channel offset, subtle dark scanlines and tape border
        r = ImageChops.offset(r, 2, 0)
        b = ImageChops.offset(b, -2, 0)
        out = Image.merge('RGB',(r,g,b))
        draw = ImageDraw.Draw(out)
        for y in range(0,h,4):
            draw.line((0,y,w,y),fill=(16,24,34),width=1)
        return out
    if mode == "CYBERGLITCH":
        # A deterministic stripe displacement; animated frames vary as input varies.
        out = rgb.copy()
        r,g,b = out.split()
        out = Image.merge('RGB', (ImageChops.offset(r,3,0),g,ImageChops.offset(b,-3,0)))
        for y in range(7,h,31):
            band = out.crop((0,y,w,min(h,y+3)))
            out.paste(ImageChops.offset(band,(y*11)%21-10,0),(0,y))
        return out
    raise ValueError('Unknown creative filter: ' + mode)


def filtered_still(original, mode):
    if mode == "NORMAL":
        return None
    with Image.open(original) as img:
        result = filter_image(img, mode)
    path = original.with_name(original.stem + "_" + mode.lower() + ".png")
    result.save(path)
    return path


def take_photo(mode="NORMAL", resolution=(3280, 2464), sound=True, rotation=180):
    output = reserve_path("Photos", "8mp" if resolution == (3280, 2464) else "photo", "jpg")
    click(sound)
    w, h = resolution
    try:
        run_command(["rpicam-still", "-n", "-t", "1300", "--width", str(w),
                     "--height", str(h), "--mode", FULL_STILL_MODE,
                     "--rotation", str(rotation), "-o", str(output)], timeout=35)
        with Image.open(output) as im:
            im.verify()
        filtered = filtered_still(output, mode)
    except Exception:
        if output.exists() and output.stat().st_size < 1024:
            output.unlink()
        raise
    return output, filtered


class MjpegStream:
    """Low-res only camera preview; never used as a substitute for 8MP stills."""
    def __init__(self, rotation=180, fps=15):
        self.rotation = rotation
        self.fps = fps
        self.proc = None
        self.worker = None
        self.lock = threading.Condition()
        self.seq = 0
        self.jpg = None
        self.running = False
        self.error = None
        self.logfile = None

    def start(self):
        if self.proc is not None:
            return
        self.error = None
        self.running = True
        self.logfile = camera_log()
        try:
            self.proc = subprocess.Popen([
                "rpicam-vid", "-n", "-t", "0", "--codec", "mjpeg",
                "--width", "320", "--height", "240", "--mode", FULL_FOV_MODE,
                "--framerate", str(self.fps),
                "--rotation", str(self.rotation), "--quality", "72", "-o", "-"], stdout=subprocess.PIPE, stderr=self.logfile, bufsize=0)
        except Exception:
            self.running = False
            self.logfile.close()
            self.logfile = None
            raise
        self.worker = threading.Thread(target=self._read, daemon=True)
        self.worker.start()

    def _read(self):
        data = bytearray()
        try:
            while self.running and self.proc and self.proc.stdout:
                chunk = self.proc.stdout.read(16384)
                if not chunk:
                    break
                data.extend(chunk)
                while True:
                    start = data.find(b"\xff\xd8")
                    if start < 0:
                        if len(data) > 2:
                            del data[:-2]
                        break
                    if start:
                        del data[:start]
                    end = data.find(b"\xff\xd9", 2)
                    if end < 0:
                        if len(data) > 3_000_000:
                            data.clear()
                        break
                    jpg = bytes(data[:end+2])
                    del data[:end+2]
                    with self.lock:
                        self.jpg = jpg
                        self.seq += 1
                        self.lock.notify_all()
        except Exception as exc:
            self.error = str(exc)
        finally:
            with self.lock:
                self.lock.notify_all()

    def latest(self):
        with self.lock:
            return self.seq, self.jpg

    def next_frame(self, since, timeout=2):
        with self.lock:
            self.lock.wait_for(lambda: self.seq > since or not self.running, timeout=timeout)
            if self.seq > since:
                return self.seq, self.jpg
            return since, None

    def stop(self):
        if self.proc is None:
            return
        self.running = False
        proc = self.proc
        if proc.poll() is None:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        if self.worker is not None:
            self.worker.join(timeout=2)
        if proc.stdout is not None:
            proc.stdout.close()
        self.proc = None
        self.worker = None
        if self.logfile is not None:
            self.logfile.close()
            self.logfile = None


def save_gif(frames, mode, output, duration=133):
    if not frames:
        raise RuntimeError("No frames received from camera")
    processed = [filter_image(frame, mode, animated=True).convert("P", palette=Image.Palette.ADAPTIVE,
                   colors=32 if mode != "GBOY" else 2) for frame in frames]
    processed[0].save(output, format="GIF", save_all=True, append_images=processed[1:],
                      duration=duration, loop=0, disposal=2, optimize=False)
    return output


def gif_burst(stream, mode="NORMAL", count=15, sound=True, progress=None):
    # ~2s at 7.5 captured frames per second, independent of faster viewfinder.
    output = reserve_path("GIFs", mode.lower(), "gif")
    click(sound)
    seq, _ = stream.latest()
    frames = []
    started = time.monotonic()
    for index in range(count):
        target_time = started + (index * 2.0 / count)
        delay = target_time - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        seq, jpg = stream.next_frame(seq, timeout=2.5)
        if jpg is None:
            break
        try:
            with Image.open(io.BytesIO(jpg)) as image:
                image.load()
                frames.append(image.convert("RGB"))
        except OSError:
            continue
        if progress:
            progress(len(frames), count)
    if len(frames) < 2:
        raise RuntimeError(f"Camera provided only {len(frames)} GIF frames; see {LOG}")
    save_gif(frames, mode, output)
    return output, len(frames)


class Recording:
    """Video recording with graceful MP4 finalization; H264/ffmpeg fallback."""
    def __init__(self, resolution=(1280, 720, 30, 8_000_000), rotation=180):
        self.resolution = resolution
        self.rotation = rotation
        self.proc = None
        self.logfile = None
        self.path = None
        self.raw = None
        self.started = None

    def start(self, seconds=0, sound=True):
        w, h, fps, bitrate = self.resolution
        self.path = reserve_path("Videos", "video", "mp4")
        self.started = time.monotonic()
        self.logfile = camera_log()
        cmd = ["rpicam-vid", "-n", "-t", str(int(seconds * 1000)) if seconds else "0",
               "--width", str(w), "--height", str(h), "--mode", FULL_FOV_MODE,
               "--framerate", str(fps), "--rotation", str(self.rotation), "--bitrate", str(bitrate),
               "--codec", "libav", "--libav-format", "mp4", "-o", str(self.path)]
        try:
            self.proc = subprocess.Popen(cmd, stdout=self.logfile, stderr=self.logfile)
            time.sleep(0.7)
            if self.proc.poll() is not None:
                raise RuntimeError(f"MP4 encoder exited (code {self.proc.returncode}); see {LOG}")
        except Exception:
            if self.proc is not None and self.proc.poll() is None:
                self.proc.terminate()
                self.proc.wait(timeout=4)
            self.proc = None
            self.logfile.close()
            self.logfile = None
            raise
        click(sound)
        return self.path

    def stop(self, sound=True):
        if self.proc is None:
            raise RuntimeError("Video is not recording")
        try:
            if self.proc.poll() is None:
                self.proc.send_signal(signal.SIGINT)
                try:
                    self.proc.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    self.proc.terminate()
                    self.proc.wait(timeout=3)
                    raise RuntimeError(f"Video encoder did not stop gracefully; inspect {LOG}")
            if not self.path.exists() or self.path.stat().st_size < 1024:
                raise RuntimeError(f"Video file missing or empty; see {LOG}")
            click(sound)
            return self.path
        finally:
            self.proc = None
            if self.logfile is not None:
                self.logfile.close()
                self.logfile = None


# --------------------- Creative capture modes (v0.5) -----------------------

def encode_mp4(frames, destination, look="NORMAL", fps=15, size=MOVIE_SIZE):
    """Convert ordered source images to a practical 4:3 timelapse/stop-motion MP4.

    Frames remain on disk, unmodified; temporary resized frames are removed.
    Uses software encoding, which may take time on the Cardputer Zero CPU.
    """
    frames = [Path(f) for f in frames]
    if not frames:
        raise RuntimeError("Cannot encode video without frames")
    if not shutil.which("ffmpeg"):
        raise RuntimeError("FFmpeg is required: sudo apt install ffmpeg")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fps = max(1, min(30, int(fps)))
    with tempfile.TemporaryDirectory(prefix="cybercam_frames_") as td:
        folder = Path(td)
        for i, filename in enumerate(frames):
            with Image.open(filename) as source:
                # Expand to a letterboxed 4:3 frame; no camera FoV crop.
                frame = preview_size(source, size)
            if look != "NORMAL":
                frame = filter_image(frame, look, preview=True)
            frame.save(folder / f"frame_{i:06d}.png")
        source_pattern = str(folder / "frame_%06d.png")
        base = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-framerate", str(fps), "-i", source_pattern,
                "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
        # Try libx264, and fall back to MPEG-4 Part 2 for minimal FFmpeg builds.
        failures = []
        for encoder in (["-c:v", "libx264", "-preset", "ultrafast", "-crf", "26"],
                        ["-c:v", "mpeg4", "-q:v", "5"]):
            command = base + encoder + [str(destination)]
            try:
                run_command(command, timeout=max(120, len(frames)*12))
                if destination.stat().st_size < 1024:
                    raise RuntimeError("Encoded MP4 was empty")
                return destination
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                failures.append(str(exc))
                destination.unlink(missing_ok=True)
        raise RuntimeError("MP4 export failed (frames preserved): " + "; ".join(failures))


class TimelapseCapture:
    """Manual stop from UI; fixed frame count for CLI. Originals are retained."""
    def __init__(self, interval=5, resolution=(1640,1232), rotation=180, look="NORMAL", fps=15):
        self.interval = max(2.0, float(interval))
        self.resolution = resolution
        self.rotation = rotation
        self.look = look
        self.fps = fps
        self.stop_event = threading.Event()
        self.stamp = timestamp()
        self.folder = ROOT / "Timelapse" / ("lapse_" + self.stamp)
        self.output = ROOT / "Timelapse" / ("lapse_" + self.stamp + ".mp4")
        self.frames = []

    def stop(self):
        self.stop_event.set()

    def run(self, count=None, progress=None):
        if count is not None and not (1 <= count <= 20000):
            raise ValueError("Timelapse frame count must be 1..20000")
        self.folder.mkdir(parents=True, exist_ok=True)
        n = 0
        # Scheduling is relative to the end of each still capture; ensures the
        # camera does not spawn overlapping processes on slower systems.
        while not self.stop_event.is_set() and (count is None or n < count):
            path = self.folder / f"frame_{n:06d}.jpg"
            w,h = self.resolution
            command = ["rpicam-still", "-n", "-t", "950", "--mode", FULL_STILL_MODE,
                       "--width", str(w), "--height", str(h),
                       "--rotation", str(self.rotation), "-o", str(path)]
            run_command(command, timeout=35)
            if path.stat().st_size < 1024:
                raise RuntimeError("Timelapse frame is empty: " + str(path))
            self.frames.append(path)
            n += 1
            if progress:
                progress(n)
            if count is not None and n >= count:
                break
            if self.stop_event.wait(self.interval):
                break
        if not self.frames:
            raise RuntimeError("Timelapse stopped without any frames")
        if progress:
            progress(len(self.frames), exporting=True)
        encode_mp4(self.frames, self.output, self.look, self.fps)
        return self.output


class StopMotionSession:
    """Capture manually from the running preview with onion-skin framing."""
    def __init__(self, look="NORMAL"):
        self.stamp = timestamp()
        self.folder = ROOT / "StopMotion" / ("frames_" + self.stamp)
        self.output = ROOT / "StopMotion" / ("stopmotion_" + self.stamp + ".mp4")
        self.gif_output = ROOT / "GIFs" / ("stopmotion_" + self.stamp + ".gif")
        self.look = look
        self.frames = []
        self.last_frame = None

    def capture(self, jpg):
        if not jpg:
            raise RuntimeError("No live preview frame to capture")
        with Image.open(io.BytesIO(jpg)) as source:
            image = source.convert("RGB")
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / f"frame_{len(self.frames):06d}.jpg"
        image.save(path, quality=92)
        self.frames.append(path)
        self.last_frame = image
        return path

    def onion_skin(self, current):
        if self.last_frame is None:
            return current
        previous = preview_size(self.last_frame, current.size)
        return Image.blend(current.convert("RGB"), previous, .34)

    def export(self):
        if len(self.frames) < 2:
            raise RuntimeError("Stop motion needs at least two frames")
        # GIF playback and MP4 both retain the chosen look and source images.
        pictures = []
        for filename in self.frames:
            with Image.open(filename) as img:
                pictures.append(img.convert("RGB"))
        self.gif_output.parent.mkdir(parents=True, exist_ok=True)
        save_gif(pictures, self.look, self.gif_output, duration=125)
        return encode_mp4(self.frames, self.output, self.look, fps=8)


def boomerang_gif(stream, look="CYBERGLITCH", count=12, sound=True, progress=None):
    """Short forward/reverse GIF burst (no separate video or original frames)."""
    output = reserve_path("GIFs", "boomerang_" + look.lower(), "gif")
    click(sound)
    seq, _ = stream.latest()
    frames = []
    start = time.monotonic()
    for i in range(count):
        delay = start + (1.8 * i/count) - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        seq, jpg = stream.next_frame(seq, timeout=2.5)
        if not jpg:
            break
        try:
            with Image.open(io.BytesIO(jpg)) as im:
                frames.append(im.convert("RGB"))
        except OSError:
            continue
        if progress:
            progress(len(frames), count)
    if len(frames) < 3:
        raise RuntimeError("Boomerang needs at least three camera frames")
    bounce = frames + list(reversed(frames[1:-1]))
    save_gif(bounce, look, output, duration=95)
    return output, len(bounce)


# ------------------------ Authenticated Wi-Fi gallery ---------------------

WEB_FOLDERS = ("Photos", "GIFs", "Videos", "Timelapse", "StopMotion")
WEB_EXTENSIONS = frozenset((".jpg", ".jpeg", ".png", ".gif", ".mp4"))


def web_media(root=ROOT):
    root = Path(root)
    found = []
    for name in WEB_FOLDERS:
        directory = root / name
        if directory.is_symlink() or not directory.exists():
            continue
        # Only public output files in each folder, not thousands of timelapse
        # source frames inside per-session subdirectories.
        for path in directory.iterdir():
            if path.is_file() and not path.is_symlink() and path.suffix.lower() in WEB_EXTENSIONS:
                found.append(path)
    return sorted(found, key=lambda p:p.stat().st_mtime_ns, reverse=True)


def safe_web_path(rel, root=ROOT):
    """Only allow files inside recognized media dirs, never links or traversal."""
    root = Path(root).resolve()
    path = Path(rel)
    if path.is_absolute() or len(path.parts) != 2 or path.parts[0] not in WEB_FOLDERS:
        raise ValueError("Invalid media path")
    target = root.joinpath(*path.parts)
    if (root/path.parts[0]).is_symlink() or target.is_symlink() or target.suffix.lower() not in WEB_EXTENSIONS:
        raise ValueError("Unsupported media")
    try:
        resolved = target.resolve(strict=True)
    except OSError:
        raise ValueError("File not found")
    if resolved.parent != (root/path.parts[0]).resolve() or not resolved.is_relative_to(root):
        raise ValueError("Invalid target")
    return resolved


class GalleryHTTP(BaseHTTPRequestHandler):
    server_version = "CyberCam/0.5"

    def log_message(self, fmt, *args):
        # Do not log URLs, as they may include the one-time gallery access token.
        return

    def _headers(self, status, content_type, length=None, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; media-src 'self'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'")
        if length is not None:
            self.send_header("Content-Length", str(length))
        for k,v in (extra or {}).items():
            self.send_header(k,v)
        self.end_headers()

    def _authorized(self):
        cookie = cookies.SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except cookies.CookieError:
            return False
        session = cookie.get("cybercam_session")
        return bool(session and hmac.compare_digest(session.value, self.server.gallery_token))

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        # One-time token sets an HttpOnly cookie and redirects to URL without token.
        if parsed.path == "/" and "key" in params:
            value = params.get("key", [""])[0]
            if not hmac.compare_digest(value, self.server.gallery_token):
                self._headers(403, "text/plain; charset=utf-8", 0)
                return
            self._headers(303,"text/plain; charset=utf-8",0,
                          {"Location":"/", "Set-Cookie":
                           "cybercam_session="+value+"; HttpOnly; SameSite=Strict; Path=/"})
            return
        if not self._authorized():
            body=b"CyberCam: access token required. Use the URL shown by 'cybercam web'."
            self._headers(403,"text/plain; charset=utf-8",len(body))
            self.wfile.write(body)
            return
        if parsed.path == "/":
            self._index()
        elif parsed.path == "/media":
            self._media(params)
        else:
            self._headers(404,"text/plain; charset=utf-8",0)

    def _index(self):
        items = web_media(self.server.media_root)
        cards = []
        for item in items[:150]:
            rel = item.relative_to(self.server.media_root).as_posix()
            link = "/media?path=" + urllib.parse.quote(rel, safe="")
            name = html.escape(item.name)
            if item.suffix.lower() == ".mp4":
                art = '<video controls preload="metadata" playsinline src="'+link+'"></video>'
            else:
                art = '<img loading="lazy" alt="'+name+'" src="'+link+'">'
            cards.append('<article>'+art+'<div class="info"><strong>'+name+'</strong><a href="'+link+'&download=1">Download</a></div></article>')
        html_doc = '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CyberCam Gallery</title><style>body{margin:0;background:#080e18;color:#e8f9fa;font:15px system-ui,sans-serif}header{padding:20px 5%;background:#112134;border-bottom:2px solid #11dddd}h1{margin:0;color:#17f4e4}small{color:#abc}main{padding:15px 5%;display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px}article{background:#132333;border:1px solid #275565;border-radius:10px;overflow:hidden}img,video{width:100%;height:200px;object-fit:contain;background:#070c12}.info{padding:12px;overflow-wrap:anywhere}.info strong{display:block;margin-bottom:8px}a{color:#1bfff0}</style></head><body><header><h1>CYBERCAM</h1><small>by Cybern0mads · Local Wi-Fi Gallery · Downloads</small></header><main>'''+"".join(cards)+'''</main></body></html>'''
        data=html_doc.encode("utf-8")
        self._headers(200,"text/html; charset=utf-8",len(data))
        self.wfile.write(data)

    def _media(self, params):
        try:
            rel = params.get("path", [""])[0]
            path = safe_web_path(rel,self.server.media_root)
        except (ValueError, OSError):
            self._headers(404,"text/plain; charset=utf-8",0)
            return
        size=path.stat().st_size
        start,end=0,size-1
        range_header=self.headers.get("Range", "")
        if range_header:
            try:
                if not range_header.startswith("bytes=") or "," in range_header:
                    raise ValueError()
                first,last=range_header[6:].split("-",1)
                if not first:
                    n=int(last)
                    start=max(0,size-n)
                else:
                    start=int(first)
                if last and first:
                    end=min(size-1,int(last))
                if start<0 or start> end or start>=size:
                    raise ValueError()
            except (ValueError,IndexError):
                self._headers(416,"text/plain; charset=utf-8",0,{"Content-Range":f"bytes */{size}"})
                return
        headers={"Accept-Ranges":"bytes"}
        if params.get("download", [""])[0] == "1":
            headers["Content-Disposition"]='attachment; filename="'+path.name.replace('"','')+'"'
        if range_header:
            headers["Content-Range"]=f"bytes {start}-{end}/{size}"
        mime=mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self._headers(206 if range_header else 200,mime,end-start+1,headers)
        try:
            with path.open("rb") as handle:
                handle.seek(start)
                remaining=end-start+1
                while remaining>0:
                    chunk=handle.read(min(65536,remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining-=len(chunk)
        except (BrokenPipeError,ConnectionResetError):
            pass


def serve_gallery(bind="127.0.0.1", port=8765, root=ROOT, token=None):
    token=token or secrets.token_urlsafe(20)
    with ThreadingHTTPServer((bind,int(port)), GalleryHTTP) as server:
        server.gallery_token=token
        server.media_root=Path(root).resolve()
        print(f"CyberCam Wi-Fi Gallery on {bind}:{port}",flush=True)
        print(f"Open http://<Cardputer-IP>:{port}/?key={token}",flush=True)
        if bind == "127.0.0.1":
            print(f"For Mac: ssh -L {port}:127.0.0.1:{port} pi0@CardputerZero.local",flush=True)
            print(f"Then open http://127.0.0.1:{port}/?key={token}",flush=True)
        print("Anyone with this URL can access CyberCam media while sharing is on. Press Ctrl+C to stop.",flush=True)
        try:
            server.serve_forever(poll_interval=.2)
        except KeyboardInterrupt:
            pass


FBIOGET_VSCREENINFO = 0x4600
FBIOGET_FSCREENINFO = 0x4602
FIX_FMT = "16sL" + "I" * 6 + "HHHI"


class Framebuffer:
    def __init__(self, path="/dev/fb0"):
        self.fd = os.open(path, os.O_RDWR)
        self.mm = None
        try:
            v = bytearray(160)
            fcntl.ioctl(self.fd, FBIOGET_VSCREENINFO, v)
            self.w, self.h, _, _, self.xoff, self.yoff, self.bpp, _ = struct.unpack_from("8I", v)
            f = bytearray(struct.calcsize(FIX_FMT))
            fcntl.ioctl(self.fd, FBIOGET_FSCREENINFO, f)
            parsed = struct.unpack(FIX_FMT, f)
            size, self.stride = parsed[2], parsed[8]
            if self.bpp != 16:
                raise RuntimeError(f"CyberCam requires RGB565 framebuffer, found {self.bpp}bpp")
            if self.stride < (self.xoff + self.w) * 2:
                raise RuntimeError("Framebuffer stride too small")
            self.mm = mmap.mmap(self.fd, size, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
        except Exception:
            os.close(self.fd)
            raise

    def draw(self, image):
        if image.size != (self.w, self.h):
            image = image.resize((self.w, self.h), Image.Resampling.BILINEAR)
        rgb = image.convert("RGB")
        rowbytes = self.w * 2
        if np is not None:
            # Vectorized RGB565 little-endian, avoids ~54k per-pixel Python loops.
            a = np.asarray(rgb, dtype=np.uint8)
            r = a[:, :, 0].astype(np.uint16)
            g = a[:, :, 1].astype(np.uint16)
            b = a[:, :, 2].astype(np.uint16)
            values = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
            out = values.astype("<u2", copy=False).tobytes()
        else:
            px = rgb.tobytes()
            out = bytearray(self.w * self.h * 2)
            for i in range(self.w * self.h):
                pos = i * 3
                r, g, b = px[pos], px[pos + 1], px[pos + 2]
                value = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
                out[2 * i] = value & 255
                out[2 * i + 1] = (value >> 8) & 255
        if self.stride == rowbytes and self.xoff == 0 and self.yoff == 0:
            self.mm[:len(out)] = out
        else:
            for y in range(self.h):
                offset = (self.yoff + y) * self.stride + self.xoff * 2
                self.mm[offset: offset + rowbytes] = out[y * rowbytes:(y+1) * rowbytes]

    def close(self):
        if self.mm is not None:
            self.mm.close()
        os.close(self.fd)



# Playback is deliberately limited to CyberCam's own media directories.
# Gallery file browsing never modifies or deletes recordings.
GALLERY_DIRS = ("Photos", "GIFs", "Videos", "Timelapse", "StopMotion")
GALLERY_SUFFIXES = frozenset((".jpg", ".jpeg", ".png", ".gif", ".mp4"))
VIDEO_PLAYBACK_FPS = 15


def gallery_files(root=None):
    root = ROOT if root is None else Path(root)
    paths = []
    for folder in GALLERY_DIRS:
        directory = root / folder
        if not directory.is_dir():
            continue
        for path in directory.iterdir():
            if path.is_file() and not path.is_symlink() and path.suffix.lower() in GALLERY_SUFFIXES:
                paths.append(path)
    # Newest first; fall back to filename on identical timestamps.
    return sorted(paths, key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True)


class VideoPlayback:
    """Decode MP4 to small RGB frames with ffmpeg; never monopolize the UI thread.

    Audio is intentionally disabled because CyberCam records silent video.
    No attempt is made to accelerate decode when hardware APIs are unavailable.
    """
    def __init__(self, path, size=(224, 170), fps=VIDEO_PLAYBACK_FPS):
        self.path = Path(path)
        self.size = size
        self.fps = fps
        self.proc = None
        self.worker = None
        self.stop_event = threading.Event()
        self.paused = False
        self.completed = False
        self.error = None
        self.frame = None
        self.seq = 0
        self.lock = threading.Lock()

    def command(self):
        w, h = self.size
        # scale+pad preserves the full recorded field of view.
        vf = (f"fps={self.fps},scale={w}:{h}:force_original_aspect_ratio=decrease,"
              f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=0x0e141f")
        return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                "-threads", "2", "-i", str(self.path), "-an", "-vf", vf,
                "-pix_fmt", "rgb24", "-f", "rawvideo", "-y", "pipe:1"]

    def start(self):
        if not shutil.which("ffmpeg"):
            raise RuntimeError("Video playback needs ffmpeg (sudo apt install ffmpeg)")