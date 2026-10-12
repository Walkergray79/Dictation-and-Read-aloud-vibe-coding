# Changelog

The version number is shown in the tray icon's tooltip and in **About** in the tray menu. About also copies the details to the clipboard, ready to paste into an email or chat.

## 1.11.1
- New `custom_words_prompt.txt` in the app folder: a ready-made prompt for Gemini, ChatGPT or any AI chat. Paste it in with a word (e.g. DCS[N]) and it answers with lines in the exact `custom_words.txt` format, ready to copy across. It's included in the release ZIP and the portable bundle.

## 1.11.0
- **Portable bundle:** `make_portable_bundle.bat` builds a self-contained folder and ZIP on a PC with internet: a portable Python, all the packages and the speech models. Copy it to a work PC and run it, with no Python install, no admin rights and no internet needed.
- The `.bat` launchers find a portable Python automatically: a bundled `python\` folder, then WinPython next to the app, then the installed Python. `run_first_time.bat` shows which one is used.
- A portable Python keeps its packages inside its own folder and ignores the user's personal packages.
- `uninstall.bat` offers to delete a bundled Python and removes the new files.
- Fixed: pressing the dictation key, or changing CPU usage, while the models were still loading logged an error instead of saying "still loading" (bug since 1.7.0).

## 1.10.0
- **Transcribe recordings:** use **Options → Recordings → Choose audio files to transcribe...**
  - Works with WAV, M4A (iPhone voice memos), MP3, OGG, FLAC, AAC and video files such as MP4.
  - The text is saved as a `.txt` next to each recording. It never overwrites an existing file; if the folder is read-only, it saves to Documents\Dictation Transcripts instead.
  - Paragraphs break at natural pauses, with optional `[mm:ss]` timestamps. Custom words are applied.
  - Choose the model for recordings: same as dictation, Fast or Accurate.
  - Runs in the background with its own copy of the model, so dictation keeps working. Progress shows in Options and the status bubble, and **Stop** saves what's done so far.
- **CPU usage** setting in **Options → General**:
  - **Full speed** (default, unchanged);
  - **Balanced**: half the cores, below-normal priority;
  - **Gentle**: 2 cores, simpler decoding, Windows Efficiency mode.

  It applies to dictation, read-aloud and recordings, and takes effect without a restart.
- `STATS file` lines in the log for each transcribed recording.

## 1.9.0
- New **Options** window (right-click menu → **Options...**) for the set-and-forget settings, so the right-click menu only has everyday items.
  - **General:** status bubble, Typing/Pasting, dictation model.
  - **Keys:** change hotkeys.
  - **Tools:** live statistics, model updates, open app folder, custom words, pin icon.
  - **About:** version info and a copy button.
- Changes in Options apply immediately, with no OK/Apply button.
- If tkinter is missing, those settings appear under **More** in the right-click menu instead.
- Quitting now always closes the app straight away.

## 1.8.0
- **Open app folder** in the tray menu opens this copy's installation folder in File Explorer. That's where you'll find `custom_words.txt`, `settings.json`, the log and `uninstall.bat`.

## 1.7.0
- **Keys** in the tray menu: change the dictation, read-aloud and stop keys. Pick a key from the list, or choose "Press a key to choose..." and press the key you want.
  - Changes apply immediately and are saved.
  - Choosing a key another function already uses swaps the two.
  - Letters, Enter, Shift and similar typing keys are refused, so a hotkey can never block normal typing.
  - Esc can only be the stop key.
  - "Reset to F9 / F10 / Esc" restores the defaults.

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
