# Dictation & Read-Aloud Helper

An offline dictation (speech-to-text) and read-aloud (text-to-speech) tool for Windows, built to help with dyslexia. It works like Windows Voice Typing (Win + H), but all processing happens on your own PC. You don't need admin rights or a GPU.

| Key | What it does |
|---|---|
| **Hold F9** | Records while you hold the key. When you let go, it types what you said into the active window. |
| **F10** | Reads the highlighted text aloud. Press F10 again to stop. If nothing is highlighted, it reads the clipboard. |
| **Esc** | Stops reading, or cancels a dictation in progress. Esc is only intercepted while the app is busy. |

**Chimes:** high rising = recording started · low falling = recording stopped · bright pop = text typed · low "bonk" = nothing heard, nothing selected, or an error.

**Tray icon colour:** grey = loading · blue = ready · red = listening · amber = transcribing · green = reading.

Right-click the tray icon to choose the **microphone**, **reading voice**, **reading speed**, **dictation model** (Fast `base.en` / Accurate `small.en`), or to **Quit**. Your choices are saved in `settings.json`.

## Engines

- **STT:** [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (CTranslate2) running on the CPU with INT8. It has no PyTorch dependency. Voice-activity detection removes silence, which cuts down on made-up text.
- **TTS:** [kokoro-onnx](https://github.com/thewh1teagle/kokoro-onnx), a neural voice. It uses the INT8 model (about 90 MB) on ONNX Runtime and bundles its own espeak-ng, so nothing needs to be installed system-wide.
- Both models download once into `.cache/` next to the script. After that, no internet connection is needed.

Disk use is about 1 GB in total: roughly 600 MB of Python packages, 145 MB for `base.en` (or 480 MB for `small.en`), and 120 MB for Kokoro.

---

## Installing Python without admin rights

Choose **one** option. Use **Python 3.12** (kokoro-onnx supports 3.10–3.13).

### Option A: official installer, per-user (simplest)
1. Download the *Windows installer (64-bit)* for Python 3.12 from python.org.
2. Run it. **Untick** "Use admin privileges when installing py.exe" and **tick** "Add python.exe to PATH".
3. Click **Install Now**. It installs to `%LOCALAPPDATA%\Programs\Python` and doesn't need admin.

### Option B: WinPython (fully portable, nothing "installed")
1. Download a WinPython 3.12 "dot" release (the small one).
2. Extract it to any folder you can write to, such as `C:\Users\<you>\Tools\WinPython` or a USB stick.
3. Open *WinPython Command Prompt.exe* in that folder and run the commands below from it.

### Option C: `uv` (one small exe)
```bat
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
uv venv --python 3.12 .venv
.venv\Scripts\activate
```

> The python.org *embeddable* zip also works, but it needs extra setup. Uncomment `import site` in `python312._pth`, then run `get-pip.py`. Options A and B are easier.

## Where to put the files

Put the files in a **short** folder path, so that `dictation_app.py` sits directly inside it:

```
C:\Users\<you>\DictationHelper\dictation_app.py     <- good
C:\Users\<you>\DictationHelper\Dictation-and-Read-aloud-vibe-coding-claude-...\Dictation-and-...\dictation_app.py   <- too long
```

When you use "Extract All" on a GitHub ZIP, Windows nests the long-named folder twice. Move the files up so they sit directly in `DictationHelper`. Windows limits file paths to 260 characters, and the model files need room inside `.cache`.

## First run (online, once)

```bat
cd path\to\this\folder
python dictation_app.py
```
You can also double-click **`run_first_time.bat`**.

On the first run, the script:
1. Detects missing packages and runs `pip install` for them. It uses `--user` automatically if the Python folder is read-only.
2. Restarts itself.
3. Downloads the Whisper and Kokoro models into `.cache/`. Progress is shown in the console and written to `dictation_app.log`.
4. Plays a chime and shows a "Ready!" notification.

## Everyday use (offline)

Double-click **`run_silent.bat`**, which runs `pythonw`, so no console window appears. To start the helper automatically at login, press **Win + R**, type `shell:startup`, and put a shortcut to `run_silent.bat` in that folder.

## Configuration

The constants at the top of `dictation_app.py` control the behaviour:

- `DICTATE_KEY`, `READ_KEY`, `STOP_KEY`: can be `f1`…`f24`, `pause`, `scroll_lock`, `insert`, `ctrl_r`, or `menu`.
- `OUTPUT_MODE`: `"paste"` (fast; uses the clipboard and restores it afterwards) or `"type"` (presses each key, for apps that block pasting).
- `MAX_RECORD_SECONDS`: a safety limit on recording length (120 s).
- `READ_CLIPBOARD_IF_NO_SELECTION`, `MAX_READ_CHARS`, `VOICES`, `SPEEDS`.

---

## How the threading works

```
Main thread ─────────── pystray Win32 message loop (tray icon + menu)
pynput hook thread ──── low-level keyboard hook → pushes "dictate_down/up", "read", "stop"
                         onto a queue and returns immediately (swallows F9/F10)
controller thread ───── owns the recorder: opens/closes the mic stream, plays chimes,
                         starts the 120 s watchdog, spawns the reader thread
PortAudio callback ──── appends mic blocks to a list while recording
transcriber thread ──── Whisper → paste/type → "success" chime (one job at a time, in order)
reader thread ───────── Ctrl+C → read clipboard → restore clipboard → Speaker.speak()
tts-synth thread ────── Kokoro, one sentence at a time → bounded queue
tts-playback thread ─── writes 50 ms blocks to the speakers, checks the stop flag between blocks
chime threads ───────── winsound.PlaySound from in-memory WAVs (short-lived)
hook-health thread ──── restarts the keyboard hook if it ever dies
```

- **Windows unhooks slow keyboard hooks.** If a low-level hook takes longer than about 300 ms, Windows silently removes it. So the hook callback only does a dictionary lookup and a `queue.put`. Everything slow, such as opening the mic, Whisper, Kokoro, or the clipboard, runs on other threads.
- **pystray must own the main thread.** Its menu callbacks only change settings or set a stop flag, so the tray never freezes.
- **CPU-heavy work doesn't block the other threads.** Whisper (CTranslate2) and Kokoro (ONNX Runtime) release the GIL while they compute.
- **Read-aloud streams sentence by sentence.** Synthesis and playback form a producer/consumer pipeline, so a long passage starts within about one sentence's synthesis time. Pressing Esc or F10 calls `stream.abort()` and stops the sound within about 50 ms.
- **The clipboard is never used by two jobs at once.** A single lock guards it, so a dictation paste and a read-aloud copy can't overlap.

## Error handling

- **Stuck hotkey:** auto-repeat is ignored, a missed key-up is fixed by the next press/release, and the watchdog stops any recording after 120 s.
- **Mic unplugged or unavailable:** errors are caught. You hear the error chime, a notification explains the problem, and whatever audio was captured is still transcribed. Use *Microphone → Refresh list* after plugging in a new device.
- **Empty selection or clipboard:** you hear the error chime and nothing else happens. If the clipboard is locked by another app, the script retries, and in paste mode it falls back to typing the text.
- **Errors in any thread** are logged to `dictation_app.log`. They never close the tray app.
- **Only one copy runs at a time** (a named mutex enforces this), so text is never typed twice.

## Troubleshooting & limits on a locked-down PC

- **`pip` or model downloads fail behind a corporate proxy:**
  - Set `HTTPS_PROXY=http://proxy:port` before the first run.
  - `truststore` makes Python trust the Windows certificate store, which handles TLS inspection.
  - If GitHub or Hugging Face are blocked, download the files on another machine and copy them in:
    - `kokoro-v1.0.int8.onnx` and `voices-v1.0.bin` go in `.cache\kokoro\`.
    - You can also copy the whole `.cache` folder from a machine where the app already works.
- **Hotkeys do nothing in some windows:** Windows doesn't let a normal app send keys to programs running *as administrator* (UIPI). Run the target app normally.
- **Security software:** keyboard hooks and simulated key presses look like a keylogger to some security tools. If the app is blocked, ask IT to allow `pythonw.exe` for this folder.
- **Clipboard restore is text-only.** If you had an image on the clipboard, it won't be restored after dictating or reading.
- **Slow transcription:** use *Dictation model → Fast (base.en)*. `small.en` is more accurate but about 3× slower on the CPU.
- **Check the log:** `dictation_app.log` next to the script records each step.
