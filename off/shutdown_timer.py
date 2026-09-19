"""
Shutdown Timer v1.2 — умный таймер выключения ПК.
Запуск: python shutdown_timer.py
"""
from __future__ import annotations

import ctypes
import http.server
import json
import logging
import os
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Callable, Optional, Tuple

import customtkinter as ctk

try:
    import psutil
except ImportError:
    psutil = None

try:
    from PIL import Image, ImageDraw
except ImportError:
    Image = None


__version__ = "1.2.0"


APP_DIR = Path(os.environ.get("APPDATA", Path.home())) / "ShutdownTimer"
APP_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = APP_DIR / "config.json"
LOG_PATH = APP_DIR / "app.log"


def _assets_dir() -> Path:
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent / "assets"
        if exe_dir.is_dir():
            return exe_dir
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            p = Path(meipass) / "assets"
            if p.is_dir():
                return p
    return Path(__file__).resolve().parent / "assets"


ASSETS_DIR = _assets_dir()


def setup_logging() -> None:
    handler = RotatingFileHandler(
        LOG_PATH, maxBytes=512_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)


log = logging.getLogger("shutdown_timer")


DARK = {
    "bg": "#0F0F12", "card": "#1A1A1F", "input": "#232329",
    "border": "#2A2A32", "text": "#F5F5F7", "text_secondary": "#8E8E93",
    "accent": "#4A9EFF", "accent_hover": "#6BB0FF",
    "success": "#30D158", "danger": "#FF453A", "warning": "#FFD60A",
    "track": "#2A2A32",
}
LIGHT = {
    "bg": "#F5F5F7", "card": "#FFFFFF", "input": "#F0F0F3",
    "border": "#E0E0E5", "text": "#1C1C1E", "text_secondary": "#6E6E73",
    "accent": "#007AFF", "accent_hover": "#3B92FF",
    "success": "#34C759", "danger": "#FF3B30", "warning": "#FFCC00",
    "track": "#E0E0E5",
}

WIN_W, WIN_H = 440, 780
RADIUS_CARD, RADIUS_BTN, RADIUS_INPUT = 16, 12, 12
PAD, GAP, BTN_H, INPUT_H = 16, 12, 44, 52

_FAMILY = "Segoe UI"


def init_fonts() -> None:
    global _FAMILY
    try:
        import tkinter.font as tkfont
        for name in ("Segoe UI Variable", "Segoe UI", "Inter", "Helvetica"):
            if name in tkfont.families():
                _FAMILY = name
                return
    except Exception:
        pass


def F(sz: int, w: str = "normal") -> tuple:
    return (_FAMILY, sz, w)


def F_BODY() -> tuple:    return F(13)
def F_SMALL() -> tuple:   return F(11)
def F_TIMER() -> tuple:   return F(52, "bold")
def F_BUTTON() -> tuple:  return F(13, "bold")
def F_INPUT() -> tuple:   return F(18)


def find_free_port(start: int = 8765, end: int = 8865) -> int:
    for p in range(start, end + 1):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("0.0.0.0", p))
                return p
        except OSError:
            continue
    for _ in range(50):
        p = 49152 + secrets.randbelow(16383)
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("0.0.0.0", p))
                return p
        except OSError:
            continue
    return 8765


def generate_pin(length: int = 6) -> str:
    low = 10 ** (length - 1)
    high = 10 ** length - 1
    return str(low + secrets.randbelow(high - low + 1))


def is_port_free(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", port))
            return True
    except OSError:
        return False


def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


@dataclass
class Settings:
    theme: str = "dark"
    action: str = "Выключить"
    force_close: bool = False
    minimize_to_tray: bool = True
    history: list = field(default_factory=list)
    battery_threshold: int = 20
    min_delay_minutes: int = 30
    last_steam_appid: str = ""

    autostart: bool = False
    notify_before_sec: int = 60
    sound_on_action: bool = True
    check_updates_on_start: bool = True
    github_repo: str = ""

    mobile_sync_enabled: bool = False
    mobile_sync_port: int = 0
    mobile_sync_pin: str = ""

    def ensure_mobile_credentials(self) -> tuple:
        changed = False

        if (not self.mobile_sync_port
                or not (1024 <= self.mobile_sync_port <= 65535)
                or not is_port_free(self.mobile_sync_port)):
            self.mobile_sync_port = find_free_port()
            changed = True

        if not self.mobile_sync_pin or not self.mobile_sync_pin.isdigit():
            self.mobile_sync_pin = generate_pin(6)
            changed = True

        if changed:
            self.save()
            log.info("Mobile credentials: port=%s pin=%s",
                     self.mobile_sync_port, self.mobile_sync_pin)

        return self.mobile_sync_port, self.mobile_sync_pin

    def save(self) -> None:
        try:
            CONFIG_PATH.write_text(
                json.dumps(asdict(self), ensure_ascii=False, indent=2),
                encoding="utf-8")
        except Exception as e:
            log.exception("save settings: %s", e)

    @classmethod
    def load(cls) -> "Settings":
        if not CONFIG_PATH.exists():
            return cls()
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            known = {f for f in cls.__dataclass_fields__}
            return cls(**{k: v for k, v in data.items() if k in known})
        except Exception:
            return cls()

    def push_history(self, value: str) -> None:
        if not value:
            return
        self.history = [v for v in self.history if v != value]
        self.history.insert(0, value)
        self.history = self.history[:5]


_AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_AUTOSTART_NAME = "ShutdownTimer"


def _autostart_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    script = Path(__file__).resolve()
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    py = pythonw if pythonw.exists() else Path(sys.executable)
    return f'"{py}" "{script}"'


def set_autostart(enabled: bool) -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_KEY,
                            0, winreg.KEY_SET_VALUE) as k:
            if enabled:
                winreg.SetValueEx(k, _AUTOSTART_NAME, 0, winreg.REG_SZ,
                                  _autostart_command())
            else:
                try:
                    winreg.DeleteValue(k, _AUTOSTART_NAME)
                except FileNotFoundError:
                    pass
        return True
    except Exception as e:
        log.exception("autostart: %s", e)
        return False


def is_autostart_enabled() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_KEY) as k:
            try:
                winreg.QueryValueEx(k, _AUTOSTART_NAME)
                return True
            except FileNotFoundError:
                return False
    except Exception:
        return False


WORD_TIMES = {
    "полночь": (0, 0), "midnight": (0, 0),
    "утро": (8, 0), "утром": (8, 0), "morning": (8, 0),
    "обед": (13, 0), "noon": (12, 0),
    "день": (14, 0), "днем": (14, 0), "afternoon": (14, 0),
    "вечер": (19, 0), "вечером": (19, 0), "evening": (19, 0),
    "ночь": (23, 0), "ночью": (23, 0), "night": (23, 0),
}

UNIT_SECONDS = {
    "с": 1, "s": 1, "sec": 1, "secs": 1, "сек": 1, "секунд": 1,
    "м": 60, "m": 60, "min": 60, "mins": 60, "мин": 60, "минут": 60,
    "ч": 3600, "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600,
    "hours": 3600, "час": 3600, "часа": 3600, "часов": 3600,
    "д": 86400, "d": 86400, "day": 86400, "days": 86400,
    "день": 86400, "дня": 86400, "дней": 86400,
}


def parse_time_input(text: str, now: Optional[datetime] = None
                     ) -> Tuple[Optional[datetime], Optional[str]]:
    if now is None:
        now = datetime.now()

    if not text or not text.strip():
        return None, "Введите время"

    t = text.strip().lower()

    tomorrow = False
    for kw in ("завтра", "tomorrow"):
        if kw in t:
            tomorrow = True
            t = t.replace(kw, " ").strip()

    t = re.sub(r"\b(at|в|on)\b", " ", t).strip()
    t = re.sub(r"\s+", " ", t).strip()

    for kw in ("через ", "in ", "after "):
        if t.startswith(kw):
            return _parse_relative(t[len(kw):].strip(), now)

    for word, (h, m) in WORD_TIMES.items():
        if word in t:
            base = now + timedelta(days=1) if tomorrow else now
            target = base.replace(hour=h, minute=m, second=0, microsecond=0)
            if not tomorrow and target <= now:
                target += timedelta(days=1)
            return target, None

    m = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$", t)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        s = int(m.group(3)) if m.group(3) else 0
        if 0 <= h < 24 and 0 <= mi < 60 and 0 <= s < 60:
            base = now + timedelta(days=1) if tomorrow else now
            target = base.replace(hour=h, minute=mi, second=s, microsecond=0)
            if not tomorrow and target <= now:
                target += timedelta(days=1)
            return target, None

    if re.match(r"^[\d\s]+[a-zа-я]+", t) and not re.match(r"^\d+$", t):
        return _parse_relative(t, now)

    if t.isdigit():
        n = int(t)
        if len(t) >= 4:
            h, mi = int(t[:-2]), int(t[-2:])
            if h < 24 and mi < 60:
                base = now + timedelta(days=1) if tomorrow else now
                target = base.replace(hour=h, minute=mi,
                                      second=0, microsecond=0)
                if not tomorrow and target <= now:
                    target += timedelta(days=1)
                return target, None
        elif len(t) == 3:
            h, mi = int(t[0]), int(t[1:])
            if h < 24 and mi < 60:
                base = now + timedelta(days=1) if tomorrow else now
                target = base.replace(hour=h, minute=mi,
                                      second=0, microsecond=0)
                if not tomorrow and target <= now:
                    target += timedelta(days=1)
                return target, None
        elif len(t) <= 2:
            if n <= 24:
                base = now + timedelta(days=1) if tomorrow else now
                target = base.replace(hour=n, minute=0,
                                      second=0, microsecond=0)
                if not tomorrow and target <= now:
                    target += timedelta(days=1)
                return target, None
            return now + timedelta(minutes=n), None

    return None, "Не удалось распознать время"


def _parse_relative(text: str, now: datetime
                    ) -> Tuple[Optional[datetime], Optional[str]]:
    t = text.strip().lower()
    if not t:
        return None, "Укажите интервал"

    if "полчас" in t or "half an hour" in t or "half hour" in t:
        return now + timedelta(minutes=30), None
    if re.fullmatch(r"(час|hour|an hour|one hour)", t):
        return now + timedelta(hours=1), None
    if re.fullmatch(r"(минута|минуту|a minute|one minute)", t):
        return now + timedelta(minutes=1), None

    total = 0
    found = False
    for num, unit in re.findall(r"(\d+)\s*([a-zа-я]*)", t):
        n = int(num)
        unit = unit.strip()
        if unit == "" or unit not in UNIT_SECONDS:
            total += n * 60
        else:
            total += n * UNIT_SECONDS[unit]
        found = True

    if found and total > 0:
        return now + timedelta(seconds=total), None
    return None, "Не удалось распознать интервал"


def format_delta(td: timedelta) -> str:
    secs = int(td.total_seconds())
    if secs <= 0:
        return "сейчас"

    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)

    parts = []
    if d: parts.append(f"{d} д")
    if h: parts.append(f"{h} ч")
    if m and not d: parts.append(f"{m} мин")
    if s and not d and not h: parts.append(f"{s} с")

    return "через " + " ".join(parts) if parts else "сейчас"


def format_hms(seconds: float) -> str:
    secs = max(0, int(seconds))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def format_gb(b: float) -> str:
    return f"{b / 1e9:.2f} ГБ"


def describe_target(target: datetime, now: Optional[datetime] = None) -> str:
    if now is None:
        now = datetime.now()

    day_word = "сегодня"
    if target.date() == (now + timedelta(days=1)).date():
        day_word = "завтра"
    elif target.date() != now.date():
        day_word = target.strftime("%d.%m")

    return f"{day_word} в {target.strftime('%H:%M')} · {format_delta(target - now)}"


ACTION_SHUTDOWN = "Выключить"
ACTION_RESTART = "Перезагрузить"
ACTION_SLEEP = "Спящий режим"
ACTION_HIBERNATE = "Гибернация"
ACTION_LOCK = "Блокировка"
ALL_ACTIONS = [ACTION_SHUTDOWN, ACTION_RESTART, ACTION_SLEEP,
               ACTION_HIBERNATE, ACTION_LOCK]


def execute_action(action: str, force: bool = False) -> bool:
    try:
        if action == ACTION_SHUTDOWN:
            cmd = ["shutdown", "/s", "/t", "0"] + (["/f"] if force else [])
            subprocess.Popen(cmd)
        elif action == ACTION_RESTART:
            cmd = ["shutdown", "/r", "/t", "0"] + (["/f"] if force else [])
            subprocess.Popen(cmd)
        elif action == ACTION_SLEEP:
            subprocess.Popen(["rundll32.exe",
                              "powrprof.dll,SetSuspendState", "0,1,0"])
        elif action == ACTION_HIBERNATE:
            subprocess.Popen(["shutdown", "/h"])
        elif action == ACTION_LOCK:
            subprocess.Popen(["rundll32.exe", "user32.dll,LockWorkStation"])
        else:
            return False
        log.info("Action '%s' (force=%s)", action, force)
        return True
    except Exception as e:
        log.exception("action error: %s", e)
        return False


def play_sound() -> None:
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONASTERISK)
    except Exception:
        pass


def notify(title: str, message: str) -> None:
    def _run():
        try:
            from win10toast import ToastNotifier
            ToastNotifier().show_toast(title, message, duration=5,
                                       threaded=False)
            return
        except Exception:
            pass
        try:
            from plyer import notification
            notification.notify(title=title, message=message,
                                app_name="Shutdown Timer", timeout=5)
            return
        except Exception:
            pass
        log.info("NOTIFY: %s — %s", title, message)

    threading.Thread(target=_run, daemon=True).start()


def _version_tuple(v: str) -> tuple:
    parts = re.findall(r"\d+", v or "")
    parts = [int(p) for p in parts[:3]]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def is_newer_version(candidate: str, current: str) -> bool:
    return _version_tuple(candidate) > _version_tuple(current)


def check_github_release(repo: str) -> Optional[dict]:
    if not repo or "/" not in repo:
        return None

    url = f"https://api.github.com/repos/{repo}/releases/latest"
    req = urllib.request.Request(url, headers={
        "User-Agent": f"ShutdownTimer/{__version__}",
        "Accept": "application/vnd.github+json",
    })

    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read().decode("utf-8"))

    tag = (data.get("tag_name") or "").lstrip("vV").strip()

    exe_url = None
    exe_name = None
    for a in data.get("assets", []) or []:
        name = (a.get("name") or "").lower()
        if name.endswith(".exe") and "setup" not in name:
            exe_url = a.get("browser_download_url")
            exe_name = a.get("name")
            break

    if exe_url is None:
        for a in data.get("assets", []) or []:
            name = (a.get("name") or "").lower()
            if name.endswith(".exe"):
                exe_url = a.get("browser_download_url")
                exe_name = a.get("name")
                break

    return {
        "version": tag,
        "name": data.get("name", ""),
        "body": data.get("body", ""),
        "html_url": data.get("html_url", ""),
        "exe_url": exe_url,
        "exe_name": exe_name,
        "published_at": data.get("published_at", ""),
    }


def _download_file(url: str, dest: Path,
                   progress_cb: Optional[Callable[[int, int], None]] = None
                   ) -> None:
    req = urllib.request.Request(url, headers={
        "User-Agent": f"ShutdownTimer/{__version__}"})

    with urllib.request.urlopen(req, timeout=30) as r:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        chunk = 64 * 1024

        with open(dest, "wb") as f:
            while True:
                buf = r.read(chunk)
                if not buf:
                    break
                f.write(buf)
                got += len(buf)
                if progress_cb:
                    try:
                        progress_cb(got, total)
                    except Exception:
                        pass


def _schedule_exe_replace(new_exe: Path) -> bool:
    if not getattr(sys, "frozen", False):
        return False

    current_exe = Path(sys.executable).resolve()
    bat = Path(tempfile.gettempdir()) / "shutdown_timer_update.bat"
    log_path = Path(tempfile.gettempdir()) / "shutdown_timer_update.log"

    script = f'''@echo off
chcp 65001 >nul
echo === Update log === > "{log_path}"
echo Waiting for process exit... >> "{log_path}"
:wait
tasklist /FI "PID eq {os.getpid()}" 2>nul | find "{os.getpid()}" >nul
if not errorlevel 1 (
    timeout /t 1 /nobreak >nul
    goto wait
)
echo Copying {new_exe} -> {current_exe} >> "{log_path}"
copy /Y "{new_exe}" "{current_exe}" >> "{log_path}" 2>&1
if errorlevel 1 (
    echo COPY FAILED >> "{log_path}"
    exit /b 1
)
echo Starting new version... >> "{log_path}"
start "" "{current_exe}"
echo Done >> "{log_path}"
del "%~f0"
'''

    try:
        bat.write_text(script, encoding="utf-8")

        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200

        subprocess.Popen(
            ["cmd", "/c", str(bat)],
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
        return True
    except Exception as e:
        log.exception("schedule_exe_replace: %s", e)
        return False


class TimerLogic:
    def __init__(self, on_tick: Callable[[float], None],
                 on_finish: Callable[[], None],
                 interval: float = 0.2):
        self.on_tick = on_tick
        self.on_finish = on_finish
        self.interval = interval

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._pause = threading.Event()

        self._end_time: Optional[float] = None
        self._remaining: Optional[float] = None
        self.total: float = 0.0

    @property
    def active(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @property
    def paused(self) -> bool:
        return self._pause.is_set()

    def start(self, seconds: float) -> None:
        self.stop()
        self.total = seconds
        self._end_time = time.monotonic() + seconds
        self._remaining = None
        self._stop.clear()
        self._pause.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=0.5)
        self._thread = None
        self._end_time = None

    def pause(self) -> None:
        if self._end_time and not self._pause.is_set():
            self._remaining = self._end_time - time.monotonic()
            self._pause.set()

    def resume(self) -> None:
        if self._pause.is_set() and self._remaining is not None:
            self._end_time = time.monotonic() + self._remaining
            self._pause.clear()

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self._pause.is_set():
                remaining = self._end_time - time.monotonic()
                if remaining <= 0:
                    self.on_tick(0.0)
                    if not self._stop.is_set():
                        try:
                            self.on_finish()
                        except Exception:
                            log.exception("on_finish")
                    return
                self.on_tick(remaining)
            self._stop.wait(self.interval)


def find_steam_path() -> Optional[str]:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Valve\Steam") as k:
            try:
                path, _ = winreg.QueryValueEx(k, "SteamPath")
                p = path.replace("/", "\\")
                if os.path.isdir(p):
                    return p
            except FileNotFoundError:
                pass
            try:
                exe, _ = winreg.QueryValueEx(k, "SteamExe")
                p = os.path.dirname(exe.replace("/", "\\"))
                if os.path.isdir(p):
                    return p
            except FileNotFoundError:
                pass
    except Exception as e:
        log.warning("find_steam_path: %s", e)
    return None


def parse_acf(path: str) -> dict:
    try:
        txt = Path(path).read_text(encoding="utf-8-sig", errors="ignore")
    except OSError:
        return {}
    return {m.group(1): m.group(2)
            for m in re.finditer(r'"([^"]+)"\s+"([^"]*)"', txt)}


def find_steam_libraries() -> list:
    steam = find_steam_path()
    if not steam:
        return []
    libs = []
    primary = os.path.join(steam, "steamapps")
    if os.path.isdir(primary):
        libs.append(primary)
    vdf = os.path.join(primary, "libraryfolders.vdf")
    if os.path.isfile(vdf):
        try:
            txt = Path(vdf).read_text(encoding="utf-8-sig", errors="ignore")
        except OSError:
            txt = ""
        for m in re.finditer(r'"path"\s+"([^"]+)"', txt):
            p = m.group(1).replace("\\\\", "\\")
            sa = os.path.join(p, "steamapps")
            if os.path.isdir(sa) and sa not in libs:
                libs.append(sa)
    return libs


_STATE_NAMES = {
    0: "не установлено", 1: "обновление требуется",
    2: "обновление требуется", 4: "установлено",
    6: "обновление требуется", 1024: "загрузка начата",
    1026: "загрузка", 1042: "загрузка идёт", 1046: "загрузка идёт",
}


def _state_name(flags: int) -> str:
    return _STATE_NAMES.get(flags, f"статус {flags}")


def list_steam_downloads() -> list:
    out = []
    for sa in find_steam_libraries():
        try:
            files = os.listdir(sa)
        except OSError:
            continue
        for f in files:
            if not (f.startswith("appmanifest_") and f.endswith(".acf")):
                continue
            fp = os.path.join(sa, f)
            data = parse_acf(fp)
            try:
                flags = int(data.get("StateFlags", "0"))
            except ValueError:
                flags = 0
            if flags in (0, 4):
                continue
            name = data.get("name", "") or f"AppID {data.get('appid','?')}"
            try:
                dl = int(data.get("BytesDownloaded", "0"))
            except ValueError:
                dl = 0
            try:
                tt = int(data.get("BytesToDownload", "0"))
            except ValueError:
                tt = 0
            percent = min(100.0, dl / tt * 100.0) if tt > 0 else 0.0
            out.append({
                "appid": str(data.get("appid", "")),
                "name": name, "flags": flags,
                "downloaded": dl, "total": tt, "percent": percent,
            })
    out.sort(key=lambda x: (-x["percent"], x["name"].lower()))
    return out


def _flags_of(appid: str) -> Optional[int]:
    name = f"appmanifest_{appid}.acf"
    for sa in find_steam_libraries():
        p = os.path.join(sa, name)
        if os.path.isfile(p):
            data = parse_acf(p)
            try:
                return int(data.get("StateFlags", "0"))
            except ValueError:
                return None
    return None


class BaseTrigger:
    def __init__(self, callback: Callable[[], None]):
        self.callback = callback
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                if self.check():
                    if not self._stop.is_set():
                        self.callback()
                    return
            except Exception:
                log.exception("trigger check")
            self._stop.wait(3)

    def check(self) -> bool:
        raise NotImplementedError


class SteamAppDoneTrigger(BaseTrigger):
    def __init__(self, callback, appid: str):
        super().__init__(callback)
        self.appid = str(appid)
        self._seen_active = False

    def check(self) -> bool:
        flags = _flags_of(self.appid)
        if flags is None:
            return False
        if flags == 4:
            return self._seen_active
        if flags != 0:
            self._seen_active = True
        return False


class ProcessExitTrigger(BaseTrigger):
    def __init__(self, callback, process_name: str):
        super().__init__(callback)
        self.process_name = process_name.lower()
        self._seen = False

    def check(self) -> bool:
        if psutil is None:
            return False
        found = False
        for p in psutil.process_iter(["name"]):
            try:
                if (p.info.get("name") or "").lower() == self.process_name:
                    found = True
                    break
            except Exception:
                continue
        if found:
            self._seen = True
            return False
        return self._seen


class BatteryTrigger(BaseTrigger):
    def __init__(self, callback, threshold: int = 20):
        super().__init__(callback)
        self.threshold = threshold

    def check(self) -> bool:
        if psutil is None:
            return False
        b = psutil.sensors_battery()
        return b is not None and b.percent <= self.threshold


class MinDelayTrigger(BaseTrigger):
    def __init__(self, callback, inner: BaseTrigger, min_seconds: int):
        super().__init__(callback)
        self.inner = inner
        self.min_seconds = min_seconds
        self._start = time.time()

    def check(self) -> bool:
        if (time.time() - self._start) < self.min_seconds:
            return False
        return self.inner.check()

    def stop(self):
        super().stop()
        self.inner.stop()


_ICON_HICON = None


def _make_fallback_png(size: int = 256):
    if Image is None:
        return None
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=int(size * 0.22),
                        fill=(74, 158, 255, 255))
    white = (255, 255, 255, 255)
    d.polygon([(size * 0.30, size * 0.25),
               (size * 0.70, size * 0.25),
               (size * 0.50, size * 0.48)], fill=white)
    d.polygon([(size * 0.50, size * 0.52),
               (size * 0.30, size * 0.75),
               (size * 0.70, size * 0.75)], fill=white)
    return img


def _ensure_ico_file() -> Optional[Path]:
    if Image is None:
        return None
    ico = ASSETS_DIR / "icon.ico"
    if ico.is_file():
        return ico
    for name in ("icon.png", "icon.jpg", "icon.jpeg", "app.png", "app.jpg"):
        p = ASSETS_DIR / name
        if p.is_file():
            try:
                img = Image.open(p).convert("RGBA")
                side = max(img.size)
                canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
                canvas.paste(img, ((side - img.width) // 2,
                                   (side - img.height) // 2))
                canvas = canvas.resize((256, 256), Image.LANCZOS)
                out = APP_DIR / "_app_icon.ico"
                canvas.save(out, format="ICO",
                            sizes=[(16, 16), (24, 24), (32, 32),
                                   (48, 48), (64, 64), (128, 128), (256, 256)])
                return out
            except Exception as e:
                log.warning("icon convert %s: %s", p, e)
    try:
        img = _make_fallback_png(256)
        if img:
            out = APP_DIR / "_app_icon.ico"
            img.save(out, format="ICO",
                     sizes=[(16, 16), (24, 24), (32, 32),
                            (48, 48), (64, 64), (128, 128), (256, 256)])
            return out
    except Exception as e:
        log.warning("fallback ico: %s", e)
    return None


def _get_hwnd(root) -> int:
    try:
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        if hwnd:
            return hwnd
    except Exception:
        pass
    try:
        return root.winfo_id()
    except Exception:
        return 0


def apply_icon(root) -> None:
    global _ICON_HICON
    ico = _ensure_ico_file()
    if not ico:
        return
    try:
        root.wm_iconbitmap(default=str(ico))
    except Exception:
        pass
    try:
        hwnd = _get_hwnd(root)
        if not hwnd:
            return
        IMAGE_ICON = 1
        LR_LOADFROMFILE = 0x00000010
        LR_DEFAULTSIZE = 0x00000040
        hicon = ctypes.windll.user32.LoadImageW(
            0, str(ico), IMAGE_ICON, 0, 0,
            LR_LOADFROMFILE | LR_DEFAULTSIZE)
        if not hicon:
            hicon = ctypes.windll.user32.LoadImageW(
                0, str(ico), IMAGE_ICON, 0, 0, LR_LOADFROMFILE)
        if hicon:
            WM_SETICON = 0x0080
            ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, 0, hicon)
            ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, 1, hicon)
            _ICON_HICON = hicon
    except Exception as e:
        log.warning("taskbar icon: %s", e)


def build_tray_image(size: int = 64):
    if Image is None:
        return None
    for name in ("icon.png", "icon.ico", "icon.jpg", "icon.jpeg"):
        p = ASSETS_DIR / name
        if p.is_file():
            try:
                return Image.open(p).convert("RGBA").resize(
                    (size, size), Image.LANCZOS)
            except Exception as e:
                log.warning("tray icon: %s", e)
    return _make_fallback_png(size)


MOBILE_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<meta name="theme-color" content="#0F0F12">
<title>Shutdown Timer</title>
<style>
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
background:#0F0F12;color:#F5F5F7;min-height:100vh;padding:16px;
display:flex;flex-direction:column;gap:12px;max-width:440px;margin:0 auto}
.title{font-size:13px;color:#8E8E93;text-align:center;letter-spacing:1px}
.timer{background:#1A1A1F;border:1px solid #2A2A32;border-radius:18px;padding:22px;text-align:center}
.timer-num{font-size:44px;font-weight:700;letter-spacing:-1px;font-variant-numeric:tabular-nums;line-height:1.1}
.timer-status{font-size:12px;color:#8E8E93;margin-top:6px;min-height:16px}
.status-active{color:#30D158!important}
.status-paused{color:#FFD60A!important}
.chips{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
.chip{background:#232329;color:#F5F5F7;border:1px solid #2A2A32;border-radius:12px;
padding:14px 0;font-size:15px;font-weight:600;cursor:pointer;transition:.15s}
.chip:active{transform:scale(.96);background:#2A2A32}
.input-row{display:flex;gap:8px}
input[type=text]{flex:1;background:#232329;color:#F5F5F7;border:1px solid #2A2A32;
border-radius:12px;padding:14px 16px;font-size:16px;outline:none;min-width:0}
input[type=text]:focus{border-color:#4A9EFF}
input[type=text]::placeholder{color:#8E8E93}
.btn{border:none;border-radius:12px;padding:14px;font-size:15px;font-weight:700;
cursor:pointer;transition:.15s;width:100%;color:#FFF}
.btn:active{transform:scale(.98)}
.btn-primary{background:#4A9EFF}
.btn-danger{background:#FF453A}
.btn-ghost{background:#232329;color:#F5F5F7;border:1px solid #2A2A32}
select{width:100%;background:#232329;color:#F5F5F7;border:1px solid #2A2A32;
border-radius:12px;padding:14px;font-size:15px;outline:none;-webkit-appearance:none;
background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'><path d='M1 1l5 5 5-5' stroke='%238E8E93' stroke-width='2' fill='none' stroke-linecap='round'/></svg>");
background-repeat:no-repeat;background-position:right 16px center;padding-right:40px}
.footer{font-size:11px;color:#8E8E93;text-align:center;margin-top:8px}
.row-btns{display:grid;grid-template-columns:1fr 1fr;gap:8px}
</style>
</head>
<body>
<div class="title">SHUTDOWN TIMER</div>
<div class="timer">
  <div class="timer-num" id="timerNum">00:00:00</div>
  <div class="timer-status" id="timerStatus">Не активно</div>
</div>
<div class="chips">
  <button class="chip" onclick="quick(15)">15 мин</button>
  <button class="chip" onclick="quick(30)">30 мин</button>
  <button class="chip" onclick="quick(60)">1 ч</button>
  <button class="chip" onclick="quick(120)">2 ч</button>
  <button class="chip" onclick="quick(240)">4 ч</button>
  <button class="chip" onclick="quick(480)">8 ч</button>
</div>
<div class="input-row">
  <input type="text" id="timeInput" placeholder="30м · 1233 · через час" autocomplete="off" autocapitalize="off">
  <button class="btn btn-primary" style="width:auto;padding:14px 22px;flex:none" onclick="startText()">▶</button>
</div>
<select id="actionSelect" onchange="setAction(this.value)">
  <option>Выключить</option>
  <option>Перезагрузить</option>
  <option>Спящий режим</option>
  <option>Гибернация</option>
  <option>Блокировка</option>
</select>
<div class="row-btns">
  <button class="btn btn-ghost" id="pauseBtn" onclick="pauseResume()">⏸ Пауза</button>
  <button class="btn btn-danger" onclick="cancelAll()">✕ Отмена</button>
</div>
<div class="footer" id="footer">v—</div>
<script>
const PIN = new URLSearchParams(location.search).get('pin') || '';
const withPin = (p) => PIN ? (p.includes('?') ? p+'&pin='+encodeURIComponent(PIN)
                                               : p+'?pin='+encodeURIComponent(PIN)) : p;
async function api(path, data) {
  try {
    const r = await fetch(withPin(path), {
      method: data ? 'POST' : 'GET',
      headers: {'Content-Type':'application/json'},
      body: data ? JSON.stringify(data) : undefined
    });
    return await r.json();
  } catch(e) { return null; }
}
async function refresh() {
  const s = await api('/api/status');
  if (!s) return;
  document.getElementById('timerNum').textContent = s.remaining_hms || '00:00:00';
  const st = document.getElementById('timerStatus');
  if (s.active) {
    st.textContent = s.paused ? 'Пауза' : ('Работает · ' + s.action);
    st.className = 'timer-status ' + (s.paused ? 'status-paused' : 'status-active');
    document.getElementById('pauseBtn').textContent = s.paused ? '▶ Продолжить' : '⏸ Пауза';
  } else {
    st.textContent = 'Не активно';
    st.className = 'timer-status';
    document.getElementById('pauseBtn').textContent = '⏸ Пауза';
  }
  const sel = document.getElementById('actionSelect');
  if (s.action && sel.value !== s.action) sel.value = s.action;
  document.getElementById('footer').textContent = 'Shutdown Timer v' + s.version;
}
function quick(min) { api('/api/start', {seconds: min*60}).then(refresh); }
function startText() {
  const t = document.getElementById('timeInput').value.trim();
  if (!t) return;
  api('/api/start', {text: t}).then(r => {
    if (r && r.ok) document.getElementById('timeInput').value = '';
    refresh();
  });
}
function cancelAll() { api('/api/cancel', {}).then(refresh); }
function pauseResume() { api('/api/pause', {}).then(refresh); }
function setAction(a) { api('/api/action', {action: a}).then(refresh); }
document.getElementById('timeInput').addEventListener('keydown', e => {
  if (e.key === 'Enter') startText();
});
refresh();
setInterval(refresh, 1000);
</script>
</body>
</html>
"""


LOGIN_HTML = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PIN</title>
<style>
body{font-family:-apple-system,"Segoe UI",Roboto,sans-serif;background:#0F0F12;
color:#F5F5F7;display:flex;align-items:center;justify-content:center;
min-height:100vh;margin:0;padding:24px}
.box{background:#1A1A1F;border:1px solid #2A2A32;border-radius:18px;padding:28px;
max-width:340px;width:100%;text-align:center}
h2{margin:0 0 16px;font-weight:600}
input{width:100%;background:#232329;color:#F5F5F7;border:1px solid #2A2A32;
border-radius:12px;padding:14px 16px;font-size:20px;outline:none;text-align:center;
letter-spacing:4px;margin-bottom:12px;box-sizing:border-box}
input:focus{border-color:#4A9EFF}
button{width:100%;background:#4A9EFF;color:#FFF;border:none;border-radius:12px;
padding:14px;font-size:15px;font-weight:700;cursor:pointer}
</style></head><body>
<div class="box">
<h2>🔒 PIN</h2>
<input id="pin" type="tel" inputmode="numeric" maxlength="8" autofocus placeholder="····">
<button onclick="go()">Войти</button>
</div>
<script>
function go(){
  const v=document.getElementById('pin').value.trim();
  if(v)location.href='/?pin='+encodeURIComponent(v);
}
document.getElementById('pin').addEventListener('keydown',e=>{if(e.key==='Enter')go()});
</script>
</body></html>
"""


class MobileSyncHandler(http.server.BaseHTTPRequestHandler):
    app_ref = None
    pin = ""

    def log_message(self, *args):
        pass

    def _check_pin(self) -> bool:
        if not self.pin:
            return True
        q = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(q)
        return params.get("pin", [""])[0] == self.pin

    def _json(self, code: int, data: dict) -> None:
        try:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            pass

    def _html(self, code: int, html: str) -> None:
        try:
            body = html.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            pass

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            if not self._check_pin():
                self._html(401, LOGIN_HTML)
                return
            self._html(200, MOBILE_HTML)
            return
        if path == "/api/status":
            if not self._check_pin():
                self._json(401, {"error": "unauthorized"})
                return
            self._json(200, self.app_ref._mobile_status())
            return
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        self._html(404, "Not found")

    def do_POST(self):
        if not self._check_pin():
            self._json(401, {"error": "unauthorized"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            data = {}

        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/api/start":
                res = self.app_ref._mobile_start(
                    seconds=data.get("seconds"), text=data.get("text", ""))
                self._json(200, res)
            elif path == "/api/cancel":
                self.app_ref._mobile_cancel()
                self._json(200, {"ok": True})
            elif path == "/api/pause":
                self.app_ref._mobile_toggle_pause()
                self._json(200, {"ok": True})
            elif path == "/api/action":
                self.app_ref._mobile_set_action(data.get("action", ""))
                self._json(200, {"ok": True})
            else:
                self._json(404, {"error": "not found"})
        except Exception as e:
            log.exception("mobile handler")
            self._json(500, {"error": str(e)})


class MobileSyncServer:
    def __init__(self, app, port: int = 8765, pin: str = ""):
        self.app = app
        self.port = port
        self.pin = pin
        self._httpd: Optional[http.server.ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def running(self) -> bool:
        return self._httpd is not None

    def start(self) -> bool:
        if self._httpd:
            return True

        MobileSyncHandler.app_ref = self.app
        MobileSyncHandler.pin = self.pin or ""

        tried = []
        candidates = [self.port] + [self.port + i for i in range(1, 31)]
        httpd = None
        used_port = self.port

        for p in candidates:
            if p < 1024 or p > 65535:
                continue
            tried.append(p)
            try:
                httpd = http.server.ThreadingHTTPServer(
                    ("0.0.0.0", p), MobileSyncHandler)
                httpd.daemon_threads = True
                used_port = p
                break
            except OSError as e:
                log.debug("port %s busy: %s", p, e)
                httpd = None
                continue

        if httpd is None:
            log.error("mobile start failed, tried ports: %s", tried)
            return False

        self._httpd = httpd
        if used_port != self.port:
            log.info("port %s was busy, using %s", self.port, used_port)
            self.port = used_port
            try:
                self.app.settings.mobile_sync_port = used_port
                self.app.settings.save()
            except Exception:
                pass

        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        daemon=True)
        self._thread.start()
        log.info("Mobile sync started on :%s (pin=%s)",
                 self.port, self.pin or "—")
        return True

    def stop(self) -> None:
        if not self._httpd:
            return
        try:
            self._httpd.shutdown()
        except Exception:
            pass
        try:
            self._httpd.server_close()
        except Exception:
            pass
        self._httpd = None
        self._thread = None
        log.info("Mobile sync stopped")

    def restart(self, port: int, pin: str) -> bool:
        self.stop()
        self.port = port
        self.pin = pin
        return self.start()


class TrayIcon:
    def __init__(self, on_show, on_toggle, on_cancel, on_quit,
                 on_check_updates=None):
        self.on_show = on_show
        self.on_toggle = on_toggle
        self.on_cancel = on_cancel
        self.on_quit = on_quit
        self.on_check_updates = on_check_updates
        self._icon = None

    def start(self) -> None:
        try:
            import pystray
        except ImportError:
            log.warning("pystray не установлен")
            return
        img = build_tray_image()
        if img is None:
            return
        items = [
            pystray.MenuItem("Показать", lambda *_: self.on_show(),
                             default=True),
            pystray.MenuItem("Пауза / Продолжить", lambda *_: self.on_toggle()),
            pystray.MenuItem("Отменить", lambda *_: self.on_cancel()),
            pystray.Menu.SEPARATOR,
        ]
        if self.on_check_updates:
            items.append(pystray.MenuItem("Проверить обновления",
                                          lambda *_: self.on_check_updates()))
        items.append(pystray.MenuItem("Выход", lambda *_: self.on_quit()))

        self._icon = pystray.Icon("shutdown_timer", img,
                                  f"Shutdown Timer v{__version__}",
                                  pystray.Menu(*items))
        threading.Thread(target=self._icon.run, daemon=True).start()

    def stop(self) -> None:
        if self._icon:
            try:
                self._icon.stop()
            except Exception:
                pass

    def set_title(self, text: str) -> None:
        if self._icon:
            try:
                self._icon.title = text
            except Exception:
                pass


class Card(ctk.CTkFrame):
    def __init__(self, master, palette: dict, **kw):
        super().__init__(master, fg_color=palette["card"],
                         corner_radius=RADIUS_CARD, border_width=1,
                         border_color=palette["border"], **kw)


class PrimaryButton(ctk.CTkButton):
    def __init__(self, master, palette: dict, text: str, command=None,
                 color=None, hover=None, text_color="#FFFFFF"):
        super().__init__(master, text=text, command=command, height=BTN_H,
                         corner_radius=RADIUS_BTN, font=F_BUTTON(),
                         fg_color=color or palette["accent"],
                         hover_color=hover or palette["accent_hover"],
                         text_color=text_color)


class GhostButton(ctk.CTkButton):
    def __init__(self, master, palette: dict, text: str, command=None):
        super().__init__(master, text=text, command=command, height=36,
                         corner_radius=RADIUS_BTN, font=F_BODY(),
                         fg_color="transparent", hover_color=palette["input"],
                         text_color=palette["text"], border_width=1,
                         border_color=palette["border"])


class Chip(ctk.CTkButton):
    def __init__(self, master, palette: dict, text: str, command=None):
        super().__init__(master, text=text, command=command, height=32,
                         width=60, corner_radius=10, font=F_SMALL(),
                         fg_color=palette["input"],
                         hover_color=palette["border"],
                         text_color=palette["text"], border_width=1,
                         border_color=palette["border"])


class IconButton(ctk.CTkButton):
    def __init__(self, master, palette: dict, text: str, command=None,
                 hover_color=None):
        super().__init__(master, text=text, command=command, width=36,
                         height=28, corner_radius=8, font=F(13),
                         fg_color="transparent",
                         hover_color=hover_color or palette["input"],
                         text_color=palette["text"])


class CheckRow(ctk.CTkCheckBox):
    def __init__(self, master, palette: dict, text: str,
                 variable=None, command=None):
        super().__init__(master, text=text, variable=variable, command=command,
                         font=F_BODY(), text_color=palette["text"],
                         fg_color=palette["accent"],
                         hover_color=palette["accent_hover"],
                         border_color=palette["border"], corner_radius=6,
                         checkbox_width=18, checkbox_height=18)


class RadioRow(ctk.CTkRadioButton):
    def __init__(self, master, palette: dict, text: str, variable, value,
                 command=None):
        super().__init__(master, text=text, variable=variable, value=value,
                         command=command, font=F_BODY(),
                         text_color=palette["text"],
                         fg_color=palette["accent"],
                         hover_color=palette["accent_hover"],
                         border_color=palette["border"])


def _set_appwindow(root) -> None:
    try:
        GWL_EXSTYLE = -20
        WS_EX_APPWINDOW = 0x00040000
        WS_EX_TOOLWINDOW = 0x00000080
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        style = (style & ~WS_EX_TOOLWINDOW) | WS_EX_APPWINDOW
        ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
        root.withdraw()
        root.after(10, root.deiconify)
    except Exception:
        pass


class ShutdownTimerApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        init_fonts()
        self.settings = Settings.load()
        self.palette = DARK if self.settings.theme == "dark" else LIGHT

        self.title(f"Shutdown Timer v{__version__}")
        self.geometry(f"{WIN_W}x{WIN_H}")
        self.resizable(False, False)
        self.overrideredirect(True)
        self.configure(fg_color=self.palette["bg"])

        self.after(20, lambda: _set_appwindow(self))
        self.after(120, lambda: apply_icon(self))
        self.after(500, lambda: apply_icon(self))

        self.timer = TimerLogic(self._on_tick, self._on_finish)
        self._active_trigger: Optional[BaseTrigger] = None
        self._trigger_kind: Optional[str] = None
        self._last_parsed: Optional[datetime] = None

        self._steam_items: dict = {}
        self._steam_by_appid: dict = {}
        self._selected_appid: str = self.settings.last_steam_appid or ""

        self._last_tray_update: float = 0.0
        self._warned_before: bool = False

        self._update_info: Optional[dict] = None
        self._update_downloading: bool = False

        self.tray = TrayIcon(
            self._show_window, self._toggle_pause, self._cancel_all,
            self._quit_app,
            on_check_updates=lambda: self._check_updates(silent=False))

        _port, _pin = self.settings.ensure_mobile_credentials()
        self.mobile_server = MobileSyncServer(self, port=_port, pin=_pin)

        self._build_titlebar()
        self._build_body()

        self.protocol("WM_DELETE_WINDOW", self._minimize)
        self.bind("<Escape>", lambda e: self._minimize())

        self.tray.start()

        if self.settings.check_updates_on_start and self.settings.github_repo:
            self.after(3000, lambda: self._check_updates(silent=True))

        self.after(500, self._refresh_steam_downloads)
        self.after(2000, self._auto_refresh_steam)
        self.after(800, self._sync_autostart_ui)

        if self.settings.mobile_sync_enabled:
            self.after(1500, self._mobile_start_server_ui)

    def _build_titlebar(self) -> None:
        p = self.palette
        bar = ctk.CTkFrame(self, fg_color=p["bg"], corner_radius=0, height=40)
        bar.pack(fill="x", side="top")
        bar.pack_propagate(False)

        title = ctk.CTkLabel(bar, text="⏻  Shutdown Timer",
                             font=F(13, "bold"), text_color=p["text"])
        title.pack(side="left", padx=(16, 0))

        IconButton(bar, p, "✕", command=self._minimize,
                   hover_color=p["danger"]).pack(side="right", padx=(0, 8), pady=6)
        IconButton(bar, p, "─", command=self._minimize
                   ).pack(side="right", pady=6)
        self.theme_btn = IconButton(
            bar, p, "🌙" if self.settings.theme == "dark" else "☀",
            command=self._toggle_theme)
        self.theme_btn.pack(side="right", pady=6)

        for w in (bar, title):
            w.bind("<Button-1>", self._start_move)
            w.bind("<B1-Motion>", self._do_move)
            w.bind("<Double-Button-1>", lambda e: self._minimize())

    def _start_move(self, e) -> None:
        self._dx, self._dy = e.x, e.y

    def _do_move(self, e) -> None:
        self.geometry(f"+{self.winfo_x() + e.x - self._dx}+"
                      f"{self.winfo_y() + e.y - self._dy}")

    def _build_body(self) -> None:
        p = self.palette
        body = ctk.CTkFrame(self, fg_color=p["bg"], corner_radius=0)
        body.pack(fill="both", expand=True, padx=PAD, pady=(0, PAD))

        self.segment = ctk.CTkSegmentedButton(
            body, values=["⏱ Время", "⚡ События", "⚙ Настройки"],
            command=self._on_tab_change, height=38, font=F_BODY(),
            corner_radius=RADIUS_BTN, fg_color=p["input"],
            selected_color=p["accent"], selected_hover_color=p["accent_hover"],
            unselected_color=p["input"], unselected_hover_color=p["border"],
            text_color=p["text"])
        self.segment.pack(fill="x", pady=(4, GAP))
        self.segment.set("⏱ Время")

        self.tab_time = ctk.CTkFrame(body, fg_color="transparent")
        self.tab_events = ctk.CTkFrame(body, fg_color="transparent")
        self.tab_settings = ctk.CTkFrame(body, fg_color="transparent")
        self.tab_time.pack(fill="both", expand=True)

        self._build_time_tab()
        self._build_events_tab()
        self._build_settings_tab()

    def _build_time_tab(self) -> None:
        p = self.palette
        parent = self.tab_time

        timer_card = Card(parent, p)
        timer_card.pack(fill="x", pady=(0, GAP))
        self.timer_label = ctk.CTkLabel(timer_card, text="00:00:00",
                                        font=F_TIMER(), text_color=p["text"])
        self.timer_label.pack(pady=(14, 0))
        self.status_label = ctk.CTkLabel(
            timer_card, text="Готов к запуску",
            font=F_SMALL(), text_color=p["text_secondary"])
        self.status_label.pack(pady=(0, 14))

        input_card = Card(parent, p)
        input_card.pack(fill="x", pady=(0, GAP))
        self.entry = ctk.CTkEntry(
            input_card,
            placeholder_text="Введите время… (1233, 30м, завтра в 9)",
            height=INPUT_H, corner_radius=RADIUS_INPUT, font=F_INPUT(),
            fg_color=p["input"], border_color=p["border"],
            text_color=p["text"], placeholder_text_color=p["text_secondary"])
        self.entry.pack(fill="x", padx=14, pady=(14, 6))
        self.entry.bind("<KeyRelease>", self._on_entry_key)
        self.entry.bind("<Return>", lambda e: self._start_from_input())

        self.preview = ctk.CTkLabel(
            input_card, text="Введите время и нажмите Enter",
            font=F_SMALL(), text_color=p["text_secondary"], anchor="w")
        self.preview.pack(fill="x", padx=16, pady=(0, 12))

        ctk.CTkLabel(parent, text="Пресеты:", anchor="w",
                     font=F_SMALL(), text_color=p["text_secondary"]
                     ).pack(fill="x", pady=(0, 4))
        chips = ctk.CTkFrame(parent, fg_color="transparent")
        chips.pack(fill="x", pady=(0, GAP))
        for label, mins in [("15м", 15), ("30м", 30), ("1ч", 60),
                            ("2ч", 120), ("4ч", 240)]:
            Chip(chips, p, label,
                 command=lambda m=mins: self._set_preset(m)
                 ).pack(side="left", padx=(0, 6))

        opts = Card(parent, p)
        opts.pack(fill="x", pady=(0, GAP))
        row = ctk.CTkFrame(opts, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=(12, 4))
        ctk.CTkLabel(row, text="Действие:", font=F_BODY(),
                     text_color=p["text"]).pack(side="left")

        self.action_var = ctk.StringVar(value=self.settings.action)
        ctk.CTkOptionMenu(
            row, values=ALL_ACTIONS, variable=self.action_var,
            font=F_BODY(), dropdown_font=F_BODY(),
            fg_color=p["input"], button_color=p["input"],
            button_hover_color=p["border"], dropdown_fg_color=p["card"],
            dropdown_hover_color=p["input"], dropdown_text_color=p["text"],
            text_color=p["text"], corner_radius=10, width=160
        ).pack(side="right")

        self.force_var = ctk.BooleanVar(value=self.settings.force_close)
        CheckRow(opts, p, "Принудительно закрыть программы",
                 variable=self.force_var).pack(anchor="w", padx=16, pady=(4, 12))

        self.start_btn = PrimaryButton(parent, p, "▶  ЗАПУСТИТЬ ТАЙМЕР",
                                       command=self._on_main_button)
        self.start_btn.pack(fill="x")

        self.pause_btn = GhostButton(parent, p, "⏸ Пауза",
                                     command=self._toggle_pause)

    def _build_events_tab(self) -> None:
        p = self.palette
        parent = self.tab_events

        trig_card = Card(parent, p)
        trig_card.pack(fill="x", pady=(0, GAP))
        ctk.CTkLabel(trig_card, text="Что отслеживать:",
                     font=F_BODY(), text_color=p["text"]
                     ).pack(anchor="w", padx=16, pady=(12, 8))

        self.trig_var = ctk.StringVar(value="steam_app")
        RadioRow(trig_card, p, "Steam: завершение установки игры",
                 self.trig_var, "steam_app").pack(anchor="w", padx=16, pady=2)

        row_steam = ctk.CTkFrame(trig_card, fg_color="transparent")
        row_steam.pack(fill="x", padx=32, pady=(0, 4))
        self.steam_var = ctk.StringVar(value="(нет активных загрузок)")
        self.steam_menu = ctk.CTkOptionMenu(
            row_steam, values=["(нет активных загрузок)"],
            variable=self.steam_var, font=F_SMALL(), dropdown_font=F_SMALL(),
            fg_color=p["input"], button_color=p["input"],
            button_hover_color=p["border"], dropdown_fg_color=p["card"],
            dropdown_text_color=p["text"], text_color=p["text"],
            corner_radius=8, width=280,
            command=self._on_steam_select)
        self.steam_menu.pack(side="left")
        GhostButton(row_steam, p, "↻",
                    command=self._refresh_steam_downloads
                    ).pack(side="left", padx=4)

        self.steam_progress = ctk.CTkProgressBar(
            trig_card, height=8, corner_radius=4,
            fg_color=p["track"], progress_color=p["accent"])
        self.steam_progress.set(0)
        self.steam_progress.pack(fill="x", padx=32, pady=(2, 4))

        self.steam_info = ctk.CTkLabel(
            trig_card, text="", font=F_SMALL(),
            text_color=p["text_secondary"], anchor="w", justify="left")
        self.steam_info.pack(fill="x", padx=32, pady=(0, 10))

        RadioRow(trig_card, p, "Программа закроется",
                 self.trig_var, "process_exit").pack(anchor="w", padx=16, pady=2)
        row_proc = ctk.CTkFrame(trig_card, fg_color="transparent")
        row_proc.pack(fill="x", padx=32, pady=(0, 8))
        self.process_var = ctk.StringVar(value="")
        self.process_menu = ctk.CTkOptionMenu(
            row_proc, values=["(нет процессов)"] + self._list_processes(),
            variable=self.process_var, font=F_SMALL(), dropdown_font=F_SMALL(),
            fg_color=p["input"], button_color=p["input"],
            button_hover_color=p["border"], dropdown_fg_color=p["card"],
            dropdown_text_color=p["text"], text_color=p["text"],
            corner_radius=8, width=200)
        self.process_menu.pack(side="left")
        GhostButton(row_proc, p, "↻",
                    command=self._refresh_processes).pack(side="left", padx=4)
        GhostButton(row_proc, p, "Обзор…",
                    command=self._browse_exe).pack(side="left")

        if psutil is not None:
            try:
                if psutil.sensors_battery() is not None:
                    RadioRow(trig_card, p, "Заряд батареи ниже",
                             self.trig_var, "battery"
                             ).pack(anchor="w", padx=16, pady=2)
                    row_b = ctk.CTkFrame(trig_card, fg_color="transparent")
                    row_b.pack(fill="x", padx=32, pady=(0, 8))
                    self.battery_var = ctk.StringVar(
                        value=str(self.settings.battery_threshold))
                    ctk.CTkEntry(row_b, textvariable=self.battery_var, width=60,
                                 font=F_SMALL(), fg_color=p["input"],
                                 border_color=p["border"],
                                 text_color=p["text"],
                                 corner_radius=8).pack(side="left")
                    ctk.CTkLabel(row_b, text="%", font=F_SMALL(),
                                 text_color=p["text_secondary"]
                                 ).pack(side="left", padx=4)
            except Exception:
                pass

        act_card = Card(parent, p)
        act_card.pack(fill="x", pady=(0, GAP))

        row2 = ctk.CTkFrame(act_card, fg_color="transparent")
        row2.pack(fill="x", padx=14, pady=(12, 8))
        ctk.CTkLabel(row2, text="Действие:", font=F_BODY(),
                     text_color=p["text"]).pack(side="left")
        self.event_action_var = ctk.StringVar(value=self.settings.action)
        ctk.CTkOptionMenu(
            row2, values=ALL_ACTIONS, variable=self.event_action_var,
            font=F_BODY(), dropdown_font=F_BODY(),
            fg_color=p["input"], button_color=p["input"],
            button_hover_color=p["border"], dropdown_fg_color=p["card"],
            dropdown_text_color=p["text"], text_color=p["text"],
            corner_radius=10, width=160).pack(side="right")

        row3 = ctk.CTkFrame(act_card, fg_color="transparent")
        row3.pack(fill="x", padx=16, pady=(0, 12))
        self.min_delay_var = ctk.BooleanVar(value=False)
        CheckRow(row3, p, "Не раньше чем через",
                 variable=self.min_delay_var).pack(side="left")
        self.min_delay_entry = ctk.CTkEntry(
            row3, width=50, font=F_SMALL(), fg_color=p["input"],
            border_color=p["border"], text_color=p["text"], corner_radius=8)
        self.min_delay_entry.insert(0, str(self.settings.min_delay_minutes))
        self.min_delay_entry.pack(side="left", padx=6)
        ctk.CTkLabel(row3, text="мин", font=F_SMALL(),
                     text_color=p["text_secondary"]).pack(side="left")

        self.trigger_btn = PrimaryButton(parent, p, "⚡  АКТИВИРОВАТЬ",
                                         command=self._on_trigger_button)
        self.trigger_btn.pack(fill="x")

    def _build_settings_tab(self) -> None:
        p = self.palette
        parent = self.tab_settings

        scroll = ctk.CTkScrollableFrame(parent, fg_color="transparent",
                                        corner_radius=0)
        scroll.pack(fill="both", expand=True)

        appearance = Card(scroll, p)
        appearance.pack(fill="x", pady=(0, GAP))
        ctk.CTkLabel(appearance, text="Внешний вид", font=F_BODY(),
                     text_color=p["text"]
                     ).pack(anchor="w", padx=16, pady=(12, 6))

        row_theme = ctk.CTkFrame(appearance, fg_color="transparent")
        row_theme.pack(fill="x", padx=16, pady=(0, 12))
        ctk.CTkLabel(row_theme, text="Тема:", font=F_SMALL(),
                     text_color=p["text_secondary"]).pack(side="left")
        self.theme_var = ctk.StringVar(
            value="Тёмная" if self.settings.theme == "dark" else "Светлая")
        ctk.CTkOptionMenu(
            row_theme, values=["Тёмная", "Светлая"],
            variable=self.theme_var, font=F_SMALL(), dropdown_font=F_SMALL(),
            fg_color=p["input"], button_color=p["input"],
            button_hover_color=p["border"], dropdown_fg_color=p["card"],
            dropdown_text_color=p["text"], text_color=p["text"],
            corner_radius=8, width=140,
            command=self._on_theme_change).pack(side="right")

        behavior = Card(scroll, p)
        behavior.pack(fill="x", pady=(0, GAP))
        ctk.CTkLabel(behavior, text="Поведение", font=F_BODY(),
                     text_color=p["text"]
                     ).pack(anchor="w", padx=16, pady=(12, 6))

        self.tray_var = ctk.BooleanVar(value=self.settings.minimize_to_tray)
        CheckRow(behavior, p, "Сворачивать в трей вместо закрытия",
                 variable=self.tray_var, command=self._on_tray_change
                 ).pack(anchor="w", padx=16, pady=(0, 4))

        self.autostart_var = ctk.BooleanVar(value=self.settings.autostart)
        CheckRow(behavior, p, "Запускать при входе в Windows",
                 variable=self.autostart_var,
                 command=self._on_autostart_change
                 ).pack(anchor="w", padx=16, pady=(0, 12))

        notif = Card(scroll, p)
        notif.pack(fill="x", pady=(0, GAP))
        ctk.CTkLabel(notif, text="Уведомления", font=F_BODY(),
                     text_color=p["text"]
                     ).pack(anchor="w", padx=16, pady=(12, 6))

        row_n = ctk.CTkFrame(notif, fg_color="transparent")
        row_n.pack(fill="x", padx=16, pady=(0, 4))
        ctk.CTkLabel(row_n, text="Уведомить за", font=F_SMALL(),
                     text_color=p["text_secondary"]).pack(side="left")
        self.notify_before_var = ctk.StringVar(
            value=str(self.settings.notify_before_sec))
        ctk.CTkEntry(row_n, textvariable=self.notify_before_var, width=60,
                     font=F_SMALL(), fg_color=p["input"],
                     border_color=p["border"], text_color=p["text"],
                     corner_radius=8).pack(side="left", padx=6)
        ctk.CTkLabel(row_n, text="сек (0 = отключено)", font=F_SMALL(),
                     text_color=p["text_secondary"]).pack(side="left")

        self.sound_var = ctk.BooleanVar(value=self.settings.sound_on_action)
        CheckRow(notif, p, "Звуковой сигнал при срабатывании",
                 variable=self.sound_var, command=self._on_sound_change
                 ).pack(anchor="w", padx=16, pady=(4, 12))

        upd = Card(scroll, p)
        upd.pack(fill="x", pady=(0, GAP))
        ctk.CTkLabel(upd, text=f"Обновления  ·  v{__version__}",
                     font=F_BODY(), text_color=p["text"]
                     ).pack(anchor="w", padx=16, pady=(12, 6))

        ctk.CTkLabel(upd, text="GitHub repo (owner/name):", font=F_SMALL(),
                     text_color=p["text_secondary"], anchor="w"
                     ).pack(fill="x", padx=16, pady=(0, 2))
        self.repo_entry = ctk.CTkEntry(
            upd, placeholder_text="user/shutdown-timer",
            font=F_SMALL(), height=32, corner_radius=8,
            fg_color=p["input"], border_color=p["border"],
            text_color=p["text"])
        self.repo_entry.pack(fill="x", padx=16, pady=(0, 6))
        if self.settings.github_repo:
            self.repo_entry.insert(0, self.settings.github_repo)

        self.check_upd_var = ctk.BooleanVar(
            value=self.settings.check_updates_on_start)
        CheckRow(upd, p, "Проверять при запуске",
                 variable=self.check_upd_var,
                 command=self._on_check_updates_change
                 ).pack(anchor="w", padx=16, pady=(0, 6))

        self.update_banner = ctk.CTkLabel(
            upd, text="", font=F_SMALL(), text_color=p["success"],
            anchor="w", justify="left")

        row_upd = ctk.CTkFrame(upd, fg_color="transparent")
        row_upd.pack(fill="x", padx=16, pady=(0, 8))
        GhostButton(row_upd, p, "Проверить сейчас",
                    command=lambda: self._check_updates(silent=False)
                    ).pack(side="left")

        self.update_btn = PrimaryButton(
            upd, p, "⬇  ОБНОВИТЬ", command=self._start_update)

        self.update_progress = ctk.CTkProgressBar(
            upd, height=6, corner_radius=3,
            fg_color=p["track"], progress_color=p["accent"])

        mob = Card(scroll, p)
        mob.pack(fill="x", pady=(0, GAP))

        ctk.CTkLabel(mob, text="Синхронизация с телефоном",
                     font=F_BODY(), text_color=p["text"]
                     ).pack(anchor="w", padx=16, pady=(12, 4))

        ctk.CTkLabel(
            mob,
            text="Телефон должен быть в той же Wi-Fi сети.\n"
                 "Порт и PIN подобраны автоматически для твоего ПК.",
            font=F_SMALL(), text_color=p["text_secondary"],
            anchor="w", justify="left"
        ).pack(fill="x", padx=16, pady=(0, 8))

        self.mobile_enabled_var = ctk.BooleanVar(
            value=self.settings.mobile_sync_enabled)
        CheckRow(mob, p, "Включить мобильную синхронизацию",
                 variable=self.mobile_enabled_var,
                 command=self._on_mobile_toggle
                 ).pack(anchor="w", padx=16, pady=(0, 8))

        row_p = ctk.CTkFrame(mob, fg_color="transparent")
        row_p.pack(fill="x", padx=16, pady=(0, 4))
        ctk.CTkLabel(row_p, text="Порт:", font=F_SMALL(),
                     text_color=p["text_secondary"]).pack(side="left")
        self.mobile_port_var = ctk.StringVar(
            value=str(self.settings.mobile_sync_port))
        self.mobile_port_entry = ctk.CTkEntry(
            row_p, textvariable=self.mobile_port_var, width=80,
            font=F_SMALL(), fg_color=p["input"],
            border_color=p["border"], text_color=p["text"],
            corner_radius=8)
        self.mobile_port_entry.pack(side="left", padx=6)
        GhostButton(row_p, p, "🎲",
                    command=self._regen_port).pack(side="left")

        pin_card = ctk.CTkFrame(mob, fg_color=p["input"],
                                corner_radius=12)
        pin_card.pack(fill="x", padx=16, pady=(6, 4))

        pin_row = ctk.CTkFrame(pin_card, fg_color="transparent")
        pin_row.pack(fill="x", padx=14, pady=10)

        ctk.CTkLabel(pin_row, text="PIN", font=F_SMALL(),
                     text_color=p["text_secondary"]).pack(side="left")

        self.mobile_pin_label = ctk.CTkLabel(
            pin_row, text=self.settings.mobile_sync_pin or "—",
            font=F(24, "bold"), text_color=p["accent"])
        self.mobile_pin_label.pack(side="left", padx=(10, 0))

        GhostButton(pin_row, p, "🎲",
                    command=self._regen_pin).pack(side="right")

        ctk.CTkLabel(mob, text="Ссылка для телефона:",
                     font=F_SMALL(), text_color=p["text_secondary"],
                     anchor="w").pack(fill="x", padx=16, pady=(8, 2))

        self.mobile_url_label = ctk.CTkLabel(
            mob, text="—", font=F(13, "bold"),
            text_color=p["accent"], anchor="w", cursor="hand2",
            wraplength=340, justify="left")
        self.mobile_url_label.pack(fill="x", padx=16, pady=(0, 4))
        self.mobile_url_label.bind(
            "<Button-1>", lambda e: self._open_mobile_url())

        ctk.CTkLabel(mob, text="💡 Нажми на ссылку чтобы скопировать",
                     font=F_SMALL(), text_color=p["text_secondary"],
                     anchor="w").pack(fill="x", padx=16, pady=(0, 12))

        diag = Card(scroll, p)
        diag.pack(fill="x", pady=(0, GAP))
        ctk.CTkLabel(diag, text="Диагностика", font=F_BODY(),
                     text_color=p["text"]
                     ).pack(anchor="w", padx=16, pady=(12, 6))

        row_diag = ctk.CTkFrame(diag, fg_color="transparent")
        row_diag.pack(fill="x", padx=16, pady=(0, 12))
        GhostButton(row_diag, p, "Открыть лог",
                    command=self._open_log).pack(side="left", padx=(0, 6))
        GhostButton(row_diag, p, "Папка данных",
                    command=self._open_data_folder).pack(side="left", padx=(0, 6))
        GhostButton(row_diag, p, "Сброс",
                    command=self._reset_settings).pack(side="left")

    def _on_tab_change(self, value: str) -> None:
        self.tab_time.pack_forget()
        self.tab_events.pack_forget()
        self.tab_settings.pack_forget()
        if value == "⏱ Время":
            self.tab_time.pack(fill="both", expand=True)
        elif value == "⚡ События":
            self.tab_events.pack(fill="both", expand=True)
            self._refresh_steam_downloads()
        else:
            self.tab_settings.pack(fill="both", expand=True)

    def _on_theme_change(self, value: str) -> None:
        new_theme = "dark" if value == "Тёмная" else "light"
        if new_theme == self.settings.theme:
            return
        self.settings.theme = new_theme
        self.settings.save()
        self.theme_btn.configure(
            text="🌙" if new_theme == "dark" else "☀")
        messagebox.showinfo("Тема",
                            "Перезапустите приложение для применения темы.",
                            parent=self)

    def _on_tray_change(self) -> None:
        self.settings.minimize_to_tray = bool(self.tray_var.get())
        self.settings.save()

    def _on_autostart_change(self) -> None:
        enabled = bool(self.autostart_var.get())
        ok = set_autostart(enabled)
        if not ok:
            self.autostart_var.set(not enabled)
            messagebox.showerror("Ошибка",
                                 "Не удалось изменить автозапуск.",
                                 parent=self)
            return
        self.settings.autostart = enabled
        self.settings.save()

    def _sync_autostart_ui(self) -> None:
        real = is_autostart_enabled()
        if real != self.autostart_var.get():
            self.autostart_var.set(real)
            self.settings.autostart = real
            self.settings.save()

    def _on_sound_change(self) -> None:
        self.settings.sound_on_action = bool(self.sound_var.get())
        self.settings.save()

    def _on_check_updates_change(self) -> None:
        self.settings.check_updates_on_start = bool(self.check_upd_var.get())
        self.settings.save()

    def _save_notify_before(self) -> None:
        try:
            v = int(self.notify_before_var.get())
            v = max(0, min(v, 3600))
        except ValueError:
            v = 60
        self.settings.notify_before_sec = v
        self.notify_before_var.set(str(v))
        self.settings.save()

    def _save_repo(self) -> None:
        v = self.repo_entry.get().strip()
        v = re.sub(r"^https?://github\.com/", "", v)
        v = re.sub(r"\.git$", "", v)
        v = v.strip("/")
        if v != self.settings.github_repo:
            self.settings.github_repo = v
            self.settings.save()

    def _open_log(self) -> None:
        try:
            os.startfile(str(LOG_PATH))
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось открыть лог:\n{e}",
                                 parent=self)

    def _open_data_folder(self) -> None:
        try:
            os.startfile(str(APP_DIR))
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось открыть папку:\n{e}",
                                 parent=self)

    def _reset_settings(self) -> None:
        if not messagebox.askyesno(
                "Сброс настроек",
                "Сбросить все настройки к значениям по умолчанию?",
                parent=self):
            return
        try:
            CONFIG_PATH.unlink(missing_ok=True)
        except Exception:
            pass
        messagebox.showinfo("Сброс",
                            "Настройки сброшены. Перезапустите приложение.",
                            parent=self)

    def _check_updates(self, silent: bool = True) -> None:
        try:
            self._save_repo()
        except Exception:
            pass

        repo = self.settings.github_repo
        if not repo:
            if not silent:
                messagebox.showinfo(
                    "Обновления",
                    "Укажите GitHub repo в формате owner/name\n"
                    "например: myusername/shutdown-timer",
                    parent=self)
            return

        def _work():
            try:
                info = check_github_release(repo)
            except urllib.error.HTTPError as e:
                log.warning("update check HTTP: %s", e)
                if not silent:
                    self.after(0, lambda: messagebox.showerror(
                        "Обновления",
                        f"GitHub API вернул {e.code}.\n"
                        f"Возможно у репозитория нет релизов.",
                        parent=self))
                return
            except Exception as e:
                log.exception("update check")
                if not silent:
                    self.after(0, lambda: messagebox.showerror(
                        "Обновления", f"Ошибка проверки:\n{e}", parent=self))
                return

            if info is None:
                if not silent:
                    self.after(0, lambda: messagebox.showinfo(
                        "Обновления", "Релиз не найден", parent=self))
                return

            if is_newer_version(info["version"], __version__):
                self.after(0, lambda: self._show_update_available(info))
            else:
                if not silent:
                    self.after(0, lambda: messagebox.showinfo(
                        "Обновления",
                        f"У вас последняя версия ({__version__}).",
                        parent=self))

        threading.Thread(target=_work, daemon=True).start()

    def _show_update_available(self, info: dict) -> None:
        self._update_info = info
        text = f"Доступна версия {info['version']} (у вас {__version__})"
        self.update_banner.configure(text=text)
        self.update_banner.pack(fill="x", padx=16, pady=(6, 4),
                                before=self.update_btn)
        if info.get("exe_url"):
            self.update_btn.configure(text=f"⬇  ОБНОВИТЬ ДО {info['version']}")
            self.update_btn.pack(fill="x", padx=16, pady=(0, 8))
        else:
            self.update_btn.configure(text="Открыть страницу релиза")
            self.update_btn.pack(fill="x", padx=16, pady=(0, 8))
        notify("Shutdown Timer", f"Доступно обновление {info['version']}")

    def _start_update(self) -> None:
        if self._update_downloading:
            return
        info = self._update_info
        if not info:
            if self.settings.github_repo:
                webbrowser.open(
                    f"https://github.com/{self.settings.github_repo}/releases")
            return

        exe_url = info.get("exe_url")
        if not exe_url:
            webbrowser.open(info.get("html_url", ""))
            return

        if not getattr(sys, "frozen", False):
            messagebox.showinfo(
                "Обновление",
                "Авто-обновление работает только для собранного .exe.\n"
                "Сейчас запущен .py-скрипт — откроется страница релиза.",
                parent=self)
            webbrowser.open(info.get("html_url", ""))
            return

        self._update_downloading = True
        self.update_btn.configure(text="Скачивание…", state="disabled")
        self.update_progress.set(0)
        self.update_progress.pack(fill="x", padx=16, pady=(0, 8))

        def _progress(got: int, total: int) -> None:
            if total > 0:
                self.after(0, lambda: self.update_progress.set(got / total))

        def _work():
            try:
                tmp = Path(tempfile.gettempdir()) / "ShutdownTimer_new.exe"
                log.info("Downloading update from %s -> %s", exe_url, tmp)
                _download_file(exe_url, tmp, progress_cb=_progress)
                log.info("Download done, size=%s", tmp.stat().st_size)
                ok = _schedule_exe_replace(tmp)
                if not ok:
                    raise RuntimeError("Не удалось запустить обновление")
                self.after(0, self._quit_for_update)
            except Exception as e:
                log.exception("update download")
                self._update_downloading = False
                self.after(0, lambda: self._update_failed(e))

        threading.Thread(target=_work, daemon=True).start()

    def _update_failed(self, e: Exception) -> None:
        try:
            self.update_progress.pack_forget()
            self.update_btn.configure(text="⬇  ОБНОВИТЬ", state="normal")
        except Exception:
            pass
        messagebox.showerror("Обновление",
                             f"Не удалось обновить:\n{e}", parent=self)

    def _quit_for_update(self) -> None:
        log.info("Quitting for update")
        try:
            self.timer.stop()
            if self._active_trigger:
                self._active_trigger.stop()
            self.mobile_server.stop()
            self.tray.stop()
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass
        os._exit(0)

    def _on_steam_select(self, label: str) -> None:
        appid = self._steam_items.get(label, "")
        if appid:
            self._selected_appid = appid
            self.settings.last_steam_appid = appid
            self.settings.save()

    def _refresh_steam_downloads(self) -> None:
        try:
            items = list_steam_downloads()
        except Exception:
            log.exception("list_steam_downloads")
            items = []

        self._steam_items = {}
        self._steam_by_appid = {it["appid"]: it for it in items}

        if not items:
            self.steam_menu.configure(values=["(нет активных загрузок)"])
            self.steam_var.set("(нет активных загрузок)")
            self.steam_progress.set(0)
            steam = find_steam_path()
            libs = find_steam_libraries()
            if not steam:
                info = "Steam не найден в реестре"
            elif not libs:
                info = f"Steam: {steam}\nБиблиотеки не найдены"
            else:
                info = f"Steam: {steam}\nЗагрузок нет"
            self.steam_info.configure(text=info)
            return

        values = []
        for it in items:
            label = it["name"]
            if label in self._steam_items:
                label = f'{it["name"]} (AppID {it["appid"]})'
            self._steam_items[label] = it["appid"]
            values.append(label)

        self.steam_menu.configure(values=values)

        desired_label = None
        for label, appid in self._steam_items.items():
            if appid == self._selected_appid:
                desired_label = label
                break
        if desired_label is None:
            desired_label = values[0]
            self._selected_appid = self._steam_items[values[0]]
        self.steam_var.set(desired_label)

        self._update_steam_progress()

    def _update_steam_progress(self) -> None:
        it = self._steam_by_appid.get(self._selected_appid)
        if not it:
            self.steam_progress.set(0)
            self.steam_info.configure(text="")
            return
        percent = it["percent"] / 100.0
        self.steam_progress.set(max(0.0, min(1.0, percent)))
        state = _state_name(it["flags"])
        if it["total"] > 0:
            info = (f'{it["percent"]:.1f}%  ·  '
                    f'{format_gb(it["downloaded"])} / {format_gb(it["total"])}'
                    f'  ·  {state}')
        else:
            info = state
        self.steam_info.configure(text=info)

    def _auto_refresh_steam(self) -> None:
        try:
            if self.segment.get() == "⚡ События":
                items = list_steam_downloads()
                self._steam_by_appid = {it["appid"]: it for it in items}
                if len(items) != len(self._steam_items):
                    self._refresh_steam_downloads()
                else:
                    current_appids = {it["appid"] for it in items}
                    if current_appids != set(self._steam_items.values()):
                        self._refresh_steam_downloads()
                    else:
                        self._update_steam_progress()
        except Exception:
            log.exception("auto_refresh_steam")
        self.after(2000, self._auto_refresh_steam)

    def _toggle_theme(self) -> None:
        new = "light" if self.settings.theme == "dark" else "dark"
        self.settings.theme = new
        self.settings.save()
        try:
            self.theme_var.set("Тёмная" if new == "dark" else "Светлая")
        except Exception:
            pass
        self.theme_btn.configure(text="🌙" if new == "dark" else "☀")
        messagebox.showinfo("Тема",
                            "Перезапустите приложение для применения темы.",
                            parent=self)

    def _minimize(self) -> None:
        if self.settings.minimize_to_tray:
            self.withdraw()
        else:
            self.iconify()

    def _show_window(self) -> None:
        self.after(0, lambda: (self.deiconify(), self.lift(), self.focus_force()))

    def _quit_app(self) -> None:
        def _do():
            try:
                self.timer.stop()
                if self._active_trigger:
                    self._active_trigger.stop()
                self.mobile_server.stop()
                self.tray.stop()
            except Exception:
                pass
            try:
                self.after(0, self.destroy)
            except Exception:
                os._exit(0)
        self.after(0, _do)

    def _on_entry_key(self, _e) -> None:
        text = self.entry.get()
        target, err = parse_time_input(text)
        p = self.palette
        if not text.strip():
            self.preview.configure(text="Введите время и нажмите Enter",
                                   text_color=p["text_secondary"])
            self.entry.configure(border_color=p["border"])
            self._last_parsed = None
            return
        if target:
            self.preview.configure(text="→ " + describe_target(target),
                                   text_color=p["text_secondary"])
            self.entry.configure(border_color=p["accent"])
            self._last_parsed = target
        else:
            self.preview.configure(text="✗ " + (err or "не распознано"),
                                   text_color=p["danger"])
            self.entry.configure(border_color=p["danger"])
            self._last_parsed = None

    def _set_preset(self, minutes: int) -> None:
        self.entry.delete(0, "end")
        if minutes < 60:
            self.entry.insert(0, f"{minutes}м")
        else:
            self.entry.insert(0, f"{minutes // 60}ч")
        self._on_entry_key(None)

    def _start_from_input(self) -> None:
        target, err = parse_time_input(self.entry.get())
        if not target:
            self.preview.configure(text="✗ " + (err or "не распознано"),
                                   text_color=self.palette["danger"])
            return
        seconds = max(1.0, (target - datetime.now()).total_seconds())
        self._start_timer(seconds)

    def _start_timer(self, seconds: float) -> None:
        try:
            self._save_notify_before()
        except Exception:
            pass

        self.settings.push_history(self.entry.get())
        self.settings.action = self.action_var.get()
        self.settings.force_close = self.force_var.get()
        self.settings.save()

        self._warned_before = False

        self.timer.start(seconds)
        self.status_label.configure(
            text="Работает · " + format_hms(seconds),
            text_color=self.palette["success"])
        self.start_btn.configure(
            text="✕  ОТМЕНИТЬ",
            fg_color=self.palette["danger"], hover_color="#FF6B60",
            command=self._cancel_all)
        self.pause_btn.pack(fill="x", pady=(GAP, 0))
        self.pause_btn.configure(text="⏸ Пауза")

    def _toggle_pause(self) -> None:
        if not self.timer.active:
            return
        if self.timer.paused:
            self.timer.resume()
            self.pause_btn.configure(text="⏸ Пауза")
            self.status_label.configure(text_color=self.palette["success"])
            self.tray.set_title(
                f"Shutdown Timer — ▶ {format_hms(self.timer._remaining or 0)}")
        else:
            self.timer.pause()
            self.pause_btn.configure(text="▶ Продолжить")
            self.status_label.configure(text_color=self.palette["warning"])
            self.tray.set_title("Shutdown Timer — ⏸ пауза")

    def _cancel_all(self) -> None:
        try:
            self.timer.stop()
        except Exception:
            pass
        if self._active_trigger:
            try:
                self._active_trigger.stop()
            except Exception:
                pass
            self._active_trigger = None
        self._trigger_kind = None
        self._reset_ui("Отменено")
        self.tray.set_title(f"Shutdown Timer v{__version__}")

    def _reset_ui(self, msg: str = "Готов к запуску") -> None:
        self.timer_label.configure(text="00:00:00")
        self.status_label.configure(text=msg,
                                    text_color=self.palette["text_secondary"])
        self.start_btn.configure(
            text="▶  ЗАПУСТИТЬ ТАЙМЕР",
            fg_color=self.palette["accent"],
            hover_color=self.palette["accent_hover"],
            command=self._on_main_button)
        self.pause_btn.pack_forget()
        self.trigger_btn.configure(
            text="⚡  АКТИВИРОВАТЬ",
            fg_color=self.palette["accent"],
            command=self._on_trigger_button)
        self._warned_before = False
        self.tray.set_title(f"Shutdown Timer v{__version__}")

    def _on_main_button(self) -> None:
        self._start_from_input()

    def _on_tick(self, remaining: float) -> None:
        if (self.settings.notify_before_sec > 0
                and not self._warned_before
                and 0 < remaining <= self.settings.notify_before_sec):
            self._warned_before = True
            secs = int(remaining)
            act = self.action_var.get()
            try:
                notify("Shutdown Timer", f"{act} через {secs} сек")
            except Exception:
                pass
            if self.settings.sound_on_action:
                play_sound()

        try:
            self.after(0, lambda: self.timer_label.configure(
                text=format_hms(remaining)))
        except Exception:
            pass

        now = time.monotonic()
        if now - self._last_tray_update >= 1.0:
            self._last_tray_update = now
            try:
                self.tray.set_title(
                    f"Shutdown Timer — осталось {format_hms(remaining)}")
            except Exception:
                pass

    def _on_finish(self) -> None:
        action = self.action_var.get()
        force = self.force_var.get()

        def _do():
            if self.settings.sound_on_action:
                play_sound()
            notify("Shutdown Timer", f"Выполняется: {action}")
            execute_action(action, force)
        self.after(0, _do)

    def _list_processes(self) -> list:
        if psutil is None:
            return []
        names = set()
        for p in psutil.process_iter(["name"]):
            try:
                n = p.info.get("name")
                if n:
                    names.add(n)
            except Exception:
                continue
        return sorted(names)[:60]

    def _refresh_processes(self) -> None:
        self.process_menu.configure(
            values=["(нет процессов)"] + self._list_processes())

    def _browse_exe(self) -> None:
        path = filedialog.askopenfilename(
            title="Выберите .exe", filetypes=[("Executable", "*.exe")],
            parent=self)
        if path:
            self.process_var.set(os.path.basename(path))

    def _on_trigger_button(self) -> None:
        if self._active_trigger:
            self._cancel_all()
            return

        kind = self.trig_var.get()
        action = self.event_action_var.get()
        min_delay_sec = 0
        if self.min_delay_var.get():
            try:
                min_delay_sec = int(self.min_delay_entry.get()) * 60
            except ValueError:
                min_delay_sec = 0

        def _fire():
            if self.settings.sound_on_action:
                play_sound()
            notify("Shutdown Timer", f"Событие сработало: {kind}")
            execute_action(action, self.force_var.get())
            self.after(0, lambda: self._reset_ui("Событие сработало"))
            self.after(0, self._refresh_steam_downloads)
            self._active_trigger = None
            self._trigger_kind = None

        try:
            if kind == "steam_app":
                appid = self._selected_appid
                if not appid or appid not in self._steam_by_appid:
                    messagebox.showwarning(
                        "Steam",
                        "Нет активных загрузок.\n\n"
                        "1. Начни качать игру в Steam\n"
                        "2. Нажми ↻",
                        parent=self)
                    return
                inner = SteamAppDoneTrigger(_fire, appid)
            elif kind == "process_exit":
                name = self.process_var.get()
                if not name or name.startswith("("):
                    messagebox.showwarning("Внимание", "Выберите процесс",
                                           parent=self)
                    return
                inner = ProcessExitTrigger(_fire, name)
            elif kind == "battery":
                try:
                    thr = int(self.battery_var.get())
                except Exception:
                    thr = 20
                inner = BatteryTrigger(_fire, thr)
            else:
                return
        except Exception as e:
            messagebox.showerror("Ошибка", str(e), parent=self)
            return

        trigger = MinDelayTrigger(_fire, inner, min_delay_sec) \
            if min_delay_sec > 0 else inner
        trigger.start()
        self._active_trigger = trigger
        self._trigger_kind = kind
        self.trigger_btn.configure(text="✕  ОТМЕНИТЬ ТРИГГЕР",
                                   fg_color=self.palette["danger"])
        self.status_label.configure(text="Ожидание события…",
                                    text_color=self.palette["warning"])
        self.tray.set_title("Shutdown Timer — ожидание события…")

    def _call_in_main(self, fn, timeout: float = 3.0):
        result = {"value": None, "done": threading.Event()}

        def wrapper():
            try:
                result["value"] = fn()
            except Exception:
                log.exception("_call_in_main")
            finally:
                result["done"].set()

        try:
            self.after(0, wrapper)
        except Exception:
            return None
        result["done"].wait(timeout=timeout)
        return result["value"]

    def _mobile_status(self) -> dict:
        def _read():
            active = self.timer.active
            remaining = 0.0
            if active and self.timer._end_time is not None:
                remaining = max(0.0, self.timer._end_time - time.monotonic())
            return {
                "version": __version__,
                "active": bool(active),
                "paused": bool(self.timer.paused),
                "remaining": remaining,
                "remaining_hms": format_hms(remaining) if active else "00:00:00",
                "action": self.action_var.get(),
                "all_actions": ALL_ACTIONS,
                "history": list(self.settings.history[:5]),
            }
        return self._call_in_main(_read) or {
            "version": __version__, "active": False,
            "paused": False, "remaining": 0.0,
            "remaining_hms": "00:00:00", "action": "Выключить",
            "all_actions": ALL_ACTIONS, "history": [],
        }

    def _mobile_start(self, seconds=None, text: str = "") -> dict:
        def _do():
            try:
                secs = None
                if seconds is not None:
                    try:
                        secs = float(seconds)
                    except (TypeError, ValueError):
                        secs = None
                if secs is None and text:
                    target, err = parse_time_input(text)
                    if target:
                        secs = max(1.0, (target - datetime.now()).total_seconds())
                    else:
                        return {"ok": False, "error": err or "не распознано"}
                if not secs or secs <= 0:
                    return {"ok": False, "error": "no duration"}
                if self._active_trigger:
                    try:
                        self._active_trigger.stop()
                    except Exception:
                        pass
                    self._active_trigger = None
                self._start_timer(secs)
                return {"ok": True, "seconds": secs}
            except Exception as e:
                log.exception("mobile start")
                return {"ok": False, "error": str(e)}
        return self._call_in_main(_do) or {"ok": False, "error": "timeout"}

    def _mobile_cancel(self) -> None:
        try:
            self.after(0, self._cancel_all)
        except Exception:
            pass

    def _mobile_toggle_pause(self) -> None:
        try:
            self.after(0, self._toggle_pause)
        except Exception:
            pass

    def _mobile_set_action(self, action: str) -> None:
        if action not in ALL_ACTIONS:
            return

        def _do():
            self.action_var.set(action)
            self.event_action_var.set(action)
            self.settings.action = action
            self.settings.save()

        try:
            self.after(0, _do)
        except Exception:
            pass

    def _mobile_start_server_ui(self) -> None:
        port = self.settings.mobile_sync_port
        pin = self.settings.mobile_sync_pin
        if self.mobile_server.running:
            ok = True
        else:
            ok = self.mobile_server.start()
        self._update_mobile_url_label(ok)

    def _on_mobile_toggle(self) -> None:
        enabled = bool(self.mobile_enabled_var.get())

        port = self.settings.mobile_sync_port or find_free_port()
        pin = self.settings.mobile_sync_pin or generate_pin(6)
        self.settings.mobile_sync_port = port
        self.settings.mobile_sync_pin = pin
        self.settings.mobile_sync_enabled = enabled
        self.settings.save()

        try:
            self.mobile_port_var.set(str(port))
            self.mobile_pin_label.configure(text=pin)
        except Exception:
            pass

        if enabled:
            ok = self.mobile_server.restart(port=port, pin=pin)
            if not ok:
                self.mobile_enabled_var.set(False)
                self.settings.mobile_sync_enabled = False
                self.settings.save()
                messagebox.showerror(
                    "Ошибка",
                    f"Не удалось открыть порт {port}.\n"
                    "Проверь брандмауэр Windows.",
                    parent=self)
                self._update_mobile_url_label(False)
                return
            self._update_mobile_url_label(True)
            self.tray.notify(f"Синхронизация: порт {port}")
        else:
            self.mobile_server.stop()
            self._update_mobile_url_label(False)

    def _update_mobile_url_label(self, running: bool) -> None:
        if not running:
            try:
                self.mobile_url_label.configure(
                    text="⏸ Синхронизация выключена",
                    text_color=self.palette["text_secondary"])
            except Exception:
                pass
            return
        ip = get_local_ip()
        pin = self.settings.mobile_sync_pin
        url = f"http://{ip}:{self.settings.mobile_sync_port}/"
        if pin:
            url += f"?pin={pin}"
        try:
            self.mobile_url_label.configure(
                text=url, text_color=self.palette["accent"])
        except Exception:
            pass

    def _open_mobile_url(self) -> None:
        if not self.mobile_server.running:
            return
        ip = get_local_ip()
        url = f"http://{ip}:{self.settings.mobile_sync_port}/"
        pin = self.settings.mobile_sync_pin
        if pin:
            url += f"?pin={pin}"
        try:
            self.clipboard_clear()
            self.clipboard_append(url)
            self.update()
            self.tray.notify("Ссылка скопирована в буфер")
        except Exception:
            pass

    def _regen_pin(self) -> None:
        if not messagebox.askyesno(
                "PIN",
                "Сгенерировать новый PIN?\n"
                "Старая ссылка перестанет работать.",
                parent=self):
            return
        new_pin = generate_pin(6)
        self.settings.mobile_sync_pin = new_pin
        self.settings.save()
        try:
            self.mobile_pin_label.configure(text=new_pin)
        except Exception:
            pass
        if self.mobile_server.running:
            self.mobile_server.pin = new_pin
        self._update_mobile_url_label(self.mobile_server.running)
        self.tray.notify(f"PIN обновлён: {new_pin}")

    def _regen_port(self) -> None:
        new_port = find_free_port(
            start=self.settings.mobile_sync_port + 1,
            end=self.settings.mobile_sync_port + 50)
        if new_port == self.settings.mobile_sync_port:
            new_port = find_free_port()
        self.settings.mobile_sync_port = new_port
        self.settings.save()
        try:
            self.mobile_port_var.set(str(new_port))
        except Exception:
            pass
        if self.mobile_server.running:
            ok = self.mobile_server.restart(
                port=new_port, pin=self.settings.mobile_sync_pin)
            if not ok:
                messagebox.showerror("Ошибка",
                                     f"Не удалось занять порт {new_port}",
                                     parent=self)
        self._update_mobile_url_label(self.mobile_server.running)


def _single_instance() -> bool:
    try:
        k = ctypes.windll.kernel32
        k.CreateMutexW(None, False, "ShutdownTimer_SingleInstance")
        return k.GetLastError() != 183
    except Exception:
        return True


def main() -> None:
    if not _single_instance():
        try:
            ctypes.windll.user32.MessageBoxW(
                0, "Shutdown Timer уже запущен.", "Shutdown Timer", 0x40)
        except Exception:
            pass
        return

    setup_logging()
    log.info("=== Shutdown Timer v%s запущен ===", __version__)
    log.info("Assets dir: %s (exists=%s)", ASSETS_DIR, ASSETS_DIR.is_dir())
    log.info("Frozen: %s", getattr(sys, "frozen", False))
    try:
        app = ShutdownTimerApp()
        app.mainloop()
    except Exception:
        log.exception("Фатальная ошибка")
        raise
    finally:
        log.info("=== Shutdown Timer завершён ===")


if __name__ == "__main__":
    main()