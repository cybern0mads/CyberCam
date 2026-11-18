# CyberCam Changelog

## v0.5.3 — Reliable timelapse export (2026-11-18)
- Encode normal timelapses directly from sequential JPEGs, skipping temporary PNG conversion.
- Stream filtered timelapse and stop-motion frames to FFmpeg while preserving selected looks.
- Publish a completed MP4 only after FFmpeg succeeds; original JPEG frames remain available after failure.
- Show persistent capture/export progress with accurate Gallery busy state and no early READY.
- Log export errors and successful outputs in the camera log; regression coverage for normal/filtered exports.
- On-device verified: timelapse MP4 output and post-export Gallery access worked on Cardputer Zero.


## v0.5.2 — Final navigation layout (2026-11-18)
- Dedicated, high-contrast **9 GALLERY** button visible on the main camera view.
- Gallery is also a selectable full-width seventh tile in **0 ALL MODES**.
- Capture controls, seven original looks, nine new looks, wide video, timelapse,
  stop motion, boomerang, on-device gallery and Wi-Fi gallery unchanged.
- Look, resolution/interval, and control hints placed on viewfinder overlays to
  retain the original live preview width without crowding the sidebar.
- Menu arrows and Enter can now select Gallery; number keys still work.
- Offline verification only; physical device test recommended before store release.

# Changelog

## 0.5.1 — Mode Picker and Timelapse Usability

- Replace hidden `0` mode cycling with a six-tile Modes menu with arrow/Enter and numbered shortcuts.
- Add one-key `4 TIMELAPSE` from the main camera UI; label interval and `6 START/STOP`.
- Preserve photo, GIF, MP4, gallery, filters, stop-motion, Boomerang, shutter sound, and Wi-Fi Gallery.
- Add UI and key-routing regression tests; no capture pipeline changes.


## 0.5.0 — Creative Camera Upgrade

- Add eight new retro looks: GAMEBOY+, VHS1998, CYBERVISION, FILM1999, VIRTUALRED, C64RETRO, NEONEDGE, PIXELDREAM, plus CYBERGLITCH for glitch GIFs.
- Add keyboard Timelapse with MP4 export and preserved source stills; CLI fixed-frame timelapse.
- Add manual Stop Motion with previous-frame onion-skin preview and GIF/MP4 export.
- Add Boomerang forward-and-back GIF loops.
- Add optional token-protected Wi-Fi Gallery with secure-localhost default, browser viewing/download, HTTP byte-range video support.
- Preserve all v0.4.1 filters, original 8MP stills, existing video presets, in-device Gallery, shutter click and neon icon.
- Cache film vignette and font rendering, keep preview 15 FPS target, avoid high-resolution live effect processing.
- Add regression and HTTP/FFmpeg offline integration tests.

## 0.4.1 — Neon icon

- Neon cyan/purple Cybern0mads launcher and store icons; no feature change from 0.4.0.

## 0.4.0 — Gallery

- On-device photo/GIF/MP4 playback and 9 GALLERY shortcut.
