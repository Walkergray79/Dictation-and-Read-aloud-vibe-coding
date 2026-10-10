# Changelog

The version number is shown in the tray icon's tooltip and in **About** in the tray menu. About also copies the details to the clipboard, ready to paste into an email or chat.

## 1.6.0
- Version number shown in the tray tooltip, the About box and the log.
- About box with the app, model and Python versions. The details are copied to the clipboard.
- **Updates → Check for model updates now:** checks whether newer dictation or voice models, or newer speech engines, exist. It downloads nothing unless you say yes. A dictation model update downloads alongside the current model and only replaces it once complete.
- **Updates → Check automatically at startup:** off by default, so the app stays fully offline.
- Turned off the Hugging Face library's own "new version" check.

## 1.5.0
- `STATS` timing lines in the console and log after every dictation and read-aloud, like tic/toc in MATLAB.
- **Show statistics** in the tray menu shows session totals.

## 1.4.0
- Pause switch: left-click the tray icon. The icon turns grey with ⏸ and F9/F10 work as normal keys.
- On Windows 11 the tray icon pins itself to the taskbar.
- The uninstaller can keep `custom_words.txt`.

## 1.3.0
- `uninstall.bat` for quick, clean removal. Python itself is kept.

## 1.2.0
- Dictated text is typed by default, which fixes text not appearing in some apps. Paste mode is still available.
- Read-aloud is about 6× faster: it uses the full-precision voice model and speech starts after the first short sentence.
- On-screen status bubble.
- `custom_words.txt` for spellings, acronym fix-ups and pronunciations.

## 1.1.0
- Fixed model download failing on long Windows folder paths.

## 1.0.0
- First release: hold-F9 dictation (faster-whisper), F10 read-aloud (Kokoro), tray menu, chimes, self-installing.
