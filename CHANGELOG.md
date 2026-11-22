# Changelog

What changed in Lanterna, version by version, the newest first. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions
are `0.x`, development releases, published as Flatpak updates from the app's
own repository and as Windows installers. The same notes, shorter, are in
the app's metainfo, which software centres show.

## [0.2.0] - 2026-11-22

The first public version.

### Added
- The app's own icon: a lantern with its flame folded like paper, on the
  round disc of my other apps.
- The window: the TVs on the network, photos, videos and films chosen or
  dropped, and the controls while they are on (pause, seek, previous and next,
  the TV's volume, stop). Space and the arrow keys work them too.
- The screen or a window on the TV, with the sound, through the system's own
  dialog; the computer stays awake while something is on, and keeps quiet
  while its sound plays on the TV.

### Fixed
- Going to another point of a film whose sound is converted failed about one
  time in ten with a real film, and sent the window back to its first page or
  left it waiting for ever. It now works from any point, and dragging the bar
  sends one seek, where the button is let go.
- The computer stays quiet during mirroring in the Flatpak as well.

### Removed
- The sample feature of the template (a folder counted).

## [0.1.0] - 2026-10-25 (not published)

### Added
- The first version: a folder counted (files, folders, size), with the
  preferences, the translations, the installer for Windows and the Flatpak
  for Linux every app of this author has.

[0.2.0]: https://github.com/daniel-g-carrasco/lanterna/releases/tag/v0.2.0
