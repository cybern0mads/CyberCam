"""Regression tests for CyberCam v0.5.3 timelapse export and Gallery state."""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import cybercam as cam


class TimelapseExportTest(unittest.TestCase):
    def test_direct_jpeg_export_no_intermediate_png_and_valid_mp4(self):
        if not cam.shutil.which("ffmpeg"):
            self.skipTest("FFmpeg unavailable")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            frames = []
            for i in range(12):
                jpg = root / f"frame_{i:06d}.jpg"
                Image.new("RGB", (640, 480), (i*10, 30, 140)).save(jpg)
                frames.append(jpg)
            sizes = [p.stat().st_size for p in frames]
            out = root / "output.mp4"
            progress = []
            self.assertEqual(
                cam.encode_mp4(frames, out, "NORMAL", fps=15,
                               progress=lambda *args: progress.append(args)), out)
            self.assertTrue(out.is_file())
            self.assertGreater(out.stat().st_size, 1024)
            self.assertTrue(any(p[0] == "DONE" and p[1] == len(frames) for p in progress))
            self.assertEqual(sizes, [p.stat().st_size for p in frames])
            self.assertFalse((root / "output.partial.mp4").exists())
            self.assertEqual(sorted(x.suffix for x in root.iterdir()).count(".png"), 0)
            info = cam.subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(out)],
                capture_output=True, text=True, check=True)
            self.assertAlmostEqual(float(info.stdout.strip()), 12 / 15, delta=0.1)

    def test_filtered_export_preserves_jpegs(self):
        if not cam.shutil.which("ffmpeg"):
            self.skipTest("FFmpeg unavailable")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            frames = []
            for i in range(5):
                path = root / f"frame_{i:06d}.jpg"
                Image.new("RGB", (400, 300), (i*30, 80, 100)).save(path)
                frames.append(path)
            out = root / "filtered.mp4"
            progress = []
            cam.encode_mp4(frames, out, look="VIRTUALRED", fps=8,
                           progress=lambda *args: progress.append(args))
            self.assertGreater(out.stat().st_size, 1024)
            self.assertTrue(all(p.is_file() for p in frames))
            self.assertTrue(any(p[0] == "PROCESSING" for p in progress))
            self.assertEqual(progress[-1], ("DONE", len(frames), len(frames)))

    def test_failed_export_retains_frames_and_no_partial_mp4(self):
        if not cam.shutil.which("ffmpeg"):
            self.skipTest("FFmpeg unavailable")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            frames = []
            for i in range(3):
                path = root / f"frame_{i:06d}.jpg"
                path.write_bytes(b"not a real JPEG")
                frames.append(path)
            output = root / "bad.mp4"
            with self.assertRaises(RuntimeError):
                cam.encode_mp4(frames, output, fps=15)
            self.assertFalse(output.exists())
            self.assertFalse((root / "bad.partial.mp4").exists())
            self.assertEqual(len(list(root.glob("frame_*.jpg"))), 3)


class TimelapseUITest(unittest.TestCase):
    def setUp(self):
        keys = ["KEY_ESC", "KEY_Q", "KEY_9", "KEY_4", "KEY_LEFT",
                "KEY_5", "KEY_RIGHT", "KEY_ENTER", "KEY_SPACE", "KEY_6",
                "KEY_0", "KEY_M", "KEY_UP", "KEY_DOWN", "KEY_TAB",
                "KEY_1", "KEY_2", "KEY_3", "KEY_P", "KEY_G", "KEY_V",
                "KEY_7", "KEY_R", "KEY_8", "KEY_S", "KEY_F"]
        self.stub = SimpleNamespace(**{name: i + 1 for i, name in enumerate(keys)})
        self.keypatch = patch.object(cam, "ecodes", self.stub)
        self.keypatch.start()
        self.addCleanup(self.keypatch.stop)
        self.ui = cam.CyberCamUI()

    def test_gallery_shows_correct_message_during_export(self):
        self.ui.busy = True
        self.ui.timelapse = object()
        self.ui.timelapse_exporting = True
        self.ui.key(self.stub.KEY_9)
        self.assertEqual(self.ui.status, "EXPORTING MP4 - PLEASE WAIT")
        self.assertIsNone(self.ui.gallery)
        self.assertEqual(cam.capture_action_label("TIMELAPSE", "ENCODING 100/228"), "6 WAIT")
        self.assertEqual(cam.capture_action_label("TIMELAPSE", "READY"), "6 START")

    def test_stop_key_does_not_interrupt_export(self):
        session = SimpleNamespace(stop=lambda: self.fail("stop() called during export"))
        self.ui.timelapse = session
        self.ui.timelapse_exporting = True
        self.ui.status = "ENCODING 120/228"
        self.ui.key(self.stub.KEY_6)
        self.assertEqual(self.ui.status, "ENCODING 120/228")

    def test_gallery_unlocks_after_worker_finishes(self):
        self.ui.mode = "TIMELAPSE"
        class Session:
            def __init__(self, **kwargs):
                pass
            def run(self, progress=None, export_progress=None):
                progress(1)
                progress(1, exporting=True)
                export_progress("ENCODING", 1, 1)
        with patch.object(cam, "TimelapseCapture", Session), \
             patch.object(self.ui.stream, "stop"), patch.object(self.ui.stream, "start"):
            self.ui.do_timelapse()
            self.ui.worker.join(timeout=5)
            self.assertFalse(self.ui.worker.is_alive())
            self.assertIsNone(self.ui.timelapse)
            self.assertFalse(self.ui.timelapse_exporting)
            self.assertFalse(self.ui.busy)
            self.assertEqual(self.ui.status, "SAVED TIMELAPSE MP4")
            with patch.object(cam, "Gallery", return_value=SimpleNamespace()):
                self.ui.enter_gallery()
                self.assertIsNotNone(self.ui.gallery)


if __name__ == "__main__":
    unittest.main()
