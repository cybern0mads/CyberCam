#!/usr/bin/env python3
"""Build an architecture-independent Debian package for CyberCam."""
from pathlib import Path
import math
import shutil
import subprocess
import struct
import wave
import random
from PIL import Image, ImageDraw, ImageFont

project = Path(__file__).resolve().parents[1]
root = project / 'packaging' / 'stage'
if root.exists():
    shutil.rmtree(root)
(root / 'DEBIAN').mkdir(parents=True)
(root / 'usr/lib/cybercam').mkdir(parents=True)
(root / 'usr/bin').mkdir(parents=True)
(root / 'usr/share/APPLaunch/applications').mkdir(parents=True)
(root / 'usr/share/APPLaunch/share/images').mkdir(parents=True)
shutil.copy2(project / 'src/cybercam.py', root / 'usr/lib/cybercam/cybercam.py')
(root / 'usr/bin/cybercam').write_text('#!/bin/sh\nexec /usr/bin/python3 /usr/lib/cybercam/cybercam.py "$@"\n')
(root / 'usr/bin/cybercam').chmod(0o755)
(root / 'DEBIAN/control').write_text('''Package: cybercam
Version: 0.5.2
Section: graphics
Priority: optional
Architecture: all
Maintainer: Cybern0mads <358342606+cybern0mads@users.noreply.github.com>
Depends: python3, python3-pil, python3-evdev, python3-numpy, alsa-utils, ffmpeg
Description: CyberCam by Cybern0mads - retro camera for Cardputer Zero
 Full-resolution photos, retro looks, GIF/MP4, timelapse, gallery and Wi-Fi sharing.
 Uses the IMX219 camera, RGB565 framebuffer and evdev keyboard.
''')
(root / 'usr/share/APPLaunch/applications/cybercam.desktop').write_text('''[Desktop Entry]
Type=Application
Name=CyberCam
Comment=Photos, retro GIF and MP4 video by Cybern0mads
Exec=/usr/bin/cybercam ui
Icon=share/images/cybercam.png
Terminal=false
Categories=Utility;
StartupNotify=false
''')

shutil.copy2(project / 'packaging' / 'icon.png', root / 'usr/share/APPLaunch/share/images/cybercam.png')
shutil.copy2(project / 'packaging' / 'icon.png', project / 'cybercam_icon.png')

# Synthetic, original short two-pulse shutter sound, no third-party audio.
rate = 24000
random.seed(37)
samples = []
for i in range(int(rate * 0.16)):
    t = i/rate
    one = math.exp(-52*t) * (0.68*math.sin(2*math.pi*900*t) + 0.25*random.uniform(-1,1))
    s = max(0, t-0.055)
    two = math.exp(-48*s) * (0.55*math.sin(2*math.pi*1180*s) + 0.17*random.uniform(-1,1)) if t >= 0.055 else 0
    value = max(-1,min(1,(one+two)*0.58))
    samples.append(int(value*32767))
with wave.open(str(root / 'usr/lib/cybercam/shutter.wav'), 'wb') as f:
    f.setnchannels(1)
    f.setsampwidth(2)
    f.setframerate(rate)
    f.writeframes(struct.pack('<%dh' % len(samples), *samples))

for folder in root.rglob('*'):
    if folder.is_dir():
        folder.chmod(0o755)
(root / 'DEBIAN').chmod(0o755)
(root / 'DEBIAN/control').chmod(0o644)
output = project.parent / 'cybercam_0.5.2_all.deb'
subprocess.run(['dpkg-deb', '--build', '--root-owner-group', str(root), str(output)], check=True)
print(output)
