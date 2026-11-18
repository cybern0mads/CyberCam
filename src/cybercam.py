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
        cmd = self.command()
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        self.worker = threading.Thread(target=self._decode, daemon=True)
        self.worker.start()

    def _decode(self):
        frame_bytes = self.size[0] * self.size[1] * 3
        n = 0
        started = time.monotonic()
        proc = self.proc
        try:
            while not self.stop_event.is_set() and proc is not None and proc.stdout:
                # Pause before read, so a paused view stays on one frame.
                while self.paused and not self.stop_event.is_set():
                    time.sleep(0.03)
                    started = time.monotonic() - n / self.fps
                if self.stop_event.is_set():
                    break
                data = bytearray()
                while len(data) < frame_bytes and not self.stop_event.is_set():
                    part = proc.stdout.read(frame_bytes - len(data))
                    if not part:
                        break
                    data.extend(part)
                if len(data) != frame_bytes:
                    break
                frame = Image.frombytes("RGB", self.size, bytes(data))
                with self.lock:
                    self.frame = frame
                    self.seq += 1
                n += 1
                remaining = started + (n / self.fps) - time.monotonic()
                if remaining > 0:
                    self.stop_event.wait(remaining)
            if not self.stop_event.is_set():
                if n == 0:
                    self.error = "No video frames decoded"
                self.completed = True
        except Exception as exc:
            self.error = str(exc)
            self.completed = True
        finally:
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                except OSError:
                    pass

    def latest(self):
        with self.lock:
            return self.seq, self.frame

    def stop(self):
        self.stop_event.set()
        proc = self.proc
        if proc is not None:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)
            if proc.stdout:
                proc.stdout.close()
        if self.worker is not None and self.worker is not threading.current_thread():
            self.worker.join(timeout=2)
        self.proc = None
        self.worker = None


class Gallery:
    """Local playback state; safe for use from UI loop, no background camera work."""
    def __init__(self, root=None):
        self.items = gallery_files(root)
        self.index = 0
        self.reader = None
        self.video = None
        self.scene = None
        self.playing = True
        self.full = False
        self.next_frame_at = 0.0
        self.seq = 0
        self.message = ""
        self.kind = ""
        self._video_seq = -1
        self.select(0)

    @property
    def path(self):
        if not self.items:
            return None
        return self.items[self.index]

    def close_current(self):
        if self.video is not None:
            self.video.stop()
            self.video = None
        if self.reader is not None:
            self.reader.close()
            self.reader = None

    def close(self):
        self.close_current()

    def select(self, offset):
        self.close_current()
        self.full = False
        self.message = ""
        self._video_seq = -1
        self.playing = True
        self.index = (self.index + offset) % len(self.items) if self.items else 0
        self.scene = None
        path = self.path
        self.kind = path.suffix.lower().lstrip(".") if path else ""
        self.seq += 1
        if path is None:
            self.message = "NO MEDIA - CAPTURE FIRST"
            return
        if self.kind == "mp4":
            self.message = "PRESS 6 TO PLAY"
            return
        try:
            self.reader = Image.open(path)
            self.reader.seek(0)
            self.scene = self.reader.convert("RGB")
            self.next_frame_at = time.monotonic() + self._delay()
        except (OSError, ValueError) as exc:
            self.message = "IMAGE ERROR: " + str(exc)[:20]
            self.close_current()

    def _delay(self):
        if self.reader is None:
            return 0.13
        return max(0.04, min(3, self.reader.info.get("duration", 130) / 1000.0))

    def toggle(self):
        if not self.items:
            return
        if self.kind == "mp4":
            if self.video is None or self.video.completed:
                if self.video is not None:
                    self.video.stop()
                self.video = VideoPlayback(self.path)
                try:
                    self.video.start()
                except (OSError, RuntimeError) as exc:
                    self.message = str(exc)[:35]
                    self.video = None
                    return
                self.message = "PLAYING"
                self.playing = True
            else:
                self.video.paused = not self.video.paused
                self.playing = not self.video.paused
                self.message = "PLAYING" if self.playing else "PAUSED"
        elif self.kind == "gif":
            self.playing = not self.playing
            self.message = "PLAYING" if self.playing else "PAUSED"
        else:
            self.full = not self.full
        self.seq += 1

    def tick(self):
        now = time.monotonic()
        if self.kind == "gif" and self.playing and self.reader is not None and now >= self.next_frame_at:
            try:
                frame_index = (self.reader.tell() + 1) % self.reader.n_frames
                self.reader.seek(frame_index)
                self.scene = self.reader.convert("RGB")
                self.next_frame_at = now + self._delay()
                self.seq += 1
            except (OSError, ValueError, EOFError):
                self.message = "GIF READ ERROR"
                self.playing = False
        if self.kind == "mp4" and self.video is not None:
            seq, frame = self.video.latest()
            if frame is not None and seq != self._video_seq:
                self.scene = frame
                self._video_seq = seq
                self.seq += 1
            if self.video.completed:
                self.message = "FINISHED - 6 REPLAY" if not self.video.error else "VIDEO ERROR"
        return self.scene


def draw_gallery(scene, gallery, width=320, height=170):
    """Gallery lives on the LCD; all buttons stay visible on a 320x170 device."""
    if gallery.full and gallery.kind not in ("gif", "mp4") and scene is not None:
        image = preview_size(scene, (width, height))
        d = ImageDraw.Draw(image)
        d.rectangle((0, height - 16, width, height), fill=(13, 22, 35))
        d.text((6, height - 13), "4 PREV  5 NEXT  6 BACK  9 CAMERA", font=font(9), fill=MINT)
        return image
    image = Image.new("RGB", (width, height), (10, 16, 25))
    panel_w = 96 if width >= 290 else max(65, width // 3)
    left_w = width - panel_w
    if scene is not None:
        image.paste(preview_size(scene, (left_w, height)), (0, 0))
    d = ImageDraw.Draw(image)
    d.rectangle((left_w, 0, width, height), fill=(13, 22, 35))
    d.line((left_w, 0, left_w, height), fill=MINT, width=2)
    d.text((left_w+7, 4), "GALLERY", font=font(11, True), fill=MINT)
    if gallery.items:
        d.text((left_w+7, 22), f"{gallery.index+1}/{len(gallery.items)} {gallery.kind.upper()}",
               font=font(9), fill=PAPER)
        name = gallery.path.name
        d.text((left_w+7, 39), name[:14], font=font(8), fill=(175, 190, 198))
        d.text((left_w+7, 50), name[14:28], font=font(8), fill=(175, 190, 198))
    else:
        d.text((6, height//2), "NO SAVED MEDIA", font=font(11, True), fill=PAPER)
    for y, label in ((67, "4 < PREV"), (91, "5 > NEXT")):
        d.rounded_rectangle((left_w+5,y,width-5,y+21), radius=4,
                            fill=(27,36,49), outline=(55,70,80), width=1)
        d.text((left_w+12,y+4), label, font=font(10), fill=PAPER)
    if gallery.kind == "mp4":
        action = "6 PAUSE" if gallery.video and not gallery.video.completed and gallery.playing else "6 PLAY"
    elif gallery.kind == "gif":
        action = "6 PAUSE" if gallery.playing else "6 PLAY"
    else:
        action = "6 FULL"
    d.rounded_rectangle((left_w+5,118,width-5,140), radius=4, fill=MINT)
    d.text((left_w+12,123), action, font=font(9, True), fill=(9,24,28))
    d.text((left_w+7,149), "9 CAMERA", font=font(9, True), fill=(168,215,205))
    if gallery.message:
        d.rectangle((2,height-19,left_w-2,height-2), fill=(17,32,43))
        d.text((6,height-16), gallery.message[:27], font=font(9), fill=MINT)
    return image


def get_keyboards():
    if list_devices is None:
        return []
    found = []
    for path in list_devices():
        try:
            dev = InputDevice(path)
            capabilities = dev.capabilities().get(ecodes.EV_KEY, [])
            if ecodes.KEY_ESC in capabilities and ecodes.KEY_ENTER in capabilities:
                found.append(dev)
            else:
                dev.close()
        except (OSError, PermissionError):
            continue
    # Favor Cardputer's TCA8418 keyboard; avoid stealing unrelated USB devices.
    found.sort(key=lambda d: ("tca8418" not in d.name.lower(), d.path))
    return found[:1]


def read_keys(devices):
    if not devices:
        return []
    try:
        readable, _, _ = select.select([d.fd for d in devices], [], [], 0)
        keys = []
        for dev in devices:
            if dev.fd in readable:
                for event in dev.read():
                    if event.type == ecodes.EV_KEY and event.value == 1:
                        keys.append(event.code)
        return keys
    except (OSError, BlockingIOError):
        return []


@lru_cache(maxsize=32)
def font(size=12, bold=False):
    base = "/usr/share/fonts/truetype/dejavu/DejaVuSans"
    try:
        return ImageFont.truetype(base + ("-Bold" if bold else "") + ".ttf", size)
    except OSError:
        return ImageFont.load_default()


def capture_action_label(mode, status):
    """Show the actual hardware capture key prominently on the button."""
    if mode == "VIDEO":
        return "6 STOP" if status == "REC" else "6 RECORD"
    if mode == "TIMELAPSE":
        return "6 STOP" if status.startswith("LAPSE") else "6 START"
    if mode == "STOPMOTION":
        return "6 FRAME"
    if mode == "BOOMERANG":
        return "6 LOOP"
    return "6 CAPTURE"


def draw_mode_menu(current="PHOTO", selected=0, width=320, height=170):
    """Seven genuinely selectable tiles, with a prominent Gallery destination.

    6 camera modes in a two-column grid; Gallery spans the full bottom row.
    """
    canvas = Image.new("RGB", (width, height), (9, 16, 28))
    d = ImageDraw.Draw(canvas)
    d.rectangle((0, 0, width, 23), fill=(17, 31, 46))
    d.text((9, 4), "CYBERCAM  /  SELECT MODE", font=font(12, True), fill=MINT)
    d.text((width-55, 7), "0 BACK", font=font(8), fill=(175, 195, 209))
    choices = (
        ("PHOTO", "1 PHOTO", "8MP + FILTERS"),
        ("GIF", "2 GIF", "ANIMATED BURST"),
        ("VIDEO", "3 VIDEO", "MP4 RECORDING"),
        ("TIMELAPSE", "4 TIMELAPSE", "FRAMES TO MP4"),
        ("STOPMOTION", "5 STOP MOTION", "ONION-SKIN FRAMES"),
        ("BOOMERANG", "6 BOOMERANG", "FORWARD + REVERSE"),
    )
    gutter, gap = 8, 8
    tile_w = (width - 2*gutter - gap)//2
    for index, (name, title, subtitle) in enumerate(choices):
        col, row = index%2, index//2
        x = gutter + col*(tile_w+gap)
        y = 26 + row*33
        active = index == selected
        bg = (31, 83, 83) if active else (24, 38, 55)
        outline = MINT if active else (57, 77, 96)
        d.rounded_rectangle((x, y, x+tile_w-1, y+29), radius=5,
                            fill=bg, outline=outline, width=2 if active else 1)
        d.text((x+9, y+2), title, font=font(10, active), fill=PAPER if active else (205, 225, 233))
        d.text((x+9, y+18), subtitle, font=font(8), fill=(164, 220, 210) if active else (156, 177, 188))
        if name == current:
            d.ellipse((x+tile_w-15, y+5, x+tile_w-9, y+11), fill=MAGENTA)
    active = selected == len(MODES)
    d.rounded_rectangle((gutter, 127, width-gutter, 150), radius=6,
                        fill=(79, 40, 104) if active else (36, 35, 70),
                        outline=MINT if active else (191, 110, 224), width=2 if active else 1)
    # Graphic album icon signals a different destination from the camera modes.
    d.rounded_rectangle((18, 132, 32, 144), radius=2, outline=MINT, width=2)
    d.ellipse((21, 134, 25, 138), fill=MINT)
    d.line((20, 142, 24, 139, 27, 141, 30, 137), fill=MINT, width=1)
    d.text((40, 131), "9 GALLERY", font=font(11, True), fill=PAPER)
    d.text((139, 134), "VIEW PHOTOS / GIFS / VIDEO", font=font(9), fill=(199, 216, 238))
    d.rectangle((0, height-17, width, height), fill=(13, 26, 38))
    d.text((9, height-14), "ARROWS MOVE  |  ENTER SELECT  |  0 BACK", font=font(8), fill=(190, 218, 222))
    return canvas


def draw_ui(scene, mode="PHOTO", style="NORMAL", resolution="8MP", status="READY",
            sound=True, recording_seconds=0, width=320, height=170, fps=0):
    """Final camera UI: Photo/GIF/Video/Lapse + a full-width 9 GALLERY button.

    Gallery is a prominent action rather than a tiny keyboard hint. The
    viewfinder remains as wide as v0.5.1, preserving full-sensor framing.
    """
    image = Image.new("RGB", (width, height), (10, 16, 25))
    panel_w = 99 if width >= 290 else max(74, width//3)
    view_w = width - panel_w
    if scene is not None:
        image.paste(preview_size(scene, (view_w, height)), (0, 0))
    d = ImageDraw.Draw(image)
    d.rectangle((view_w, 0, width, height), fill=(13, 22, 35))
    d.line((view_w, 0, view_w, height), fill=MINT, width=2)
    d.text((view_w+7, 2), "CYBERCAM", font=font(10, True), fill=MINT)
    d.text((view_w+7, 16), "0 ALL MODES", font=font(8, True), fill=(176, 211, 235))
    for i, (value, label) in enumerate((
        ("PHOTO", "1 PHOTO"), ("GIF", "2 GIF"),
        ("VIDEO", "3 VIDEO"), ("TIMELAPSE", "4 TIMELAPSE"),
    )):
        y = 29 + i*18
        active = mode == value
        d.rounded_rectangle((view_w+5, y, width-5, y+16), radius=4,
                            fill=(36, 98, 91) if active else (27, 36, 49),
                            outline=MINT if active else (50, 70, 82), width=1)
        d.text((view_w+12, y+2), label, font=font(9, active),
               fill=PAPER if active else (183, 204, 216))
    # High-contrast, one-press Gallery action. No nested menus needed.
    d.rounded_rectangle((view_w+5, 104, width-5, 127), radius=5,
                        fill=(52, 40, 85), outline=(185, 100, 225), width=2)
    d.text((view_w+12, 108), "9 GALLERY", font=font(11, True), fill=PAPER)
    recording_active = mode == "VIDEO" and status == "REC"
    d.rounded_rectangle((view_w+5, 132, width-5, 154), radius=5,
                        fill=MAGENTA if recording_active else MINT)
    d.text((view_w+10, 136), capture_action_label(mode, status),
           font=font(9, True), fill=(9, 24, 28))
    d.text((view_w+7, 158), f"{fps:.0f}FPS {'SND' if sound else 'MUTE'}", font=font(8), fill=(177, 199, 204))
    if mode == "TIMELAPSE":
        detail, hint = f"LAPSE: EVERY {resolution}", "7 INTERVAL  |  6 START/STOP"
    elif mode == "STOPMOTION":
        detail, hint = f"STOP MOTION: {resolution}", "6 FRAME  |  7 EXPORT"
    elif mode == "BOOMERANG":
        detail, hint = "BOOMERANG", "5 LOOK  |  6 LOOP"
    elif mode == "VIDEO":
        detail, hint = f"VIDEO: {resolution}", "7 SIZE  |  6 RECORD/STOP"
    elif mode == "PHOTO":
        detail, hint = f"{style}  /  {resolution}", "5 LOOK  |  7 SIZE"
    else:
        detail, hint = style, "5 LOOK  |  6 CAPTURE"
    d.rectangle((2, 1, min(view_w-2, 198), 17), fill=(14, 30, 43))
    d.text((6, 3), detail[:29], font=font(9, True), fill=MINT)
    if status == "REC":
        d.rounded_rectangle((5, 22, min(view_w-5, 114), 44), radius=4, fill=(100, 21, 39))
        d.ellipse((12, 29, 20, 37), fill=(255, 66, 89))
        d.text((25, 27), f"REC {recording_seconds}s", font=font(11, True), fill=PAPER)
    elif scene is None:
        d.text((10, height//2), "STARTING CAMERA", font=font(10), fill=PAPER)
    d.rectangle((2, height-17, view_w-2, height-2), fill=(17, 32, 43))
    d.text((6, height-15), hint[:34], font=font(8), fill=(178, 214, 225))
    if status not in ("READY", "REC"):
        d.rectangle((2, height-34, view_w-2, height-18), fill=(17, 32, 43))
        d.text((6, height-32), status[:31], font=font(9, True),
               fill=MAGENTA if status.startswith("ERR") else MINT)
    return image


class CyberCamUI:
    def __init__(self):
        self.settings = load_settings()
        self.mode = "PHOTO"
        self.mode_menu = False
        self.mode_selected = 0
        self.gallery = None
        self.status = "READY"
        self.stream = MjpegStream(rotation=self.settings["rotation"], fps=self.settings["preview_fps"])
        self.recording = None
        self.timelapse = None
        self.stopmotion = None
        self.display = None
        self.devices = []
        self.busy = False
        self.preview = None
        self.last_seq = -1
        self.running = True
        self.status_deadline = 0
        self.worker = None
        self.fps = 0.0
        self.frame_counter = 0
        self.fps_time = time.monotonic()

    def set_status(self, value, delay=3):
        self.status = value
        self.status_deadline = time.monotonic() + delay

    def do_photo(self):
        def operation():
            self.stream.stop()
            try:
                original, effect = take_photo(self.settings["filter"],
                                RESOLUTIONS[self.settings["resolution"]], self.settings["sound"], self.settings["rotation"])
                self.set_status("SAVED " + (effect or original).name[:27], 4)
            finally:
                self.stream.start()
        self.job(operation, "CAPTURING 8MP...")

    def do_gif(self):
        def operation():
            path, count = gif_burst(self.stream, self.settings["filter"],
                progress=lambda n, total: self.set_status(f"GIF {n}/{total}", 8),
                sound=self.settings["sound"])
            self.set_status(f"GIF SAVED {count} FRAMES", 4)
        self.job(operation, "GIF 0/15")

    def do_boomerang(self):
        def operation():
            path, count = boomerang_gif(self.stream, self.settings["filter"],
                sound=self.settings["sound"],
                progress=lambda n, total:self.set_status(f"LOOP {n}/{total}", 10))
            self.set_status(f"BOOM SAVED {count}F", 5)
        self.job(operation, "BOOMERANG...")

    def do_stopmotion_frame(self):
        if self.stopmotion is None:
            self.stopmotion = StopMotionSession(self.settings["filter"])
        seq, jpg = self.stream.latest()
        try:
            self.stopmotion.capture(jpg)
            click(self.settings["sound"])
            self.set_status(f"FRAME {len(self.stopmotion.frames)} - 7 EXPORT", 6)
        except (OSError, RuntimeError) as exc:
            self.set_status("ERR " + str(exc)[:26], 5)

    def export_stopmotion(self):
        if self.stopmotion is None or len(self.stopmotion.frames) < 2:
            self.set_status("NEED AT LEAST 2 FRAMES", 4)
            return
        session = self.stopmotion
        def operation():
            path = session.export()
            self.stopmotion = None
            self.set_status("SAVED STOP MOTION MP4+GIF", 6)
        self.job(operation, "EXPORTING MP4 + GIF...")

    def do_timelapse(self):
        interval = TIMELAPSE_INTERVALS[self.settings["timelapse_interval"]]
        self.timelapse = TimelapseCapture(interval=interval,
                    resolution=RESOLUTIONS[self.settings["resolution"]],
                    rotation=self.settings["rotation"],
                    look=self.settings["filter"])
        session = self.timelapse
        def operation():
            self.stream.stop()
            try:
                path = session.run(progress=lambda n, exporting=False:
                    self.set_status("EXPORTING LAPSE MP4..." if exporting
                                    else f"LAPSE {n}F - 6 STOP", 40))
                self.set_status("SAVED TIMELAPSE MP4", 6)
            finally:
                self.timelapse = None
                self.stream.start()
        self.job(operation, "LAPSE STARTING...")

    def start_recording(self):
        if self.busy:
            return
        self.busy = True
        try:
            self.stream.stop()
            self.recording = Recording(VIDEOS[self.settings["video"]], rotation=self.settings["rotation"])
            self.recording.start(sound=self.settings["sound"])
            self.status = "REC"
        except Exception as exc:
            self.recording = None
            self.set_status("ERR " + str(exc)[:37], 7)
            self.stream.start()
        finally:
            self.busy = False

    def stop_recording(self):
        def operation():
            try:
                path = self.recording.stop(sound=self.settings["sound"])
                self.set_status("VIDEO SAVED " + path.name[:20], 5)
            finally:
                self.recording = None
                self.stream.start()
        self.job(operation, "FINALIZING MP4...")

    def job(self, task, headline):
        if self.busy:
            return
        self.busy = True
        self.set_status(headline, 30)
        def wrapper():
            try:
                task()
            except Exception as exc:
                self.set_status("ERR " + str(exc)[:36], 8)
                # Still camera may be stopped after a failed operation.
                if self.stream.proc is None and self.recording is None:
                    try:
                        self.stream.start()
                    except Exception:
                        pass
            finally:
                self.busy = False
        self.worker = threading.Thread(target=wrapper, daemon=True)
        self.worker.start()

    def enter_gallery(self):
        if self.recording is not None or self.busy:
            self.set_status("STOP RECORDING FIRST", 3)
            return
        self.stream.stop()
        self.gallery = Gallery()
        self.preview = None
        self.last_seq = -1

    def leave_gallery(self):
        if self.gallery is None:
            return
        self.gallery.close()
        self.gallery = None
        self.preview = None
        self.last_seq = -1
        self.fps_time = time.monotonic()
        self.frame_counter = 0
        self.stream.start()

    def select_mode(self, value):
        """Switch mode without starting a capture. Preserve all media/settings."""
        if value not in MODES:
            raise ValueError(value)
        self.mode = value
        self.mode_selected = MODES.index(value)
        self.mode_menu = False
        self.set_status("READY", .1)

    def key(self, code):
        if self.gallery is not None:
            if code in (ecodes.KEY_ESC, ecodes.KEY_Q, ecodes.KEY_9):
                self.leave_gallery()
            elif code in (ecodes.KEY_4, ecodes.KEY_LEFT):
                self.gallery.select(-1)
            elif code in (ecodes.KEY_5, ecodes.KEY_RIGHT):
                self.gallery.select(1)
            elif code in (ecodes.KEY_ENTER, ecodes.KEY_SPACE, ecodes.KEY_6):
                self.gallery.toggle()
            return
        # Key 6 interrupts timelapse even while its worker is marked busy.
        if self.timelapse is not None and code in (ecodes.KEY_6, ecodes.KEY_ENTER, ecodes.KEY_SPACE):
            self.timelapse.stop()
            self.set_status("EXPORTING LAPSE...", 60)
            return
        if self.mode_menu:
            if code in (ecodes.KEY_ESC, ecodes.KEY_Q, ecodes.KEY_0, ecodes.KEY_M):
                self.mode_menu = False
            elif code in (ecodes.KEY_LEFT, ecodes.KEY_RIGHT, ecodes.KEY_UP, ecodes.KEY_DOWN, ecodes.KEY_TAB):
                if code == ecodes.KEY_LEFT:
                    self.mode_selected = (self.mode_selected - 1) % len(MENU_CHOICES)
                elif code in (ecodes.KEY_RIGHT, ecodes.KEY_TAB):
                    self.mode_selected = (self.mode_selected + 1) % len(MENU_CHOICES)
                elif code == ecodes.KEY_UP:
                    self.mode_selected = 4 if self.mode_selected == 6 else max(0, self.mode_selected - 2)
                else:
                    self.mode_selected = 6 if self.mode_selected >= 4 else self.mode_selected + 2
            elif code in (ecodes.KEY_1, ecodes.KEY_2, ecodes.KEY_3,
                          ecodes.KEY_4, ecodes.KEY_5, ecodes.KEY_6):
                number = (ecodes.KEY_1, ecodes.KEY_2, ecodes.KEY_3,
                          ecodes.KEY_4, ecodes.KEY_5, ecodes.KEY_6).index(code)
                self.select_mode(MODES[number])
            elif code in (ecodes.KEY_ENTER, ecodes.KEY_SPACE):
                if self.mode_selected == len(MODES):
                    self.mode_menu = False
                    self.enter_gallery()
                else:
                    self.select_mode(MODES[self.mode_selected])
            elif code == ecodes.KEY_9:
                self.mode_menu = False
                self.enter_gallery()
            return
        if code == ecodes.KEY_9:
            self.enter_gallery()
            return
        if code in (ecodes.KEY_ESC, ecodes.KEY_Q):
            if not self.busy:
                self.running = False
            return
        if self.busy:
            return
        if code in (ecodes.KEY_ENTER, ecodes.KEY_SPACE, ecodes.KEY_6):
            if self.mode == "PHOTO":
                self.do_photo()
            elif self.mode == "GIF":
                self.do_gif()
            elif self.mode == "VIDEO":
                if self.recording:
                    self.stop_recording()
                else:
                    self.start_recording()
            elif self.mode == "TIMELAPSE":
                self.do_timelapse()
            elif self.mode == "STOPMOTION":
                self.do_stopmotion_frame()
            elif self.mode == "BOOMERANG":
                self.do_boomerang()
            return
        # Never switch the camera mode during an active MP4 recording.
        if self.recording:
            return
        if code in (ecodes.KEY_0, ecodes.KEY_M):
            self.mode_menu = True
            self.mode_selected = MODES.index(self.mode)
        elif code in (ecodes.KEY_1, ecodes.KEY_2, ecodes.KEY_3, ecodes.KEY_4,
                      ecodes.KEY_P, ecodes.KEY_G, ecodes.KEY_V):
            value = {ecodes.KEY_1: "PHOTO", ecodes.KEY_2: "GIF",
                     ecodes.KEY_3: "VIDEO", ecodes.KEY_4: "TIMELAPSE",
                     ecodes.KEY_P: "PHOTO", ecodes.KEY_G: "GIF",
                     ecodes.KEY_V: "VIDEO"}[code]
            self.select_mode(value)
        elif code in (ecodes.KEY_UP, ecodes.KEY_DOWN, ecodes.KEY_TAB):
            direction = -1 if code == ecodes.KEY_UP else 1
            self.select_mode(MODES[(MODES.index(self.mode) + direction) % len(MODES)])
        elif code in (ecodes.KEY_5, ecodes.KEY_F, ecodes.KEY_RIGHT, ecodes.KEY_LEFT):
            direction = -1 if code == ecodes.KEY_LEFT else 1
            i = FILTERS.index(self.settings["filter"])
            self.settings["filter"] = FILTERS[(i + direction) % len(FILTERS)]
            save_settings(self.settings)
        elif code in (ecodes.KEY_7, ecodes.KEY_R):
            if self.mode == "STOPMOTION":
                self.export_stopmotion()
                return
            if self.mode == "TIMELAPSE":
                field, count = "timelapse_interval", len(TIMELAPSE_INTERVALS)
            else:
                field = "video" if self.mode == "VIDEO" else "resolution"
                count = len(VIDEOS) if field == "video" else len(RESOLUTIONS)
            self.settings[field] = (self.settings[field] + 1) % count
            save_settings(self.settings)
        elif code in (ecodes.KEY_8, ecodes.KEY_S):
            self.settings["sound"] = not self.settings["sound"]
            save_settings(self.settings)
            click(self.settings["sound"])

    def run(self, gallery_only=False):
        try:
            self.display = Framebuffer()
            self.devices = get_keyboards()
            if not self.devices:
                raise RuntimeError("No evdev keyboard accessible; see README permissions")
            if gallery_only:
                self.gallery = Gallery()
            else:
                self.stream.start()
            last_draw = 0.0
            last_status = None
            while self.running:
                for code in read_keys(self.devices):
                    self.key(code)
                now = time.monotonic()
                if self.gallery is not None:
                    scene = self.gallery.tick()
                    gallery_state = ("gallery", self.gallery.seq, self.gallery.message)
                    if gallery_state != last_status or now - last_draw > 0.3:
                        self.display.draw(draw_gallery(scene, self.gallery,
                                                      width=self.display.w, height=self.display.h))
                        last_draw = now
                        last_status = gallery_state
                    time.sleep(0.02)
                    continue
                if self.mode_menu:
                    menu_state = ("modes", self.mode, self.mode_selected)
                    if menu_state != last_status or now - last_draw > 2.0:
                        self.display.draw(draw_mode_menu(self.mode, self.mode_selected,
                                           width=self.display.w, height=self.display.h))
                        last_draw = now
                        last_status = menu_state
                    time.sleep(0.03)
                    continue
                if self.status != "REC" and now > self.status_deadline:
                    self.status = "READY"
                seq, jpg = self.stream.latest()
                new_frame = seq != self.last_seq and jpg is not None
                if new_frame:
                    try:
                        with Image.open(io.BytesIO(jpg)) as im:
                            self.preview = im.convert("RGB")
                        self.last_seq = seq
                        self.frame_counter += 1
                    except OSError:
                        new_frame = False
                if now - self.fps_time >= 1.0:
                    self.fps = self.frame_counter / max(.001, now - self.fps_time)
                    self.frame_counter = 0
                    self.fps_time = now
                if self.status == "REC" and self.recording and self.recording.proc and self.recording.proc.poll() is not None:
                    self.set_status("ERR recorder stopped; press ENTER", 6)
                # Only redraw when the camera provides a frame or UI state changes.
                # While recording, draw clock twice a second, not 8x unnecessarily.
                state = (self.status, self.mode, self.settings["filter"],
                         self.settings["resolution"], self.settings["video"],
                         self.settings["timelapse_interval"], self.settings["sound"])
                if not new_frame and state == last_status and now - last_draw < (0.5 if self.recording else 1.0):
                    time.sleep(0.012)
                    continue
                last_draw = now
                last_status = state
                mode = self.mode
                style = self.settings["filter"]
                if self.preview is not None and self.status != "REC":
                    # Filter only LCD-size frames. 15 FPS is a target, not guaranteed.
                    scene = preview_size(self.preview, (self.display.w - 96, self.display.h))
                    if mode == "STOPMOTION" and self.stopmotion is not None:
                        scene = self.stopmotion.onion_skin(scene)
                    if style != "NORMAL" and mode != "VIDEO":
                        try:
                            scene = filter_image(scene, style, preview=True)
                        except Exception:
                            pass
                else:
                    scene = self.preview
                w, h = RESOLUTIONS[self.settings["resolution"]]
                if mode == "VIDEO":
                    w, h, fps, bitrate = VIDEOS[self.settings["video"]]
                    label = VIDEO_LABELS[self.settings["video"]]
                elif mode == "TIMELAPSE":
                    label = str(TIMELAPSE_INTERVALS[self.settings["timelapse_interval"]]) + "s"
                elif mode == "STOPMOTION":
                    label = str(len(self.stopmotion.frames) if self.stopmotion else 0) + "F"
                else:
                    label = f"{w}x{h}" if mode == "PHOTO" else "15F/2S"
                elapsed = int(now - self.recording.started) if self.recording and self.recording.started else 0
                self.display.draw(draw_ui(scene, mode, style, label,
                    self.status, self.settings["sound"], elapsed,
                    width=self.display.w, height=self.display.h, fps=self.fps))
                time.sleep(0.008)
        finally:
            if self.gallery is not None:
                self.gallery.close()
                self.gallery = None
            if self.recording is not None:
                try:
                    self.recording.stop(sound=False)
                except Exception:
                    pass
            self.stream.stop()
            if self.worker and self.worker.is_alive():
                self.worker.join(timeout=15)
            for dev in self.devices:
                dev.close()
            if self.display:
                self.display.close()


def command_diagnose():
    print("CyberCam", VERSION, "diagnostic")
    print("Device:", sys.platform, os.uname().machine if hasattr(os, "uname") else "unknown")
    for command in ("rpicam-still", "rpicam-vid", "aplay", "ffmpeg"):
        print(f"{command}:", shutil.which(command) or "MISSING")
    for name in ("/dev/fb0", "/dev/input", str(SHUTTER)):
        print(name, "OK" if Path(name).exists() else "MISSING")
    print("evdev:", "available" if InputDevice else "missing: sudo apt install python3-evdev")
    print("numpy acceleration:", "available" if np is not None else "unavailable")
    print("settings:", load_settings())
    print("full-field camera mode (preview/video):", FULL_FOV_MODE)
    print("video presets:", VIDEO_LABELS)
    print("output:", ROOT)
    print("log:", LOG)
    return 0


def command_demo():
    """Offline demonstration without requiring a device or camera."""
    d = Image.new("RGB", (640, 480), (16, 29, 44))
    draw = ImageDraw.Draw(d)
    for x in range(0, 640, 13):
        draw.line((x, 0, (x + 210) % 640, 480), fill=(x % 120 + 40, 120, 145), width=1)
    draw.ellipse((173, 53, 467, 346), fill=(50, 204, 156), outline=(240, 240, 240), width=4)
    draw.rectangle((238, 158, 400, 257), fill=(18, 39, 55))
    draw.text((263, 197), "CYBER", font=font(22, True), fill=PAPER)
    path = Path.cwd() / "cybercam_ui_demo.png"
    draw_ui(d).save(path)
    gifpath = Path.cwd() / "cybercam_gboy_demo.gif"
    frames = [ImageEnhance.Brightness(d).enhance(0.65+i/20) for i in range(15)]
    save_gif(frames, "GREEN", gifpath)
    print("Created", path, "and", gifpath)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="CyberCam for Cardputer Zero")
    parser.add_argument("command", nargs="?", choices=["ui", "gallery", "photo", "gif", "video", "timelapse", "boomerang", "web", "demo", "diagnose", "config"], default="ui")
    parser.add_argument("--filter", choices=FILTERS, default="NORMAL")
    parser.add_argument("--resolution", choices=[f"{w}x{h}" for w, h in RESOLUTIONS], default="3280x2464")
    parser.add_argument("--seconds", type=int, default=8, help="CLI video duration, 1-600 seconds")
    parser.add_argument("--no-sound", action="store_true")
    parser.add_argument("--rotation", type=int, choices=(0, 180), default=None,
                        help="Photo, GIF and video orientation (0 or 180)")
    parser.add_argument("--preview-fps", type=int, choices=(12, 15, 20, 24, 30), default=None,
                        help="Set preview target FPS with the config command")
    parser.add_argument("--video-quality", choices=("wide", "720p", "1080p", "480p"), default="wide")
    parser.add_argument("--frames", type=int, default=12, help="Timelapse frame count, 1..20000")
    parser.add_argument("--interval", type=float, default=5, help="Timelapse interval seconds, minimum 2")
    parser.add_argument("--fps", type=int, default=15, help="Timelapse output FPS, 1..30")
    parser.add_argument("--bind", default="127.0.0.1", help="Gallery listen address; 0.0.0.0 allows LAN access")
    parser.add_argument("--port", type=int, default=8765, help="Gallery HTTP port")
    args = parser.parse_args(argv)
    try:
        settings = load_settings()
        if args.command == "config":
            if args.rotation is not None:
                settings["rotation"] = args.rotation
            if args.preview_fps is not None:
                settings["preview_fps"] = args.preview_fps
            if args.rotation is not None or args.preview_fps is not None:
                save_settings(settings)
            print(json.dumps(settings, indent=2))
            return 0
        rotation = settings["rotation"] if args.rotation is None else args.rotation
        if args.command == "demo":
            return command_demo()
        if args.command == "diagnose":
            return command_diagnose()
        if args.command == "gallery":
            return CyberCamUI().run(gallery_only=True) or 0
        if args.command == "web":
            if not (1024 <= args.port <= 65535):
                parser.error("--port must be 1024..65535")
            serve_gallery(bind=args.bind, port=args.port)
            return 0
        if args.command == "photo":
            dims = tuple(map(int, args.resolution.split("x")))
            original, filtered = take_photo(args.filter, dims, not args.no_sound, rotation)
            print("Original:", original)
            if filtered:
                print("Filtered:", filtered)
            return 0
        if args.command == "gif":
            s = MjpegStream(rotation=rotation, fps=settings["preview_fps"])
            try:
                s.start()
                seq, first = s.next_frame(s.seq, timeout=8)
                if first is None:
                    raise RuntimeError(f"No camera preview frames; see {LOG}")
                path, count = gif_burst(s, args.filter, sound=not args.no_sound)
                print(f"GIF ({count} frames):", path)
            finally:
                s.stop()
            return 0
        if args.command == "timelapse":
            if not (2 <= args.interval <= 1800 and 1 <= args.frames <= 20000 and 1 <= args.fps <= 30):
                parser.error("Timelapse --interval 2..1800; --frames 1..20000; --fps 1..30")
            session = TimelapseCapture(interval=args.interval,
                         resolution=tuple(map(int,args.resolution.split("x"))),
                         rotation=rotation, look=args.filter, fps=args.fps)
            path = session.run(count=args.frames, progress=lambda n,exporting=False:
                    print("Encoding MP4..." if exporting else f"Captured {n}/{args.frames}",flush=True))
            print("Timelapse MP4:",path,"Original frames:",session.folder)
            return 0
        if args.command == "boomerang":
            stream = MjpegStream(rotation=rotation,fps=settings["preview_fps"])
            try:
                stream.start()
                _, jpg = stream.next_frame(stream.seq,timeout=8)
                if not jpg:
                    raise RuntimeError("Camera preview unavailable")
                path,count = boomerang_gif(stream,args.filter,sound=not args.no_sound)
                print(f"Boomerang ({count} frames):",path)
            finally:
                stream.stop()
            return 0
        if args.command == "video":
            if not (1 <= args.seconds <= 600):
                parser.error("--seconds must be 1..600")
            index = {"wide": 0, "1080p": 1, "720p": 2, "480p": 3}[args.video_quality]
            record = Recording(VIDEOS[index], rotation=rotation)
            path = record.start(seconds=args.seconds, sound=not args.no_sound)
            print(f"Recording {args.seconds}s at {args.video_quality}; saving to {path}...")
            try:
                record.proc.wait(timeout=args.seconds + 20)
            except (KeyboardInterrupt, subprocess.TimeoutExpired):
                pass
            print("Saved:", record.stop(sound=not args.no_sound))
            return 0
        return CyberCamUI().run() or 0
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"CyberCam ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
