#!/usr/bin/env python3
"""
Windows AirPlay - приложение с окном.

Запускает AirPlay-приёмник (airplay_audio.py) в фоновом потоке и показывает:
статус, имя устройства, выбор колонок, громкость с телефона, журнал.
При закрытии окна сворачивается в трей (рядом с часами).

Установка:              pip install zeroconf pycryptodome av sounddevice numpy customtkinter pystray pillow
Запуск из исходников:   python app.py
Запуск свёрнутым:       python app.py --minimized
"""
import argparse
import asyncio
import os
import queue
import socket
import sys
import threading
import time
import traceback

import customtkinter as ctk

import airplay_audio as core

TRAY_ERROR = ""
try:  # значок в трее - необязательная часть
    import pystray
    from PIL import ImageTk

    from icon_art import make_icon

    HAVE_TRAY = True
except Exception as _e:  # pragma: no cover
    HAVE_TRAY = False
    TRAY_ERROR = f"{type(_e).__name__}: {_e}"

APP_TITLE = "Windows AirPlay"
AUTOSTART_NAME = "WindowsAirPlay"
DEFAULT_DEVICE_LABEL = "Устройство по умолчанию"
LATENCY_LABELS = {
    "min": "Минимальная (≈0,2 с)",
    "normal": "Обычная (≈0,25 с)",
    "safe": "Надёжная (≈0,65 с)",
}

LOCK_PORT = 47625  # локальный порт-«замок»: не даёт запустить вторую копию приложения

GREEN, BLUE, RED, GRAY = "#2ecc71", "#3b8ed0", "#e74c3c", "#7f8c8d"


def crash_log_path() -> str:
    return os.path.join(os.path.dirname(core.config_path()), "crash.log")


def crash_log(text: str):
    """Дописать сообщение об ошибке в crash.log (у окна без консоли других следов не остаётся)."""
    try:
        path = crash_log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {text}\n")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
#  Сервер в фоновом потоке
# --------------------------------------------------------------------------- #
class ServerThread:
    """Запускает core.serve() в отдельном потоке со своим циклом asyncio."""

    def __init__(self, events: "queue.Queue"):
        self.q = events
        self.loop = None
        self.task = None
        self.thread = None
        self._stop_requested = False

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self, name: str, device_name):
        if self.running:
            return
        core.OUTPUT_DEVICE_NAME = device_name
        core.EVENT_HOOK = lambda kind, data=None: self.q.put((kind, data))
        self._stop_requested = False
        self.thread = threading.Thread(target=self._run, args=(name,), daemon=True)
        self.thread.start()

    def _run(self, name: str):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop
        self.task = loop.create_task(core.serve(name))
        if self._stop_requested:  # «Остановить» нажали раньше, чем задача появилась
            self.task.cancel()
        try:
            loop.run_until_complete(self.task)
        except asyncio.CancelledError:
            pass
        except Exception as e:  # на всякий случай - чтобы окно не зависло в "работает"
            self.q.put(("error", f"Ошибка: {e}"))
        finally:
            try:
                pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
                for t in pending:
                    t.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            loop.close()
            self.loop = None
            self.task = None
            # сначала освобождаем «слот», потом сообщаем окну: иначе окно могло бы увидеть, что поток
            # ещё «жив», и не перезапустить приёмник после смены названия
            self.thread = None
            self.q.put(("thread_done", None))

    def stop(self):
        self._stop_requested = True
        loop, task = self.loop, self.task
        if loop and task:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass

    def join(self, timeout: float = 3.0):
        if self.thread:
            self.thread.join(timeout)


# --------------------------------------------------------------------------- #
#  Автозапуск с Windows (реестр, без прав администратора)
# --------------------------------------------------------------------------- #
def _autostart_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --minimized'
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")  # запуск скрипта без консольного окна
    if os.path.exists(pyw):
        exe = pyw
    return f'"{exe}" "{os.path.abspath(__file__)}" --minimized'


def autostart_supported() -> bool:
    return sys.platform == "win32"


def autostart_enabled() -> bool:
    if not autostart_supported():
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
            winreg.QueryValueEx(k, AUTOSTART_NAME)
            return True
    except OSError:
        return False


def set_autostart(enable: bool) -> bool:
    if not autostart_supported():
        return False
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE
        ) as k:
            if enable:
                winreg.SetValueEx(k, AUTOSTART_NAME, 0, winreg.REG_SZ, _autostart_command())
            else:
                try:
                    winreg.DeleteValue(k, AUTOSTART_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------- #
#  Одна копия приложения
# --------------------------------------------------------------------------- #
def acquire_single_instance():
    """Возвращает сокет-замок или None, если приложение уже запущено."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        s.listen(5)
        return s
    except OSError:
        s.close()
        return None


def notify_existing():
    """Просим уже запущенную копию показать своё окно."""
    try:
        with socket.create_connection(("127.0.0.1", LOCK_PORT), timeout=1) as c:
            c.sendall(b"show")
    except OSError:
        pass


# --------------------------------------------------------------------------- #
#  Окно
# --------------------------------------------------------------------------- #
class App(ctk.CTk):
    def __init__(self, start_minimized: bool = False):
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        super().__init__()

        self.title(APP_TITLE)
        self.geometry("460x720")
        self.minsize(420, 520)
        self.q: "queue.Queue" = queue.Queue()
        self.server = ServerThread(self.q)
        self.tray = None
        self.state_name = "stopped"   # stopped | starting | running | playing
        self.pending_start = False    # запустить заново, когда остановится (смена имени)
        self.quitting = False
        self.lock_sock = None

        cfg = core.load_config()
        self.name_value = cfg.get("name") or core.DEFAULT_NAME
        self.device_name = cfg.get("device") or DEFAULT_DEVICE_LABEL
        if cfg.get("debug"):
            core.VERBOSE = True      # «Подробный журнал» включён в окне раньше (или запуск с --debug)
        self.latency_key = cfg.get("latency") if cfg.get("latency") in LATENCY_LABELS else "normal"
        core.LATENCY_MODE = self.latency_key
        self.minimize_to_tray = bool(cfg.get("tray", True)) and HAVE_TRAY

        self._set_window_icon()
        self._build_ui()
        if not HAVE_TRAY:
            self.add_log(f"Значок в трее недоступен ({TRAY_ERROR or 'нет библиотек pystray/pillow'})")
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(150, self.pump)

        if start_minimized:
            self.withdraw()  # сразу прячем, чтобы окно не мигало при автозапуске
            self.after(300, self.hide_to_tray)
        self.start_server()

    # ---- интерфейс ---------------------------------------------------------
    def _set_window_icon(self):
        if not HAVE_TRAY:
            return
        try:
            self._icon_photo = ImageTk.PhotoImage(make_icon(64))
            self.iconphoto(True, self._icon_photo)
        except Exception:
            pass

    def _build_ui(self):
        pad = {"padx": 18}
        # вся панель в прокручиваемой области: на низких экранах (768 px) ничего не обрезается
        self.body = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.body.pack(fill="both", expand=True)

        head = ctk.CTkFrame(self.body, fg_color="transparent")
        head.pack(fill="x", pady=(18, 4), **pad)
        ctk.CTkLabel(head, text="🎵  " + APP_TITLE, font=ctk.CTkFont(size=22, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(head, text="Звук с iPhone, iPad и Mac на этот компьютер",
                     text_color=GRAY).pack(anchor="w")

        # статус
        box = ctk.CTkFrame(self.body, corner_radius=12)
        box.pack(fill="x", pady=10, **pad)
        row = ctk.CTkFrame(box, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=(12, 2))
        self.dot = ctk.CTkLabel(row, text="●", text_color=GRAY, font=ctk.CTkFont(size=22))
        self.dot.pack(side="left")
        self.status = ctk.CTkLabel(row, text="Остановлен", font=ctk.CTkFont(size=16, weight="bold"))
        self.status.pack(side="left", padx=8)
        self.hint = ctk.CTkLabel(box, text="", text_color=GRAY, wraplength=370, justify="left")
        self.hint.pack(fill="x", padx=14, pady=(0, 12), anchor="w")

        self.toggle_btn = ctk.CTkButton(self.body, text="Запустить", height=38, command=self.toggle_server)
        self.toggle_btn.pack(fill="x", **pad)

        # имя
        ctk.CTkLabel(self.body, text="Название устройства (так оно видно в AirPlay)",
                     anchor="w").pack(fill="x", pady=(16, 2), **pad)
        name_row = ctk.CTkFrame(self.body, fg_color="transparent")
        name_row.pack(fill="x", **pad)
        self.name_entry = ctk.CTkEntry(name_row, height=34)
        self.name_entry.insert(0, self.name_value)
        self.name_entry.pack(side="left", fill="x", expand=True)
        self.name_entry.bind("<Return>", lambda e: self.save_name())
        ctk.CTkButton(name_row, text="Сохранить", width=100, height=34, command=self.save_name).pack(
            side="left", padx=(8, 0))

        # устройство вывода
        ctk.CTkLabel(self.body, text="Куда выводить звук", anchor="w").pack(fill="x", pady=(14, 2), **pad)
        self.devices = core.list_output_devices()
        labels = [DEFAULT_DEVICE_LABEL] + [n for _, n in self.devices]
        if self.device_name not in labels:
            self.device_name = DEFAULT_DEVICE_LABEL
        dev_row = ctk.CTkFrame(self.body, fg_color="transparent")
        dev_row.pack(fill="x", **pad)
        self.device_menu = ctk.CTkOptionMenu(dev_row, values=labels, command=self.on_device, height=34,
                                             dynamic_resizing=False)
        self.device_menu.set(self.device_name)
        self.device_menu.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(dev_row, text="⟳", width=40, height=34, command=self.refresh_devices).pack(
            side="left", padx=(8, 0))

        # задержка
        ctk.CTkLabel(self.body, text="Задержка звука (меньше — быстрее пауза и смена роликов)",
                     anchor="w").pack(fill="x", pady=(14, 2), **pad)
        self.latency_menu = ctk.CTkOptionMenu(self.body, values=list(LATENCY_LABELS.values()),
                                              command=self.on_latency, height=34, dynamic_resizing=False)
        self.latency_menu.set(LATENCY_LABELS[self.latency_key])
        self.latency_menu.pack(fill="x", **pad)

        # громкость
        vol_row = ctk.CTkFrame(self.body, fg_color="transparent")
        vol_row.pack(fill="x", pady=(16, 0), **pad)
        ctk.CTkLabel(vol_row, text="Громкость на телефоне").pack(side="left")
        self.vol_label = ctk.CTkLabel(vol_row, text="—", text_color=GRAY)
        self.vol_label.pack(side="right")
        self.vol_bar = ctk.CTkProgressBar(self.body)
        self.vol_bar.set(0)
        self.vol_bar.pack(fill="x", pady=(4, 0), **pad)

        # настройки
        self.autostart_var = ctk.BooleanVar(value=autostart_enabled())
        cb = ctk.CTkCheckBox(self.body, text="Запускать вместе с Windows", variable=self.autostart_var,
                             command=self.on_autostart)
        cb.pack(anchor="w", pady=(16, 4), **pad)
        if not autostart_supported():
            cb.configure(state="disabled")
        self.tray_var = ctk.BooleanVar(value=self.minimize_to_tray)
        cb2 = ctk.CTkCheckBox(self.body, text="При закрытии окна сворачивать в трей", variable=self.tray_var,
                              command=self.on_tray_option)
        cb2.pack(anchor="w", pady=(0, 4), **pad)
        if not HAVE_TRAY:
            cb2.configure(state="disabled")

        self.debug_var = ctk.BooleanVar(value=bool(core.VERBOSE))
        ctk.CTkCheckBox(self.body, text="Подробный журнал (для диагностики)", variable=self.debug_var,
                        command=self.on_debug).pack(anchor="w", pady=(0, 4), **pad)

        # журнал
        log_head = ctk.CTkFrame(self.body, fg_color="transparent")
        log_head.pack(fill="x", pady=(10, 2), **pad)
        ctk.CTkLabel(log_head, text="Журнал").pack(side="left")
        ctk.CTkButton(log_head, text="Папка", width=70, height=26, command=self.open_logs_folder).pack(
            side="right", padx=(6, 0))
        ctk.CTkButton(log_head, text="Копировать", width=100, height=26, command=self.copy_log).pack(side="right")
        self.log_box = ctk.CTkTextbox(self.body, height=150, state="disabled", font=ctk.CTkFont(size=12))
        self.log_box.pack(fill="x", pady=(0, 16), **pad)

    # ---- вспомогательное ---------------------------------------------------
    def logs_dir(self) -> str:
        return os.path.dirname(core.config_path())

    def _write_log_file(self, line: str):
        """Дублируем журнал в файл app.log (до ~1 МБ, затем старая часть переименовывается в app.log.1)."""
        try:
            os.makedirs(self.logs_dir(), exist_ok=True)
            path = os.path.join(self.logs_dir(), "app.log")
            if os.path.exists(path) and os.path.getsize(path) > 1_000_000:
                os.replace(path, path + ".1")
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def add_log(self, text: str):
        line = f"{time.strftime('%H:%M:%S')}  {text}"
        self._write_log_file(line)
        self.log_box.configure(state="normal")
        self.log_box.insert("end", line + "\n")
        try:  # в подробном режиме строк очень много: держим в окне только последние ~600
            if int(self.log_box.index("end-1c").split(".")[0]) > 700:
                self.log_box.delete("1.0", "101.0")
        except Exception:
            pass
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def on_debug(self):
        core.VERBOSE = bool(self.debug_var.get())
        core.save_config(debug=core.VERBOSE)
        self.add_log("Подробный журнал " + ("включён" if core.VERBOSE else "выключен"))

    def copy_log(self):
        """Скопировать весь журнал из окна в буфер обмена (потом вставить сообщением)."""
        try:
            self.log_box.configure(state="normal")
            text = self.log_box.get("1.0", "end").strip()
            self.log_box.configure(state="disabled")
            self.clipboard_clear()
            self.clipboard_append(text)
            self.update()  # чтобы Windows сохранила буфер обмена
            self.add_log(f"Журнал скопирован в буфер обмена ({len(text.splitlines())} строк). Вставьте его: Ctrl+V")
        except Exception:
            self.report_error("копирование журнала")

    def open_logs_folder(self):
        """Открыть папку с app.log, crash.log и config.json."""
        try:
            os.makedirs(self.logs_dir(), exist_ok=True)
            if hasattr(os, "startfile"):
                os.startfile(self.logs_dir())  # только Windows
            else:
                self.add_log(f"Папка с журналами: {self.logs_dir()}")
        except Exception:
            self.report_error("открытие папки с журналами")

    def set_status(self, state: str, text: str, hint: str = ""):
        self.state_name = state
        color = {"stopped": GRAY, "stopping": GRAY, "starting": BLUE, "running": GREEN,
                 "playing": BLUE, "error": RED}[state]
        self.dot.configure(text_color=color)
        self.status.configure(text=text)
        self.hint.configure(text=hint)
        active = state in ("starting", "running", "playing")
        self.toggle_btn.configure(text="Остановить" if active else "Запустить")
        if self.tray is not None:
            try:
                self.tray.update_menu()
            except Exception:
                pass

    def selected_device_name(self):
        """Имя выбранного устройства вывода или None (= по умолчанию в Windows)."""
        return None if self.device_name == DEFAULT_DEVICE_LABEL else self.device_name

    # ---- действия ----------------------------------------------------------
    def start_server(self):
        if self.server.running:
            self.pending_start = True  # поток ещё останавливается - запустим сразу после него
            return
        name = core.clean_name(self.name_entry.get()) or core.DEFAULT_NAME
        self.name_value = name
        core.save_config(name=name)
        self.set_status("starting", "Запускаю…")
        self.server.start(name, self.selected_device_name())

    def stop_server(self):
        self.pending_start = False
        self.set_status("stopping", "Останавливаю…")
        self.server.stop()

    def toggle_server(self):
        if self.state_name in ("starting", "running", "playing"):
            self.stop_server()
        else:
            self.start_server()

    def save_name(self):
        name = core.clean_name(self.name_entry.get())
        if not name:
            self.add_log("Название не может быть пустым")
            return
        self.name_entry.delete(0, "end")
        self.name_entry.insert(0, name)
        if name == self.name_value and self.server.running:
            self.add_log("Название не изменилось")
            return
        self.name_value = name
        core.save_config(name=name)
        self.add_log(f"Новое название: «{name}»")
        if self.server.running:
            self.pending_start = True
            self.set_status("stopping", "Перезапускаю с новым названием…")
            self.server.stop()
        else:
            self.start_server()

    def on_device(self, choice: str):
        self.device_name = choice
        core.save_config(device="" if choice == DEFAULT_DEVICE_LABEL else choice)
        core.OUTPUT_DEVICE_NAME = self.selected_device_name()
        self.add_log(f"Устройство вывода: {choice}")

    def on_latency(self, choice: str):
        for key, label in LATENCY_LABELS.items():
            if label == choice:
                self.latency_key = key
        core.LATENCY_MODE = self.latency_key
        core.save_config(latency=self.latency_key)
        self.add_log(f"Задержка звука: {choice}")

    def refresh_devices(self):
        """Перечитать список устройств (после подключения наушников, смены вывода в FxSound и т. п.)."""
        if self.state_name == "playing":
            self.add_log("Список устройств нельзя обновить во время воспроизведения")
            return
        core.refresh_audio_devices()
        self.devices = core.list_output_devices()
        labels = [DEFAULT_DEVICE_LABEL] + [n for _, n in self.devices]
        if self.device_name not in labels:
            self.add_log(f"Устройство «{self.device_name}» не найдено, выбрано устройство по умолчанию")
            self.device_name = DEFAULT_DEVICE_LABEL
            core.save_config(device="")
            core.OUTPUT_DEVICE_NAME = None
        self.device_menu.configure(values=labels)
        self.device_menu.set(self.device_name)
        self.add_log(f"Список устройств обновлён ({len(self.devices)} шт.)")

    def on_autostart(self):
        ok = set_autostart(self.autostart_var.get())
        if not ok:
            self.autostart_var.set(autostart_enabled())
            self.add_log("Не удалось изменить автозапуск")
        else:
            self.add_log("Автозапуск включён" if self.autostart_var.get() else "Автозапуск выключен")

    def on_tray_option(self):
        self.minimize_to_tray = self.tray_var.get()
        core.save_config(tray=self.minimize_to_tray)

    # ---- события от сервера ------------------------------------------------
    def report_error(self, what: str):
        """Записать ошибку в crash.log и показать в журнале окна (не прерывая работу интерфейса)."""
        text = f"{what}\n{traceback.format_exc()}"
        crash_log(text)
        try:
            self.add_log(f"Ошибка: {what} (подробности в {crash_log_path()})")
        except Exception:
            pass

    def report_callback_exception(self, exc, val, tb):  # ошибки в обработчиках кнопок и т. п.
        crash_log("Ошибка в интерфейсе:\n" + "".join(traceback.format_exception(exc, val, tb)))
        try:
            self.add_log(f"Ошибка в интерфейсе: {val} (подробности в {crash_log_path()})")
        except Exception:
            pass

    def pump(self):
        """Раз в 150 мс забирает события сервера. Ошибка в одном событии не должна «заморозить» окно."""
        try:
            while True:
                try:
                    kind, data = self.q.get_nowait()
                except queue.Empty:
                    break
                try:
                    self.handle_event(kind, data)
                except Exception:
                    self.report_error(f"обработка события «{kind}»")
        finally:
            if not self.quitting:
                try:
                    self.after(150, self.pump)
                except Exception:
                    pass

    def _running_hint(self, data) -> str:
        return (f"Откройте AirPlay на телефоне и выберите «{data['name']}».\n"
                f"Адрес этого компьютера: {data['ip']}:{data.get('port', 5000)}"
                + ("" if data.get("key") else "\n⚠ Нет файла ключа: часть устройств не подключится"))

    def handle_event(self, kind, data):
        if self.quitting:
            return  # окно закрывается: оставшиеся в очереди события уже никому не нужны
        if kind == "started":
            self.last_started = data
            self.set_status("running", "Работает", self._running_hint(data))
        elif kind == "ip_changed":
            self.last_started = {**(getattr(self, "last_started", None) or {}), **data}
            self.add_log(f"Адрес компьютера изменился: {data['ip']}")
            if self.state_name == "running":
                self.set_status("running", "Работает", self._running_hint(self.last_started))
        elif kind == "connected":
            self.set_status("playing", f"Играет: {data}", "Звук идёт на выбранное устройство")
        elif kind == "disconnected":
            if self.state_name == "playing":
                self.set_status("running", "Работает", f"Ожидаю подключения к «{self.name_value}»")
            self.vol_bar.set(0)
            self.vol_label.configure(text="—")
        elif kind == "volume":
            self.vol_bar.set(data / 100)
            self.vol_label.configure(text=f"{data}%")
        elif kind == "log":
            self.add_log(data)
        elif kind == "error":
            self.set_status("error", "Ошибка", str(data))
        elif kind == "thread_done":
            if self.state_name != "error":
                self.set_status("stopped", "Остановлен", "Нажмите «Запустить», чтобы приёмник снова появился в AirPlay")
            self.vol_bar.set(0)
            self.vol_label.configure(text="—")
            if self.pending_start and not self.quitting:
                self.pending_start = False
                self.start_server()
        elif kind == "ui_show":
            self.show_window()
        elif kind == "ui_toggle":
            self.toggle_server()
        elif kind == "ui_quit":
            self.quit_app()

    # ---- трей --------------------------------------------------------------
    def _tray_menu(self):
        return pystray.Menu(
            pystray.MenuItem("Открыть окно", lambda icon, item: self.q.put(("ui_show", None)), default=True),
            pystray.MenuItem(
                lambda item: "Остановить" if self.state_name in ("starting", "running", "playing") else "Запустить",
                lambda icon, item: self.q.put(("ui_toggle", None)),
            ),
            pystray.MenuItem("Выход", lambda icon, item: self.q.put(("ui_quit", None))),
        )

    def hide_to_tray(self):
        if not HAVE_TRAY:
            self.iconify()
            return
        if self.tray is None:
            try:
                self.tray = pystray.Icon("WindowsAirPlay", make_icon(64), APP_TITLE, self._tray_menu())
                self.tray.run_detached()
            except Exception:
                self.tray = None
                self.iconify()
                return
        self.withdraw()

    def show_window(self):
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
            self.tray = None
        self.deiconify()
        self.lift()
        self.focus_force()

    def on_close(self):
        if self.minimize_to_tray and HAVE_TRAY:
            self.hide_to_tray()
        else:
            self.quit_app()

    def listen_for_second_launch(self, lock_sock):
        """Если пользователь запустил вторую копию, она просит нас показать окно."""
        self.lock_sock = lock_sock

        def loop():
            while True:
                try:
                    conn, _ = lock_sock.accept()
                except OSError:
                    return  # сокет закрыт при выходе
                try:
                    conn.settimeout(1)
                    if conn.recv(16).startswith(b"show"):
                        self.q.put(("ui_show", None))
                except OSError:
                    pass
                finally:
                    conn.close()

        threading.Thread(target=loop, daemon=True).start()

    def quit_app(self):
        if self.quitting:  # «Выход» нажали дважды (например, в трее) - второй раз ничего не делаем
            return
        self.quitting = True
        if self.lock_sock is not None:
            try:
                self.lock_sock.close()
            except OSError:
                pass
        self.pending_start = False
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
            self.tray = None
        self.server.stop()
        self.server.join(3.0)
        self.destroy()


def install_crash_logging():
    """Ошибки в фоновых потоках и неперехваченные исключения - в crash.log (и в консоль, как обычно)."""
    old_sys_hook = sys.excepthook
    old_thread_hook = threading.excepthook

    def thread_hook(args):
        crash_log("Ошибка в потоке %s:\n%s" % (
            args.thread.name if args.thread else "?",
            "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))))
        old_thread_hook(args)

    def sys_hook(t, v, tb):
        crash_log("Неперехваченная ошибка:\n" + "".join(traceback.format_exception(t, v, tb)))
        old_sys_hook(t, v, tb)

    threading.excepthook = thread_hook
    sys.excepthook = sys_hook


def main():
    install_crash_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--minimized", action="store_true", help="запуститься свёрнутым в трей")
    ap.add_argument("--debug", action="store_true", help="подробный журнал")
    args, _ = ap.parse_known_args()
    core.VERBOSE = args.debug
    lock = acquire_single_instance()
    if lock is None:  # приложение уже работает - просто покажем его окно
        notify_existing()
        return
    try:
        app = App(start_minimized=args.minimized)
        app.listen_for_second_launch(lock)
        app.mainloop()
    except Exception:
        crash_log("Ошибка при запуске:\n" + traceback.format_exc())
        try:  # у приложения нет консоли - показываем причину в окне сообщения
            import tkinter
            from tkinter import messagebox

            root = tkinter.Tk()
            root.withdraw()
            messagebox.showerror(APP_TITLE, f"Не удалось запустить приложение.\nПодробности записаны в файл:\n{crash_log_path()}")
            root.destroy()
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
