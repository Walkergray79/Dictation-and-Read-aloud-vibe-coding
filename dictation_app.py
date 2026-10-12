"""
Dictation & Read-Aloud Helper
=============================

A lightweight, fully offline dictation (speech-to-text) and read-aloud
(text-to-speech) assistant for Windows, built to help with dyslexia.

    Hold  F9   -> dictate. Release to transcribe and type into the focused app.
    Press F10  -> read the highlighted text aloud (press F10 again to stop).
    Press Esc  -> stop reading / cancel a dictation in progress.
    (All three keys can be changed from the tray menu: Keys.)

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
    # Not inside a venv -> --user is a legal fallback. Never for a portable Python
    # (started with -s): its packages must stay inside its own folder.
    if sys.prefix == sys.base_prefix and not sys.flags.no_user_site:
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
    flags = ["-s"] if sys.flags.no_user_site else []  # keep a portable Python portable
    sys.exit(subprocess.call([sys.executable] + flags + sys.argv))


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
APP_VERSION = "1.11.0"  # bump on every release; see CHANGELOG.md
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
os.environ.setdefault("HF_HUB_DISABLE_UPDATE_CHECK", "1")  # never query PyPI for new versions

# ---- Hotkeys -----------------------------------------------------------------
# Changeable from the tray menu (Keys); these are the defaults. Single keys only
# (no combos) so "hold to talk" is reliable, and only keys that aren't needed for
# typing, because a hotkey is swallowed and never reaches other apps.
DEFAULT_KEYS = {
    "dictate": "f9",   # hold to dictate
    "read": "f10",     # press to read the selection; press again to stop
    "stop": "esc",     # stops reading / cancels a recording (only swallowed while busy)
}
HOTKEY_CHOICES = {  # name (as stored in settings.json): (Windows virtual-key code, label)
    **{f"f{n}": (0x6F + n, f"F{n}") for n in range(1, 25)},
    "esc": (0x1B, "Esc"),
    "pause": (0x13, "Pause/Break"),
    "scroll_lock": (0x91, "Scroll Lock"),
    "insert": (0x2D, "Insert"),
    "print_screen": (0x2C, "Print Screen"),
    "ctrl_r": (0xA3, "Right Ctrl"),
    "menu": (0x5D, "Menu key"),
}
ROLE_NAMES = {"dictate": "Dictation (hold)", "read": "Read aloud", "stop": "Stop / cancel"}
KEYS = dict(DEFAULT_KEYS)  # the keys in use right now (loaded from settings at startup)


def key_allowed(role: str, name: str) -> bool:
    # Esc is too important to swallow all the time, so it can only be the stop key
    # (which is only swallowed while the app is busy).
    return name in HOTKEY_CHOICES and (name != "esc" or role == "stop")


def key_label(role: str) -> str:
    return HOTKEY_CHOICES[KEYS[role]][1]


def with_keys(text: str) -> str:
    """Fill {dictate} / {read} / {stop} placeholders with the current key names."""
    return text.format(dictate=key_label("dictate"), read=key_label("read"), stop=key_label("stop"))

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

# ---- CPU usage (Options -> General) ---------------------------------------------
# "full" is the original behaviour. The others trade speed for a smaller CPU spike.
CPU_MODES = {
    "full": "Full speed (default)",
    "balanced": "Balanced - half the cores, lower priority",
    "gentle": "Gentle - 2 cores, lowest impact (slowest)",
}
DEFAULT_CPU_MODE = "full"


def cpu_threads(mode: str) -> int:
    full = max(1, min(8, os.cpu_count() or 4))
    return {"balanced": max(1, full // 2), "gentle": min(2, full)}.get(mode, full)


def beam_size(mode: str) -> int:
    return 1 if mode == "gentle" else 5  # 1 = greedy decoding: about half the work


def apply_cpu_priority(mode: str) -> None:
    """Below-normal priority (and Windows 11 'Efficiency mode' for gentle) so other
    programs always come first. Full speed restores the normal settings."""
    if not IS_WINDOWS:
        return
    try:
        k32 = ctypes.windll.kernel32
        proc = k32.GetCurrentProcess()
        k32.SetPriorityClass(proc, 0x20 if mode == "full" else 0x4000)  # NORMAL / BELOW_NORMAL

        class PowerThrottling(ctypes.Structure):
            _fields_ = [("Version", ctypes.c_ulong), ("ControlMask", ctypes.c_ulong),
                        ("StateMask", ctypes.c_ulong)]
        eco = mode == "gentle"
        state = PowerThrottling(1, 1 if eco else 0, 1 if eco else 0)  # 0/0 = let Windows decide
        k32.SetProcessInformation(proc, 4, ctypes.byref(state), ctypes.sizeof(state))
    except Exception as exc:
        log.debug("Could not set CPU priority: %s", exc)


# ---- Recordings (Options -> Recordings) -------------------------------------------
AUDIO_FILE_TYPES = [
    ("Audio and video files", "*.wav *.m4a *.mp3 *.ogg *.oga *.opus *.flac *.aac *.wma *.amr "
                              "*.3gp *.mp4 *.m4v *.mov *.webm *.mkv"),
    ("All files", "*.*"),
]
PARAGRAPH_GAP_SECONDS = 2.0   # a pause this long in a recording starts a new paragraph
PARAGRAPH_MAX_CHARS = 700     # ...and very long paragraphs are split at a sentence end

# ---- Text-to-speech ----------------------------------------------------------
KOKORO_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
# Full-precision model (~325 MB). The int8 variant is smaller but ~6x SLOWER on CPUs
# (its quantised ops aren't optimised in ONNX Runtime), so it's only a fallback.
KOKORO_MODEL = "kokoro-v1.0.onnx"
KOKORO_MODEL_FALLBACK = "kokoro-v1.0.int8.onnx"
KOKORO_VOICES = "voices-v1.0.bin"
KOKORO_MODEL_RELEASE = "1.0"  # matches "model-files-v1.0" in KOKORO_URL
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
from PIL import Image, ImageDraw

# --download-models (used to build a portable bundle) needs no sound, keyboard or
# tray, so it must work even on a PC/server where those can't start.
HEADLESS = "--download-models" in sys.argv
try:
    import sounddevice as sd
    from pynput import keyboard
    import pystray
except Exception:
    if not HEADLESS:
        raise
    sd = keyboard = pystray = None

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
        self.check_updates_on_start = False  # off: the app stays fully offline
        self.cpu_mode = DEFAULT_CPU_MODE
        self.file_model = "dictation"    # "dictation" = same model as dictation, or a model name
        self.file_timestamps = False
        self.file_open_when_done = True
        self.dictate_key = DEFAULT_KEYS["dictate"]
        self.read_key = DEFAULT_KEYS["read"]
        self.stop_key = DEFAULT_KEYS["stop"]
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            for key in self.__dict__:
                if key in data:
                    setattr(self, key, data[key])
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.warning("Could not read settings (%s); using defaults", exc)

    def validate(self):
        if self.cpu_mode not in CPU_MODES:
            self.cpu_mode = DEFAULT_CPU_MODE
        if self.file_model not in ("dictation", *WHISPER_MODELS.values()):
            self.file_model = "dictation"

    def apply_keys(self):
        """Copy the saved hotkeys into KEYS, falling back to the defaults if the
        file holds an unknown key or the same key twice."""
        wanted = {"dictate": self.dictate_key, "read": self.read_key, "stop": self.stop_key}
        if (all(key_allowed(role, name) for role, name in wanted.items())
                and len(set(wanted.values())) == 3):
            KEYS.update(wanted)
        else:
            log.warning("Invalid hotkeys in settings (%s); using defaults", wanted)
            KEYS.update(DEFAULT_KEYS)
            self.dictate_key, self.read_key, self.stop_key = (DEFAULT_KEYS[r] for r in
                                                              ("dictate", "read", "stop"))

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
_kb = keyboard.Controller() if keyboard else None
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


def load_whisper(model_name: str, threads: int = 0):
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
                        cpu_threads=threads or cpu_threads("full"))


def load_kokoro(threads: int = 0):
    """threads=0 keeps ONNX Runtime's own default (all cores) - the Full speed setting."""
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
    if threads:
        try:
            import onnxruntime as rt
            opts = rt.SessionOptions()
            opts.intra_op_num_threads = threads
            opts.inter_op_num_threads = 1
            session = rt.InferenceSession(str(model), opts, providers=["CPUExecutionProvider"])
            return Kokoro.from_session(session, str(voices))
        except Exception as exc:
            log.warning("Could not limit read-aloud threads (%s); using the default", exc)
    return Kokoro(str(model), str(voices))


# =============================================================================
# 5b. VERSION INFO & MODEL UPDATE CHECKS (only ever run when you ask)
# =============================================================================
def _pkg_version(name: str) -> str:
    try:
        from importlib.metadata import version
        return version(name)
    except Exception:
        return "?"


def whisper_local_revision(model_name: str):
    """Commit id of the downloaded Whisper model (recorded by huggingface_hub)."""
    meta = CACHE_DIR / "whisper" / model_name / ".cache" / "huggingface" / "download" / "model.bin.metadata"
    try:
        return meta.read_text(encoding="utf-8").splitlines()[0].strip() or None
    except (OSError, IndexError):
        return None


def version_report(app) -> str:
    """Plain-text summary for the About box - easy for colleagues to copy and send."""
    rev = whisper_local_revision(app.settings.whisper_model)
    voice_file = KOKORO_MODEL if (CACHE_DIR / "kokoro" / KOKORO_MODEL).exists() else KOKORO_MODEL_FALLBACK
    return "\n".join([
        f"{APP_NAME}",
        f"Version {APP_VERSION}",
        "",
        f"Dictation model: {app.settings.whisper_model} (revision {rev[:7] if rev else 'unknown'})",
        f"Read-aloud voice: {voice_file}, voice {app.settings.voice}, speed {app.settings.speed:g}x",
        f"Insert mode: {app.settings.output_mode}",
        "",
        f"Python {sys.version.split()[0]} ({sys.executable})",
        f"faster-whisper {_pkg_version('faster-whisper')}, ctranslate2 {_pkg_version('ctranslate2')}, "
        f"kokoro-onnx {_pkg_version('kokoro-onnx')}, onnxruntime {_pkg_version('onnxruntime')}",
        f"Folder: {APP_DIR}",
    ])


def _get_json(url: str, timeout: float = 15):
    req = urllib.request.Request(url, headers={"User-Agent": f"DictationHelper/{APP_VERSION}",
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _version_tuple(v: str):
    return tuple(int(x) for x in re.findall(r"\d+", v))


def check_model_updates(whisper_model: str) -> dict:
    """Ask Hugging Face / GitHub / PyPI whether anything newer exists.
    Downloads nothing. Each check is independent, so one blocked site
    (common on work networks) doesn't spoil the others."""
    result = {"lines": [], "whisper_update": False, "anything_new": False}
    lines = result["lines"]

    # 1) Dictation model: compare the downloaded revision with the latest on Hugging Face
    try:
        from faster_whisper.utils import _MODELS
        from huggingface_hub import HfApi
        latest = HfApi().model_info(_MODELS[whisper_model], timeout=15).sha
        local = whisper_local_revision(whisper_model)
        if local and latest and local != latest:
            result["whisper_update"] = result["anything_new"] = True
            lines.append(f"Dictation model {whisper_model}: UPDATE AVAILABLE "
                         f"({local[:7]} -> {latest[:7]})")
        elif local:
            lines.append(f"Dictation model {whisper_model}: up to date ({local[:7]})")
        else:
            lines.append(f"Dictation model {whisper_model}: latest is {latest[:7]} "
                         "(installed revision unknown)")
    except Exception as exc:
        lines.append(f"Dictation model: could not check ({type(exc).__name__})")

    # 2) Read-aloud voice: newer "model-files-vX.Y" releases on GitHub
    try:
        releases = _get_json("https://api.github.com/repos/thewh1teagle/kokoro-onnx/releases?per_page=50")
        tags = [r.get("tag_name", "") for r in releases]
        versions = sorted((_version_tuple(t), t) for t in tags if t.startswith("model-files-v"))
        if versions and versions[-1][0] > _version_tuple(KOKORO_MODEL_RELEASE):
            result["anything_new"] = True
            lines.append(f"Read-aloud voice: newer release {versions[-1][1]} exists "
                         f"(you have v{KOKORO_MODEL_RELEASE}; needs a future app version)")
        else:
            lines.append(f"Read-aloud voice: up to date (v{KOKORO_MODEL_RELEASE})")
    except Exception as exc:
        lines.append(f"Read-aloud voice: could not check ({type(exc).__name__})")

    # 3) Speech engines (Python packages) - for information only
    for pkg in ("faster-whisper", "kokoro-onnx"):
        try:
            latest = _get_json(f"https://pypi.org/pypi/{pkg}/json")["info"]["version"]
            installed = _pkg_version(pkg)
            if installed != "?" and _version_tuple(latest) > _version_tuple(installed):
                result["anything_new"] = True
                lines.append(f"{pkg}: {installed} installed, {latest} available")
            else:
                lines.append(f"{pkg}: up to date ({installed})")
        except Exception as exc:
            lines.append(f"{pkg}: could not check ({type(exc).__name__})")
    return result


def update_whisper_model(model_name: str) -> None:
    """Download the latest revision into a side folder, then swap it in, so a
    failed or interrupted download never breaks the working model."""
    from faster_whisper.utils import download_model
    import shutil

    final = CACHE_DIR / "whisper" / model_name
    fresh = CACHE_DIR / "whisper" / (model_name + ".new")
    old = CACHE_DIR / "whisper" / (model_name + ".old")
    for d in (fresh, old):
        shutil.rmtree(d, ignore_errors=True)
    download_model(model_name, output_dir=str(fresh))
    if final.exists():
        final.rename(old)
    try:
        fresh.rename(final)
    except OSError:
        if old.exists():
            old.rename(final)  # put the working model back
        raise
    shutil.rmtree(old, ignore_errors=True)


def message_box(text: str, yes_no: bool = False) -> bool:
    """Windows message box (returns True for Yes). Falls back to the log elsewhere."""
    if not IS_WINDOWS:
        log.info("%s", text)
        return False
    MB_YESNO, MB_ICONQUESTION, MB_ICONINFO = 0x4, 0x20, 0x40
    MB_SETFOREGROUND, MB_TOPMOST = 0x10000, 0x40000
    flags = MB_SETFOREGROUND | MB_TOPMOST | ((MB_YESNO | MB_ICONQUESTION) if yes_no else MB_ICONINFO)
    return ctypes.windll.user32.MessageBoxW(None, text, f"{APP_NAME} v{APP_VERSION}", flags) == 6


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

    def speak(self, text: str, pressed_at=None, copy_s: float = 0.0) -> None:
        """pressed_at: time.perf_counter() of the F10 press (for the stats line)."""
        self.stop()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="tts-playback", daemon=True,
                                        args=(text, self._stop, pressed_at or time.perf_counter(),
                                              copy_s))
        self._thread.start()

    def _synth(self, chunks, out_q: queue.Queue, stop: threading.Event, timing: dict):
        try:
            voice = self.app.settings.voice
            lang = "en-gb" if voice.startswith("b") else "en-us"
            for chunk in chunks:
                if stop.is_set():
                    break
                with self._tts_lock:
                    tic = time.perf_counter()
                    audio, sr = self.app.tts.create(chunk, voice=voice,
                                                    speed=float(self.app.settings.speed), lang=lang)
                    timing["synth"] += time.perf_counter() - tic
                    timing["speech"] += len(audio) / sr
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

    def _run(self, text: str, stop: threading.Event, pressed_at: float, copy_s: float):
        self.app.set_state("preparing")
        stream = None
        played = 0
        chunks = []
        timing = {"synth": 0.0, "speech": 0.0}
        first_sound = None
        samples_played = 0
        out_rate = None
        stopped = False
        try:
            chunks = split_chunks(self.app.vocab.pronounce(prepare_text(text)))
            q = queue.Queue(maxsize=4)
            threading.Thread(target=self._synth, args=(chunks, q, stop, timing),
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
                if first_sound is None:
                    first_sound = time.perf_counter() - pressed_at
                for i in range(0, audio.size, block):
                    if stop.is_set():
                        break
                    stream.write(audio[i:i + block])
                    samples_played += min(block, audio.size - i)
            stopped = stop.is_set()
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
            try:
                self.app.stats.read(text, len(chunks), copy_s, first_sound, timing["synth"],
                                    timing["speech"], samples_played / out_rate if out_rate else 0.0,
                                    stopped)
            except Exception:
                log.debug("Could not record read-aloud stats", exc_info=True)
            self.app.set_state("idle")


# =============================================================================
# 8. GLOBAL HOTKEYS
# =============================================================================
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x100, 0x101, 0x104, 0x105
LLKHF_INJECTED = 0x10


class HotkeyListener:
    """Low-level keyboard hook (pynput) that runs on its own thread.

    The hook callback must return within a few hundred milliseconds or Windows
    silently unhooks it, so this class does *nothing* except push small events
    onto a queue; the App's controller thread does the real work.
    On Windows our hotkeys are swallowed so F10 doesn't open app menus, etc."""

    def __init__(self, events: queue.Queue, is_busy, is_paused, is_capturing):
        self.events = events
        self.is_busy = is_busy            # callable -> True while recording/speaking
        self.is_paused = is_paused        # callable -> True when hotkeys are paused
        self.is_capturing = is_capturing  # callable -> True while choosing a new key
        self._down = set()                # hotkeys whose key-down we took (and swallowed)
        self._capture_down = set()        # keys pressed while choosing a new key
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
        vk = data.vkCode
        is_down = msg in (WM_KEYDOWN, WM_SYSKEYDOWN)
        # "Press a key to choose": the next key press is captured, not acted on.
        if vk in self._capture_down and not is_down:
            self._capture_down.discard(vk)
            self._listener.suppress_event()
        if self.is_capturing():
            if is_down:
                if vk not in self._capture_down:
                    self._capture_down.add(vk)
                    self.events.put(("capture", vk))
                self._listener.suppress_event()
            return True
        names = {HOTKEY_CHOICES[KEYS[role]][0]: role for role in ("dictate", "read", "stop")}
        name = names.get(vk)
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
        for role in ("dictate", "read", "stop"):
            try:
                if key == getattr(keyboard.Key, KEYS[role]):
                    return role
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
        self.ok = False  # True once Tk is up (the Options window needs it too)
        threading.Thread(target=self._run, name="status-bubble", daemon=True).start()

    # ---- thread-safe API ----------------------------------------------------
    def show(self, text: str, color, animate: bool = True):
        self._q.put(("show", text, color, animate, None))

    def flash(self, text: str, color, seconds: float = 1.6):
        self._q.put(("show", text, color, False, seconds))

    def hide(self):
        self._q.put(("hide",))

    def call(self, fn):
        """Run fn(root) on the Tk thread - used to open the Options window."""
        self._q.put(("call", fn))

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
        self.ok = True

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
                    if msg[0] == "call":
                        try:
                            msg[1](root)
                        except Exception:
                            log.exception("Window error")
                        continue
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


def decode_audio_file(path: Path) -> np.ndarray:
    """Any audio/video file -> 16 kHz mono float32 for Whisper, using PyAV (installed
    with faster-whisper). Done here rather than with faster_whisper.decode_audio,
    which breaks with newer PyAV releases (it passes an option PyAV 19 removed)."""
    import av
    resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
    chunks = []

    def keep(frames):
        if frames is None:
            return
        for fr in (frames if isinstance(frames, list) else [frames]):
            chunks.append(fr.to_ndarray().reshape(-1))

    with av.open(str(path)) as container:
        if not container.streams.audio:
            raise ValueError("this file has no audio track")
        for frame in container.decode(audio=0):
            keep(resampler.resample(frame))
        try:
            keep(resampler.resample(None))  # flush the last few samples
        except Exception:
            pass
    if not chunks:
        raise ValueError("no audio could be read from this file")
    return np.concatenate(chunks).astype(np.float32) / 32768.0


class FileTranscriber:
    """Transcribes recordings (WAV, M4A, MP3, ... even video) chosen in
    Options -> Recordings. Runs in the background with its OWN copy of the
    model, so dictation keeps working while a long file is processed.
    Each transcript is saved as a .txt next to the recording."""

    def __init__(self, app):
        self.app = app
        self._q = queue.Queue()
        self._cancel = threading.Event()
        self._model = None          # (name, threads, WhisperModel), freed when idle
        self.status = "Idle"        # shown in the Options window
        self.busy = False
        self.done = self.total = 0
        threading.Thread(target=self._run, name="file-transcriber", daemon=True).start()

    def add(self, paths):
        paths = [Path(p) for p in paths]
        if not self.busy:
            self.done, self.total = 0, 0
        self.total += len(paths)
        for path in paths:
            self._q.put(path)
        log.info("Queued %d recording(s) for transcription", len(paths))

    def cancel(self):
        if self.busy:
            self._cancel.set()
            while not self._q.empty():  # drop the files still waiting
                try:
                    self._q.get_nowait()
                except queue.Empty:
                    break

    # ---- worker thread -------------------------------------------------------------
    def _run(self):
        while True:
            path = self._q.get()
            self.busy = True
            self._cancel.clear()
            try:
                self._transcribe_file(path)
            except Exception as exc:
                log.exception("Could not transcribe %s", path)
                self._progress(f"Failed: {path.name} ({type(exc).__name__})")
                self.app.flash(f"Couldn't transcribe {path.name[:24]}", "error")
            finally:
                self.done += 1
            if self._q.empty():
                self.busy = False
                self._model = None  # free the memory until it's needed again
                if not self.status.startswith(("Failed", "Stopped")):
                    self.status = f"Finished {self.done} file(s)"

    def _get_model(self):
        st = self.app.settings
        name = st.whisper_model if st.file_model == "dictation" else st.file_model
        threads = cpu_threads(st.cpu_mode)
        if self._model is None or self._model[:2] != (name, threads):
            if not (CACHE_DIR / "whisper" / name / "model.bin").exists():
                self._progress(f"Downloading the {name} model (one time)...")
            else:
                self._progress(f"Loading the {name} model...")
            self._model = (name, threads, load_whisper(name, threads))
        return self._model[0], self._model[2]

    def _progress(self, text, show_bubble=True):
        self.status = text
        if show_bubble and self.app.state in ("idle", "paused"):  # never cover dictation/reading
            self.app.bubble.show(text, STATE_COLORS["transcribing"])

    def _transcribe_file(self, path: Path):
        if not path.is_file():
            raise FileNotFoundError(path)
        app, st = self.app, self.app.settings
        name, model = self._get_model()
        count = f" ({self.done + 1} of {self.total})" if self.total > 1 else ""
        short = path.name if len(path.name) <= 28 else path.name[:25] + "..."
        self._progress(f"Transcribing {short}...{count}")
        tic = time.perf_counter()
        audio = decode_audio_file(path)  # WAV, M4A, MP3, OGG, FLAC, MP4, ...
        segments, info = model.transcribe(
            audio,
            language="en" if name.endswith(".en") else None,
            beam_size=beam_size(st.cpu_mode),
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
            condition_on_previous_text=False,
            initial_prompt=app.vocab.prompt(),
        )
        duration = float(getattr(info, "duration", 0) or 0) or audio.size / SAMPLE_RATE
        paragraphs, current, start, last_end = [], [], 0.0, None
        stopped = False

        def flush():
            if current:
                paragraphs.append((start, app.vocab.fix(" ".join(current))))
                current.clear()

        for seg in segments:
            if self._cancel.is_set():
                stopped = True
                break
            text = seg.text.strip()
            if not text:
                continue
            gap = seg.start - last_end if last_end is not None else 0
            if gap >= PARAGRAPH_GAP_SECONDS or (
                    sum(map(len, current)) > PARAGRAPH_MAX_CHARS and current[-1][-1:] in ".!?"):
                flush()
            if not current:
                start = seg.start
            current.append(text)
            last_end = seg.end
            if duration:
                pct = min(99, int(seg.end * 100 / duration))
                self._progress(f"Transcribing {short} - {pct}%{count}")
        flush()

        def stamp(sec):
            sec = int(sec)
            return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 \
                else f"{sec // 60:02d}:{sec % 60:02d}"

        body = "\n\n".join(f"[{stamp(t)}] {txt}" if st.file_timestamps else txt
                           for t, txt in paragraphs)
        header = (f"Transcript of {path.name} ({stamp(duration)})\n"
                  f"Made {time.strftime('%d %b %Y %H:%M')} by {APP_NAME} v{APP_VERSION}, "
                  f"model {name}\n\n")
        footer = "\n\n[Stopped early - the rest of the recording was not transcribed]" if stopped else ""
        out = self._write(path, header + (body or "(No speech was recognised.)") + footer + "\n")
        took = time.perf_counter() - tic
        words = len(body.split())
        log.info("STATS file | %s | %s of audio | transcribed in %.1fs (%.2fx real-time, %s) | "
                 "%d words | saved %s%s", path.name, stamp(duration), took,
                 took / duration if duration else 0, name, words, out, " | STOPPED" if stopped else "")
        if stopped:
            self.status = f"Stopped - partial transcript saved as {out.name}"
            app.flash("Stopped - partial transcript saved", "warn")
        else:
            self.status = f"Saved {out.name}"
            app.flash(f"Transcript saved: {out.name[:28]}")
        if st.file_open_when_done:
            try:
                if IS_WINDOWS:
                    os.startfile(str(out))
            except Exception:
                log.debug("Could not open %s", out, exc_info=True)

    @staticmethod
    def _write(recording: Path, text: str) -> Path:
        """Save next to the recording (never overwriting), or in Documents if
        that folder is read-only (e.g. a phone or network share)."""
        folders = [recording.parent, Path.home() / "Documents" / "Dictation Transcripts"]
        for folder in folders:
            try:
                folder.mkdir(parents=True, exist_ok=True)
                out = folder / (recording.stem + ".txt")
                n = 2
                while out.exists():
                    out = folder / f"{recording.stem} ({n}).txt"
                    n += 1
                out.write_text(text, encoding="utf-8")
                return out
            except OSError:
                continue
        raise OSError("Could not save the transcript anywhere")


class OptionsWindow:
    """The Options window: everything you set once and rarely touch again,
    kept out of the right-click menu. Runs on the Tk thread (see StatusBubble.call).
    Every change applies immediately - there is no OK/Apply button to forget."""

    FONT = ("Segoe UI", 11)

    def __init__(self, root, app):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk, self.app = tk, ttk, app
        self.win = win = tk.Toplevel(root)
        win.title(f"{APP_NAME} - Options")
        win.resizable(False, False)
        win.protocol("WM_DELETE_WINDOW", self.close)
        style = ttk.Style(win)
        for widget in (".", "TButton", "TCheckbutton", "TRadiobutton", "TLabel", "TLabelframe.Label"):
            style.configure(widget, font=self.FONT)

        tabs = ttk.Notebook(win)
        tabs.pack(fill="both", expand=True, padx=10, pady=(10, 0))
        self._general_tab(tabs)
        self._keys_tab(tabs)
        self._recordings_tab(tabs)
        self._tools_tab(tabs)
        self._about_tab(tabs)
        ttk.Button(win, text="Close", command=self.close).pack(anchor="e", padx=10, pady=10)
        self.tabs = tabs

        if IS_WINDOWS:  # give it its own taskbar button so it can't get lost behind windows
            try:
                win.update_idletasks()
                hwnd = ctypes.windll.user32.GetParent(win.winfo_id()) or win.winfo_id()
                ex = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
                ctypes.windll.user32.SetWindowLongW(hwnd, -20, ex | 0x00040000)  # WS_EX_APPWINDOW
            except Exception:
                pass
        self._sync()
        self.show()

    # ---- tabs -----------------------------------------------------------------
    def _general_tab(self, tabs):
        tk, ttk, app = self.tk, self.ttk, self.app
        f = ttk.Frame(tabs, padding=14)
        tabs.add(f, text="General")
        self.v_bubble = tk.BooleanVar()
        ttk.Checkbutton(f, text="Show the status bubble (Listening..., Reading aloud...)",
                        variable=self.v_bubble,
                        command=lambda: app._set_setting("show_bubble", self.v_bubble.get())
                        ).pack(anchor="w", pady=3)

        box = ttk.LabelFrame(f, text="Insert dictated text by", padding=8)
        box.pack(fill="x", pady=8)
        self.v_output = tk.StringVar()
        for label, value in (("Typing (works in most apps)", "type"),
                             ("Pasting (faster for long text)", "paste")):
            ttk.Radiobutton(box, text=label, value=value, variable=self.v_output,
                            command=lambda: app._set_setting("output_mode", self.v_output.get())
                            ).pack(anchor="w")

        box = ttk.LabelFrame(f, text="CPU usage (dictation, read-aloud and recordings)", padding=8)
        box.pack(fill="x", pady=8)
        self.v_cpu = tk.StringVar()
        for value, label in CPU_MODES.items():
            ttk.Radiobutton(box, text=label, value=value, variable=self.v_cpu,
                            command=lambda: app._set_setting("cpu_mode", self.v_cpu.get())
                            ).pack(anchor="w")
        ttk.Label(box, wraplength=440, justify="left", foreground="#555555", text=(
            "Lower settings stop the processor spiking and keep other programs smooth, "
            "but transcription takes longer. The STATS lines in the log show the difference."
        )).pack(anchor="w", pady=(4, 0))

        box = ttk.LabelFrame(f, text="Dictation model", padding=8)
        box.pack(fill="x", pady=8)
        self.v_model = tk.StringVar()
        for label, value in WHISPER_MODELS.items():
            hint = "  - quicker" if value == "base.en" else "  - more accurate, better in noise"
            ttk.Radiobutton(box, text=label + hint, value=value, variable=self.v_model,
                            command=lambda: app._set_setting("whisper_model", self.v_model.get())
                            ).pack(anchor="w")

    def _keys_tab(self, tabs):
        tk, ttk, app = self.tk, self.ttk, self.app
        f = ttk.Frame(tabs, padding=14)
        tabs.add(f, text="Keys")
        self.key_boxes = {}
        for row, role in enumerate(("dictate", "read", "stop")):
            names = [n for n in HOTKEY_CHOICES if key_allowed(role, n)]
            ttk.Label(f, text=ROLE_NAMES[role]).grid(row=row, column=0, sticky="w", pady=6)
            box = ttk.Combobox(f, state="readonly", width=14, font=self.FONT,
                               values=[HOTKEY_CHOICES[n][1] for n in names])
            box.grid(row=row, column=1, padx=10)
            box.bind("<<ComboboxSelected>>", lambda e, r=role, b=box, ns=names:
                     app._assign_key(r, ns[b.current()]))
            ttk.Button(f, text="Press a key...",
                       command=lambda r=role: app._start_key_capture(r)).grid(row=row, column=2)
            self.key_boxes[role] = box
        ttk.Button(f, text="Reset to " + " / ".join(HOTKEY_CHOICES[DEFAULT_KEYS[r]][1] for r in
                                                    ("dictate", "read", "stop")),
                   command=app._reset_keys).grid(row=3, column=0, columnspan=3, sticky="w", pady=(14, 4))
        ttk.Label(f, wraplength=430, justify="left", text=(
            "Allowed: F1-F24, Pause, Scroll Lock, Insert, Print Screen, Right Ctrl, Menu key. "
            "Esc can only be the stop key. Picking a key that's already used swaps the two. "
            "On laptops where F keys change brightness/volume, Right Ctrl or Pause can be easier."
        )).grid(row=4, column=0, columnspan=3, sticky="w")

    def _recordings_tab(self, tabs):
        tk, ttk, app = self.tk, self.ttk, self.app
        f = ttk.Frame(tabs, padding=14)
        tabs.add(f, text="Recordings")
        ttk.Label(f, wraplength=440, justify="left", text=(
            "Turn recordings into text - for example voice memos from your phone. "
            "Works with WAV, M4A, MP3, OGG, FLAC, AAC and video files such as MP4. "
            "The text is saved as a .txt file next to each recording."
        )).pack(anchor="w")
        ttk.Button(f, text="Choose audio files to transcribe...",
                   command=self._choose_files).pack(anchor="w", pady=10)

        box = ttk.LabelFrame(f, text="Model for recordings", padding=8)
        box.pack(fill="x")
        self.v_file_model = tk.StringVar()
        for label, value in (("Same as dictation", "dictation"),
                             ("Fast (base.en)", "base.en"),
                             ("Accurate (small.en) - best for long or noisy recordings", "small.en")):
            ttk.Radiobutton(box, text=label, value=value, variable=self.v_file_model,
                            command=lambda: app._set_setting("file_model", self.v_file_model.get())
                            ).pack(anchor="w")
        ttk.Label(box, foreground="#555555", text="Accurate is about 3x slower and downloads "
                  "once (~480 MB, needs internet the first time).").pack(anchor="w", pady=(4, 0))
        self.v_stamps = tk.BooleanVar()
        ttk.Checkbutton(f, text="Add timestamps, e.g. [02:15], at the start of each paragraph",
                        variable=self.v_stamps,
                        command=lambda: app._set_setting("file_timestamps", self.v_stamps.get())
                        ).pack(anchor="w", pady=(10, 2))
        self.v_open = tk.BooleanVar()
        ttk.Checkbutton(f, text="Open the transcript when it's finished", variable=self.v_open,
                        command=lambda: app._set_setting("file_open_when_done", self.v_open.get())
                        ).pack(anchor="w")

        box = ttk.LabelFrame(f, text="Progress", padding=8)
        box.pack(fill="x", pady=(10, 0))
        self.v_file_status = tk.StringVar()
        ttk.Label(box, textvariable=self.v_file_status, wraplength=420, justify="left"
                  ).pack(side="left", anchor="w")
        self.cancel_btn = ttk.Button(box, text="Stop", command=app.files.cancel)
        self.cancel_btn.pack(side="right")

    def _choose_files(self):
        from tkinter import filedialog
        paths = filedialog.askopenfilenames(parent=self.win, title="Choose recordings to transcribe",
                                            filetypes=AUDIO_FILE_TYPES)
        if paths:
            self.app.files.add(paths)

    def _tools_tab(self, tabs):
        tk, ttk, app = self.tk, self.ttk, self.app
        f = ttk.Frame(tabs, padding=14)
        tabs.add(f, text="Tools")

        box = ttk.LabelFrame(f, text="Statistics (this session)", padding=8)
        box.pack(fill="x")
        self.v_stats = tk.StringVar()
        ttk.Label(box, textvariable=self.v_stats, justify="left").pack(anchor="w")

        box = ttk.LabelFrame(f, text="Updates", padding=8)
        box.pack(fill="x", pady=8)
        ttk.Button(box, text="Check for model updates now", command=lambda: threading.Thread(
            target=app._check_updates, args=(True,), name="update-check", daemon=True).start()
                   ).pack(anchor="w")
        self.v_autoupdate = tk.BooleanVar()
        ttk.Checkbutton(box, text="Check automatically at startup (otherwise fully offline)",
                        variable=self.v_autoupdate,
                        command=lambda: app._set_setting("check_updates_on_start",
                                                         self.v_autoupdate.get())
                        ).pack(anchor="w", pady=(6, 0))

        box = ttk.LabelFrame(f, text="Files and taskbar", padding=8)
        box.pack(fill="x")
        row = ttk.Frame(box)
        row.pack(anchor="w")
        ttk.Button(row, text="Open app folder", command=app._open_app_folder).pack(side="left")
        ttk.Button(row, text="Edit custom words", command=app._edit_vocab).pack(side="left", padx=8)
        ttk.Button(row, text="Pin icon to taskbar", command=lambda: threading.Thread(
            target=app._pin_icon, args=(True,), name="tray-pin", daemon=True).start()
                   ).pack(side="left")

    def _about_tab(self, tabs):
        tk, ttk = self.tk, self.ttk
        f = ttk.Frame(tabs, padding=14)
        tabs.add(f, text="About")
        self.about = tk.Text(f, width=62, height=12, wrap="word", font=self.FONT,
                             relief="flat", background=f.winfo_toplevel().cget("background"))
        self.about.pack(fill="both")
        ttk.Button(f, text="Copy to clipboard (for sending to support)",
                   command=self._copy_about).pack(anchor="w", pady=(8, 0))

    # ---- behaviour ----------------------------------------------------------------
    def _copy_about(self):
        text = version_report(self.app)
        threading.Thread(target=lambda: (clip_set(text), self.app.flash("Copied version info")),
                         daemon=True).start()

    def _sync(self):
        """Keep the widgets in step with the settings (they can change elsewhere,
        e.g. a key chosen by pressing it, or the tray menu)."""
        if not self.win.winfo_exists():
            return
        st = self.app.settings
        self.v_bubble.set(bool(st.show_bubble))
        self.v_output.set(st.output_mode)
        self.v_model.set(st.whisper_model)
        self.v_autoupdate.set(bool(st.check_updates_on_start))
        self.v_cpu.set(st.cpu_mode)
        self.v_file_model.set(st.file_model)
        self.v_stamps.set(bool(st.file_timestamps))
        self.v_open.set(bool(st.file_open_when_done))
        self.v_file_status.set(self.app.files.status)
        self.cancel_btn.configure(state="normal" if self.app.files.busy else "disabled")
        for role, box in self.key_boxes.items():
            box.set(HOTKEY_CHOICES[KEYS[role]][1])
        self.v_stats.set(self.app.stats.summary().replace("STATS session | ", "")
                         .replace(" | ", "\n"))
        about = version_report(self.app)
        if self.about.get("1.0", "end-1c") != about:
            self.about.configure(state="normal")
            self.about.delete("1.0", "end")
            self.about.insert("1.0", about)
            self.about.configure(state="disabled")
        self.win.after(700, self._sync)

    def show(self):
        self.win.deiconify()
        self.win.lift()
        self.win.attributes("-topmost", True)   # pop in front of other windows...
        self.win.after(300, lambda: self.win.attributes("-topmost", False))  # ...then behave normally
        self.win.focus_force()

    def close(self):
        self.app._options = None
        self.win.destroy()


class Stats:
    """Tic/toc-style timings, printed to the console (and dictation_app.log)
    after every dictation and read-aloud, plus a session summary on Quit."""

    def __init__(self):
        self._lock = threading.Lock()
        self.started = time.time()
        self.dictations = self.dictated_words = 0
        self.dictated_audio = 0.0
        self.reads = self.read_words = 0
        self.read_audio = 0.0

    @staticmethod
    def _words(text: str) -> int:
        return len(text.split())

    def dictation(self, spoke_s, wait_s, transcribe_s, insert_s, total_s, text, model):
        words = self._words(text)
        with self._lock:
            self.dictations += 1
            self.dictated_words += words
            self.dictated_audio += spoke_s
            n = self.dictations
        rtf = transcribe_s / spoke_s if spoke_s else 0
        queued = f" | waited {wait_s:.1f}s in queue" if wait_s >= 0.05 else ""
        log.info("STATS dictation #%d | spoke %.1fs%s | transcribe %.2fs (%.2fx real-time, %s) | "
                 "inserted %d words / %d chars in %.2fs | total %.2fs from key release",
                 n, spoke_s, queued, transcribe_s, rtf, model, words, len(text), insert_s, total_s)

    def read(self, text, chunks, copy_s, first_sound_s, synth_s, speech_s, played_s, stopped):
        words = self._words(text)
        with self._lock:
            self.reads += 1
            self.read_words += words
            self.read_audio += played_s
            n = self.reads
        rtf = synth_s / speech_s if speech_s else 0
        first = f"{first_sound_s:.2f}s" if first_sound_s is not None else "-"
        log.info("STATS read-aloud #%d | %d words / %d chars in %d chunk(s) | copy %.2fs | "
                 "first sound %s after %s | synth %.2fs for %.1fs of speech (%.2fx real-time) | "
                 "played %.1fs (%s)", n, words, len(text), chunks, copy_s, first, key_label("read"),
                 synth_s, speech_s, rtf, played_s, "stopped early" if stopped else "finished")

    def summary(self) -> str:
        up = int(time.time() - self.started)
        with self._lock:
            return (f"STATS session | running {up // 3600}h {up % 3600 // 60:02d}m | "
                    f"{self.dictations} dictations, {self.dictated_words} words, "
                    f"{self.dictated_audio:.0f}s spoken | {self.reads} reads, "
                    f"{self.read_words} words, {self.read_audio:.0f}s listened")


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
    "idle": "Ready - hold {dictate} to dictate, {read} to read",
    "recording": "Listening...",
    "transcribing": "Transcribing...",
    "preparing": "Getting ready to read...",
    "speaking": "Reading aloud ({read}/{stop} to stop)",
    "error": "Model loading failed - see dictation_app.log",
    "paused": "Paused - click the icon to turn back on",
}
BUBBLE_TEXT = {  # on-screen status bubble; text ending in "..." gets animated dots
    "loading": "Loading speech models...",
    "recording": "Listening - let go of {dictate} when done",
    "transcribing": "Writing down what you said...",
    "preparing": "Getting ready to read...",
    "speaking": "Reading aloud - {stop} to stop",
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
        self.settings.apply_keys()
        self.settings.validate()
        self._capture_role = None  # set while "Press a key to choose" is waiting
        self.stt = None
        self.tts = None
        self.state = "loading"
        self.events = queue.Queue()
        self.transcribe_q = queue.Queue()
        self.recorder = Recorder()
        self.speaker = Speaker(self)
        self.vocab = Vocabulary(VOCAB_FILE)
        self.stats = Stats()
        self.bubble = StatusBubble()
        self.bubble.enabled = bool(self.settings.show_bubble)
        self._options = None  # the Options window, while open
        self.files = FileTranscriber(self)
        self._detail = None
        self.paused = False
        self._load_failed = False
        self.hotkeys = self._new_hotkey_listener()
        self._record_started = 0.0
        self._watchdog = None
        self._pending_transcriptions = 0
        self._pending_lock = threading.Lock()
        self._running = True
        self.icon = pystray.Icon("dictation_helper", make_icon(STATE_COLORS["loading"]),
                                 f"{APP_NAME} v{APP_VERSION}\n{STATE_LABELS['loading']}",
                                 self._build_menu())

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
            text = with_keys(text)
            if detail:
                text = f"{text}  ({detail})"
            self.bubble.show(text, STATE_COLORS[state])
        elif state == "idle":
            self.bubble.hide()
        if changed:
            try:
                self.icon.icon = make_icon(STATE_COLORS.get(state, STATE_COLORS["idle"]),
                                           paused=(state == "paused"))
                self.icon.title = f"{APP_NAME} v{APP_VERSION}\n{with_keys(STATE_LABELS.get(state, state))}"[:127]
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

        # Everyday items only; set-and-forget settings live in the Options window.
        return Menu(
            # default=True -> a left-click on the tray icon toggles pause
            Item(lambda item: "Turn back on" if self.paused else "Pause (F9/F10 work as normal keys)",
                 lambda icon, item: self.toggle_pause(), default=True),
            Item(lambda item: with_keys(STATE_LABELS.get(self.state, self.state)), None, enabled=False),
            Menu.SEPARATOR,
            Item("Microphone", Menu(mic_items)),
            Item("Reading voice", Menu(lambda: choice_items("voice", VOICES.items()))),
            Item("Reading speed", Menu(lambda: choice_items(
                "speed", [(f"{s:g}x", s) for s in SPEEDS]))),
            Item("Stop reading", lambda icon, item: self.speaker.stop(),
                 enabled=lambda item: self.speaker.busy),
            Menu.SEPARATOR,
            Item("Edit custom words...", lambda icon, item: self._edit_vocab()),
            Item("Options...", lambda icon, item: self._open_options()),
            # Fallback when tkinter is missing (e.g. the embeddable Python): no Options
            # window is possible, so the same settings appear here instead.
            Item("More", Menu(lambda: self._fallback_items(choice_items)),
                 visible=lambda item: not self.bubble.ok),
            Menu.SEPARATOR,
            Item("Quit", lambda icon, item: self.quit()),
        )

    def _fallback_items(self, choice_items):
        Item, Menu = pystray.MenuItem, pystray.Menu
        return [
            Item("Dictation model", Menu(lambda: choice_items("whisper_model", WHISPER_MODELS.items()))),
            Item("Keys", Menu(lambda: self._key_menu_items())),
            Item("Insert dictated text by", Menu(lambda: choice_items("output_mode", [
                ("Typing (works in most apps)", "type"),
                ("Pasting (faster for long text)", "paste")]))),
            Item("Show statistics", lambda icon, item: self._show_stats()),
            Item("Check for model updates now", lambda icon, item: threading.Thread(
                target=self._check_updates, args=(True,), name="update-check", daemon=True).start()),
            Item("Check for updates at startup", lambda icon, item: self._set_setting(
                "check_updates_on_start", not self.settings.check_updates_on_start),
                 checked=lambda item: bool(self.settings.check_updates_on_start)),
            Item("Open app folder", lambda icon, item: self._open_app_folder()),
            Item("Pin icon to taskbar", lambda icon, item: threading.Thread(
                target=self._pin_icon, args=(True,), name="tray-pin", daemon=True).start()),
            Item(f"About (version {APP_VERSION})", lambda icon, item: threading.Thread(
                target=self._show_about, name="about", daemon=True).start()),
        ]

    def _open_options(self):
        if not self.bubble.ok:
            self.notify("The Options window needs tkinter (included with the python.org "
                        "installer). Use the 'More' menu instead.")
            return

        def open_on_tk_thread(root):
            if self._options is not None and self._options.win.winfo_exists():
                self._options.show()  # already open: bring it to the front
            else:
                self._options = OptionsWindow(root, self)
        self.bubble.call(open_on_tk_thread)

    def _set_setting(self, key, value):
        old = getattr(self.settings, key)
        setattr(self.settings, key, value)
        self.settings.save()
        log.info("Setting %s = %r", key, value)
        if key == "cpu_mode" and value != old:
            apply_cpu_priority(value)
            threading.Thread(target=self._reload_for_cpu_mode, name="model-loader", daemon=True).start()
        if key == "whisper_model" and value != old:
            threading.Thread(target=self._reload_whisper, name="model-loader", daemon=True).start()
        if key == "show_bubble":
            self.bubble.enabled = bool(value)
            if not value:
                self.bubble.hide()
        self.icon.update_menu()

    def _new_hotkey_listener(self):
        return HotkeyListener(self.events, self.is_busy, lambda: self.paused,
                              lambda: self._capture_role is not None)

    # ---- changing the hotkeys ------------------------------------------------
    def _key_menu_items(self):
        Item, Menu = pystray.MenuItem, pystray.Menu
        common = [f"f{n}" for n in range(1, 13)] + ["pause", "scroll_lock", "insert",
                                                    "print_screen", "ctrl_r", "menu"]

        def pick(role, name):
            return lambda icon, item: self._assign_key(role, name)

        def is_on(role, name):
            return lambda item: KEYS[role] == name

        def role_menu(role):
            names = (["esc"] if role == "stop" else []) + common
            if KEYS[role] not in names:  # e.g. F13 chosen by pressing it
                names.append(KEYS[role])
            return Menu(
                Item("Press a key to choose...", lambda icon, item: self._start_key_capture(role)),
                Menu.SEPARATOR,
                *[Item(HOTKEY_CHOICES[n][1], pick(role, n), checked=is_on(role, n), radio=True)
                  for n in names])

        def label(role):
            return lambda item: f"{ROLE_NAMES[role]}: {key_label(role)}"

        return [Item(label(role), role_menu(role)) for role in ("dictate", "read", "stop")] + [
            Menu.SEPARATOR,
            Item(f"Reset to {HOTKEY_CHOICES[DEFAULT_KEYS['dictate']][1]} / "
                 f"{HOTKEY_CHOICES[DEFAULT_KEYS['read']][1]} / "
                 f"{HOTKEY_CHOICES[DEFAULT_KEYS['stop']][1]}",
                 lambda icon, item: self._reset_keys())]

    def _assign_key(self, role: str, name: str):
        if not key_allowed(role, name):
            self.flash(f"{HOTKEY_CHOICES.get(name, (0, name))[1]} can't be the "
                       f"{ROLE_NAMES[role].lower()} key", "warn")
            return
        old = KEYS[role]
        if name == old:
            self.flash(f"{ROLE_NAMES[role]} key is already {HOTKEY_CHOICES[name][1]}")
            return
        message = f"{ROLE_NAMES[role]} key is now {HOTKEY_CHOICES[name][1]}"
        for other in ("dictate", "read", "stop"):
            if other != role and KEYS[other] == name:
                if not key_allowed(other, old):
                    self.flash(f"{HOTKEY_CHOICES[name][1]} is already the "
                               f"{ROLE_NAMES[other].lower()} key", "warn")
                    return
                KEYS[other] = old  # swap, so no two functions share a key
                message = (f"Swapped: {ROLE_NAMES[role]} = {HOTKEY_CHOICES[name][1]}, "
                           f"{ROLE_NAMES[other]} = {HOTKEY_CHOICES[old][1]}")
        KEYS[role] = name
        self.settings.dictate_key, self.settings.read_key, self.settings.stop_key = (
            KEYS["dictate"], KEYS["read"], KEYS["stop"])
        self.settings.save()
        self.hotkeys._down.clear()
        log.info("Hotkeys changed: dictate=%s read=%s stop=%s", *(key_label(r) for r in
                                                                  ("dictate", "read", "stop")))
        self._refresh_key_labels()
        self.flash(message)

    def _reset_keys(self):
        KEYS.update(DEFAULT_KEYS)
        self.hotkeys._down.clear()
        self.settings.dictate_key, self.settings.read_key, self.settings.stop_key = (
            KEYS["dictate"], KEYS["read"], KEYS["stop"])
        self.settings.save()
        self._refresh_key_labels()
        self.flash(with_keys("Keys reset: {dictate} dictate, {read} read, {stop} stop"))

    def _refresh_key_labels(self):
        try:
            self.icon.title = f"{APP_NAME} v{APP_VERSION}\n{with_keys(STATE_LABELS.get(self.state, self.state))}"[:127]
            self.icon.update_menu()
        except Exception:
            pass

    def _start_key_capture(self, role: str):
        if self.is_busy():
            self.flash("Finish dictating or reading first", "warn")
            return
        self._capture_role = role
        cancel = "Esc to cancel" if role != "stop" else "wait 10 s to cancel"
        self.bubble.show(f"Press the new {ROLE_NAMES[role].lower()} key ({cancel})",
                         STATE_COLORS["transcribing"])
        timer = threading.Timer(10, self.events.put, (("capture_timeout", role),))
        timer.daemon = True
        timer.start()

    def _on_key_captured(self, vk: int):
        role = self._capture_role
        if role is None:
            return
        name = next((n for n, (code, _) in HOTKEY_CHOICES.items() if code == vk), None)
        if name == "esc" and role != "stop":
            self._capture_role = None
            self._restore_bubble()
            self.flash("No change", "warn")
            return
        if name is None or not key_allowed(role, name):
            # keep waiting - letters, Enter, Shift etc. are needed for typing
            self.bubble.show("That key can't be used - try an F key, Pause or Insert",
                             STATE_COLORS["transcribing"])
            return
        self._capture_role = None
        self._restore_bubble()
        self._assign_key(role, name)

    def _on_capture_timeout(self, role: str):
        if self._capture_role == role:
            self._capture_role = None
            self._restore_bubble()
            self.flash("No key pressed - nothing changed", "warn")

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
            self.flash(with_keys("On - hold {dictate} to dictate, {read} to read"))
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

    def _show_about(self):
        report = version_report(self)
        log.info("About:\n%s", report)
        copied = clip_set(report)
        message_box(report + ("\n\n(Copied to the clipboard - paste it into an email or chat.)"
                              if copied else ""))

    def _check_updates(self, interactive: bool):
        """interactive=True: from the menu (shows results, offers to install).
        False: the optional startup check (only speaks up if something is new)."""
        if interactive:
            self.bubble.show("Checking for model updates...", STATE_COLORS["transcribing"])
        log.info("Checking for model updates (%s)", "requested" if interactive else "at startup")
        result = check_model_updates(self.settings.whisper_model)
        for line in result["lines"]:
            log.info("UPDATE CHECK | %s", line)
        if interactive:
            self._restore_bubble()
        if not interactive:
            if result["anything_new"]:
                self.notify("Model updates are available - see Updates in the tray menu.")
            return
        summary = "\n".join(result["lines"])
        if result["whisper_update"]:
            if message_box(summary + "\n\nDownload the dictation model update now? "
                           "(needs internet; the current model keeps working until it finishes)",
                           yes_no=True):
                self._install_whisper_update()
        else:
            message_box(summary + ("" if result["anything_new"] else "\n\nEverything is up to date."))

    def _restore_bubble(self):
        """Put the bubble back to whatever the app is doing right now."""
        text = BUBBLE_TEXT.get(self.state)
        if text:
            self.bubble.show(with_keys(text), STATE_COLORS[self.state])
        else:
            self.bubble.hide()

    def _install_whisper_update(self):
        name = self.settings.whisper_model
        try:
            self.bubble.show("Downloading model update...", STATE_COLORS["loading"])
            update_whisper_model(name)
            self.stt = load_whisper(name, cpu_threads(self.settings.cpu_mode))
            log.info("Dictation model %s updated to %s", name, whisper_local_revision(name))
            self._restore_bubble()
            self.flash("Model updated")
        except Exception as exc:
            log.exception("Model update failed")
            self._restore_bubble()
            self.flash("Model update failed - see the log", "error")
            self.notify(f"Model update failed: {exc}. The previous model is still in use.")

    def _open_app_folder(self):
        """Open this copy's installation folder (where dictation_app.py lives) in Explorer."""
        log.info("Opening app folder: %s", APP_DIR)
        try:
            if IS_WINDOWS:
                os.startfile(str(APP_DIR))
            else:
                subprocess.Popen(["xdg-open", str(APP_DIR)])
        except Exception as exc:
            log.exception("Could not open the app folder")
            self.notify(f"Could not open {APP_DIR}: {exc}")

    def _show_stats(self):
        summary = self.stats.summary()
        log.info(summary)
        self.notify(summary.replace("STATS session | ", "").replace(" | ", "\n"))

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
            tic = time.perf_counter()
            self.stt = load_whisper(self.settings.whisper_model, cpu_threads(self.settings.cpu_mode))
            stt_s = time.perf_counter() - tic
            tic = time.perf_counter()
            self.tts = load_kokoro(self._tts_threads())
            if self.settings.voice not in self.tts.get_voices():
                self.settings.voice = DEFAULT_VOICE
            self.tts.create("Ready.", voice=self.settings.voice, speed=1.0, lang="en-us")  # warm-up
            log.info("STATS startup | dictation model %s loaded in %.1fs | read-aloud voice loaded "
                     "in %.1fs", self.settings.whisper_model, stt_s, time.perf_counter() - tic)
            self.set_state("idle")
            chime("success")
            self.flash(with_keys("Ready - hold {dictate} to dictate, {read} to read"))
            if self.settings.check_updates_on_start:
                threading.Thread(target=self._check_updates, args=(False,), name="update-check",
                                 daemon=True).start()
            self.notify(with_keys("Ready! Hold {dictate} to dictate, "
                                  "press {read} to read highlighted text."))
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

    def _tts_threads(self) -> int:
        mode = self.settings.cpu_mode
        return 0 if mode == "full" else cpu_threads(mode)

    def _reload_for_cpu_mode(self):
        """Thread counts are fixed when a model loads, so reload both. The old
        models keep working until the new ones are ready."""
        if self.stt is None or self.tts is None:
            return  # still starting up - the new setting is used anyway
        mode = self.settings.cpu_mode
        try:
            tic = time.perf_counter()
            stt = load_whisper(self.settings.whisper_model, cpu_threads(mode))
            tts = load_kokoro(self._tts_threads())
            self.stt, self.tts = stt, tts
            log.info("CPU usage set to %s (%d threads, beam %d) - models reloaded in %.1fs",
                     mode, cpu_threads(mode), beam_size(mode), time.perf_counter() - tic)
            self.flash(f"CPU usage: {CPU_MODES[mode].split(' - ')[0].replace(' (default)', '')}")
        except Exception:
            log.exception("Could not apply the CPU setting")
            self.flash("Couldn't apply the CPU setting - see the log", "error")

    def _reload_whisper(self):
        try:
            self.set_state("loading")
            model = load_whisper(self.settings.whisper_model, cpu_threads(self.settings.cpu_mode))
            self.stt = model
            log.info("Switched dictation model to %s", self.settings.whisper_model)
        except Exception as exc:
            log.exception("Model switch failed")
            self.notify(f"Could not load {self.settings.whisper_model}: {exc}")
        finally:
            self.set_state("idle")

    def _setup(self, icon):
        icon.visible = True
        apply_cpu_priority(self.settings.cpu_mode)
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
        log.info("%s v%s starting (Python %s)", APP_NAME, APP_VERSION, sys.version.split()[0])
        self.icon.run(setup=self._setup)  # blocks: tray message loop on main thread
        log.info("Exited")
        logging.shutdown()
        os._exit(0)  # don't wait for the Tk/audio threads to wind down on their own

    def quit(self):
        self._running = False
        log.info(self.stats.summary())
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
                    self.hotkeys = self._new_hotkey_listener()
                    self.hotkeys.start()
                except Exception:
                    log.exception("Could not restart hotkey listener")

    def _controller_loop(self):
        """Single thread that owns recording state; events arrive from the hook."""
        while self._running:
            event = self.events.get()
            if event is None:
                break
            if isinstance(event, tuple):  # ("capture", vk) / ("capture_timeout", role)
                try:
                    if event[0] == "capture":
                        self._on_key_captured(event[1])
                    else:
                        self._on_capture_timeout(event[1])
                except Exception:
                    log.exception("Error handling %s", event)
                continue
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
        self.transcribe_q.put((audio, time.perf_counter()))  # tic: key released

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
        pressed_at = time.perf_counter()  # tic: F10 pressed
        chime("read")                # instant "I heard you"...
        self.set_state("preparing")  # ...and the bubble appears before any slow work
        threading.Thread(target=self._read_selection, args=(pressed_at,), name="reader",
                         daemon=True).start()

    def _read_selection(self, pressed_at: float):
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
        self.speaker.speak(text, pressed_at, time.perf_counter() - pressed_at)

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
            job = self.transcribe_q.get()
            if job is None:
                break
            audio, released_at = job
            try:
                started = time.perf_counter()
                text = self._transcribe(audio)
                transcribed = time.perf_counter()
                if text:
                    output_text(text + " ", self.settings.output_mode)
                    chime("success")
                    self.flash("Done")
                    done = time.perf_counter()
                    self.stats.dictation(audio.size / SAMPLE_RATE, started - released_at,
                                         transcribed - started, done - transcribed,
                                         done - released_at, text, self.settings.whisper_model)
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
        model_name = self.settings.whisper_model
        segments, _info = self.stt.transcribe(
            audio,
            language="en" if model_name.endswith(".en") else None,
            beam_size=beam_size(self.settings.cpu_mode),
            vad_filter=True,                       # drops silence -> fewer hallucinations
            vad_parameters={"min_silence_duration_ms": 500},
            condition_on_previous_text=False,
            without_timestamps=True,
            initial_prompt=self.vocab.prompt(),    # custom words -> preferred spellings
        )
        text = " ".join(seg.text.strip() for seg in segments).strip()
        text = self.vocab.fix(re.sub(r"\s+", " ", text))
        log.info("Heard: %r", text)
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


def download_models_only() -> int:
    """`python dictation_app.py --download-models [--whisper=small.en]`: fetch the
    speech models into .cache and exit (used by make_portable_bundle.bat)."""
    names = [a.split("=", 1)[1] for a in sys.argv if a.startswith("--whisper=")]
    try:
        for name in dict.fromkeys([DEFAULT_WHISPER_MODEL] + names):
            log.info("Preparing dictation model %s ...", name)
            load_whisper(name)
        log.info("Preparing read-aloud voice ...")
        load_kokoro()
    except Exception:
        log.exception("Model download failed")
        return 1
    log.info("All models are in %s", CACHE_DIR)
    return 0


def main():
    if "--download-models" in sys.argv:
        sys.exit(download_models_only())
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
