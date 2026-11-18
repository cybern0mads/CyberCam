"""Offline unit/integration coverage for CyberCam v0.5 creative features."""
import io
import json
import sys
import time
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from unittest.mock import patch
from PIL import Image,ImageDraw
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import cybercam


class TestCreative(unittest.TestCase):
    def setUp(self):
        self.scene=Image.new('RGB',(640,480))
        draw=ImageDraw.Draw(self.scene)
        for x in range(640):
            draw.line((x,0,x,479),fill=(x%256,(x//2)%256,255-x%256))
        draw.ellipse((140,50,400,300),outline='white',width=7)

    def test_preserves_original_seven_filters_and_adds_eight(self):
        original=('NORMAL','MONO','GBOY','GREEN','4TONE','PIXEL','HICON')
        new=('GAMEBOY+','VHS1998','CYBERVISION','FILM1999','VIRTUALRED','C64RETRO','NEONEDGE','PIXELDREAM')
        for f in original+new+('CYBERGLITCH',):
            self.assertIn(f,cybercam.FILTERS)
        self.assertEqual(len(set(cybercam.FILTERS)),16)

    def test_look_dimensions_and_preview(self):
        for f in cybercam.FILTERS:
            with self.subTest(f=f):
                img=cybercam.filter_image(self.scene,f,preview=True)
                self.assertEqual(img.size,self.scene.size)
                self.assertEqual(img.mode,'RGB')
                small=cybercam.filter_image(self.scene,f,animated=True)
                self.assertEqual(small.size,(240,176))

    def test_high_resolution_filtered_still(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'photo.jpg'
            Image.new('RGB',(3280,2464),(100,60,150)).save(path)
            result=cybercam.filtered_still(path,'VIRTUALRED')
            with Image.open(result) as im:
                self.assertEqual(im.size,(3280,2464))
            with Image.open(path) as im:
                self.assertEqual(im.size,(3280,2464))

    def test_boomerang_order_and_duration(self):
        class Stream:
            def __init__(self):self.n=0
            def latest(self):return 0,None
            def next_frame(self,seq,timeout=2):
                self.n+=1
                buffer=io.BytesIO()
                Image.new('RGB',(40,30),(self.n*17,50,60)).save(buffer,'JPEG')
                return self.n,buffer.getvalue()
        with tempfile.TemporaryDirectory() as td, patch.object(cybercam,'ROOT',Path(td)):
            with patch.object(cybercam.time,'sleep',lambda sec:None):
                path,count=cybercam.boomerang_gif(Stream(),'CYBERGLITCH',count=6,sound=False)
            self.assertEqual(count,10)
            with Image.open(path) as im:
                self.assertTrue(im.n_frames>=3)
                self.assertEqual(im.size,(240,176))

    def test_stop_motion_ghost_and_export(self):
        def jpeg(color):
            bio=io.BytesIO()
            Image.new('RGB',(320,240),color).save(bio,'JPEG')
            return bio.getvalue()
        with tempfile.TemporaryDirectory() as td,patch.object(cybercam,'ROOT',Path(td)):
            session=cybercam.StopMotionSession('NORMAL')
            session.capture(jpeg('red'))
            onion=session.onion_skin(Image.new('RGB',(224,170),'blue'))
            self.assertEqual(onion.size,(224,170))
            session.capture(jpeg('blue'))
            with patch.object(cybercam,'encode_mp4',return_value=session.output) as mock:
                path=session.export()
            self.assertTrue(session.gif_output.exists())
            self.assertEqual(path,session.output)
            mock.assert_called_once()
            self.assertEqual(len(session.frames),2)

    def test_timelapse_handles_early_stop_and_exports(self):
        import threading
        with tempfile.TemporaryDirectory() as td,patch.object(cybercam,'ROOT',Path(td)):
            session=cybercam.TimelapseCapture(interval=2,look='NORMAL')
            def simulate(command,timeout=45):
                Image.effect_noise((640,480),75).convert('RGB').save(command[-1])
                if len(session.frames)>=1:session.stop()
            with patch.object(cybercam,'run_command',simulate), patch.object(cybercam,'encode_mp4',return_value=session.output) as mock:
                result=session.run(count=15)
            self.assertEqual(result,session.output)
            self.assertGreaterEqual(len(session.frames),1)
            mock.assert_called_once()

    def test_mp4_export_ffmpeg_real_sample(self):
        if not cybercam.shutil.which('ffmpeg'):
            self.skipTest('ffmpeg not installed')
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            paths=[]
            for i in range(3):
                path=root/f'f{i}.jpg'
                Image.new('RGB',(320,240),(i*90,10,80)).save(path)
                paths.append(path)
            dest=root/'sample.mp4'
            result=cybercam.encode_mp4(paths,dest,fps=8)
            self.assertEqual(result,dest)
            self.assertGreater(dest.stat().st_size,1024)

    def test_all_extra_actions_have_labels(self):
        for mode in cybercam.EXTRA_MODES:
            self.assertIn('6 ',cybercam.capture_action_label(mode,'READY'))
            self.assertEqual(cybercam.draw_ui(self.scene,mode=mode).size,(320,170))
        self.assertEqual(cybercam.capture_action_label('TIMELAPSE','LAPSE 2F'),'6 STOP')


class TestWifi(unittest.TestCase):
    def test_file_escaping_and_limited_directories(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            photos=root/'Photos';photos.mkdir()
            path=photos/'a.jpg';path.write_bytes(b'jpeg')
            self.assertEqual(cybercam.safe_web_path('Photos/a.jpg',root),path)
            for bad in ['../secret.txt','Photos/../secret.txt','Photos/a.jpg/else','/etc/passwd','Unknown/a.jpg']:
                with self.subTest(bad=bad):
                    with self.assertRaises(ValueError):cybercam.safe_web_path(bad,root)
            (root/'other.txt').write_bytes(b'secret')
            (photos/'secret.jpg').symlink_to(root/'other.txt')
            with self.assertRaises(ValueError): cybercam.safe_web_path('Photos/secret.jpg',root)
            self.assertEqual([x.name for x in cybercam.web_media(root)],['a.jpg'])

    def test_wifi_server_auth_and_range(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            photos=root/'Photos';photos.mkdir()
            path=photos/'image.jpg';path.write_bytes(b'0123456789')
            server=cybercam.ThreadingHTTPServer(('127.0.0.1',0),cybercam.GalleryHTTP)
            server.gallery_token='test-token';server.media_root=root
            worker=threading.Thread(target=server.serve_forever,daemon=True)
            worker.start()
            url=f'http://127.0.0.1:{server.server_port}'
            try:
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    urllib.request.urlopen(url+'/media?path=Photos%2Fimage.jpg',timeout=2)
                self.assertEqual(cm.exception.code,403)
                opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
                # Use standard cookie jar to check the key bootstrap redirect.
                import http.cookiejar
                opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
                with opener.open(url+'/?key=test-token',timeout=2) as page:
                    self.assertIn(b'CYBERCAM',page.read())
                req=urllib.request.Request(url+'/media?path=Photos%2Fimage.jpg',headers={'Range':'bytes=2-5'})
                with opener.open(req,timeout=2) as response:
                    self.assertEqual(response.status,206)
                    self.assertEqual(response.read(),b'2345')
            finally:
                server.shutdown();server.server_close();worker.join(timeout=2)

if __name__=='__main__':
    unittest.main()
