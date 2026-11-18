import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import cybercam

class TestCyberCam(unittest.TestCase):
    def setUp(self):
        self.sample = Image.new('RGB', (640, 480), (190, 115, 34))

    def test_filter_dimensions(self):
        for mode in cybercam.FILTERS:
            with self.subTest(mode=mode):
                picture = cybercam.filter_image(self.sample, mode)
                self.assertEqual(picture.mode, 'RGB')
                self.assertEqual(picture.size, (640, 480))

    def test_gboy_binary_colors(self):
        result = cybercam.filter_image(self.sample, 'GBOY')
        self.assertTrue(set(result.getdata()).issubset({(0,0,0),(255,255,255)}))

    def test_gif_15_frames(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / 'animated.gif'
            frames = [Image.new('RGB', (640,480), (i*12, 110, 80)) for i in range(15)]
            cybercam.save_gif(frames, 'NORMAL', out)
            with Image.open(out) as im:
                self.assertEqual(im.format, 'GIF')
                self.assertEqual(im.n_frames, 15)
                self.assertEqual(im.size, (240,176))

    def test_gboy_animated_dimensions(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / 'g.gif'
            frames = [Image.linear_gradient('L').convert('RGB').point(lambda p: (p+i*15)%256) for i in range(6)]
            cybercam.save_gif(frames, 'GREEN', out)
            with Image.open(out) as im:
                self.assertEqual(im.size, (240,176))
                self.assertEqual(im.n_frames, 6)

    def test_photo_keeps_original(self):
        with tempfile.TemporaryDirectory() as td:
            def fake_command(cmd, timeout=45):
                Image.new('RGB', (3280,2464), (10,30,60)).save(cmd[-1])
            with patch.object(cybercam, 'ROOT', Path(td)), patch.object(cybercam, 'run_command', fake_command):
                original, filtered = cybercam.take_photo('GREEN', sound=False)
                self.assertTrue(original.exists())
                self.assertTrue(filtered.exists())
                with Image.open(original) as im:
                    self.assertEqual(im.size, (3280,2464))
                with Image.open(filtered) as im:
                    self.assertEqual(im.size, (3280,2464))

    def test_ui_render(self):
        self.assertEqual(cybercam.draw_ui(self.sample).size, (320,170))

    def test_preview_default(self):
        with patch.object(cybercam, 'CONFIG', Path('/tmp/cybercam-test-config-file-does-not-exist')):
            settings = cybercam.load_settings()
        self.assertEqual(settings['preview_fps'], 15)
        self.assertEqual(settings['rotation'], 180)

    def test_8mp_rotation_command(self):
        commands = []
        def mock_run(command, timeout=45):
            commands.append(command)
            Image.new('RGB', (3280,2464), (20,30,60)).save(command[-1])
        with tempfile.TemporaryDirectory() as td, patch.object(cybercam, 'ROOT', Path(td)), patch.object(cybercam, 'run_command', mock_run):
            original, effect = cybercam.take_photo('NORMAL', sound=False)
            self.assertTrue(original.exists())
            self.assertIsNone(effect)
        self.assertIn('--rotation', commands[0])
        self.assertEqual(commands[0][commands[0].index('--rotation') + 1], '180')

    def test_video_presets(self):
        self.assertEqual(cybercam.VIDEOS[0], (1280,960,30,8_000_000))
        self.assertEqual(cybercam.VIDEOS[1], (1440,1080,30,12_000_000))

    def test_full_field_sensor_mode_is_used_for_preview(self):
        commands = []
        class FakeProc:
            def __init__(self):
                self.stdout = io.BytesIO(b"")
            def poll(self):
                return None
            def send_signal(self, sig):
                pass
            def wait(self, timeout=None):
                return 0
        with tempfile.TemporaryDirectory() as td:
            with patch.object(cybercam, 'LOG', Path(td)/'camera.log'), \
                 patch.object(cybercam.subprocess, 'Popen', side_effect=lambda cmd, **kw: (commands.append(cmd), FakeProc())[1]):
                stream = cybercam.MjpegStream()
                stream.start()
                stream.stop()
        self.assertEqual(commands[0][commands[0].index('--mode') + 1], '1640:1232:10:P')
        self.assertEqual(commands[0][commands[0].index('--framerate') + 1], '15')

    def test_full_field_sensor_mode_used_for_video(self):
        commands = []
        class FakeProc:
            returncode = None
            def poll(self):
                return None
            def send_signal(self, sig):
                pass
            def wait(self, timeout=None):
                return 0
            def terminate(self):
                pass
        with tempfile.TemporaryDirectory() as td:
            with patch.object(cybercam, 'ROOT', Path(td)), \
                 patch.object(cybercam, 'LOG', Path(td)/'camera.log'), \
                 patch.object(cybercam.time, 'sleep', lambda x: None), \
                 patch.object(cybercam.subprocess, 'Popen', side_effect=lambda cmd, **kw: (commands.append(cmd), FakeProc())[1]):
                r = cybercam.Recording(cybercam.VIDEOS[0])
                r.start(sound=False)
                self.assertEqual(commands[0][commands[0].index('--mode') + 1], '1640:1232:10:P')
                self.assertEqual(commands[0][commands[0].index('--width') + 1], '1280')
                self.assertEqual(commands[0][commands[0].index('--height') + 1], '960')
                r.logfile.close()

    def test_capture_key_label_in_every_mode(self):
        self.assertEqual(cybercam.capture_action_label('PHOTO', 'READY'), '6 CAPTURE')
        self.assertEqual(cybercam.capture_action_label('GIF', 'READY'), '6 CAPTURE')
        self.assertEqual(cybercam.capture_action_label('VIDEO', 'READY'), '6 RECORD')
        self.assertEqual(cybercam.capture_action_label('VIDEO', 'REC'), '6 STOP')

    def test_sidebar_mode_labels(self):
        import inspect
        src = inspect.getsource(cybercam.draw_ui)
        for label in ('1 PHOTO', '2 GIF', '3 VIDEO'):
            self.assertIn(label, src)
        for mode in ('PHOTO', 'GIF', 'VIDEO'):
            self.assertEqual(cybercam.draw_ui(self.sample, mode=mode).size, (320,170))

    def test_gallery_scans_media_from_all_three_directories(self):
        import os
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for folder, name in (("Photos", "a.jpg"), ("GIFs", "b.gif"),
                                 ("Videos", "c.mp4")):
                (root / folder).mkdir()
                (root / folder / name).write_bytes(b"demo")
            (root / "Videos" / "bad.txt").write_text("ignore")
            files = cybercam.gallery_files(root)
            self.assertEqual({p.suffix for p in files}, {".jpg", ".gif", ".mp4"})
            self.assertEqual(len(files), 3)

    def test_gallery_can_show_photo_without_modifying_original(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td) / "Photos"
            folder.mkdir()
            Image.new("RGB", (640, 480), (10, 80, 90)).save(folder / "sample.jpg")
            before = (folder / "sample.jpg").read_bytes()
            g = cybercam.Gallery(td)
            try:
                self.assertEqual(g.kind, "jpg")
                self.assertEqual(g.scene.size, (640, 480))
                self.assertEqual(cybercam.draw_gallery(g.tick(), g).size, (320, 170))
                g.toggle()
                self.assertTrue(g.full)
                self.assertEqual(cybercam.draw_gallery(g.tick(), g).size, (320, 170))
            finally:
                g.close()
            self.assertEqual((folder / "sample.jpg").read_bytes(), before)

    def test_animated_gallery_progresses(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td) / "GIFs"
            folder.mkdir()
            frames = [Image.new("RGB", (64,64), (i*70, 0, 0)) for i in range(3)]
            frames[0].save(folder / "test.gif", save_all=True,
                           append_images=frames[1:], duration=100, loop=0)
            g = cybercam.Gallery(td)
            try:
                self.assertEqual(g.kind, "gif")
                g.next_frame_at = 0
                g.tick()
                self.assertEqual(g.reader.tell(), 1)
                g.toggle()
                self.assertFalse(g.playing)
            finally:
                g.close()

    def test_video_decoder_preserves_aspect_ratio_and_has_pause(self):
        with tempfile.TemporaryDirectory() as td:
            player = cybercam.VideoPlayback(Path(td)/"x.mp4")
            cmd = player.command()
            self.assertIn("force_original_aspect_ratio=decrease", cmd[cmd.index("-vf")+1])
            self.assertEqual(cmd[cmd.index("-pix_fmt")+1], "rgb24")
            self.assertIn("-an", cmd)

    def test_gallery_mode_controls(self):
        # Public API for gallery can run as the device's default UI, camera not active.
        self.assertEqual(cybercam.GALLERY_DIRS, ("Photos", "GIFs", "Videos", "Timelapse", "StopMotion"))
        import inspect
        source = inspect.getsource(cybercam.CyberCamUI.key)
        self.assertIn("ecodes.KEY_9", source)
        self.assertIn("ecodes.KEY_4", source)
        self.assertIn("ecodes.KEY_5", source)

    def test_version_and_branding(self):
        self.assertEqual(cybercam.VERSION, '0.5.2')
        manifest = json.loads((Path(__file__).resolve().parents[1] / 'app-builder.json').read_text())
        self.assertEqual(manifest['store']['author']['display_name'], 'Cybern0mads')
        self.assertTrue(manifest['store']['permissions']['camera'])
        self.assertEqual(len(manifest['store']['share_code']), 4)
        self.assertTrue(manifest['store']['permissions']['network'])

if __name__ == '__main__':
    unittest.main()