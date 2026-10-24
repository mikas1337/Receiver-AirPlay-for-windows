#!/usr/bin/env python3
"""
Windows AirPlay - приложение с окном.

Запускает AirPlay-приёмник (airplay_audio.py) в фоновом потоке и показывает:
статус, имя устройства, выбор колонок, громкость с телефона, журнал.
При закрытии окна сворачивается в трей (рядом с часами).

Запуск из исходников:   python app.py
Запуск свёрнутым:       python app.py --minimized
"""
import argparse
import asyncio
import os
import queue
import sys
import threading
import time

import customtkinter as ctk

import airplay_audio as core

try:  # значок в трее - необязательная часть
    import pystray
    from PIL import ImageTk

    from icon_art import make_icon

    HAVE_TRAY = True
except Exception:  # pragma: no cover
    HAVE_TRAY = False

APP_TITLE = "Windows AirPlay"
AUTOSTART_NAME = "WindowsAirPlay"
DEFAULT_DEVICE_LABEL = "Устройство по умолчанию"

GREEN, BLUE, RED, GRAY = "#2ecc71", "#3b8ed0", "#e74c3c", "#7f8c8d"


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

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self, name: str, device_index):
        if self.running:
            return
        core.OUTPUT_DEVICE = device_index
        core.EVENT_HOOK = lambda kind, data=None: self.q.put((kind, data))
        self.thread = threading.Thread(target=self._run, args=(name,), daemon=True)
        self.thread.start()

    def _run(self, name: str):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop
        self.task = loop.create_task(core.serve(name))
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
            self.q.put(("thread_done", None))

    def stop(self):
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
    return f'"{sys.executable}" "{os.path.abspath(__file__)}" --minimized'


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
#  Окно
# --------------------------------------------------------------------------- #
class App(ctk.CTk):
    def __init__(self, start_minimized: bool = False):
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        super().__init__()

        self.title(APP_TITLE)
        self.geometry("460x640")
        self.minsize(420, 600)
        self.q: "queue.Queue" = queue.Queue()
        self.server = ServerThread(self.q)
        self.tray = None
        self.state_name = "stopped"   # stopped | starting | running | playing
        self.pending_start = False    # запустить заново, когда остановится (смена имени)
        self.quitting = False

        cfg = core.load_config()
        self.name_value = cfg.get("name") or core.DEFAULT_NAME
        self.device_name = cfg.get("device") or DEFAULT_DEVICE_LABEL
        self.minimize_to_tray = bool(cfg.get("tray", True)) and HAVE_TRAY

        self._set_window_icon()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(150, self.pump)

        self.start_server()
        if start_minimized:
            self.after(300, self.hide_to_tray)

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

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", pady=(18, 4), **pad)
        ctk.CTkLabel(head, text="🎵  " + APP_TITLE, font=ctk.CTkFont(size=22, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(head, text="Звук с iPhone, iPad и Mac на этот компьютер",
                     text_color=GRAY).pack(anchor="w")

        # статус
        box = ctk.CTkFrame(self, corner_radius=12)
        box.pack(fill="x", pady=10, **pad)
        row = ctk.CTkFrame(box, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=(12, 2))
        self.dot = ctk.CTkLabel(row, text="●", text_color=GRAY, font=ctk.CTkFont(size=22))
        self.dot.pack(side="left")
        self.status = ctk.CTkLabel(row, text="Остановлен", font=ctk.CTkFont(size=16, weight="bold"))
        self.status.pack(side="left", padx=8)
        self.hint = ctk.CTkLabel(box, text="", text_color=GRAY, wraplength=400, justify="left")
        self.hint.pack(fill="x", padx=14, pady=(0, 12), anchor="w")

        self.toggle_btn = ctk.CTkButton(self, text="Запустить", height=38, command=self.toggle_server)
        self.toggle_btn.pack(fill="x", **pad)

        # имя
        ctk.CTkLabel(self, text="Название устройства (так оно видно в AirPlay)",
                     anchor="w").pack(fill="x", pady=(16, 2), **pad)
        name_row = ctk.CTkFrame(self, fg_color="transparent")
        name_row.pack(fill="x", **pad)
        self.name_entry = ctk.CTkEntry(name_row, height=34)
        self.name_entry.insert(0, self.name_value)
        self.name_entry.pack(side="left", fill="x", expand=True)
        self.name_entry.bind("<Return>", lambda e: self.save_name())
        ctk.CTkButton(name_row, text="Сохранить", width=100, height=34, command=self.save_name).pack(
            side="left", padx=(8, 0))

        # устройство вывода
        ctk.CTkLabel(self, text="Куда выводить звук", anchor="w").pack(fill="x", pady=(14, 2), **pad)
        self.devices = core.list_output_devices()
        labels = [DEFAULT_DEVICE_LABEL] + [n for _, n in self.devices]
        if self.device_name not in labels:
            self.device_name = DEFAULT_DEVICE_LABEL
        self.device_menu = ctk.CTkOptionMenu(self, values=labels, command=self.on_device, height=34,
                                             dynamic_resizing=False)
        self.device_menu.set(self.device_name)
        self.device_menu.pack(fill="x", **pad)

        # громкость
        vol_row = ctk.CTkFrame(self, fg_color="transparent")
        vol_row.pack(fill="x", pady=(16, 0), **pad)
        ctk.CTkLabel(vol_row, text="Громкость на телефоне").pack(side="left")
        self.vol_label = ctk.CTkLabel(vol_row, text="—", text_color=GRAY)
        self.vol_label.pack(side="right")
        self.vol_bar = ctk.CTkProgressBar(self)
        self.vol_bar.set(0)
        self.vol_bar.pack(fill="x", pady=(4, 0), **pad)

        # настройки
        self.autostart_var = ctk.BooleanVar(value=autostart_enabled())
        cb = ctk.CTkCheckBox(self, text="Запускать вместе с Windows", variable=self.autostart_var,
                             command=self.on_autostart)
        cb.pack(anchor="w", pady=(16, 4), **pad)
        if not autostart_supported():
            cb.configure(state="disabled")
        self.tray_var = ctk.BooleanVar(value=self.minimize_to_tray)
        cb2 = ctk.CTkCheckBox(self, text="При закрытии окна сворачивать в трей", variable=self.tray_var,
                              command=self.on_tray_option)
        cb2.pack(anchor="w", pady=(0, 4), **pad)
        if not HAVE_TRAY:
            cb2.configure(state="disabled")

        # журнал
        ctk.CTkLabel(self, text="Журнал", anchor="w").pack(fill="x", pady=(10, 2), **pad)
        self.log_box = ctk.CTkTextbox(self, height=110, state="disabled", font=ctk.CTkFont(size=12))
        self.log_box.pack(fill="both", expand=True, pady=(0, 16), **pad)

    # ---- вспомогательное ---------------------------------------------------
    def add_log(self, text: str):
        stamp = time.strftime("%H:%M:%S")
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"{stamp}  {text}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

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

    def selected_device_index(self):
        for i, n in self.devices:
            if n == self.device_name:
                return i
        return None

    # ---- действия ----------------------------------------------------------
    def start_server(self):
        if self.server.running:
            self.pending_start = True  # поток ещё останавливается - запустим сразу после него
            return
        name = core.clean_name(self.name_entry.get()) or core.DEFAULT_NAME
        self.name_value = name
        core.save_config(name=name)
        self.set_status("starting", "Запускаю…")
        self.server.start(name, self.selected_device_index())

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
        core.OUTPUT_DEVICE = self.selected_device_index()
        note = "Применится при следующем подключении" if self.state_name == "playing" else "Устройство вывода выбрано"
        self.add_log(f"{note}: {choice}")

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
    def pump(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                self.handle_event(kind, data)
        except queue.Empty:
            pass
        if not self.quitting:
            self.after(150, self.pump)

    def handle_event(self, kind, data):
        if kind == "started":
            self.set_status("running", "Работает",
                            f"Откройте AirPlay на телефоне и выберите «{data['name']}».\n"
                            f"Адрес этого компьютера: {data['ip']}"
                            + ("" if data.get("key") else "\n⚠ Нет файла ключа: часть устройств не подключится"))
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

    def quit_app(self):
        self.quitting = True
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minimized", action="store_true", help="запуститься свёрнутым в трей")
    ap.add_argument("--debug", action="store_true", help="подробный журнал")
    args, _ = ap.parse_known_args()
    core.VERBOSE = args.debug
    App(start_minimized=args.minimized).mainloop()


if __name__ == "__main__":
    main()
