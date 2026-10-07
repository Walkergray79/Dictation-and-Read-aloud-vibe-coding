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
OUTPUT_MODE = "paste"            # "paste" (fast, clipboard + Ctrl+V) or "type" (key by key)

# ---- Text-to-speech ----------------------------------------------------------
KOKORO_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
KOKORO_FILES = ["kokoro-v1.0.int8.onnx", "voices-v1.0.bin"]  # int8 = ~90 MB, CPU friendly
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
READ_CLIPBOARD_IF_NO_SELECTION = True  # F10 with nothing highlighted reads the clipboard

IS_WINDOWS = sys.platform == "win32"

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
    """Tiny JSON-backed settings store (mic, voice, speed, model)."""

    def __init__(self):
        self.mic = None  # None = system default; otherwise the device *name*
        self.voice = DEFAULT_VOICE
        self.speed = 1.0
        self.whisper_model = DEFAULT_WHISPER_MODEL
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            for key in ("mic", "voice", "speed", "whisper_model"):
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


def output_text(text: str) -> bool:
    """Insert text into the currently focused window."""
    if OUTPUT_MODE == "type":
        _kb.type(text)
        return True
    with _clipboard_lock:
        saved = clip_get()
        if not clip_set(text):
            log.warning("Clipboard busy; falling back to typing")
            _kb.type(text)
            return True
        time.sleep(0.05)
        press_combo(keyboard.Key.ctrl, "v")
        time.sleep(0.3)  # let the target app read the clipboard before restoring
        if saved is not None:
            clip_set(saved)
    return True


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

    root = str(CACHE_DIR / "whisper")
    kwargs = dict(device="cpu", compute_type="int8", download_root=root,
                  cpu_threads=max(1, min(8, (os.cpu_count() or 4))))
    try:  # offline first
        return WhisperModel(model_name, local_files_only=True, **kwargs)
    except Exception:
        log.info("Whisper model '%s' not cached yet - downloading (one time)...", model_name)
        return WhisperModel(model_name, local_files_only=False, **kwargs)


def load_kokoro():
    from kokoro_onnx import Kokoro

    folder = CACHE_DIR / "kokoro"
    folder.mkdir(parents=True, exist_ok=True)
    for name in KOKORO_FILES:
        path = folder / name
        if not path.exists():
            download_file(KOKORO_URL + name, path)
    return Kokoro(str(folder / KOKORO_FILES[0]), str(folder / KOKORO_FILES[1]))


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


def split_chunks(text: str, max_len: int = 300):
    """Split into sentence-sized chunks so playback starts fast and stops fast."""
    sentences = re.split(r"(?<=[.!?…])\s+", text)
    pieces = []
    for s in sentences:
        s = s.strip()
        while len(s) > max_len:  # very long sentence: cut at a comma or space
            cut = s.rfind(", ", 0, max_len)
            if cut < max_len // 3:
                cut = s.rfind(" ", 0, max_len)
            if cut <= 0:
                cut = max_len
            pieces.append(s[:cut + 1].strip())
            s = s[cut + 1:].strip()
        if s:
            pieces.append(s)
    chunks = []
    for p in pieces:  # glue tiny fragments ("Hi." "Yes.") onto the previous chunk
        if chunks and len(chunks[-1]) < 40 and len(chunks[-1]) + len(p) < max_len:
            chunks[-1] += " " + p
        else:
            chunks.append(p)
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
        self.app.set_state("speaking")
        stream = None
        try:
            chunks = split_chunks(prepare_text(text))
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

    def __init__(self, events: queue.Queue, is_busy):
        self.events = events
        self.is_busy = is_busy  # callable -> True while recording/speaking
        self._down = set()
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
        """Translate key edges into app events. Returns True to swallow the key."""
        if is_down:
            if name in self._down:      # auto-repeat while held - ignore
                return True
            self._down.add(name)
        else:
            self._down.discard(name)
        if name == "dictate":
            self.events.put("dictate_down" if is_down else "dictate_up")
            return True
        if name == "read":
            if is_down:
                self.events.put("read")
            return True
        if name == "stop":
            if is_down and self.is_busy():
                self.events.put("stop")
                return True
            return False  # Esc behaves normally when we're idle
        return False

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
                swallow = self._emit(name, False) or name != "stop"
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
# 9. APPLICATION / CONTROLLER
# =============================================================================
STATE_COLORS = {
    "loading": (128, 128, 128),
    "idle": (40, 110, 220),
    "recording": (220, 40, 40),
    "transcribing": (235, 160, 20),
    "speaking": (30, 160, 80),
    "error": (90, 90, 90),
}
STATE_LABELS = {
    "loading": "Loading speech models...",
    "idle": f"Ready - hold {DICTATE_KEY.upper()} to dictate, {READ_KEY.upper()} to read",
    "recording": "Listening...",
    "transcribing": "Transcribing...",
    "speaking": f"Reading aloud ({READ_KEY.upper()}/{STOP_KEY.capitalize()} to stop)",
    "error": "Model loading failed - see dictation_app.log",
}


def make_icon(color) -> Image.Image:
    """Draw a simple microphone glyph on a coloured circle."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((2, 2, 62, 62), fill=color + (255,))
    white = (255, 255, 255, 255)
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
        self._busy_state = None  # state to return to when speech/transcription ends
        self.events = queue.Queue()
        self.transcribe_q = queue.Queue()
        self.recorder = Recorder()
        self.speaker = Speaker(self)
        self.hotkeys = HotkeyListener(self.events, self.is_busy)
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

    def set_state(self, state: str):
        # When one activity ends, show whichever other activity is still running.
        if state == "idle":
            if self.recorder.active:
                state = "recording"
            elif self.speaker.busy and threading.current_thread() is not self.speaker._thread:
                state = "speaking"
            elif self._pending_transcriptions > 0:
                state = "transcribing"
        if state == self.state:
            return
        self.state = state
        try:
            self.icon.icon = make_icon(STATE_COLORS.get(state, STATE_COLORS["idle"]))
            self.icon.title = f"{APP_NAME}\n{STATE_LABELS.get(state, state)}"[:127]
            self.icon.update_menu()
        except Exception:
            pass

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
            Item(lambda item: STATE_LABELS.get(self.state, self.state), None, enabled=False),
            Menu.SEPARATOR,
            Item("Microphone", Menu(mic_items)),
            Item("Reading voice", Menu(lambda: choice_items("voice", VOICES.items()))),
            Item("Reading speed", Menu(lambda: choice_items(
                "speed", [(f"{s:g}x", s) for s in SPEEDS]))),
            Item("Dictation model", Menu(lambda: choice_items("whisper_model", WHISPER_MODELS.items()))),
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
        self.icon.update_menu()

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
            self.notify(f"Ready! Hold {DICTATE_KEY.upper()} to dictate, "
                        f"press {READ_KEY.upper()} to read highlighted text.")
        except Exception as exc:
            log.exception("Model loading failed")
            self.set_state("error")
            self.notify(f"Could not load speech models: {exc}\n"
                        "Connect to the internet for the first run, then restart.")

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
        for target, name in ((self._controller_loop, "controller"),
                             (self._transcribe_loop, "transcriber"),
                             (self._load_models, "model-loader"),
                             (self._hook_health_loop, "hook-health")):
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
                    self.hotkeys = HotkeyListener(self.events, self.is_busy)
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
        if self.tts is None:
            chime("error")
            self.notify("Still loading speech models - please wait a moment.")
            return
        if self.recorder.active:
            return
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
            return
        if len(text) > MAX_READ_CHARS:
            self.notify(f"Long selection - reading the first {MAX_READ_CHARS:,} characters.")
            text = text[:MAX_READ_CHARS]
        self.speaker.speak(text)

    def _on_stop(self):
        if self.speaker.busy:
            self.speaker.stop()
        elif self.recorder.active:  # Esc while dictating = cancel
            self._finish_recording()
            self.hotkeys._down.discard("dictate")
            chime("stop")
            log.info("Dictation cancelled")
            self.set_state("idle")

    def _transcribe_loop(self):
        """Runs Whisper off the hook/tray threads; one recording at a time, in order."""
        while self._running:
            audio = self.transcribe_q.get()
            if audio is None:
                break
            try:
                text = self._transcribe(audio)
                if text:
                    output_text(text + " ")
                    chime("success")
                else:
                    log.info("No speech recognised")
                    chime("error")
            except Exception:
                log.exception("Transcription/typing failed")
                chime("error")
            finally:
                with self._pending_lock:
                    self._pending_transcriptions -= 1
                self.set_state("idle")

    def _transcribe(self, audio: np.ndarray) -> str:
        if float(np.max(np.abs(audio))) < 0.002:
            log.info("Recording is silent - is the right microphone selected / unmuted?")
            return ""
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
        )
        text = " ".join(seg.text.strip() for seg in segments).strip()
        text = re.sub(r"\s+", " ", text)
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
    import ctypes
    global _MUTEX
    _MUTEX = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\DictationReadAloudHelper")
    return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def main():
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
