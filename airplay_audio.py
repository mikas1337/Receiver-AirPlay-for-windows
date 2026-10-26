#!/usr/bin/env python3
"""
Минимальный AirPlay 1 (RAOP) аудио-приёмник.

Установка (консольная версия):
    pip install zeroconf pycryptodome av sounddevice numpy
Для приложения с окном (app.py) дополнительно:
    pip install customtkinter pystray pillow

Запуск:
    python airplay_audio.py "Мой AirPlay"

Что делает:
  1. Объявляет себя в сети через mDNS (_raop._tcp) -> появляется в списке AirPlay на iPhone/Mac.
  2. Принимает RTSP-сессию (OPTIONS/ANNOUNCE/SETUP/RECORD/FLUSH/SET_PARAMETER/TEARDOWN).
  3. Принимает RTP/UDP-пакеты, расшифровывает (AES-128-CBC), декодирует ALAC, играет через sounddevice.

Ключ (необязательно, но рекомендуется):
  Многие устройства Apple требуют «рукопожатие» с RSA-ключом AirPort Express.
  Это ПРИВАТНЫЙ ключ (им подписывают ответ и расшифровывают ключ потока); он давно опубликован
  в исходниках shairport-sync (файл common.c, переменная super_secret_key, лицензия MIT).
  Сохраните его как airport_key.pem рядом со скриптом. Без него приёмник объявляет
  только незашифрованный режим (et=0) - часть устройств к нему не подключится.
"""
import argparse
import asyncio
import base64
import json
import os
import re
import socket
import struct
import sys
import threading
import time
import traceback
import uuid

import av
import numpy as np
import sounddevice as sd
from Crypto.Cipher import AES, PKCS1_OAEP
from Crypto.PublicKey import RSA
from zeroconf import ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

DEFAULT_NAME = "Windows AirPlay"
NAME = DEFAULT_NAME  # реально выбирается в resolve_name() при запуске

VERBOSE = False       # подробный вывод (флаг --debug)

# Режимы задержки: сколько звука копить перед стартом (prebuf, в пакетах по ~8 мс)
# и какой буфер просить у звукового устройства (device; est - оценка в секундах).
LATENCY_MODE = "normal"
LATENCY_MODES = {
    "min":    {"prebuf": 10, "device": "low",  "est": 0.09},   # ~0,2 с: быстрая реакция; буфер сам растёт, если Wi-Fi неровный
    "normal": {"prebuf": 20, "device": "low",  "est": 0.09},   # ~0,25 с: баланс
    "safe":   {"prebuf": 60, "device": "high", "est": 0.18},   # ~0,65 с: максимум устойчивости
}
FRAMES_PER_PACKET = 352


def latency_params():
    return LATENCY_MODES.get(LATENCY_MODE, LATENCY_MODES["normal"])


def estimated_latency_samples(rate: int = 44100) -> int:
    """Сколько сэмплов задержки мы честно сообщаем источнику (заголовок Audio-Latency)."""
    m = latency_params()
    return int(m["prebuf"] * FRAMES_PER_PACKET + m["est"] * rate)

OUTPUT_DEVICE_NAME = None  # имя устройства вывода; None = то, что выбрано в Windows по умолчанию
EVENT_HOOK = None     # функция(kind, data) - так интерфейс узнаёт о событиях


def emit(kind, data=None):
    """Сообщить интерфейсу о событии (started, stopped, connected, disconnected, volume, error, log)."""
    if EVENT_HOOK:
        try:
            EVENT_HOOK(kind, data)
        except Exception:
            pass


def log(msg, debug=False):
    """Вывести сообщение в консоль и (если подключён интерфейс) в журнал окна.

    Подробные строки (debug=True) показываются только при запуске с --debug.
    """
    if debug and not VERBOSE:
        return
    try:
        print(msg)
    except Exception:
        # консоль с урезанной кодировкой (cp866 и т. п.) не должна ронять приёмник
        try:
            print(str(msg).encode("ascii", "replace").decode("ascii"))
        except Exception:
            pass
    emit("log", msg)


PORT = 5000
IP_WATCH_INTERVAL = 10.0  # как часто проверять, не сменился ли IP-адрес компьютера (секунды)


def _find_key_file():
    # Ищем ключ рядом с exe/скриптом, а в собранном exe - ещё и внутри упакованных данных
    dirs = []
    if getattr(sys, "frozen", False):
        dirs.append(os.path.dirname(sys.executable))
        if getattr(sys, "_MEIPASS", None):
            dirs.append(sys._MEIPASS)
    else:
        dirs.append(os.path.dirname(os.path.abspath(__file__)))
    for d in dirs:
        path = os.path.join(d, "airport_key.pem")
        if os.path.exists(path):
            return path
    return os.path.join(dirs[0], "airport_key.pem")


KEY_FILE = _find_key_file()
KEY = None
KEY_ERROR = None   # текст ошибки, если файл ключа есть, но не читается
try:
    if os.path.exists(KEY_FILE):
        with open(KEY_FILE, "rb") as _f:
            KEY = RSA.import_key(_f.read())
        if not KEY.has_private():
            KEY, KEY_ERROR = None, "в airport_key.pem не приватный ключ"
except Exception as _e:  # повреждённый файл не должен ронять приложение при старте
    KEY, KEY_ERROR = None, f"{type(_e).__name__}: {_e}"


# --------------------------------------------------------------------------- #
#  Имя устройства: сохраняется в config.json, меняется при запуске
# --------------------------------------------------------------------------- #
def config_path() -> str:
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "WindowsAirPlay", "config.json")


def load_config() -> dict:
    try:
        with open(config_path(), encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(**updates):
    path = config_path()
    data = load_config()
    data.update(updates)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except OSError as e:
        log(f"Не удалось сохранить настройки ({e}).")


def get_device_mac() -> bytes:
    """Постоянный идентификатор приёмника (6 байт, как MAC).

    Берётся один раз и запоминается: иначе при нескольких сетевых адаптерах (VPN, виртуальные
    карты) он мог бы меняться между запусками, и iPhone считал бы приёмник новым устройством.
    """
    cfg = load_config()
    saved = cfg.get("device_id")
    try:
        if isinstance(saved, str) and len(saved) == 12:
            return bytes.fromhex(saved)
    except ValueError:
        pass
    node = uuid.getnode()
    if (node >> 40) & 1:  # uuid не нашёл настоящий MAC и выдал случайное число - делаем «локальный» адрес
        node = (node & ~(1 << 40)) | (1 << 41)
    mac = node.to_bytes(6, "big")
    save_config(device_id=mac.hex())
    return mac


MAC = get_device_mac()
MAC_HEX = MAC.hex().upper()


def load_saved_name():
    return load_config().get("name")


def save_name(name: str):
    save_config(name=name)


PA_LOCK = threading.RLock()
OPEN_STREAMS = 0  # сколько звуковых потоков сейчас открыто нами
ZOMBIES = []      # потоки закрытия, которые не завершились (драйвер завис): с ними PortAudio не перезапускаем


def refresh_audio_devices(wait: float = 1.0) -> bool:
    """Обновить список устройств и «устройство по умолчанию».

    PortAudio запоминает их один раз при старте программы. Если пользователь потом переключил
    вывод (в Windows, FxSound и т. п.), без обновления мы бы продолжали играть на старое устройство.
    Обновлять можно только когда нет открытых потоков, поэтому при необходимости чуть подождём.
    """
    deadline = time.time() + wait
    while True:
        with PA_LOCK:
            ZOMBIES[:] = [z for z in ZOMBIES if z.is_alive()]
            if ZOMBIES:
                # где-то внутри PortAudio завис вызов закрытия: перезапуск библиотеки мог бы зависнуть насовсем
                log("[диагностика] список устройств не обновлён: драйвер ещё закрывает старый поток", debug=True)
                return False
            if OPEN_STREAMS == 0:
                try:
                    sd._terminate()
                except Exception as e:
                    log(f"[диагностика] не удалось остановить PortAudio: {e!r}", debug=True)
                try:
                    sd._initialize()   # после _terminate библиотека обязана быть запущена снова
                    return True
                except Exception as e:
                    log(f"[диагностика] не удалось обновить список устройств: {e!r}", debug=True)
                    return False
        if time.time() >= deadline:
            return False
        time.sleep(0.05)


MAPPER_PREFIX = "Microsoft Sound Mapper"


def list_output_devices():
    """Устройства вывода звука [(индекс, имя)] для основного аудио-API Windows."""
    with PA_LOCK:
        try:
            api = sd.query_hostapis(sd.default.hostapi)
            result = []
            for i in api["devices"]:
                d = sd.query_devices(i)
                if d["max_output_channels"] > 0 and not d["name"].startswith(MAPPER_PREFIX):
                    result.append((i, d["name"]))
            return result
        except Exception:
            return []


def mapper_device_index():
    """Индекс «Microsoft Sound Mapper» (только Windows/MME).

    Это не конкретное устройство, а «то, что Windows считает устройством по умолчанию прямо сейчас».
    Открывая поток через него, мы получаем актуальное устройство без перезагрузки списка устройств,
    поэтому переключение (например, в FxSound) обрабатывается быстро.
    """
    if sys.platform != "win32":
        return None
    with PA_LOCK:
        try:
            api = sd.query_hostapis(sd.default.hostapi)
            for i in api["devices"]:
                d = sd.query_devices(i)
                if d["max_output_channels"] > 0 and d["name"].startswith(MAPPER_PREFIX):
                    return i
        except Exception:
            pass
    return None


def find_device_index(name):
    if not name:
        return None
    for i, n in list_output_devices():
        if n == name:
            return i
    return None


def default_output_signature():
    """Имя текущего устройства вывода по умолчанию в Windows (через winmm) или None, если не узнать.

    Нужно, чтобы заметить переключение устройства во время воспроизведения.
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        winmm = ctypes.windll.winmm
        winmm.waveOutMessage.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p]
        winmm.waveOutMessage.restype = wintypes.UINT
        dev_id, flags = wintypes.DWORD(0), wintypes.DWORD(0)
        DRVM_MAPPER_PREFERRED_GET = 0x2015
        ok = False
        for wave_mapper in (0xFFFFFFFF, -1):   # WAVE_MAPPER = (UINT)-1; на разных системах принимается по-разному
            if winmm.waveOutMessage(ctypes.c_void_p(wave_mapper), DRVM_MAPPER_PREFERRED_GET,
                                    ctypes.byref(dev_id), ctypes.byref(flags)) == 0:
                ok = True
                break
        if not ok or dev_id.value == 0xFFFFFFFF:
            return None

        class WAVEOUTCAPSW(ctypes.Structure):
            _fields_ = [("wMid", wintypes.WORD), ("wPid", wintypes.WORD),
                        ("vDriverVersion", wintypes.UINT), ("szPname", wintypes.WCHAR * 32),
                        ("dwFormats", wintypes.DWORD), ("wChannels", wintypes.WORD),
                        ("wReserved1", wintypes.WORD), ("dwSupport", wintypes.DWORD)]

        caps = WAVEOUTCAPSW()
        winmm.waveOutGetDevCapsW.argtypes = [ctypes.c_size_t, ctypes.POINTER(WAVEOUTCAPSW), wintypes.UINT]
        winmm.waveOutGetDevCapsW.restype = wintypes.UINT
        if winmm.waveOutGetDevCapsW(dev_id.value, ctypes.byref(caps), ctypes.sizeof(caps)) != 0:
            return None
        return caps.szPname
    except Exception:
        return None


def clean_name(raw: str) -> str:
    """Имя в mDNS - это метка до 63 байт, из них 13 занимает префикс 'AABBCCDDEEFF@'."""
    name = " ".join(raw.replace(".", " ").split())  # без точек и лишних пробелов
    limit = 63 - 13
    while len(name.encode("utf-8")) > limit:
        name = name[:-1]
    return name.strip()


def resolve_name(cli_name, rename: bool) -> str:
    if cli_name:  # имя передано в командной строке
        name = clean_name(cli_name)
        if name:
            save_name(name)
            return name
    saved = load_saved_name()
    if saved and not rename:
        return saved
    current = saved or DEFAULT_NAME
    if sys.stdin and sys.stdin.isatty():
        print("Как назвать это устройство в списке AirPlay?")
        raw = input(f"Название [{current}]: ")
        name = clean_name(raw) or current
    else:
        name = current
    save_name(name)
    return name


STATS = {"rtp": 0, "played": 0, "errors": 0, "lost": 0, "underruns": 0, "trimmed": 0}


def b64d(s: str) -> bytes:
    s = s.strip()
    return base64.b64decode(s + "=" * (-len(s) % 4))


def local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


# --------------------------------------------------------------------------- #
#  Метаданные трека: DMAP (название, исполнитель...), обложка, позиция
# --------------------------------------------------------------------------- #
DMAP_CONTAINERS = {b"mlit", b"cmst", b"mdcl"}          # вложенные «коробки» DMAP
DMAP_TEXT = {b"minm": "title", b"asar": "artist", b"asal": "album", b"asgn": "genre"}
MAX_COVER_BYTES = 8 * 1024 * 1024


def parse_dmap(body: bytes, depth: int = 0) -> dict:
    """Разобрать DMAP: последовательность «тег (4 байта), длина (4 байта, big-endian), значение».

    Берём название (minm), исполнителя (asar), альбом (asal), жанр (asgn) и длительность в мс (astm).
    Повреждённые или обрезанные данные не вызывают ошибок: возвращается то, что удалось прочитать.
    """
    out = {}
    n = len(body)
    off = 0
    while off + 8 <= n and depth < 4:
        tag = body[off:off + 4]
        ln = struct.unpack(">I", body[off + 4:off + 8])[0]
        off += 8
        if ln > n - off:
            break  # длина выходит за пределы данных - обрезано
        val = body[off:off + ln]
        off += ln
        if tag in DMAP_CONTAINERS:
            out.update(parse_dmap(val, depth + 1))
        elif tag in DMAP_TEXT:
            text = val.decode("utf-8", "replace").strip("\x00").strip()
            if text:
                out[DMAP_TEXT[tag]] = text
        elif tag == b"astm" and ln == 4:
            out["duration"] = struct.unpack(">I", val)[0] / 1000.0
    return out


def parse_progress(text: bytes, rate: int = 44100):
    """«progress: начало/текущая/конец» (RTP-метки) -> (позиция, длительность) в секундах или None."""
    m = re.search(rb"progress:\s*(\d+)/(\d+)/(\d+)", text)
    if not m or rate <= 0:
        return None
    start, cur, end = (int(x) for x in m.groups())
    pos = ((cur - start) & 0xFFFFFFFF) / rate
    dur = ((end - start) & 0xFFFFFFFF) / rate
    if not (0 < dur < 12 * 3600) or pos > dur + 5:
        return None  # похоже на мусор (например, перехлёст счётчика)
    return pos, dur


# --------------------------------------------------------------------------- #
#  Плеер: буфер пакетов -> ALAC -> звуковая карта
# --------------------------------------------------------------------------- #
class Player:
    PREBUF = None  # None = брать из режима задержки (LATENCY_MODE); число = принудительно (для тестов)
    MAX_TARGET = 60        # потолок адаптивного буфера, пакетов (~0,5 с)
    GROW_STEP = 6          # на сколько пакетов (~48 мс) увеличиваем буфер после нехватки пакетов
    SHRINK_AFTER = 30.0    # сколько секунд без нехватки пакетов нужно, чтобы начать уменьшать буфер
    SHRINK_EVERY = 15.0    # и как часто уменьшать (на 2 пакета)
    TRIM_AT = 12           # очередь больше запаса на столько пакетов (~96 мс): сбрасываем лишнее в тихих местах
    TRIM_FORCE = 30        # а если больше на столько (~240 мс): перескакиваем вперёд один раз, любой ценой

    @property
    def prebuf(self) -> int:
        return self.PREBUF if self.PREBUF is not None else latency_params()["prebuf"]

    def __init__(self, fmtp: str):
        # fmtp: "96 352 0 16 40 10 14 2 255 0 0 44100"
        v = [int(x) for x in fmtp.split()[1:]]
        frame, compat, bits, pb, mb, kb, ch, maxrun, maxbytes, bitrate, rate = v
        extra = struct.pack(">I4sI", 36, b"alac", 0) + struct.pack(
            ">IBBBBBBHIII", frame, compat, bits, pb, mb, kb, ch, maxrun, maxbytes, bitrate, rate
        )
        self.ctx = av.CodecContext.create("alac", "r")
        self.ctx.extradata = extra
        self.rate, self.ch, self.frame = rate, ch, frame
        self._signature = None
        self._opened_name = None   # какое устройство (по имени) было выбрано при открытии потока
        self._opened_mode = None   # какой режим задержки был при открытии потока
        self._reset_device = False  # попросили сбросить буфер звукового устройства (после FLUSH)
        self._sounding = False      # звук реально идёт (для индикации и отсчёта времени в окне)
        self._flush_gen = 0         # номер «поколения»: растёт при каждом FLUSH (см. _set_sounding)
        self.floor_ts = None       # пакеты старше этой RTP-метки (из FLUSH) выбрасываем
        self.floor_seq = None
        self._floor_drops = 0
        self._target = self.prebuf     # текущий (адаптивный) размер буфера в пакетах
        self._last_trouble = time.time()
        self._last_grow_log = 0.0
        self._last_underflow = 0.0
        self.device_label = "?"

        self.buf = {}
        self.next = None
        self.last = None
        self.lock = threading.Lock()
        self.gain = 0.3
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def push(self, seq: int, data: bytes, ts=None):
        with self.lock:
            # после FLUSH источник присылает метку: всё, что старше, - остатки прежнего звука
            stale = False
            if ts is not None and self.floor_ts is not None:
                stale = ((ts - self.floor_ts) & 0xFFFFFFFF) > 0x7FFFFFFF
            elif self.floor_seq is not None:
                stale = ((seq - self.floor_seq) & 0xFFFF) > 0x7FFF
            if stale:
                self._floor_drops += 1
                if self._floor_drops > 100:  # предохранитель: метка не та - не оставляем без звука
                    self.floor_ts = self.floor_seq = None
                    self._floor_drops = 0
                else:
                    return
            else:
                self._floor_drops = 0
            if self.last is None:
                ext = seq
            else:
                ext = self.last + ((seq - (self.last & 0xFFFF) + 32768) % 65536 - 32768)
            self.last = ext
            if self.next is None or ext >= self.next:
                self.buf[ext] = data

    def flush(self, until_ts=None, until_seq=None):
        """Пауза/перемотка/смена ролика: выбросить накопленное и сбросить буфер звукового устройства."""
        with self.lock:
            self.buf.clear()
            self.next = None
            self.floor_ts = until_ts
            self.floor_seq = until_seq
            self._floor_drops = 0
            self._flush_gen += 1
        self._reset_device = True
        self._set_sounding(False)

    def _set_sounding(self, value: bool):
        if self._sounding != value:
            self._sounding = value
            emit("play_state", value)

    def set_volume_db(self, db: float):
        # AirPlay: от -30 dB (тихо) до 0 dB (макс), -144 = mute
        self.gain = 0.0 if db <= -100 else 10 ** (db / 20.0)

    def stop(self):
        self.running = False

    def _decode(self, data: bytes):
        out = []
        for fr in self.ctx.decode(av.Packet(data)):
            a = fr.to_ndarray()
            a = a.T if fr.format.is_planar else a.reshape(-1, self.ch)
            # звук приводим к 16 битам: 24/32-битный ALAC дал бы шум при простом обрезании
            if a.dtype == np.int32:
                a = a >> 16
            elif a.dtype.kind == "f":
                a = np.clip(a, -1.0, 1.0) * 32767.0
            out.append(a.astype(np.int16, copy=False))
        return np.concatenate(out) if out else None

    # --- звуковой поток -----------------------------------------------------
    def _start(self, idx):
        global OPEN_STREAMS
        with PA_LOCK:
            stream = sd.RawOutputStream(samplerate=self.rate, channels=self.ch, dtype="int16", device=idx,
                                        latency=latency_params()["device"])
            try:
                stream.start()
            except Exception:
                try:
                    stream.close()
                except Exception:
                    pass
                raise
            OPEN_STREAMS += 1
            return stream

    def _open_stream(self):
        """Открыть поток на нужном устройстве.

        Сначала быстрая попытка по уже известным данным; если не вышло (устройство пропало,
        индексы поменялись) - обновляем список устройств и пробуем ещё раз.
        """
        t0 = time.time()
        last_err = None
        self._opened_name = OUTPUT_DEVICE_NAME
        self._opened_mode = LATENCY_MODE
        for do_refresh in (False, True):
            if do_refresh:
                refresh_audio_devices()
            idx = None
            if OUTPUT_DEVICE_NAME:
                idx = find_device_index(OUTPUT_DEVICE_NAME)
                if idx is None:
                    if not do_refresh:
                        continue  # список мог устареть - обновим и поищем снова
                    log(f"Устройство «{OUTPUT_DEVICE_NAME}» сейчас недоступно, использую устройство по умолчанию")
            follows_default = idx is None
            if follows_default:
                idx = mapper_device_index()  # «то, что сейчас по умолчанию»; None = штатное по умолчанию
            try:
                stream = self._start(idx)
            except Exception as e:
                last_err = e
                if OUTPUT_DEVICE_NAME and not follows_default and do_refresh:
                    log(f"Не удалось открыть «{OUTPUT_DEVICE_NAME}» ({e}). Использую устройство по умолчанию.")
                    try:
                        stream = self._start(mapper_device_index())
                        follows_default = True
                    except Exception as e2:
                        last_err = e2
                        continue
                else:
                    continue
            # как называется устройство, на которое реально идёт звук
            label = None
            if follows_default:
                self._signature = default_output_signature()
                label = self._signature
            else:
                self._signature = None
            if not label:
                try:
                    dev = idx if idx is not None else sd.default.device[1]
                    label = sd.query_devices(dev)["name"]
                except Exception:
                    label = "?"
            self.device_label = label
            log(f"Звук выводится на: {label}")
            log(f"[диагностика] поток открыт за {time.time() - t0:.2f} с", debug=True)
            return stream
        raise last_err or RuntimeError("не удалось открыть звуковое устройство")

    def _close_stream(self, stream, broken: bool = False):
        """Закрыть поток. Если устройство пропало, обычная остановка может зависнуть - не ждём дольше 2 с."""
        global OPEN_STREAMS
        if stream is None:
            return

        def work():
            try:
                stream.abort() if broken else stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass

        t = threading.Thread(target=work, daemon=True)
        t.start()
        t.join(2.0)
        with PA_LOCK:
            OPEN_STREAMS = max(0, OPEN_STREAMS - 1)
            if t.is_alive():
                ZOMBIES.append(t)

    def _try_open(self):
        try:
            return self._open_stream()
        except Exception as e:
            log(f"[диагностика] не удалось открыть звуковой поток: {e!r}", debug=True)
            return None

    def _grow_target(self):
        """Пакеты кончались, а потом снова пошли: сеть неровная - увеличиваем запас."""
        old = self._target
        self._target = min(self.MAX_TARGET, self._target + self.GROW_STEP)
        self._last_trouble = time.time()
        STATS["underruns"] = STATS.get("underruns", 0) + 1
        if self._target != old:
            msg = f"буфер звука увеличен до {self._target * 8} мс (сеть доставляет пакеты неровно)"
            log("[диагностика] " + msg, debug=True)
            if time.time() - self._last_grow_log > 10:   # в обычный журнал - не чаще раза в 10 с
                self._last_grow_log = time.time()
                log("Wi-Fi неровный: " + msg)

    def _maybe_shrink_target(self, now):
        base = self.prebuf
        if self._target > base and now - self._last_trouble > self.SHRINK_AFTER:
            self._target = max(base, self._target - 2)
            self._last_trouble = now - (self.SHRINK_AFTER - self.SHRINK_EVERY)
            log(f"[диагностика] сеть стабильна, буфер уменьшен до {self._target * 8} мс", debug=True)

    def _run(self):
        """Поток воспроизведения. Любая неожиданная ошибка попадает в журнал, а не пропадает молча."""
        try:
            self._run_loop()
        except Exception:
            log("Критическая ошибка воспроизведения:\n" + traceback.format_exc())

    def _run_loop(self):
        stream = None
        try:
            stream = self._open_stream()
        except Exception as e:
            log(f"Не удалось открыть устройство звука: {e}")
        silence = bytes(self.frame * self.ch * 2)
        last_check = time.time()
        last_retry = 0.0
        starve_since = None
        writes_since_open = 0
        last_write_t = time.time()
        try:
            while self.running:
                now = time.time()

                # сторож: звука не было больше секунды (пауза, потеря сети, нет устройства) - сообщаем об этом
                if self._sounding and now - last_write_t > 1.0:
                    self._set_sounding(False)

                # нет потока (устройство пропало) - пробуем открыть заново раз в секунду
                if stream is None and now - last_retry > 1.0:
                    last_retry = now
                    stream = self._try_open()

                # пользователь выбрал другое устройство вывода в окне - переключаемся сразу, не дожидаясь нового подключения
                if stream is not None and (OUTPUT_DEVICE_NAME != self._opened_name
                                           or LATENCY_MODE != self._opened_mode):
                    log("Применяю новые настройки вывода звука…")
                    self._target = self.prebuf
                    self._close_stream(stream)
                    stream = self._try_open()
                    writes_since_open = 0
                    last_retry = time.time()

                # устройство по умолчанию сменилось (например, в Windows или FxSound) - переходим на него
                if stream is not None and self._signature is not None and now - last_check > 2.0:
                    last_check = now
                    sig = default_output_signature()
                    if sig and sig != self._signature:
                        log(f"Устройство по умолчанию изменилось: {sig}")
                        self._close_stream(stream)
                        stream = self._try_open()
                        writes_since_open = 0
                        last_retry = time.time()

                # FLUSH: мгновенно обрываем то, что уже лежит в буфере звукового устройства
                if self._reset_device:
                    self._reset_device = False
                    if stream is not None:
                        try:
                            stream.abort()
                            stream.start()
                            writes_since_open = 0   # первые записи после сброса «опустошают» карту - не считаем это сетью
                        except Exception:
                            self._close_stream(stream, broken=True)
                            stream = None
                            last_retry = 0.0

                self._maybe_shrink_target(now)
                pkt, lost = None, False
                pkt_gen = self._flush_gen
                with self.lock:
                    pkt_gen = self._flush_gen
                    if self.next is None:
                        starve_since = None
                        if len(self.buf) >= self._target:
                            self.next = min(self.buf)
                    if self.next is not None and len(self.buf) - self._target > self.TRIM_FORCE:
                        # очередь сильно выросла (заминка сети, дрейф часов): один раз перескакиваем вперёд,
                        # иначе задержка так и осталась бы большой навсегда
                        keep = self._target + self.TRIM_AT // 2
                        for k in sorted(self.buf)[:-keep]:
                            del self.buf[k]
                        self.next = min(self.buf)
                        STATS["trimmed"] += 1
                        log(f"[диагностика] очередь сокращена до {keep * 8} мс", debug=True)
                    if self.next is not None:
                        pkt = self.buf.pop(self.next, None)
                        if pkt is not None:
                            self.next += 1
                            starve_since = None
                        else:
                            t2 = time.time()
                            if starve_since is None:
                                starve_since = t2
                            waited = t2 - starve_since
                            if self.buf and (len(self.buf) >= self._target or waited > 0.06):
                                self.next += 1  # пакет потерян или сильно опоздал - пропускаем
                                lost = True
                                starve_since = None
                            elif not self.buf and waited > 1.0:
                                self.next = None  # поток остановился (пауза) - ждём, пока накопится заново
                                starve_since = None
                                writes_since_open = 0  # после паузы карта пуста: первые «опустошения» - не вина сети
                                self._set_sounding(False)
                if pkt is None and not lost:
                    time.sleep(0.004)
                    continue
                if lost:
                    STATS["lost"] += 1
                    pcm_bytes = silence
                else:
                    try:
                        pcm = self._decode(pkt)
                    except Exception as e:  # битый пакет
                        STATS["errors"] += 1
                        if STATS["errors"] <= 3:
                            log("[диагностика] ошибка декодера ALAC: " + repr(e), debug=True)
                        continue
                    if pcm is None:
                        continue
                    if len(self.buf) - self._target > self.TRIM_AT and max(int(pcm.max()), -int(pcm.min())) < 400:
                        STATS["trimmed"] += 1   # тихий участок: пропуск 8 мс не слышен, а задержка уменьшается
                        continue
                    if self.gain != 1.0:
                        pcm = np.clip(pcm.astype(np.float32) * self.gain, -32768, 32767).astype(np.int16)
                    pcm_bytes = pcm.tobytes()
                if stream is None:
                    continue  # устройства пока нет: пакеты отбрасываем, чтобы не копились
                try:
                    underflow = stream.write(pcm_bytes)
                    writes_since_open += 1
                    last_write_t = time.time()
                    if not lost:
                        STATS["played"] += 1
                        # если за время записи пришёл FLUSH, этот пакет - из прошлого: «играет» не ставим
                        if pkt_gen == self._flush_gen:
                            self._set_sounding(True)
                    if underflow is True and writes_since_open > 20:
                        # звуковая карта осталась без данных - это и есть слышимый треск/пропадание
                        t3 = time.time()
                        if t3 - self._last_underflow > 0.5:
                            self._grow_target()
                        self._last_underflow = t3
                        if len(self.buf) < self._target // 2:
                            with self.lock:
                                self.next = None  # не играем «по капле»: накопим запас заново
                except Exception as e:
                    log(f"Ошибка вывода звука ({e}). Пробую переоткрыть устройство.")
                    self._close_stream(stream, broken=True)
                    stream = None
                    last_retry = 0.0
        finally:
            self._close_stream(stream)


# --------------------------------------------------------------------------- #
#  UDP: аудио, control, timing
# --------------------------------------------------------------------------- #
class RtpProto(asyncio.DatagramProtocol):
    def __init__(self, session):
        self.s = session

    def datagram_received(self, data, addr):
        if len(data) < 12:
            return
        STATS["rtp"] += 1
        if STATS["rtp"] == 1:
            log(f"[диагностика] первый UDP-пакет от {addr}, {len(data)} байт, тип 0x{data[1] & 0x7F:02x}", debug=True)
        ptype = data[1] & 0x7F
        if ptype == 0x56:  # повторно отправленный пакет (внутри обычный RTP с 4-го байта)
            data = data[4:]
            if len(data) < 12:
                return
            ptype = data[1] & 0x7F
        if ptype != 0x60:  # 0x60 = аудио; sync (0x54) и прочее игнорируем
            return
        seq = struct.unpack(">H", data[2:4])[0]
        ts = struct.unpack(">I", data[4:8])[0]
        payload = data[12:]
        if self.s.aes_key:
            n = len(payload) // 16 * 16
            cipher = AES.new(self.s.aes_key, AES.MODE_CBC, self.s.aes_iv)
            payload = cipher.decrypt(payload[:n]) + payload[n:]
        if self.s.player:
            self.s.player.push(seq, payload, ts)


class NullProto(asyncio.DatagramProtocol):
    pass


# --------------------------------------------------------------------------- #
#  RTSP-сессия
# --------------------------------------------------------------------------- #
ACTIVE = None
SESSIONS = set()


class Session:
    def __init__(self, writer):
        self.writer = writer
        self.announced = False
        self.client_name = ""
        try:
            self.peer = writer.get_extra_info("peername")[0]
        except Exception:
            self.peer = "?"
        SESSIONS.add(self)
        self.player = None
        self.fmtp = None
        self.aes_key = None
        self.aes_iv = None
        self.transports = []

    def close(self):
        global ACTIVE
        if self.player:
            self.player.stop()
            self.player = None
        for t in self.transports:
            t.close()
        self.transports.clear()
        if ACTIVE is self:
            ACTIVE = None
        if self.announced:
            self.announced = False
            log(f"Отключилось: {self.client_name}")
            emit("disconnected", self.client_name)

    def abort(self):
        """Принудительно закрыть сессию вместе с сетевым соединением (при остановке приёмника)."""
        self.close()
        try:
            self.writer.close()
        except Exception:
            pass

    async def dispatch(self, method, hdr, body):
        fn = getattr(self, "do_" + method.lower(), None)
        if not fn:
            return "501 Not Implemented", {}, b""
        return await fn(hdr, body)

    def apple_response(self, challenge_b64):
        chal = b64d(challenge_b64)
        ip = socket.inet_aton(self.writer.get_extra_info("sockname")[0])
        data = (chal + ip + MAC).ljust(32, b"\x00")
        k = KEY.size_in_bytes()
        padded = b"\x00\x01" + b"\xff" * (k - 3 - len(data)) + b"\x00" + data
        sig = pow(int.from_bytes(padded, "big"), KEY.d, KEY.n).to_bytes(k, "big")
        return base64.b64encode(sig).decode().rstrip("=")

    async def do_options(self, hdr, body):
        h = {"Public": "ANNOUNCE, SETUP, RECORD, PAUSE, FLUSH, TEARDOWN, OPTIONS, GET_PARAMETER, SET_PARAMETER"}
        return "200 OK", h, b""

    async def do_announce(self, hdr, body):
        global ACTIVE
        if ACTIVE and ACTIVE is not self:
            ACTIVE.close()
        ACTIVE = self
        if self.player:  # повторный ANNOUNCE в той же сессии: старый плеер не должен «висеть» с открытым звуком
            self.player.stop()
            self.player = None
        self.aes_key = self.aes_iv = self.fmtp = None
        for k in STATS:  # счётчики - на каждое подключение отдельно
            STATS[k] = 0
        sdp = body.decode("utf-8", "ignore")
        for line in sdp.splitlines():
            if line.startswith("a=fmtp:"):
                self.fmtp = line[len("a=fmtp:"):].strip()
            elif line.startswith("a=rsaaeskey:") and KEY:
                enc = b64d(line.split(":", 1)[1])
                self.aes_key = PKCS1_OAEP.new(KEY).decrypt(enc)
            elif line.startswith("a=aesiv:"):
                self.aes_iv = b64d(line.split(":", 1)[1])
        log(f"[диагностика] ANNOUNCE: fmtp={self.fmtp!r}, шифрование={'да' if self.aes_key else 'нет'}", debug=True)
        if not self.fmtp:
            return "400 Bad Request", {}, b""
        self.player = Player(self.fmtp)
        self.client_name = hdr.get("x-apple-client-name") or self.peer
        if not self.announced:
            self.announced = True
            log(f"Подключилось: {self.client_name}")
            emit("connected", self.client_name)
        return "200 OK", {}, b""

    async def do_setup(self, hdr, body):
        loop = asyncio.get_running_loop()
        data_t, _ = await loop.create_datagram_endpoint(lambda: RtpProto(self), local_addr=("0.0.0.0", 0))
        ctrl_t, _ = await loop.create_datagram_endpoint(lambda: RtpProto(self), local_addr=("0.0.0.0", 0))
        time_t, _ = await loop.create_datagram_endpoint(NullProto, local_addr=("0.0.0.0", 0))
        self.transports += [data_t, ctrl_t, time_t]
        ports = [t.get_extra_info("sockname")[1] for t in (data_t, ctrl_t, time_t)]
        transport = (
            f"RTP/AVP/UDP;unicast;mode=record;server_port={ports[0]};"
            f"control_port={ports[1]};timing_port={ports[2]}"
        )
        return "200 OK", {"Transport": transport, "Session": "1", "Audio-Jack-Status": "connected"}, b""

    async def do_record(self, hdr, body):
        rate = self.player.rate if self.player else 44100
        return "200 OK", {"Audio-Latency": str(estimated_latency_samples(rate)), "Session": "1"}, b""

    async def do_flush(self, hdr, body):
        if self.player:
            info = hdr.get("rtp-info", "")
            m_ts = re.search(r"rtptime=(\d+)", info)
            m_seq = re.search(r"seq=(\d+)", info)
            self.player.flush(int(m_ts.group(1)) & 0xFFFFFFFF if m_ts else None,
                              int(m_seq.group(1)) & 0xFFFF if m_seq else None)
            log(f"[диагностика] FLUSH: {info}", debug=True)
        return "200 OK", {}, b""

    async def do_set_parameter(self, hdr, body):
        ct = hdr.get("content-type", "")
        rt = None
        m_rt = re.search(r"rtptime=(\d+)", hdr.get("rtp-info", ""))
        if m_rt:
            rt = int(m_rt.group(1))   # метка, по которой источник связывает название и обложку одного трека
        log(f"[диагностика] SET_PARAMETER {ct or '-'}, {len(body)} байт", debug=True)

        if ct.startswith("text/parameters") and self.player:
            m = re.search(rb"volume:\s*(-?\d+(?:\.\d+)?)", body)
            if m:
                db = float(m.group(1))
                self.player.set_volume_db(db)
                pct = 0 if db <= -100 else max(0, min(100, round((db + 30) / 30 * 100)))
                emit("volume", pct)
            prog = parse_progress(body, self.player.rate)
            if prog:
                emit("progress", {"pos": prog[0], "dur": prog[1]})
        elif ct.startswith("application/x-dmap-tagged"):
            info = parse_dmap(body)
            if info:
                info["rt"] = rt
                log(f"[диагностика] трек: {info}", debug=True)
                emit("track", info)
        elif ct.startswith("image/"):
            if 0 < len(body) <= MAX_COVER_BYTES:
                emit("cover", {"data": bytes(body), "rt": rt})
            elif not body:
                emit("cover", {"data": b"", "rt": rt})   # пустая картинка = обложки нет
        return "200 OK", {}, b""

    async def do_get_parameter(self, hdr, body):
        return "200 OK", {}, b""

    async def do_teardown(self, hdr, body):
        self.close()
        return "200 OK", {"Connection": "close"}, b""


def enable_keepalive(writer):
    """TCP keepalive: если телефон пропал без «до свидания» (выключили Wi-Fi), соединение закроется само."""
    try:
        sock = writer.get_extra_info("socket")
        if sock is None:
            return
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for name, value in (("TCP_KEEPIDLE", 10), ("TCP_KEEPINTVL", 5), ("TCP_KEEPCNT", 3)):
            opt = getattr(socket, name, None)
            if opt is not None:
                try:
                    sock.setsockopt(socket.IPPROTO_TCP, opt, value)
                except OSError:
                    pass
    except Exception:
        pass


async def handle(reader, writer):
    sess = Session(writer)
    enable_keepalive(writer)
    try:
        while True:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode("utf-8", "replace").split("\r\n")
            method = lines[0].split(" ", 1)[0]
            hdr = {}
            for l in lines[1:]:
                if ":" in l:
                    k, v = l.split(":", 1)
                    hdr[k.strip().lower()] = v.strip()
            try:
                n = max(0, int(hdr.get("content-length", 0)))
            except ValueError:
                n = 0
            body = await reader.readexactly(n) if n else b""
            try:
                status, extra, rbody = await sess.dispatch(method, hdr, body)
            except Exception as e:  # телефон должен получить ответ, а не оборванное соединение
                log(f"Ошибка при обработке запроса {method}: {e!r}")
                status, extra, rbody = "500 Internal Server Error", {}, b""
            if "apple-challenge" in hdr and KEY:  # как в shairport-sync: на любой запрос с вызовом
                try:
                    extra["Apple-Response"] = sess.apple_response(hdr["apple-challenge"])
                except Exception as e:
                    log(f"Не удалось ответить на проверку Apple: {e!r}")
            log(f"[RTSP] {method} -> {status}"
                + (" (клиент прислал Apple-Challenge)" if "apple-challenge" in hdr else ""), debug=True)
            out = f"RTSP/1.0 {status}\r\nCSeq: {hdr.get('cseq', '0')}\r\nServer: AirTunes/105.1\r\n"
            for k, v in extra.items():
                out += f"{k}: {v}\r\n"
            out += f"Content-Length: {len(rbody)}\r\n\r\n"
            writer.write(out.encode() + rbody)
            await writer.drain()
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError, OSError):
        pass
    finally:
        sess.close()
        SESSIONS.discard(sess)
        try:
            writer.close()
        except Exception:
            pass


async def serve(name):
    """Запустить приёмник и работать, пока задачу не отменят (cancel)."""
    global NAME
    NAME = name
    ip = local_ip()
    try:
        server = await asyncio.start_server(handle, "0.0.0.0", PORT)
    except OSError:
        # Порт 5000 часто занят другими программами. Это не проблема: телефон узнаёт порт
        # из mDNS-объявления, поэтому подойдёт любой свободный порт.
        try:
            server = await asyncio.start_server(handle, "0.0.0.0", 0)
        except OSError as e:
            msg = f"Не удалось занять ни порт {PORT}, ни другой свободный порт: {e}"
            log(msg)
            emit("error", msg)
            return
        log(f"Порт {PORT} занят другой программой, использую свободный порт "
            f"{server.sockets[0].getsockname()[1]}")
    port = server.sockets[0].getsockname()[1]
    props = {
        "txtvers": "1", "ch": "2", "cn": "0,1", "da": "true",
        "et": "0,1" if KEY else "0",
        "md": "0,1,2", "pw": "false", "sv": "false", "sr": "44100", "ss": "16",
        "tp": "UDP", "vn": "65537", "vs": "105.1", "am": "AirPort10,115", "sf": "0x4",
    }

    def make_info(addr):
        return ServiceInfo(
            "_raop._tcp.local.",
            f"{MAC_HEX}@{NAME}._raop._tcp.local.",
            addresses=[socket.inet_aton(addr)],
            port=port,
            properties=props,
            # имя хоста строим сами: настоящее имя компьютера может содержать русские буквы и пробелы
            server=f"airplay-{MAC_HEX}.local.",
        )

    azc = None
    info = None
    stats_task = None
    ip_task = None
    try:
        azc = AsyncZeroconf()
        info = make_info(ip)
        # allow_name_change: если такое имя в сети уже занято (например, старая копия ещё не исчезла),
        # mDNS выберет «Имя (2)» вместо ошибки
        await azc.async_register_service(info, allow_name_change=True)
        if KEY_ERROR:
            log(f"Файл ключа не прочитан ({KEY_ERROR}); приёмник работает без шифрования")

        async def stats():
            real_time_pps = 44100 / FRAMES_PER_PACKET     # ~125,3 пакета/с - так шлёт источник при обычной скорости
            last_rtp, last_t, off_count, last_warn = STATS["rtp"], time.time(), 0, 0.0
            while True:
                await asyncio.sleep(3)
                now_t = time.time()
                cur = STATS["rtp"]
                pps = (cur - last_rtp) / max(0.001, now_t - last_t) if cur >= last_rtp else 0.0
                last_rtp, last_t = cur, now_t
                if ACTIVE:
                    pl = ACTIVE.player
                    extra = ""
                    if pl is not None:
                        extra = (f", буфер {pl._target * 8} мс, очередь {len(pl.buf)}, "
                                 f"треска (опустошений) {STATS['underruns']}, срезано {STATS['trimmed']}")
                        # скорость поступления сильно отличается от реального времени -> звук будет «уплывать»
                        if pl._sounding and (pps > real_time_pps * 1.12 or pps < real_time_pps * 0.88):
                            off_count += 1
                        else:
                            off_count = 0
                        if off_count >= 2 and now_t - last_warn > 15:
                            last_warn = now_t
                            log(f"Источник шлёт звук со скоростью {pps:.0f} пакетов/с вместо {real_time_pps:.0f}: "
                                f"звук будет отставать или забегать вперёд относительно видео")
                    log(f"[диагностика] принято пакетов: {STATS['rtp']} ({pps:.0f}/с), воспроизведено: {STATS['played']},"
                        f" потеряно: {STATS['lost']}, ошибок декодера: {STATS['errors']}{extra}", debug=True)

        async def ip_watch():
            """Сеть могла смениться (Wi-Fi переподключился, ноутбук проснулся): обновляем адрес в mDNS."""
            nonlocal azc, info, ip
            while True:
                await asyncio.sleep(IP_WATCH_INTERVAL)
                new_ip = local_ip()
                if new_ip == ip or new_ip == "127.0.0.1":
                    continue
                log(f"Адрес компьютера изменился: {ip} -> {new_ip}. Обновляю объявление в сети.")
                try:
                    await azc.async_unregister_service(info)
                    await azc.async_close()
                except Exception:
                    pass
                azc = AsyncZeroconf()
                info = make_info(new_ip)
                await azc.async_register_service(info, allow_name_change=True)
                ip = new_ip
                emit("ip_changed", {"name": NAME, "ip": ip, "port": port})

        stats_task = asyncio.create_task(stats())
        ip_task = asyncio.create_task(ip_watch())
        log(f"AirPlay-приёмник «{NAME}» запущен на {ip}:{port}"
            f" ({'с ключом' if KEY else 'БЕЗ ключа airport_key.pem'})")
        emit("started", {"name": NAME, "ip": ip, "port": port, "key": KEY is not None})
        await server.serve_forever()
    except asyncio.CancelledError:
        raise
    except Exception as e:
        msg = f"Ошибка приёмника: {e}"
        log(msg)
        emit("error", msg)
    finally:
        for t in (stats_task, ip_task):
            if t:
                t.cancel()
        for sess in list(SESSIONS):
            sess.abort()
        server.close()   # порт освобождаем всегда, даже если mDNS не запустился
        if azc is not None:
            try:
                if info is not None:
                    await azc.async_unregister_service(info)
                await azc.async_close()
            except Exception:
                pass
        log("Приёмник остановлен")
        emit("stopped")


async def main(cli_name=None, rename=False):
    await serve(resolve_name(cli_name, rename))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="AirPlay-приёмник (только звук)")
    ap.add_argument("name", nargs="?", help="название устройства (сохраняется на будущее)")
    ap.add_argument("--rename", action="store_true", help="спросить название заново")
    ap.add_argument("--debug", action="store_true", help="подробный вывод для диагностики")
    ap.add_argument("--latency", choices=sorted(LATENCY_MODES), help="задержка звука: min / normal / safe")
    args = ap.parse_args()
    VERBOSE = args.debug
    if args.latency:
        save_config(latency=args.latency)
    LATENCY_MODE = load_config().get("latency", "normal")
    try:
        asyncio.run(main(args.name, args.rename))
    except KeyboardInterrupt:
        pass
