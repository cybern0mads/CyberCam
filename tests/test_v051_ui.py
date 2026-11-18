"""v0.5.1 mode-picker UI and keyboard regression tests (no camera required)."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from PIL import Image
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import cybercam as cam

KEYNAMES = ['KEY_ESC','KEY_Q','KEY_9','KEY_4','KEY_LEFT','KEY_5','KEY_RIGHT',
            'KEY_ENTER','KEY_SPACE','KEY_6','KEY_0','KEY_M','KEY_UP','KEY_DOWN',
            'KEY_TAB','KEY_1','KEY_2','KEY_3','KEY_P','KEY_G','KEY_V','KEY_7',
            'KEY_R','KEY_8','KEY_S','KEY_F']
FAKE_KEYS = SimpleNamespace(**{name:i+1 for i,name in enumerate(KEYNAMES)})

class PickerTest(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(cam, 'ecodes', FAKE_KEYS)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ui = cam.CyberCamUI()

    def test_menu_uses_no_camera_capture_and_six_choices(self):
        self.ui.key(FAKE_KEYS.KEY_0)
        self.assertTrue(self.ui.mode_menu)
        self.assertEqual(self.ui.mode, 'PHOTO')
        self.assertEqual(len(cam.MODES),6)
        self.ui.key(FAKE_KEYS.KEY_4)
        self.assertFalse(self.ui.mode_menu)
        self.assertEqual(self.ui.mode,'TIMELAPSE')
        self.assertIsNone(self.ui.timelapse)

    def test_direct_timelapse_key_from_camera(self):
        self.ui.key(FAKE_KEYS.KEY_4)
        self.assertEqual(self.ui.mode,'TIMELAPSE')
        self.assertEqual(cam.capture_action_label(self.ui.mode,'READY'), '6 START')

    def test_menu_up_down_arrow_and_enter(self):
        self.ui.key(FAKE_KEYS.KEY_0)
        self.ui.key(FAKE_KEYS.KEY_DOWN)  # third item (Video)
        self.assertEqual(self.ui.mode_selected,2)
        self.ui.key(FAKE_KEYS.KEY_DOWN)  # fifth (Stop Motion)
        self.assertEqual(self.ui.mode_selected,4)
        self.ui.key(FAKE_KEYS.KEY_ENTER)
        self.assertEqual(self.ui.mode,'STOPMOTION')
        self.assertFalse(self.ui.mode_menu)

    def test_6_selects_boomerang_only_while_menu_open(self):
        self.ui.key(FAKE_KEYS.KEY_0)
        self.ui.key(FAKE_KEYS.KEY_6)
        self.assertEqual(self.ui.mode,'BOOMERANG')
        self.assertFalse(self.ui.mode_menu)
        with patch.object(self.ui,'do_boomerang') as action:
            self.ui.key(FAKE_KEYS.KEY_6)
            action.assert_called_once()

    def test_escape_closes_menu_without_exiting(self):
        self.ui.key(FAKE_KEYS.KEY_M)
        self.ui.key(FAKE_KEYS.KEY_ESC)
        self.assertTrue(self.ui.running)
        self.assertFalse(self.ui.mode_menu)
        self.assertEqual(self.ui.mode,'PHOTO')

    def test_keeps_filter_and_sound_shortcuts(self):
        with patch.object(cam,'save_settings'):
            first = self.ui.settings['filter']
            self.ui.key(FAKE_KEYS.KEY_5)
            self.assertNotEqual(self.ui.settings['filter'],first)
            first = self.ui.settings['sound']
            with patch.object(cam,'click'):
                self.ui.key(FAKE_KEYS.KEY_8)
            self.assertNotEqual(self.ui.settings['sound'],first)

    def test_timelapse_interval_and_capture_are_separate_controls(self):
        self.ui.key(FAKE_KEYS.KEY_4)
        with patch.object(cam,'save_settings'):
            before = self.ui.settings['timelapse_interval']
            self.ui.key(FAKE_KEYS.KEY_7)
            self.assertEqual(self.ui.settings['timelapse_interval'], (before+1)%len(cam.TIMELAPSE_INTERVALS))
        with patch.object(self.ui,'do_timelapse') as capture:
            self.ui.key(FAKE_KEYS.KEY_6)
            capture.assert_called_once()

    def test_modes_menu_does_not_start_a_camera_job(self):
        with patch.object(self.ui,'do_photo') as photo, patch.object(self.ui,'do_timelapse') as lapse:
            self.ui.key(FAKE_KEYS.KEY_0)
            self.ui.key(FAKE_KEYS.KEY_4)
            photo.assert_not_called()
            lapse.assert_not_called()

    def test_no_mode_changes_while_recording(self):
        self.ui.recording = object()
        self.ui.key(FAKE_KEYS.KEY_0)
        self.assertFalse(self.ui.mode_menu)
        self.ui.key(FAKE_KEYS.KEY_4)
        self.assertEqual(self.ui.mode,'PHOTO')

    def test_picker_and_ui_renders_at_native_size(self):
        view = cam.draw_mode_menu(current='TIMELAPSE',selected=3)
        self.assertEqual(view.size,(320,170))
        view.getpixel((10,20))
        sample = Image.new('RGB',(640,480),'#41627a')
        self.assertEqual(cam.draw_ui(sample,mode='TIMELAPSE',resolution='5s').size,(320,170))
        self.assertEqual(cam.draw_ui(sample,mode='STOPMOTION',resolution='3F').size,(320,170))

    def test_video_capture_labels_preserved(self):
        self.assertEqual(cam.capture_action_label('VIDEO','READY'),'6 RECORD')
        self.assertEqual(cam.capture_action_label('VIDEO','REC'),'6 STOP')
        self.assertEqual(cam.capture_action_label('PHOTO','READY'),'6 CAPTURE')

if __name__=='__main__':
    unittest.main()