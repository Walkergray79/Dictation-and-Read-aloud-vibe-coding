"""
Dictation & Read-Aloud Helper
=============================

A lightweight, fully offline dictation (speech-to-text) and read-aloud
(text-to-speech) assistant for Windows, built to help with dyslexia.

    Hold  F9   -> dictate. Release to transcribe and type into the focused app.
    Press F10  -> read the highlighted text aloud (press F10 again to stop).
    Press Esc  -> stop reading / cancel a dictation in progress.

Run once while online:      python dictation_app.py
Afterwards (no console):    pythonw dictation_app.py

On the first run the script installs its own Python packages (no admin needed)
and downloads the speech models into a local ".cache" folder next to this file.
After that it works completely offline.
"""

# =============================================================================
# 0. SELF-BOOTSTRAP  (standard library only - must run before anything else)
# =============================================================================
import importlib.util
import os
import subprocess
import sys

# import name -> pip requirement
REQUIRED_PACKAGES = {
    "faster_whisper": "faster-whisper>=1.0.0",  # STT (CTranslate2, no PyTorch)
    "kokoro_onnx": "kokoro-onnx>=0.4.0",        # TTS (ONNX Runtime)
    "sounddevice": "sounddevice>=0.4.6",        # microphone + speakers
    "numpy": "numpy",
    "pynput": "pynput>=1.7.6",                  # global hotkeys + key simulation
    "pyperclip": "pyperclip>=1.8.2",            # clipboard
    "pystray": "pystray>=0.19.4",               # system tray icon
    "PIL": "pillow",                            # tray icon drawing
    "truststore": "truststore",                 # use Windows cert store (corporate proxies)
}


def _bootstrap() -> None:
    if not ((3, 10) <= sys.version_info[:2] <= (3, 13)):
        print(
            f"WARNING: Python {sys.version.split()[0]} detected. kokoro-onnx currently "
            "supports Python 3.10 - 3.13. Python 3.12 is recommended."
        )

    missing = [pip for mod, pip in REQUIRED_PACKAGES.items()
               if importlib.util.find_spec(mod) is None]
    if not missing:
        return

    print("First run: installing missing packages:\n  " + "\n  ".join(missing))
    print("This happens only once and can take a few minutes...\n")

    # Make sure pip itself exists (some portable Pythons ship without it).
    if importlib.util.find_spec("pip") is None:
        try:
            subprocess.check_call([sys.executable, "-m", "ensurepip", "--upgrade"])
        except Exception:
            print(
                "ERROR: pip is not available in this Python.\n"
                "Download https://bootstrap.pypa.io/get-pip.py and run:\n"
                f"  \"{sys.executable}\" get-pip.py\nthen start this script again."
            )
            input("Press Enter to exit...") if sys.stdin else None
            sys.exit(1)

    base_cmd = [sys.executable, "-m", "pip", "install",
                "--disable-pip-version-check", "--prefer-binary"]
    # Normal install works for venvs and per-user/portable Pythons; if the
    # interpreter folder is read-only we fall back to the user site-packages.
    attempts = [base_cmd + missing]
    if sys.prefix == sys.base_prefix:  # not inside a venv -> --user is legal
        attempts.append(base_cmd + ["--user"] + missing)

    for cmd in attempts:
        try:
            subprocess.check_call(cmd)
            break
        except subprocess.CalledProcessError:
            print("pip install attempt failed, trying fallback...\n")
    else:
        print("ERROR: could not install the required packages. See messages above.")
        input("Press Enter to exit...") if sys.stdin else None
        sys.exit(1)

    # Restart in a fresh interpreter so newly installed packages (and any new
    # user site-packages folder) are importable.
    print("\nPackages installed. Restarting the app...\n")
    sys.exit(subprocess.call([sys.executable] + sys.argv))


_bootstrap()

# =============================================================================
# 1. IMPORTS & CONFIGURATION
# =============================================================================
import ctypes
import io
import json
import logging
import logging.handlers
import queue
import re
import threading
import time
import urllib.request
import uuid
import wave
from pathlib import Path

APP_NAME = "Dictation & Read-Aloud Helper"
APP_DIR = Path(__file__).resolve().parent
CACHE_DIR = APP_DIR / ".cache"
SETTINGS_FILE = APP_DIR / "settings.json"
VOCAB_FILE = APP_DIR / "custom_words.txt"
LOG_FILE = APP_DIR / "dictation_app.log"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Keep every model download inside our portable .cache folder.
os.environ.setdefault("HF_HOME", str(CACHE_DIR / "huggingface"))
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

# ---- Hotkeys -----------------------------------------------------------------
# Single keys only (no combos) so "hold to talk" is reliable. Supported names:
# f1..f24, pause, scroll_lock, insert, ctrl_r (right Ctrl), menu (context-menu key)
DICTATE_KEY = "f9"       # hold to dictate
READ_KEY = "f10"         # press to read selection; press again to stop
STOP_KEY = "esc"         # stops reading / cancels a recording (only swallowed while busy)

# ---- Speech-to-text ----------------------------------------------------------
WHISPER_MODELS = {"Fast (base.en)": "base.en", "Accurate (small.en)": "small.en"}
DEFAULT_WHISPER_MODEL = "base.en"
SAMPLE_RATE = 16000              # Whisper expects 16 kHz mono
MIN_RECORD_SECONDS = 0.3         # ignore accidental taps
MAX_RECORD_SECONDS = 120         # watchdog in case the key-up event is lost
# How dictated text goes into the focused app (switchable from the tray menu):
#   "type"  - simulated key presses; works almost everywhere, never touches the clipboard
#   "paste" - clipboard + Ctrl+V; faster for long text, but some apps read the clipboard late
DEFAULT_OUTPUT_MODE = "type"
PASTE_RESTORE_DELAY = 1.0        # seconds to wait before putting the old clipboard back

# ---- Text-to-speech ----------------------------------------------------------
KOKORO_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
# Full-precision model (~325 MB). The int8 variant is smaller but ~6x SLOWER on CPUs
# (its quantised ops aren't optimised in ONNX Runtime), so it's only a fallback.
KOKORO_MODEL = "kokoro-v1.0.onnx"
KOKORO_MODEL_FALLBACK = "kokoro-v1.0.int8.onnx"
KOKORO_VOICES = "voices-v1.0.bin"
VOICES = {
    "Heart (US, female)": "af_heart",
    "Bella (US, female)": "af_bella",
    "Sarah (US, female)": "af_sarah",
    "Michael (US, male)": "am_michael",
    "Emma (UK, female)": "bf_emma",
    "George (UK, male)": "bm_george",
}
DEFAULT_VOICE = "af_heart"
SPEEDS = [0.8, 0.9, 1.0, 1.1, 1.25, 1.5]
MAX_READ_CHARS = 20000
FIRST_CHUNK_CHARS = 90                 # short first sentence -> speech starts within ~1 s
READ_CLIPBOARD_IF_NO_SELECTION = True  # F10 with nothing highlighted reads the clipboard

IS_WINDOWS = sys.platform == "win32"
LONG_PATH_WARNING = 150  # chars; model files add ~60 more and Windows caps paths at 260

# ---- Logging -------------------------------------------------------------------
log = logging.getLogger("dictation")
log.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s [%(threadName)s] %(levelname)s: %(message)s")
try:
    _fh = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=1_000_000,
                                               backupCount=2, encoding="utf-8")
    _fh.setFormatter(_fmt)
    log.addHandler(_fh)
except OSError:
    pass
if sys.stderr is not None:  # pythonw.exe has no console
    _sh = logging.StreamHandler()
    _sh.setFormatter(_fmt)
    log.addHandler(_sh)

# A crash in any background thread is logged instead of vanishing silently.
threading.excepthook = lambda args: log.error(
    "Unhandled error in thread %s", args.thread.name if args.thread else "?",
    exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

# Third-party imports (guaranteed present after bootstrap).
try:
    import truststore  # trust the Windows certificate store (corporate TLS inspection)
    truststore.inject_into_ssl()
except Exception:
    pass

import numpy as np
import pyperclip
import sounddevice as sd
from PIL import Image, ImageDraw
from pynput import keyboard
import pystray

if IS_WINDOWS:
    import winsound


# =============================================================================
# 2. SETTINGS
# =============================================================================
class Settings:
    """Tiny JSON-backed settings store (mic, voice, speed, model, ...)."""

    def __init__(self):
        self.mic = None  # None = system default; otherwise the device *name*
        self.voice = DEFAULT_VOICE
        self.speed = 1.0
        self.whisper_model = DEFAULT_WHISPER_MODEL
        self.output_mode = DEFAULT_OUTPUT_MODE
        self.show_bubble = True
        self.auto_pin_done = False  # tray icon auto-pinned once (Windows 11)
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            for key in self.__dict__:
                if key in data:
                    setattr(self, key, data[key])
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.warning("Could not read settings (%s); using defaults", exc)

    def save(self):
        try:
            SETTINGS_FILE.write_text(json.dumps(self.__dict__, indent=2), encoding="utf-8")
        except Exception as exc:
            log.warning("Could not save settings: %s", exc)


# =============================================================================
# 3. AUDIO FEEDBACK (CHIMES)
# =============================================================================
def _make_wav(notes, volume=0.22, sr=22050) -> bytes:
    """Render a list of (frequency_hz, seconds) notes to an in-memory WAV."""
    parts = []
    for freq, dur in notes:
        t = np.arange(int(sr * dur)) / sr
        env = np.minimum(1.0, t / 0.005) * np.exp(-t * 6.0 / dur)  # soft attack, decay
        tone = np.sin(2 * np.pi * freq * t) + 0.25 * np.sin(4 * np.pi * freq * t)
        parts.append(tone * env)
    audio = np.concatenate(parts) * volume / 1.25
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


CHIMES = {
    "start": _make_wav([(880, 0.06), (1320, 0.09)]),   # short, high, rising
    "stop": _make_wav([(523, 0.06), (392, 0.10)]),     # short, low, falling
    "success": _make_wav([(1046, 0.05), (1568, 0.12)], volume=0.18),  # bright "pop"
    "error": _make_wav([(220, 0.12), (196, 0.18)]),    # low double "bonk"
    "read": _make_wav([(784, 0.08)], volume=0.15),     # soft single tick: "F10 heard"
}


def chime(name: str) -> None:
    """Play a chime without blocking the caller (winsound, no extra deps)."""
    if not IS_WINDOWS:
        return

    def _play():
        try:
            winsound.PlaySound(CHIMES[name], winsound.SND_MEMORY | winsound.SND_NODEFAULT)
        except Exception as exc:
            log.debug("Chime failed: %s", exc)

    threading.Thread(target=_play, name="chime", daemon=True).start()


# =============================================================================
# 4. CLIPBOARD & KEYBOARD SIMULATION
# =============================================================================
_kb = keyboard.Controller()
_clipboard_lock = threading.Lock()  # dictation paste and read-aloud copy never overlap


def clip_get(retries: int = 5):
    """Read clipboard text. Returns None if the clipboard is locked/unreadable."""
    for _ in range(retries):
        try:
            return pyperclip.paste() or ""
        except Exception:
            time.sleep(0.05)  # another app is holding the clipboard open
    return None


def clip_set(text: str, retries: int = 5) -> bool:
    for _ in range(retries):
        try:
            pyperclip.copy(text)
            return True
        except Exception:
            time.sleep(0.05)
    return False


def press_combo(modifier, key) -> None:
    try:
        _kb.press(modifier)
        _kb.press(key)
        _kb.release(key)
    finally:
        _kb.release(modifier)  # never leave Ctrl stuck down


def foreground_window_title() -> str:
    """Title of the window that will receive dictated text (for the log)."""
    if not IS_WINDOWS:
        return "?"
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        return buf.value or f"(untitled window {hwnd})"
    except Exception:
        return "?"


def wait_for_modifiers_released(timeout: float = 1.5) -> None:
    """Don't inject text while the user still holds Ctrl/Alt/Shift/Win, or the
    letters would turn into shortcuts (Ctrl+S, Alt+F...)."""
    if not IS_WINDOWS:
        return
    get_state = ctypes.windll.user32.GetAsyncKeyState
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not any(get_state(vk) & 0x8000 for vk in (0x10, 0x11, 0x12, 0x5B, 0x5C)):
            return
        time.sleep(0.02)


def type_text(text: str) -> None:
    for ch in text:
        _kb.type(ch)
        time.sleep(0.004)  # tiny gap so busy apps (Word, Teams) don't drop keys


def output_text(text: str, mode: str) -> None:
    """Insert text into the currently focused window."""
    wait_for_modifiers_released()
    log.info("Inserting %d chars by %s into: %s", len(text),
             "typing" if mode == "type" else "pasting", foreground_window_title())
    if mode == "type":
        type_text(text)
        return
    with _clipboard_lock:
        saved = clip_get()
        if not clip_set(text):
            log.warning("Clipboard busy; falling back to typing")
            type_text(text)
            return
        time.sleep(0.05)
        press_combo(keyboard.Key.ctrl, "v")
        # Office, browsers and Teams read the clipboard lazily; restoring too soon
        # makes them paste the *old* clipboard (or nothing).
        time.sleep(PASTE_RESTORE_DELAY)
        if saved is not None:
            clip_set(saved)


def copy_selection() -> str:
    """Simulate Ctrl+C, return the copied text, then restore the clipboard."""
    with _clipboard_lock:
        saved = clip_get()
        sentinel = f"__dictation_helper_{uuid.uuid4().hex}__"
        clip_set(sentinel)
        press_combo(keyboard.Key.ctrl, "c")
        text = ""
        deadline = time.time() + 1.0
        while time.time() < deadline:
            time.sleep(0.05)
            current = clip_get(retries=1)
            if current is not None and current != sentinel:
                text = current
                break
        # Restore the user's original clipboard (text formats only).
        clip_set(saved if saved is not None else "")
    if not text.strip() and READ_CLIPBOARD_IF_NO_SELECTION and saved:
        log.info("Nothing highlighted; reading the clipboard instead")
        text = saved
    return text.strip()


# =============================================================================
# 4b. CUSTOM WORDS (dictation spellings, fix-ups, read-aloud pronunciations)
# =============================================================================
VOCAB_TEMPLATE = """\
# Custom words for the Dictation & Read-Aloud Helper.
# Edit, then save this file - changes apply straight away, no restart needed.
# Lines starting with # are notes and are ignored.
#
# 1) WORDS TO RECOGNISE - one per line. Helps dictation spell them correctly.
#    Acronyms (all capitals) are also fixed when dictation spells them out,
#    e.g. "N M U K" or "N.M.U.K." becomes "NMUK".
NMUK
#
# 2) FIX-UPS - what dictation wrote  ->  what you want instead
#    (whole words, upper/lower case doesn't matter). Example:
# share point -> SharePoint
#
# 3) READ-ALOUD PRONUNCIATION - word  =  how to say it (matches the word exactly)
NMUK = N M U K
"""


class Vocabulary:
    """Loads custom_words.txt and re-reads it whenever the file changes."""

    def __init__(self, path: Path):
        self.path = path
        self._mtime = None
        self._lock = threading.Lock()
        self.words, self.fixups, self.say = [], [], []
        try:
            if not path.exists():
                path.write_text(VOCAB_TEMPLATE, encoding="utf-8")
        except OSError as exc:
            log.warning("Could not create %s: %s", path.name, exc)

    def _refresh(self):
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return
        if mtime == self._mtime:
            return
        words, fixups, say = [], [], []
        try:
            for raw in self.path.read_text(encoding="utf-8-sig").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "->" in line:
                    heard, wanted = (x.strip() for x in line.split("->", 1))
                    if heard and wanted:
                        fixups.append((re.compile(r"(?<!\w)" + re.escape(heard) + r"(?!\w)",
                                                  re.IGNORECASE), wanted))
                elif "=" in line:
                    word, spoken = (x.strip() for x in line.split("=", 1))
                    if word and spoken:
                        say.append((re.compile(r"(?<!\w)" + re.escape(word) + r"(?!\w)"), spoken))
                        words.append(word)  # a word worth pronouncing is worth spelling too
                else:
                    words.append(line)
        except Exception as exc:
            log.warning("Could not read %s: %s", self.path.name, exc)
            return
        self.words = list(dict.fromkeys(words))  # de-duplicate, keep order
        self.fixups, self.say, self._mtime = fixups, say, mtime
        log.info("Custom words loaded: %d words, %d fix-ups, %d pronunciations",
                 len(self.words), len(fixups), len(say))

    def prompt(self):
        """Hint text for Whisper so it prefers these spellings (None if no words)."""
        with self._lock:
            self._refresh()
            words = self.words
        if not words:
            return None
        hint = ", ".join(words)
        return hint[:400] + "."

    @staticmethod
    def _acronym_fixer(word):
        letters = [re.escape(ch) for ch in word]
        pattern = re.compile(r"(?<!\w)" + r"[\s.\-]*".join(letters) + r"(?P<dot>\.)?(?!\w)",
                             re.IGNORECASE)

        def replace(m):
            found = m.group(0)
            core = found[:-1] if m.group("dot") else found
            if core.isalpha() and core.islower():
                return found  # plain lowercase word ("it", "who") - leave alone
            if m.group("dot"):
                rest = m.string[m.end():]
                # keep a full stop that really ends the sentence
                if not rest.strip() or re.match(r"\s+[A-Z]", rest):
                    return word + "."
            return word
        return pattern, replace

    def fix(self, text: str) -> str:
        """Apply fix-ups and acronym clean-up to dictated text."""
        with self._lock:
            self._refresh()
            fixups, words = self.fixups, self.words
        for pattern, wanted in fixups:
            text = pattern.sub(wanted, text)
        for word in words:
            if len(word) >= 2 and word.isalpha() and word.isupper():
                pattern, replace = self._acronym_fixer(word)
                text = pattern.sub(replace, text)
        return text

    def pronounce(self, text: str) -> str:
        """Swap words for their spoken form before reading aloud."""
        with self._lock:
            self._refresh()
            say = self.say
        for pattern, spoken in say:
            text = pattern.sub(spoken, text)
        return text

    def open_in_editor(self):
        if not self.path.exists():
            self.path.write_text(VOCAB_TEMPLATE, encoding="utf-8")
        if IS_WINDOWS:
            os.startfile(str(self.path))  # opens in Notepad (or the default .txt editor)
        else:
            log.info("Edit your custom words in %s", self.path)


# =============================================================================
# 5. MODEL MANAGEMENT (download once -> offline forever)
# =============================================================================
def download_file(url: str, dest: Path) -> None:
    tmp = dest.with_suffix(dest.suffix + ".part")
    log.info("Downloading %s ...", url)
    with urllib.request.urlopen(url, timeout=60) as resp, open(tmp, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        done, last_pct = 0, -10
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            if total:
                pct = done * 100 // total
                if pct >= last_pct + 10:
                    log.info("  %s: %d%%", dest.name, pct)
                    last_pct = pct
    tmp.replace(dest)  # atomic: a half-finished download never looks complete


def load_whisper(model_name: str):
    from faster_whisper import WhisperModel
    from faster_whisper.utils import download_model

    # Download into a plain folder (.cache\whisper\base.en\model.bin) rather than the
    # Hugging Face cache layout, whose deep "models--.../snapshots/<hash>" paths can
    # exceed Windows' 260-character path limit. Once present, no network is touched.
    model_dir = CACHE_DIR / "whisper" / model_name
    needed = ("config.json", "model.bin", "tokenizer.json")
    if not all((model_dir / f).exists() for f in needed):
        log.info("Whisper model '%s' not cached yet - downloading (one time)...", model_name)
        download_model(model_name, output_dir=str(model_dir))
    return WhisperModel(str(model_dir), device="cpu", compute_type="int8",
                        cpu_threads=max(1, min(8, (os.cpu_count() or 4))))


def load_kokoro():
    from kokoro_onnx import Kokoro

    folder = CACHE_DIR / "kokoro"
    folder.mkdir(parents=True, exist_ok=True)
    voices = folder / KOKORO_VOICES
    if not voices.exists():
        download_file(KOKORO_URL + KOKORO_VOICES, voices)
    model = folder / KOKORO_MODEL
    if not model.exists():
        try:
            download_file(KOKORO_URL + KOKORO_MODEL, model)
        except Exception as exc:
            fallback = folder / KOKORO_MODEL_FALLBACK
            if not fallback.exists():
                raise
            log.warning("Could not download %s (%s); using the slower %s for now",
                        KOKORO_MODEL, exc, KOKORO_MODEL_FALLBACK)
            model = fallback
    return Kokoro(str(model), str(voices))


# =============================================================================
# 6. MICROPHONE RECORDING
# =============================================================================
def list_input_devices():
    """Return [(index, name)] for input devices on the default host API
    (avoids the same mic appearing 3-4 times under MME/DirectSound/WASAPI)."""
    try:
        default_in = sd.default.device[0]
        host = sd.query_devices(default_in)["hostapi"] if default_in is not None and default_in >= 0 else 0
        return [(i, d["name"]) for i, d in enumerate(sd.query_devices())
                if d["max_input_channels"] > 0 and d["hostapi"] == host]
    except Exception as exc:
        log.warning("Could not list microphones: %s", exc)
        return []


def resolve_input_device(name):
    if not name:
        return None
    for idx, dev_name in list_input_devices():
        if dev_name == name:
            return idx
    log.warning("Microphone '%s' not found; using system default", name)
    return None


def resample(audio: np.ndarray, src_rate: float, dst_rate: float) -> np.ndarray:
    if int(src_rate) == int(dst_rate) or audio.size == 0:
        return audio.astype(np.float32)
    n = int(round(audio.size * dst_rate / src_rate))
    x_old = np.linspace(0.0, 1.0, audio.size, endpoint=False)
    x_new = np.linspace(0.0, 1.0, n, endpoint=False)
    return np.interp(x_new, x_old, audio).astype(np.float32)


class Recorder:
    """Collects microphone audio between start() and stop()."""

    def __init__(self):
        self._stream = None
        self._frames = []
        self._rate = SAMPLE_RATE
        self._lock = threading.Lock()

    @property
    def active(self) -> bool:
        return self._stream is not None

    def _callback(self, indata, frames, time_info, status):
        if status:
            log.debug("Mic status: %s", status)
        self._frames.append(indata[:, 0].copy())

    def start(self, device) -> None:
        with self._lock:
            self._frames = []
            try:
                stream = sd.InputStream(device=device, channels=1, samplerate=SAMPLE_RATE,
                                        dtype="float32", callback=self._callback)
            except Exception:
                # Some devices refuse 16 kHz; record natively and resample later.
                stream = sd.InputStream(device=device, channels=1, dtype="float32",
                                        callback=self._callback)
            self._rate = stream.samplerate
            try:
                stream.start()
            except Exception:
                stream.close()
                raise
            self._stream = stream

    def stop(self) -> np.ndarray:
        with self._lock:
            stream, self._stream = self._stream, None
            if stream is not None:
                try:
                    stream.stop()
                    stream.close()
                except Exception as exc:  # mic unplugged mid-recording, etc.
                    log.warning("Error closing microphone: %s", exc)
            frames, self._frames = self._frames, []
        if not frames:
            return np.zeros(0, dtype=np.float32)
        return resample(np.concatenate(frames), self._rate, SAMPLE_RATE)


# =============================================================================
# 7. TEXT-TO-SPEECH PLAYBACK
# =============================================================================
def prepare_text(text: str) -> str:
    """Clean copied text so it reads naturally (bullets, line wraps, URLs)."""
    text = re.sub(r"https?://\S+|www\.\S+", "link", text)
    bullet = re.compile(r"^\s*([-*•▪●◦>]|\d+[.)])\s+")
    out = ""
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        is_bullet = bool(bullet.match(raw))
        line = bullet.sub("", raw).strip()
        if not line:  # blank line = paragraph break -> make sure there's a pause
            if out and out[-1] not in ".!?:;":
                out += "."
            continue
        if out:
            # A wrapped line continues the sentence; a heading/bullet gets a pause.
            if out[-1] in ".!?:;," or (line[0].islower() and not is_bullet):
                out += " "
            else:
                out += ". "
        out += line
    return re.sub(r"\s+", " ", out).strip()


def _cut(s: str, limit: int):
    """Split s near `limit` chars, preferring a comma, then a space."""
    cut = s.rfind(", ", 0, limit)
    if cut < limit // 3:
        cut = s.rfind(" ", 0, limit)
    if cut <= 0:
        cut = limit
    return s[:cut + 1].strip(), s[cut + 1:].strip()


def split_chunks(text: str, max_len: int = 250, first_len: int = FIRST_CHUNK_CHARS):
    """Split into sentence-sized chunks so playback starts fast and stops fast.
    The first chunk is kept extra short: speech can only start once it has been
    synthesised, and later chunks are generated while earlier ones play."""
    pieces = []
    for s in re.split(r"(?<=[.!?\u2026])\s+", text):
        s = s.strip()
        while len(s) > max_len:  # very long sentence: cut at a comma or space
            head, s = _cut(s, max_len)
            pieces.append(head)
        if s:
            pieces.append(s)
    chunks = []
    for p in pieces:  # glue tiny fragments ("Hi." "Yes.") onto the previous chunk
        if chunks and len(chunks[-1]) < 40 and len(chunks[-1]) + len(p) < max_len:
            chunks[-1] += " " + p
        else:
            chunks.append(p)
    if chunks and len(chunks[0]) > first_len:
        head, tail = _cut(chunks[0], first_len)
        chunks[0:1] = [c for c in (head, tail) if c]
    return chunks


class Speaker:
    """Synthesises sentence-by-sentence in one thread while another plays,
    so long passages start speaking almost immediately and can be cancelled
    within ~50 ms."""

    BLOCK_SECONDS = 0.05

    def __init__(self, app):
        self.app = app
        self._stop = threading.Event()
        self._thread = None
        self._tts_lock = threading.Lock()

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stop(self) -> None:
        self._stop.set()

    def speak(self, text: str) -> None:
        self.stop()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(text, self._stop),
                                        name="tts-playback", daemon=True)
        self._thread.start()

    def _synth(self, chunks, out_q: queue.Queue, stop: threading.Event):
        try:
            voice = self.app.settings.voice
            lang = "en-gb" if voice.startswith("b") else "en-us"
            for chunk in chunks:
                if stop.is_set():
                    break
                with self._tts_lock:
                    audio, sr = self.app.tts.create(chunk, voice=voice,
                                                    speed=float(self.app.settings.speed), lang=lang)
                while not stop.is_set():
                    try:
                        out_q.put((np.asarray(audio, dtype=np.float32), sr), timeout=0.1)
                        break
                    except queue.Full:
                        pass
        except Exception:
            log.exception("TTS synthesis failed")
            chime("error")
        finally:
            out_q.put(None)

    def _run(self, text: str, stop: threading.Event):
        self.app.set_state("preparing")
        stream = None
        played = 0
        try:
            chunks = split_chunks(self.app.vocab.pronounce(prepare_text(text)))
            log.info("Reading %d characters in %d chunk(s)", len(text), len(chunks))
            q = queue.Queue(maxsize=4)
            threading.Thread(target=self._synth, args=(chunks, q, stop),
                             name="tts-synth", daemon=True).start()
            out_rate = None
            while not stop.is_set():
                try:
                    item = q.get(timeout=0.1)
                except queue.Empty:
                    continue
                if item is None:
                    break
                played += 1
                self.app.set_state("speaking", f"{played} of {len(chunks)}" if len(chunks) > 1 else None)
                audio, sr = item
                if stream is None:
                    try:
                        stream = sd.OutputStream(samplerate=sr, channels=1, dtype="float32")
                    except Exception:
                        stream = sd.OutputStream(channels=1, dtype="float32")
                    out_rate = stream.samplerate
                    stream.start()
                audio = resample(audio, sr, out_rate)
                block = int(out_rate * self.BLOCK_SECONDS)
                for i in range(0, audio.size, block):
                    if stop.is_set():
                        break
                    stream.write(audio[i:i + block])
            if stream is not None:
                if stop.is_set():
                    stream.abort()  # drop buffered audio instantly
                else:
                    stream.write(np.zeros(int(out_rate * 0.2), dtype=np.float32))
                    stream.stop()   # let the tail finish playing
        except Exception:
            log.exception("Audio playback failed")
            chime("error")
        finally:
            stop.set()  # tells the synth thread to quit too
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
            self.app.set_state("idle")


# =============================================================================
# 8. GLOBAL HOTKEYS
# =============================================================================
_VK = {f"f{n}": 0x6F + n for n in range(1, 25)}
_VK.update({"pause": 0x13, "scroll_lock": 0x91, "insert": 0x2D, "ctrl_r": 0xA3,
            "menu": 0x5D, "esc": 0x1B})
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x100, 0x101, 0x104, 0x105
LLKHF_INJECTED = 0x10


class HotkeyListener:
    """Low-level keyboard hook (pynput) that runs on its own thread.

    The hook callback must return within a few hundred milliseconds or Windows
    silently unhooks it, so this class does *nothing* except push small events
    onto a queue; the App's controller thread does the real work.
    On Windows our hotkeys are swallowed so F10 doesn't open app menus, etc."""

    def __init__(self, events: queue.Queue, is_busy, is_paused):
        self.events = events
        self.is_busy = is_busy      # callable -> True while recording/speaking
        self.is_paused = is_paused  # callable -> True when hotkeys are paused
        self._down = set()          # hotkeys whose key-down we took (and swallowed)
        self._listener = None

    def start(self):
        if IS_WINDOWS:
            self._listener = keyboard.Listener(win32_event_filter=self._win32_filter)
        else:
            self._listener = keyboard.Listener(on_press=self._on_press,
                                               on_release=self._on_release)
        self._listener.daemon = True
        self._listener.start()

    def stop(self):
        if self._listener is not None:
            self._listener.stop()

    def alive(self) -> bool:
        return self._listener is not None and self._listener.is_alive()

    def _emit(self, name: str, is_down: bool) -> bool:
        """Translate key edges into app events. Returns True to swallow the key.
        A key-up is only swallowed if we also took its key-down, so other apps
        never see a lone "up" or "down"."""
        if not is_down:
            if name not in self._down:
                return False
            self._down.discard(name)
            if name == "dictate":
                self.events.put("dictate_up")
            return True
        if name in self._down:          # auto-repeat while held - ignore
            return True
        if self.is_paused():            # paused: F9/F10/Esc behave normally
            return False
        if name == "dictate":
            self.events.put("dictate_down")
        elif name == "read":
            self.events.put("read")
        elif name == "stop":
            if not self.is_busy():
                return False            # Esc behaves normally when we're idle
            self.events.put("stop")
        self._down.add(name)
        return True

    def _win32_filter(self, msg, data):
        if data.flags & LLKHF_INJECTED:      # our own simulated Ctrl+C / Ctrl+V
            return True
        names = {_VK[DICTATE_KEY]: "dictate", _VK[READ_KEY]: "read", _VK[STOP_KEY]: "stop"}
        name = names.get(data.vkCode)
        if name is None:
            return True
        swallow = False
        try:
            if msg in (WM_KEYDOWN, WM_SYSKEYDOWN):
                swallow = self._emit(name, True)
            elif msg in (WM_KEYUP, WM_SYSKEYUP):
                swallow = self._emit(name, False)
        except Exception:
            log.exception("Hotkey filter error")
        if swallow:
            self._listener.suppress_event()  # raises internally; must be outside try
        return True

    # Non-Windows fallback (no key suppression) - handy for testing.
    def _key_name(self, key):
        for cfg, name in ((DICTATE_KEY, "dictate"), (READ_KEY, "read"), (STOP_KEY, "stop")):
            try:
                if key == getattr(keyboard.Key, cfg):
                    return name
            except AttributeError:
                pass
        return None

    def _on_press(self, key):
        name = self._key_name(key)
        if name:
            self._emit(name, True)

    def _on_release(self, key):
        name = self._key_name(key)
        if name:
            self._emit(name, False)


# =============================================================================
# 8b. STATUS BUBBLE (on-screen feedback, like the Win+H toolbar)
# =============================================================================
class StatusBubble:
    """A small always-on-top pill near the bottom of the screen showing what the
    app is doing ("Listening...", "Getting ready to read...").

    It is built so it can never get in the way of dictation:
      * it never takes keyboard focus (WS_EX_NOACTIVATE), so typing still goes
        to the app you were in;
      * mouse clicks pass straight through it (WS_EX_TRANSPARENT);
      * it has no taskbar button (WS_EX_TOOLWINDOW).
    Tk runs on its own thread; other threads only post messages to a queue.
    If tkinter isn't available (e.g. the embeddable Python), it quietly does nothing."""

    BG = "#202124"
    FG = "#ffffff"

    def __init__(self):
        self._q = queue.Queue()
        self.enabled = True
        threading.Thread(target=self._run, name="status-bubble", daemon=True).start()

    # ---- thread-safe API ----------------------------------------------------
    def show(self, text: str, color, animate: bool = True):
        self._q.put(("show", text, color, animate, None))

    def flash(self, text: str, color, seconds: float = 1.6):
        self._q.put(("show", text, color, False, seconds))

    def hide(self):
        self._q.put(("hide",))

    # ---- Tk thread ------------------------------------------------------------
    def _run(self):
        try:
            import tkinter as tk
            import tkinter.font as tkfont
        except Exception:
            log.info("tkinter not available - status bubble disabled (tray icon still works)")
            return
        try:
            root = tk.Tk()
            root.overrideredirect(True)
            root.configure(bg=self.BG)
            root.attributes("-topmost", True)
            root.attributes("-alpha", 0.0)  # invisible until styled and needed
            font = tkfont.Font(family="Segoe UI", size=13)
            frame = tk.Frame(root, bg=self.BG, padx=16, pady=9)
            frame.pack()
            dot = tk.Canvas(frame, width=16, height=16, bg=self.BG, highlightthickness=0)
            dot.pack(side="left", padx=(0, 10))
            oval = dot.create_oval(2, 2, 14, 14, fill="#888888", outline="")
            # Fixed width (in characters) so the window never needs resizing later.
            label = tk.Label(frame, text="", width=42, anchor="w", font=font,
                             fg=self.FG, bg=self.BG)
            label.pack(side="left")
            root.update_idletasks()
            w, h = root.winfo_reqwidth(), root.winfo_reqheight()
            sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
            root.geometry(f"{w}x{h}+{(sw - w) // 2}+{sh - h - 110}")
            root.update()
            hwnd = self._win32_style(root)
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
            else:
                root.withdraw()
                root.attributes("-alpha", 0.92)
        except Exception:
            log.exception("Could not create status bubble")
            return

        state = {"text": "", "color": (128, 128, 128), "animate": False,
                 "tick": 0, "hide_at": None, "visible": False}

        def set_visible(on):
            if on == state["visible"]:
                return
            state["visible"] = on
            if hwnd:
                user32 = ctypes.windll.user32
                if on:  # show + stay on top WITHOUT activating (focus stays in your app)
                    user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
                    user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
                else:
                    user32.ShowWindow(hwnd, 0)
            elif on:
                root.deiconify()
            else:
                root.withdraw()

        def poll():
            try:
                while True:
                    msg = self._q.get_nowait()
                    if msg[0] == "hide" or not self.enabled:
                        if msg[0] == "hide" and state["hide_at"] and self.enabled:
                            continue  # let a "Done"/"Nothing selected" flash finish
                        state["hide_at"] = None
                        set_visible(False)
                        continue
                    _, text, color, animate, seconds = msg
                    state.update(text=text, color=color, animate=animate, tick=0,
                                 hide_at=(time.time() + seconds) if seconds else None)
                    label.configure(text=text)
                    dot.itemconfigure(oval, fill="#%02x%02x%02x" % color)
                    set_visible(True)
            except queue.Empty:
                pass
            except Exception:
                log.exception("Status bubble error")
            if state["hide_at"] and time.time() >= state["hide_at"]:
                state["hide_at"] = None
                set_visible(False)
            if state["visible"] and state["animate"]:
                # gentle pulse + animated dots so it's obvious the app is alive
                state["tick"] += 1
                phase = (state["tick"] % 12) / 12.0
                k = 0.55 + 0.45 * abs(1 - 2 * phase)
                r, g, b = state["color"]
                dot.itemconfigure(oval, fill="#%02x%02x%02x" % (int(r * k), int(g * k), int(b * k)))
                base = state["text"].rstrip(".…")
                if base != state["text"]:
                    label.configure(text=base + "." * (1 + (state["tick"] // 4) % 3))
            root.after(80, poll)

        root.after(80, poll)
        root.mainloop()

    @staticmethod
    def _win32_style(root):
        if not IS_WINDOWS:
            return None
        try:
            user32 = ctypes.windll.user32
            hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
            GWL_EXSTYLE = -20
            ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            ex |= 0x08000000 | 0x00000080 | 0x00000020 | 0x00080000 | 0x00000008
            #     NOACTIVATE   TOOLWINDOW   TRANSPARENT  LAYERED      TOPMOST
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex)
            user32.SetLayeredWindowAttributes(hwnd, 0, 235, 0x2)  # LWA_ALPHA: slightly see-through
            return hwnd
        except Exception:
            log.exception("Could not style status bubble")
            return None


def pin_tray_icon() -> str:
    """Windows 11: mark our tray icon "always show" so it sits on the taskbar
    instead of in the ^ overflow. Per-user registry setting, no admin needed.
    Unofficial (Windows has no API for this), so failures are harmless.
    Returns "pinned", "not_found" (icon not registered yet) or "unsupported"."""
    if not IS_WINDOWS:
        return "unsupported"
    import winreg

    def value(key, name):
        try:
            return winreg.QueryValueEx(key, name)[0]
        except OSError:
            return None

    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\NotifyIconSettings")
    except OSError:
        return "unsupported"  # Windows 10 stores this in an undocumented binary blob
    exes = {os.path.normcase(sys.executable), os.path.normcase(os.path.realpath(sys.executable))}
    found = False
    with root:
        index = 0
        while True:
            try:
                sub = winreg.EnumKey(root, index)
            except OSError:
                break
            index += 1
            try:
                with winreg.OpenKey(root, sub, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
                    tip = str(value(key, "InitialTooltip") or "")
                    exe = os.path.normcase(str(value(key, "ExecutablePath") or ""))
                    if APP_NAME not in tip and exe not in exes:
                        continue
                    found = True
                    if value(key, "IsPromoted") != 1:
                        winreg.SetValueEx(key, "IsPromoted", 0, winreg.REG_DWORD, 1)
                        log.info("Tray icon set to always show (%s)", sub)
            except OSError:
                continue
    return "pinned" if found else "not_found"


# =============================================================================
# 9. APPLICATION / CONTROLLER
# =============================================================================
STATE_COLORS = {
    "loading": (128, 128, 128),
    "idle": (40, 110, 220),
    "recording": (220, 40, 40),
    "transcribing": (235, 160, 20),
    "preparing": (30, 160, 80),
    "speaking": (30, 160, 80),
    "error": (90, 90, 90),
    "paused": (150, 150, 150),
}
STATE_LABELS = {  # tray tooltip / menu header
    "loading": "Loading speech models...",
    "idle": f"Ready - hold {DICTATE_KEY.upper()} to dictate, {READ_KEY.upper()} to read",
    "recording": "Listening...",
    "transcribing": "Transcribing...",
    "preparing": "Getting ready to read...",
    "speaking": f"Reading aloud ({READ_KEY.upper()}/{STOP_KEY.capitalize()} to stop)",
    "error": "Model loading failed - see dictation_app.log",
    "paused": "Paused - click the icon to turn back on",
}
BUBBLE_TEXT = {  # on-screen status bubble; text ending in "..." gets animated dots
    "loading": "Loading speech models...",
    "recording": f"Listening - let go of {DICTATE_KEY.upper()} when done",
    "transcribing": "Writing down what you said...",
    "preparing": "Getting ready to read...",
    "speaking": f"Reading aloud - {STOP_KEY.capitalize()} to stop",
}
FLASH_COLORS = {"ok": (30, 160, 80), "warn": (235, 160, 20), "error": (220, 40, 40)}


def make_icon(color, paused: bool = False) -> Image.Image:
    """Draw a microphone glyph (or a pause sign) on a coloured circle."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((2, 2, 62, 62), fill=color + (255,))
    white = (255, 255, 255, 255)
    if paused:
        d.rounded_rectangle((20, 16, 28, 48), radius=2, fill=white)
        d.rounded_rectangle((36, 16, 44, 48), radius=2, fill=white)
        return img
    d.rounded_rectangle((24, 12, 40, 38), radius=8, fill=white)
    d.arc((17, 22, 47, 46), start=0, end=180, fill=white, width=4)
    d.line((32, 46, 32, 52), fill=white, width=4)
    d.line((24, 53, 40, 53), fill=white, width=4)
    return img


class App:
    def __init__(self):
        self.settings = Settings()
        self.stt = None
        self.tts = None
        self.state = "loading"
        self.events = queue.Queue()
        self.transcribe_q = queue.Queue()
        self.recorder = Recorder()
        self.speaker = Speaker(self)
        self.vocab = Vocabulary(VOCAB_FILE)
        self.bubble = StatusBubble()
        self.bubble.enabled = bool(self.settings.show_bubble)
        self._detail = None
        self.paused = False
        self._load_failed = False
        self.hotkeys = HotkeyListener(self.events, self.is_busy, lambda: self.paused)
        self._record_started = 0.0
        self._watchdog = None
        self._pending_transcriptions = 0
        self._pending_lock = threading.Lock()
        self._running = True
        self.icon = pystray.Icon("dictation_helper", make_icon(STATE_COLORS["loading"]),
                                 f"{APP_NAME}\n{STATE_LABELS['loading']}", self._build_menu())

    # ---- state / tray -----------------------------------------------------
    def is_busy(self) -> bool:
        return self.recorder.active or self.speaker.busy

    def set_state(self, state: str, detail=None):
        # When one activity ends, show whichever other activity is still running.
        if state == "idle":
            if self.recorder.active:
                state = "recording"
            elif self.speaker.busy and threading.current_thread() is not self.speaker._thread:
                state = "speaking"
            elif self._pending_transcriptions > 0:
                state = "transcribing"
            elif self.paused:
                state = "paused"
        if (state, detail) == (self.state, self._detail):
            return
        changed = state != self.state
        self.state, self._detail = state, detail
        text = BUBBLE_TEXT.get(state)
        if text:
            if detail:
                text = f"{text}  ({detail})"
            self.bubble.show(text, STATE_COLORS[state])
        elif state == "idle":
            self.bubble.hide()
        if changed:
            try:
                self.icon.icon = make_icon(STATE_COLORS.get(state, STATE_COLORS["idle"]),
                                           paused=(state == "paused"))
                self.icon.title = f"{APP_NAME}\n{STATE_LABELS.get(state, state)}"[:127]
                self.icon.update_menu()
            except Exception:
                pass

    def flash(self, text: str, kind: str = "ok"):
        """Brief message in the status bubble (e.g. "Done", "Nothing selected")."""
        self.bubble.flash(text, FLASH_COLORS[kind], 1.2 if kind == "ok" else 2.5)

    def notify(self, message: str):
        try:
            self.icon.notify(message, APP_NAME)
        except Exception:
            pass

    def _build_menu(self):
        Item, Menu = pystray.MenuItem, pystray.Menu

        def mic_items():
            def pick(name):
                return lambda icon, item: self._set_setting("mic", name)

            def is_on(name):
                return lambda item: self.settings.mic == name

            items = [Item("System default", pick(None), checked=is_on(None), radio=True)]
            for _, name in list_input_devices():
                items.append(Item(name, pick(name), checked=is_on(name), radio=True))
            items.append(Menu.SEPARATOR)
            items.append(Item("Refresh list", lambda icon, item: self._refresh_devices()))
            return items

        def choice_items(key, options):
            def pick(value):
                return lambda icon, item: self._set_setting(key, value)

            def is_on(value):
                return lambda item: getattr(self.settings, key) == value

            return [Item(label, pick(v), checked=is_on(v), radio=True) for label, v in options]

        return Menu(
            # default=True -> a left-click on the tray icon toggles pause
            Item(lambda item: "Turn back on" if self.paused else "Pause (F9/F10 work as normal keys)",
                 lambda icon, item: self.toggle_pause(), default=True),
            Item(lambda item: STATE_LABELS.get(self.state, self.state), None, enabled=False),
            Menu.SEPARATOR,
            Item("Microphone", Menu(mic_items)),
            Item("Reading voice", Menu(lambda: choice_items("voice", VOICES.items()))),
            Item("Reading speed", Menu(lambda: choice_items(
                "speed", [(f"{s:g}x", s) for s in SPEEDS]))),
            Item("Dictation model", Menu(lambda: choice_items("whisper_model", WHISPER_MODELS.items()))),
            Item("Insert dictated text by", Menu(lambda: choice_items("output_mode", [
                ("Typing (works in most apps)", "type"),
                ("Pasting (faster for long text)", "paste")]))),
            Item("Edit custom words...", lambda icon, item: self._edit_vocab()),
            Item("Pin icon to taskbar", lambda icon, item: threading.Thread(
                target=self._pin_icon, args=(True,), name="tray-pin", daemon=True).start()),
            Item("Show status bubble", lambda icon, item: self._set_setting(
                "show_bubble", not self.settings.show_bubble),
                 checked=lambda item: bool(self.settings.show_bubble)),
            Menu.SEPARATOR,
            Item("Stop reading", lambda icon, item: self.speaker.stop(),
                 enabled=lambda item: self.speaker.busy),
            Item("Quit", lambda icon, item: self.quit()),
        )

    def _set_setting(self, key, value):
        old = getattr(self.settings, key)
        setattr(self.settings, key, value)
        self.settings.save()
        log.info("Setting %s = %r", key, value)
        if key == "whisper_model" and value != old:
            threading.Thread(target=self._reload_whisper, name="model-loader", daemon=True).start()
        if key == "show_bubble":
            self.bubble.enabled = bool(value)
            if not value:
                self.bubble.hide()
        self.icon.update_menu()

    def toggle_pause(self):
        self.paused = not self.paused
        log.info("Hotkeys %s", "paused" if self.paused else "resumed")
        if self.paused:
            self.speaker.stop()
            self.events.put("cancel_recording")  # the controller thread owns the mic
            self.hotkeys._down.clear()
            self.set_state("idle")
            self.flash("Paused - F9 and F10 work as normal keys", "warn")
        else:
            if self.stt is not None and self.tts is not None:
                self.set_state("idle")
            else:
                self.set_state("error" if self._load_failed else "loading")
            self.flash(f"On - hold {DICTATE_KEY.upper()} to dictate, {READ_KEY.upper()} to read")
        try:
            self.icon.update_menu()
        except Exception:
            pass

    def _pin_icon(self, manual: bool = False):
        """Pin the tray icon (Windows 11). Automatic once at first start; the menu
        item re-runs it, e.g. after a Python update changed the program path."""
        if not manual and self.settings.auto_pin_done:
            return
        result = "not_found"
        for _ in range(10):  # Explorer records the icon a moment after it appears
            time.sleep(2)
            try:
                result = pin_tray_icon()
            except Exception:
                log.exception("Pinning the tray icon failed")
                result = "unsupported"
            if result != "not_found":
                break
        if result == "pinned":
            self.settings.auto_pin_done = True
            self.settings.save()
            try:  # re-add the icon so Explorer applies the setting immediately
                self.icon.visible = False
                time.sleep(0.5)
                self.icon.visible = True
            except Exception:
                pass
            if manual:
                self.flash("Icon pinned to the taskbar")
        elif manual:
            log.info("Tray icon pinning result: %s", result)
            self.flash("Couldn't pin automatically - drag the icon from ^", "warn")

    def _edit_vocab(self):
        try:
            self.vocab.open_in_editor()
        except Exception as exc:
            log.exception("Could not open custom words file")
            self.notify(f"Could not open {VOCAB_FILE.name}: {exc}")

    def _refresh_devices(self):
        if self.recorder.active or self.speaker.busy:
            self.notify("Finish dictating/reading before refreshing devices.")
            return
        try:  # PortAudio only enumerates devices at initialisation
            sd._terminate()
            sd._initialize()
        except Exception as exc:
            log.warning("Device refresh failed: %s", exc)
        self.icon.update_menu()

    # ---- startup -------------------------------------------------------------
    def _load_models(self):
        try:
            self.stt = load_whisper(self.settings.whisper_model)
            log.info("Whisper '%s' ready", self.settings.whisper_model)
            self.tts = load_kokoro()
            if self.settings.voice not in self.tts.get_voices():
                self.settings.voice = DEFAULT_VOICE
            self.tts.create("Ready.", voice=self.settings.voice, speed=1.0, lang="en-us")  # warm-up
            log.info("Kokoro TTS ready")
            self.set_state("idle")
            chime("success")
            self.flash(f"Ready - hold {DICTATE_KEY.upper()} to dictate, {READ_KEY.upper()} to read")
            self.notify(f"Ready! Hold {DICTATE_KEY.upper()} to dictate, "
                        f"press {READ_KEY.upper()} to read highlighted text.")
        except Exception as exc:
            log.exception("Model loading failed")
            self._load_failed = True
            self.set_state("error")
            if isinstance(exc, (FileNotFoundError, OSError)) and getattr(exc, "winerror", None) in (3, 206):
                hint = ("The folder path is too long for Windows. Move the app to a short folder "
                        "such as C:\\Users\\<you>\\DictationHelper, delete .cache, and run it again.")
            else:
                hint = "Connect to the internet for the first run, then restart the app."
            log.error("STARTUP FAILED: %s", hint)
            self.flash("Could not load models - see the log", "error")
            self.notify(f"Could not load speech models. {hint}")

    def _reload_whisper(self):
        try:
            self.set_state("loading")
            model = load_whisper(self.settings.whisper_model)
            self.stt = model
            log.info("Switched dictation model to %s", self.settings.whisper_model)
        except Exception as exc:
            log.exception("Model switch failed")
            self.notify(f"Could not load {self.settings.whisper_model}: {exc}")
        finally:
            self.set_state("idle")

    def _setup(self, icon):
        icon.visible = True
        self.bubble.show(BUBBLE_TEXT["loading"], STATE_COLORS["loading"])
        if IS_WINDOWS and len(str(APP_DIR)) > LONG_PATH_WARNING:
            log.warning("App folder path is %d characters long: %s", len(str(APP_DIR)), APP_DIR)
            log.warning("Windows limits paths to 260 characters. If model loading fails, move "
                        "this folder somewhere shorter, e.g. C:\\Users\\<you>\\DictationHelper")
            self.notify("This folder's path is very long, which can break model downloads. "
                        "If loading fails, move it to e.g. C:\\Users\\<you>\\DictationHelper.")
        for target, name in ((self._controller_loop, "controller"),
                             (self._transcribe_loop, "transcriber"),
                             (self._load_models, "model-loader"),
                             (self._hook_health_loop, "hook-health"),
                             (self._pin_icon, "tray-pin")):
            threading.Thread(target=target, name=name, daemon=True).start()
        self.hotkeys.start()

    def run(self):
        log.info("%s starting (Python %s)", APP_NAME, sys.version.split()[0])
        self.icon.run(setup=self._setup)  # blocks: tray message loop on main thread
        log.info("Exited")

    def quit(self):
        self._running = False
        try:
            self.speaker.stop()
            if self.recorder.active:
                self.recorder.stop()
            self.hotkeys.stop()
        finally:
            self.events.put(None)
            self.transcribe_q.put(None)
            self.icon.stop()

    # ---- background loops ---------------------------------------------------
    def _hook_health_loop(self):
        """Restart the keyboard hook if its thread ever dies."""
        while self._running:
            time.sleep(5)
            if self._running and not self.hotkeys.alive():
                log.warning("Hotkey listener stopped - restarting it")
                try:
                    self.hotkeys = HotkeyListener(self.events, self.is_busy, lambda: self.paused)
                    self.hotkeys.start()
                except Exception:
                    log.exception("Could not restart hotkey listener")

    def _controller_loop(self):
        """Single thread that owns recording state; events arrive from the hook."""
        while self._running:
            event = self.events.get()
            if event is None:
                break
            try:
                handler = {
                    "dictate_down": self._on_dictate_down,
                    "dictate_up": self._on_dictate_up,
                    "dictate_timeout": self._on_dictate_timeout,
                    "read": self._on_read,
                    "stop": self._on_stop,
                    "cancel_recording": self._cancel_recording,
                }[event]
                handler()
            except Exception:
                log.exception("Error handling %s", event)
                chime("error")

    def _on_dictate_down(self):
        if self.stt is None:
            chime("error")
            self.notify("Still loading speech models - please wait a moment.")
            return
        if self.recorder.active:
            return
        self.speaker.stop()  # don't record our own voice
        try:
            self.recorder.start(resolve_input_device(self.settings.mic))
        except Exception as exc:
            log.exception("Could not open microphone")
            chime("error")
            self.flash("Microphone problem - check the tray menu", "error")
            self.notify(f"Microphone error: {exc}")
            return
        chime("start")
        self._record_started = time.time()
        self.set_state("recording")
        self._watchdog = threading.Timer(MAX_RECORD_SECONDS, self.events.put, ("dictate_timeout",))
        self._watchdog.daemon = True
        self._watchdog.start()

    def _finish_recording(self):
        if self._watchdog:
            self._watchdog.cancel()
        audio = self.recorder.stop()
        return audio

    def _on_dictate_up(self):
        if not self.recorder.active:
            return
        audio = self._finish_recording()
        chime("stop")
        duration = audio.size / SAMPLE_RATE
        if duration < MIN_RECORD_SECONDS:
            log.info("Recording too short (%.2fs) - ignored", duration)
            self.set_state("idle")
            return
        with self._pending_lock:
            self._pending_transcriptions += 1
        self.set_state("transcribing")
        self.transcribe_q.put(audio)

    def _on_dictate_timeout(self):
        if self.recorder.active and time.time() - self._record_started >= MAX_RECORD_SECONDS - 1:
            log.warning("Recording hit %ss limit (stuck key?) - stopping", MAX_RECORD_SECONDS)
            self.hotkeys._down.discard("dictate")
            self._on_dictate_up()

    def _on_read(self):
        if self.speaker.busy:  # F10 again = stop
            self.speaker.stop()
            return
        if self.state == "preparing":  # still copying the selection - ignore double-press
            return
        if self.tts is None:
            chime("error")
            self.notify("Still loading speech models - please wait a moment.")
            return
        if self.recorder.active:
            return
        chime("read")                # instant "I heard you"...
        self.set_state("preparing")  # ...and the bubble appears before any slow work
        threading.Thread(target=self._read_selection, name="reader", daemon=True).start()

    def _read_selection(self):
        try:
            text = copy_selection()
        except Exception:
            log.exception("Copying the selection failed")
            text = ""
        if not text:
            chime("error")
            log.info("Nothing to read (no selection, clipboard empty)")
            self.flash("Nothing selected - highlight text first", "warn")
            self.set_state("idle")
            return
        if len(text) > MAX_READ_CHARS:
            self.notify(f"Long selection - reading the first {MAX_READ_CHARS:,} characters.")
            text = text[:MAX_READ_CHARS]
        self.speaker.speak(text)

    def _cancel_recording(self):
        if self.recorder.active:
            self._finish_recording()
            chime("stop")
            log.info("Dictation cancelled")
            self.set_state("idle")

    def _on_stop(self):
        if self.speaker.busy:
            self.speaker.stop()
        elif self.recorder.active:  # Esc while dictating = cancel
            self._cancel_recording()

    def _transcribe_loop(self):
        """Runs Whisper off the hook/tray threads; one recording at a time, in order."""
        while self._running:
            audio = self.transcribe_q.get()
            if audio is None:
                break
            try:
                text = self._transcribe(audio)
                if text:
                    output_text(text + " ", self.settings.output_mode)
                    chime("success")
                    self.flash("Done")
                elif text is None:
                    chime("error")
                    self.flash("No sound from mic - check the tray menu", "error")
                else:
                    log.info("No speech recognised")
                    chime("error")
                    self.flash("Didn't catch that - please try again", "warn")
            except Exception:
                log.exception("Transcription/typing failed")
                chime("error")
                self.flash("Something went wrong - see the log", "error")
            finally:
                with self._pending_lock:
                    self._pending_transcriptions -= 1
                self.set_state("idle")

    def _transcribe(self, audio: np.ndarray):
        """Returns the text, "" if no speech was recognised, or None if the mic was silent."""
        if float(np.max(np.abs(audio))) < 0.002:
            log.info("Recording is silent - is the right microphone selected / unmuted?")
            return None
        start = time.time()
        model_name = self.settings.whisper_model
        segments, _info = self.stt.transcribe(
            audio,
            language="en" if model_name.endswith(".en") else None,
            beam_size=5,
            vad_filter=True,                       # drops silence -> fewer hallucinations
            vad_parameters={"min_silence_duration_ms": 500},
            condition_on_previous_text=False,
            without_timestamps=True,
            initial_prompt=self.vocab.prompt(),    # custom words -> preferred spellings
        )
        text = " ".join(seg.text.strip() for seg in segments).strip()
        text = self.vocab.fix(re.sub(r"\s+", " ", text))
        log.info("Transcribed %.1fs of audio in %.1fs: %r",
                 audio.size / SAMPLE_RATE, time.time() - start, text)
        return text


# =============================================================================
# 10. ENTRY POINT
# =============================================================================
def _single_instance() -> bool:
    """Prevent two copies typing everything twice."""
    if not IS_WINDOWS:
        return True
    global _MUTEX
    _MUTEX = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\DictationReadAloudHelper")
    return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def main():
    if IS_WINDOWS:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    if not _single_instance():
        log.info("Another instance is already running - exiting.")
        return
    try:
        App().run()
    except KeyboardInterrupt:
        pass
    except Exception:
        log.exception("Fatal error")
        raise


if __name__ == "__main__":
    main()
