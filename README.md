# Dictation & Read-Aloud Helper

An offline dictation (speech-to-text) and read-aloud (text-to-speech) tool for Windows, built to help with dyslexia. It works like Windows Voice Typing (Win + H), but all processing happens on your own PC. You don't need admin rights or a GPU.

| Key | What it does |
|---|---|
| **Hold F9** | Records while you hold the key. When you let go, it types what you said into the active window. |
| **F10** | Reads the highlighted text aloud. Press F10 again to stop. If nothing is highlighted, it reads the clipboard. |
| **Esc** | Stops reading, or cancels a dictation in progress. Esc is only intercepted while the app is busy. |

**Chimes:** high rising = recording started · low falling = recording stopped · bright pop = text typed · low "bonk" = nothing heard, nothing selected, or an error.

**Status bubble:** while the app is working, a small bubble appears near the bottom of the screen. It shows "Listening", "Writing down what you said", "Getting ready to read" and "Reading aloud (2 of 7)". It also shows short messages such as "Done" or "Nothing selected". The bubble never takes the keyboard focus, and clicks pass straight through it. You can turn it off in **Options → General**.

**Tray icon colour:** grey = loading · blue = ready · red = listening · amber = transcribing · green = reading · grey with ⏸ = paused. No icon means the helper isn't running.

**Pause:** **left-click** the tray icon (or choose *Pause* in its menu) to turn the hotkeys off. F9, F10 and Esc then work as normal keys in other apps. Click again to turn the helper back on. It always starts switched on.

**Keeping the icon visible:**
- On **Windows 11**, the helper pins its tray icon to the taskbar automatically the first time it runs, so it isn't hidden under the **^** arrow. If the icon disappears after a Python update, use **Options → Tools → Pin icon to taskbar**.
- On **Windows 10**, drag the icon from the **^** pop-up onto the taskbar once.

**Right-click menu** (everyday items only):
- **Pause / Turn back on** (also a left-click on the icon)
- **Microphone**, **Reading voice**, **Reading speed**
- **Stop reading**
- **Edit custom words...**
- **Options...**
- **Quit**

**Options window** (set-and-forget settings, which apply as soon as you change them):

| Tab | What's there |
|---|---|
| **General** | Show the status bubble · insert dictated text by Typing or Pasting · **CPU usage** (Full speed / Balanced / Gentle) · dictation model (Fast `base.en` / Accurate `small.en`) |
| **Recordings** | Transcribe audio files (WAV, M4A, MP3, MP4...) to `.txt` · model for recordings · timestamps · progress and Stop |
| **Keys** | Change the dictation, read-aloud and stop keys (list, or "Press a key...") · reset to F9 / F10 / Esc |
| **Tools** | Live session statistics · check for model updates now · check automatically at startup · open app folder · edit custom words · pin icon to taskbar |
| **About** | Version, models and Python details, with a button to copy them for support |

If Python was installed without tkinter (for example, the "embeddable" zip), the Options window can't open. The same settings then appear under **More** in the right-click menu.

Your choices are saved in `settings.json`.

## Statistics (timings)

After every dictation and every read-aloud, the console window (when started with `run_first_time.bat`) and `dictation_app.log` show a `STATS` line, like tic/toc in MATLAB:

```
STATS dictation #3 | spoke 4.2s | transcribe 1.10s (0.26x real-time, base.en) | inserted 12 words / 63 chars in 0.30s | total 1.45s from key release
STATS read-aloud #2 | 72 words / 412 chars in 6 chunk(s) | copy 0.12s | first sound 0.90s after F10 | synth 3.10s for 24.5s of speech (0.13x real-time) | played 24.5s (finished)
```

"Real-time" compares the time taken with the length of the audio: 0.26x means 4 seconds of speech took about 1 second to process. **Options → Tools** shows live totals for the session. A summary is also logged when you quit.

## Downloading a release

Each version is published on the repository's **Releases** page (right-hand side of the GitHub page) as `DictationHelper-vX.Y.Z.zip`. The ZIP contains only the files you need, with no nested folders.
- **New install:** extract the ZIP to `C:\Users\<you>\DictationHelper\` and run `run_first_time.bat`.
- **Update:** quit the helper, then extract the new ZIP into the same folder, replacing the files. Your `.cache` (models), `settings.json` and `custom_words.txt` are kept. Start the helper again.

*For maintainers:* a release is created automatically when `main` gets a new `APP_VERSION` in `dictation_app.py`, using that version's section of `CHANGELOG.md` as the notes. You can also start it from **Actions → Release → Run workflow**.

## Version & updates

- **Which version am I running?**
  - Hover over the tray icon, or open **Options → About**.
  - About also copies the version, model and Python details to the clipboard, so colleagues can paste them into an email or chat.
  - What changed in each version is listed in `CHANGELOG.md`.
- **Model updates:** the app never goes online by itself.
  - **Options → Tools → Check for model updates now** checks Hugging Face (dictation model), GitHub (voice model) and PyPI (speech engines). It reports what it finds and asks before downloading anything.
  - **Check automatically at startup** (same place) does the same check quietly each time the app starts, and only pops up a message if something new is available. It's off by default.

## Transcribing recordings

Recorded something on your phone? Copy the file to your PC, then open **Options → Recordings → Choose audio files to transcribe...** You can pick several files at once.

- **Formats:** WAV, M4A (iPhone voice memos), MP3, OGG, FLAC, AAC, and the sound from video files such as MP4.
- **Where the text goes:** each transcript is saved as a `.txt` file next to its recording, e.g. `meeting.m4a` → `meeting.txt`. Existing files are never overwritten; a new one is saved as `meeting (2).txt`. If that folder is read-only, the transcript goes to `Documents\Dictation Transcripts`.
- **Layout:** a new paragraph starts wherever there's a pause of about 2 seconds. Tick *Add timestamps* to put `[02:15]` at the start of each paragraph. Your custom words are applied.
- **Model:** **Accurate (`small.en`)** is best for long or noisy recordings. It's about 3× slower and downloads once (~480 MB). As a rough guide, the Fast model handles 10 minutes of audio in about 1–3 minutes on a normal laptop.
- **Progress:** progress shows in the Recordings tab and the status bubble. Dictation keeps working while a recording is processed. **Stop** saves the part already done.

## CPU usage

When you release the dictation key, Whisper briefly uses several processor cores at full power. In **Options → General → CPU usage** you can choose:

- **Full speed** (default): fastest. The processor spikes for a second or two.
- **Balanced:** uses half the cores, at lower priority, so other programs always come first.
- **Gentle:** uses 2 cores and a simpler search, with Windows *Efficiency mode*. It has the least impact, but is slowest; good on battery.

The setting applies to dictation, read-aloud and recordings, and takes effect straight away. Compare the `STATS` lines in the log to see what it costs on your PC.

## Custom words (acronyms, names, jargon)

Choose **Edit custom words...** from the tray menu. `custom_words.txt` opens in Notepad. Changes apply as soon as you save; you don't need to restart. There are three kinds of line:

```
NMUK                     <- a word to recognise: dictation will prefer this spelling
share point -> SharePoint <- a fix-up: replace what dictation wrote with what you want
NMUK = N M U K           <- a pronunciation: how read-aloud should say the word
```

Acronyms in capitals are also fixed when dictation spells them out. For example, "N M U K", "N.M.U.K." and "NM UK" all become "NMUK". Ordinary lowercase words are left alone, so adding `IT` won't change "it".

## Engines

- **STT:** [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (CTranslate2) running on the CPU with INT8. It has no PyTorch dependency. Voice-activity detection removes silence, which cuts down on made-up text.
- **TTS:** [kokoro-onnx](https://github.com/thewh1teagle/kokoro-onnx), a neural voice. It uses the full-precision model (about 325 MB) on ONNX Runtime and bundles its own espeak-ng, so nothing needs to be installed system-wide. The smaller INT8 model is about 6× *slower* on CPUs, so it's only used if the full model can't be downloaded. The first sentence is kept short, so speech starts about a second after you press F10.
- Both models download once into `.cache/` next to the script. After that, no internet connection is needed.

Disk use is about 1.2 GB in total: roughly 600 MB of Python packages, 145 MB for `base.en` (or 480 MB for `small.en`), and 355 MB for Kokoro.

---

## Installing Python without admin rights

Choose **one** option. Use **Python 3.12** (kokoro-onnx supports 3.10–3.13).

### Option A: official installer, per-user (simplest)
1. Download the *Windows installer (64-bit)* for Python 3.12 from python.org.
2. Run it. **Untick** "Use admin privileges when installing py.exe" and **tick** "Add python.exe to PATH".
3. Click **Install Now**. It installs to `%LOCALAPPDATA%\Programs\Python` and doesn't need admin.

### Option B: WinPython (fully portable, nothing "installed")
1. Download a WinPython 3.12 "dot" release (the small one).
2. Extract it **into the app folder**, so you have e.g. `C:\Users\<you>\DictationHelper\WPy64-31230\`.
3. Run `run_first_time.bat` as usual. The `.bat` files find WinPython next to them automatically.

### Option D: the portable bundle (best for a locked-down or offline work PC)
See **Portable bundle** below. You build everything once on a home PC, then copy one folder.

### Option C: `uv` (one small exe)
```bat
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
uv venv --python 3.12 .venv
.venv\Scripts\activate
```

> The python.org *embeddable* zip also works, but it needs extra setup. Uncomment `import site` in `python312._pth`, then run `get-pip.py`. Options A and B are easier.

## Portable bundle (no install, no admin, no internet on the work PC)

On a PC **with** internet (e.g. at home), run **`make_portable_bundle.bat`** from the app folder. It:
1. downloads a portable Python 3.12 (via [uv](https://github.com/astral-sh/uv)), which is not installed or registered in Windows;
2. installs all the packages *inside* that Python;
3. downloads the speech models, re-using any you already have. It can also include the Accurate model and your custom words.
4. produces `DictationHelper-portable\` and `DictationHelper-portable.zip`, about 1–1.5 GB.

Copy the folder or ZIP to the work PC or a USB stick, put it somewhere short like `C:\Users\<you>\DictationHelper\`, and double-click `run_first_time.bat`, then use `run_silent.bat` from then on. The work PC needs **no Python, no admin rights and no internet**. Everything, including Python itself, lives in that one folder. To remove it, run `uninstall.bat` (it offers to delete the bundled Python too), or just delete the folder.

**Which Python do the `.bat` files use?** They look, in order, for:
1. a `python\` folder next to them (the portable bundle);
2. a WinPython `WPy64-...` folder next to them;
3. the Python installed on the PC.

`run_first_time.bat` prints which one it's using.

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

## Uninstalling

Quit the helper from the tray first, then double-click **`uninstall.bat`**. It asks you to confirm, then:

1. stops the helper if it's still running;
2. deletes the downloaded models (`.cache`), `settings.json` and the log files. It **asks** whether to keep `custom_words.txt`; a kept file stays in the folder, and a reinstall into the same folder picks it up again;
3. removes the start-at-login shortcut, if it points to this folder;
4. **asks** whether to remove the Python packages the helper installed (about 600 MB).
   - Answer **N** if you'll reinstall soon; the next first run is then much quicker.
   - `numpy` and `pillow` are always kept, because other programs often use them.
5. deletes the app files, then itself, then the folder if it's now empty.

Python itself is never removed. To reinstall, download the ZIP again and run `run_first_time.bat`.

## Changing the keys

Open **Options → Keys**. Each function shows its current key:

- **Dictation (hold): F9**
- **Read aloud: F10**
- **Stop / cancel: Esc**

Open one and either click a key in the list, or choose **Press a key to choose...** and press the key you want. The status bubble asks for the key; press Esc, or wait 10 seconds, to cancel. The new key works straight away and is remembered.

- **Allowed keys:** F1–F24, Pause/Break, Scroll Lock, Insert, Print Screen, Right Ctrl and the Menu key.
  - Letters, numbers, Enter, Shift and similar keys are refused, because a hotkey stops the key reaching other programs.
  - Esc can only be the stop key.
- **Clashes:** choosing a key another function already uses swaps the two keys.
- **On laptops:** if the F keys control brightness or volume, you may need to hold **Fn**, or turn on Fn Lock, for F9 and F10. Pause or Right Ctrl can be easier.
- **Reset to F9 / F10 / Esc** puts everything back.

## Configuration

The constants at the top of `dictation_app.py` control the behaviour:

- `DEFAULT_KEYS`: the starting hotkeys. Change them in **Options → Keys** instead (see *Changing the keys*).
- `DEFAULT_OUTPUT_MODE`: `"type"` or `"paste"` (also switchable in **Options → General**). `PASTE_RESTORE_DELAY` sets how long paste mode waits before putting your clipboard back.
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

- **Dictation is transcribed but the text doesn't appear:**
  - Make sure **Options → General → Insert dictated text by** is set to **Typing**.
  - Check `dictation_app.log`. Each dictation logs a line like `Inserting 42 chars by typing into: Document1 - Word`, which shows the window the app thought had focus.
  - If that window is right but nothing appears, the app may be running as administrator (see below).

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
