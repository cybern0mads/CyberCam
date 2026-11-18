"""v0.5.2 gallery discoverability and navigation tests."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from PIL import Image
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import cybercam as cam

KEYS = ['KEY_ESC','KEY_Q','KEY_9','KEY_4','KEY_LEFT','KEY_5','KEY_RIGHT',
        'KEY_ENTER','KEY_SPACE','KEY_6','KEY_0','KEY_M','KEY_UP','KEY_DOWN',
        'KEY_TAB','KEY_1','KEY_2','KEY_3','KEY_P','KEY_G','KEY_V','KEY_7',
        'KEY_R','KEY_8','KEY_S','KEY_F']
FAKE = SimpleNamespace(**{name:i+1 for i,name in enumerate(KEYS)})

class GalleryNavigationTest(unittest.TestCase):
    def setUp(self):
        self.patch = patch.object(cam, 'ecodes', FAKE)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.ui = cam.CyberCamUI()

    def test_menu_seven_items(self):
        self.assertEqual(len(cam.MODES), 6)
        self.assertEqual(cam.MENU_CHOICES[-1], 'GALLERY')

    def test_gallery_key_direct_from_camera(self):
        with patch.object(self.ui, 'enter_gallery') as open_gallery:
            self.ui.key(FAKE.KEY_9)
            open_gallery.assert_called_once()

    def test_gallery_key_inside_menu(self):
        self.ui.key(FAKE.KEY_0)
        with patch.object(self.ui,'enter_gallery') as open_gallery:
            self.ui.key(FAKE.KEY_9)
            self.assertFalse(self.ui.mode_menu)
            open_gallery.assert_called_once()

    def test_gallery_select_via_arrows_and_enter(self):
        self.ui.key(FAKE.KEY_0)
        self.ui.key(FAKE.KEY_DOWN)
        self.ui.key(FAKE.KEY_DOWN)
        self.ui.key(FAKE.KEY_DOWN)
        self.assertEqual(self.ui.mode_selected, 6)
        with patch.object(self.ui, 'enter_gallery') as open_gallery:
            self.ui.key(FAKE.KEY_ENTER)
            self.assertFalse(self.ui.mode_menu)
            open_gallery.assert_called_once()

    def test_gallery_does_not_reassign_capture_key(self):
        with patch.object(self.ui, 'do_photo') as take:
            self.ui.key(FAKE.KEY_6)
            take.assert_called_once()

    def test_gallery_guard_active_recording(self):
        self.ui.recording = object()
        with patch.object(self.ui.stream, 'stop') as stream_stop:
            self.ui.key(FAKE.KEY_9)
            self.assertIsNone(self.ui.gallery)
            stream_stop.assert_not_called()

    def test_visuals_native_dimensions_and_gallery_button(self):
        scene=Image.new('RGB',(640,480),'#39617b')
        self.assertEqual(cam.draw_ui(scene).size,(320,170))
        self.assertEqual(cam.draw_mode_menu(selected=6).size,(320,170))
        image=cam.draw_ui(scene)
        self.assertEqual(image.getpixel((229,115)), (52,40,85))
        self.assertEqual(image.getpixel((229,143)), cam.MINT)

if __name__ == '__main__':
    unittest.main()
