"""Графический терминал AS для контроллеров Kawasaki (только стандартная библиотека)."""
import argparse
import queue
import re
import shlex
import threading
import tkinter as tk
import unicodedata
from tkinter import filedialog, ttk
from tkinter.scrolledtext import ScrolledText

from pykrcc import pykrcc as krcc


_SAVE_LOAD_RE = re.compile(r"^\s*(?:save|load)(?:\s|/|$)", re.IGNORECASE)
_NUMBERED_CHOICES = re.compile(
    r"(?<!\d)(\d+)\s*:\s*(.*?)(?=(?:[,/)、，；]|\s+\d+\s*:|[)]|$))"
)
_LABELLED_CHOICES = re.compile(r"([^,:/()]+?)\s*:\s*(\d+)(?=\s*[,/)、，；]|$)")


def display_controller_text(msg):
    try:
        display_text = msg.encode("latin-1").decode("cp932")
        if any("\ue000" <= char <= "\uf8ff" for char in display_text):
            return msg
        return display_text
    except UnicodeError:
        return msg


def builtin_inquiry_options(msg):
    for pattern, default in krcc.DEFAULT_RESPONSES:
        if pattern not in msg:
            continue
        choice_text = unicodedata.normalize(
            "NFKC", display_controller_text(pattern)
        )
        choices = {}
        for value, label in _NUMBERED_CHOICES.findall(choice_text):
            choices[value + "\n"] = "%s — %s" % (value, label.strip())
        for label, value in _LABELLED_CHOICES.findall(choice_text):
            choices[value + "\n"] = "%s — %s" % (value, label.strip())
        if default == "\n":
            choices.setdefault(default, "Enter")
        elif default not in choices:
            choices[default] = default.strip()
        if default == "0\n" and len(choices) == 1:
            choices["9\n"] = "9"
            choices["2\n"] = "2"
        return [(default, choices[default])] + [
            (value, label) for value, label in choices.items() if value != default
        ]
    return None


class TerminalGui:
    def __init__(self, root, port=None):
        self.root = root
        self.root.title("KRCC — терминал AS")
        self.root.geometry("1000x720")
        self.root.minsize(800, 580)

        self.events = queue.Queue()
        self.actions = queue.Queue()
        self.connected = False
        self.connecting = False
        self.closing = False
        self.pending_inquiry = None

        self.connection_type = tk.StringVar(value="TCP")
        self.host = tk.StringVar(value="192.168.0.2")
        self.tcp_port = tk.StringVar(value="23")
        self.com_port = tk.StringVar(value="1")
        self.timeout = tk.StringVar(value="1000")
        self.log_path = tk.StringVar()
        self.save_path = tk.StringVar()
        self.save_programs = tk.StringVar()
        self.save_qualifiers = tk.StringVar()
        self.load_path = tk.StringVar()
        self.load_qualifier = tk.StringVar()
        self.status = tk.StringVar(value="Не подключено")
        self.progress_text = tk.StringVar()

        if port:
            self._apply_port(port)

        self._build_ui()
        self.worker = threading.Thread(target=self._worker, name="krcc-gui-worker", daemon=True)
        self.worker.start()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(100, self._process_events)

    def _apply_port(self, port):
        parts = port.split()
        if parts and parts[0].upper() == "COM":
            self.connection_type.set("COM")
            return
        if parts and parts[0].upper().startswith("COM"):
            self.connection_type.set("COM")
            self.com_port.set(parts[0][3:])
        elif len(parts) >= 2 and parts[0].upper() == "TCP":
            self.connection_type.set("TCP")
            address = parts[1].split("@")[-1]
            self.host.set(address)
            if len(parts) >= 3:
                self.tcp_port.set(parts[2])

    def _build_ui(self):
        top = ttk.Frame(self.root, padding=(8, 8, 8, 4))
        top.pack(fill=tk.X)

        connection = ttk.LabelFrame(top, text="Соединение и настройки", padding=8)
        connection.pack(fill=tk.X)
        ttk.Label(connection, text="Тип:").grid(row=0, column=0, sticky=tk.W, padx=(0, 4))
        type_box = ttk.Combobox(
            connection, textvariable=self.connection_type, values=("TCP", "COM"),
            state="readonly", width=7,
        )
        type_box.grid(row=0, column=1, sticky=tk.W, padx=(0, 10))
        type_box.bind("<<ComboboxSelected>>", self._update_connection_fields)
        ttk.Label(connection, text="IP-адрес:").grid(row=0, column=2, sticky=tk.W, padx=(0, 4))
        self.host_entry = ttk.Entry(connection, textvariable=self.host, width=20)
        self.host_entry.grid(row=0, column=3, sticky=tk.W, padx=(0, 10))
        ttk.Label(connection, text="TCP-порт:").grid(row=0, column=4, sticky=tk.W, padx=(0, 4))
        self.tcp_port_entry = ttk.Entry(connection, textvariable=self.tcp_port, width=7)
        self.tcp_port_entry.grid(row=0, column=5, sticky=tk.W, padx=(0, 10))
        ttk.Label(connection, text="COM:").grid(row=0, column=6, sticky=tk.W, padx=(0, 4))
        self.com_port_entry = ttk.Entry(connection, textvariable=self.com_port, width=5)
        self.com_port_entry.grid(row=0, column=7, sticky=tk.W, padx=(0, 10))
        ttk.Label(connection, text="Тайм-аут, мс:").grid(
            row=1, column=0, sticky=tk.W, padx=(0, 4), pady=(6, 0)
        )
        ttk.Entry(connection, textvariable=self.timeout, width=7).grid(
            row=1, column=1, sticky=tk.W, padx=(0, 10), pady=(6, 0)
        )
        self.connect_button = ttk.Button(connection, text="Подключиться", command=self.connect)
        self.connect_button.grid(row=1, column=2, sticky=tk.W, padx=(0, 4), pady=(6, 0))
        self.disconnect_button = ttk.Button(
            connection, text="Отключиться", command=self.disconnect, state=tk.DISABLED
        )
        self.disconnect_button.grid(row=1, column=3, sticky=tk.W, pady=(6, 0))

        operations = ttk.Frame(top)
        operations.pack(fill=tk.X, pady=(6, 0))
        save_frame = ttk.LabelFrame(operations, text="SAVE — сохранить с контроллера", padding=6)
        save_frame.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        ttk.Label(save_frame, text="Файл:").grid(row=0, column=0, sticky=tk.W)
        ttk.Entry(save_frame, textvariable=self.save_path).grid(
            row=0, column=1, sticky=tk.EW, padx=4
        )
        ttk.Button(save_frame, text="Обзор…", command=self._browse_save).grid(row=0, column=2)
        ttk.Label(save_frame, text="Программы:").grid(row=1, column=0, sticky=tk.W, pady=(4, 0))
        ttk.Entry(save_frame, textvariable=self.save_programs).grid(
            row=1, column=1, sticky=tk.EW, padx=4, pady=(4, 0)
        )
        ttk.Label(save_frame, text="Параметры:").grid(row=1, column=2, sticky=tk.W, pady=(4, 0))
        ttk.Entry(save_frame, textvariable=self.save_qualifiers, width=12).grid(
            row=1, column=3, sticky=tk.EW, padx=(4, 0), pady=(4, 0)
        )
        ttk.Button(save_frame, text="Сохранить (SAVE)", command=self.save_program).grid(
            row=2, column=1, sticky=tk.W, padx=4, pady=(5, 0)
        )
        save_frame.columnconfigure(1, weight=1)

        load_frame = ttk.LabelFrame(operations, text="LOAD — загрузить на контроллер", padding=6)
        load_frame.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))
        ttk.Label(load_frame, text="Файл:").grid(row=0, column=0, sticky=tk.W)
        ttk.Entry(load_frame, textvariable=self.load_path).grid(
            row=0, column=1, sticky=tk.EW, padx=4
        )
        ttk.Button(load_frame, text="Обзор…", command=self._browse_load).grid(row=0, column=2)
        ttk.Label(load_frame, text="Параметры:").grid(row=1, column=0, sticky=tk.W, pady=(4, 0))
        ttk.Entry(load_frame, textvariable=self.load_qualifier, width=12).grid(
            row=1, column=1, sticky=tk.W, padx=4, pady=(4, 0)
        )
        ttk.Button(load_frame, text="Загрузить (LOAD)", command=self.load_program).grid(
            row=1, column=2, pady=(4, 0)
        )
        load_frame.columnconfigure(1, weight=1)

        log_frame = ttk.LabelFrame(top, text="Журнал контроллера", padding=6)
        log_frame.pack(fill=tk.X, pady=(6, 0))
        ttk.Entry(log_frame, textvariable=self.log_path).pack(
            side=tk.LEFT, fill=tk.X, expand=True
        )
        ttk.Button(log_frame, text="Обзор…", command=self._choose_log).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        ttk.Button(log_frame, text="Включить журнал", command=self.start_log).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        ttk.Button(log_frame, text="Выключить", command=self.stop_log).pack(
            side=tk.LEFT, padx=(4, 0)
        )

        terminal = ttk.Frame(self.root, padding=(8, 4, 8, 8))
        terminal.pack(fill=tk.BOTH, expand=True)
        self.output = ScrolledText(terminal, wrap=tk.WORD, state=tk.DISABLED)
        self.output.pack(fill=tk.BOTH, expand=True)

        command_frame = ttk.Frame(terminal)
        command_frame.pack(fill=tk.X, pady=(8, 0))
        self.command_entry = ttk.Entry(command_frame)
        self.command_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.command_entry.bind("<Return>", self._submit_event)
        ttk.Button(command_frame, text="Отправить", command=self._submit).pack(
            side=tk.LEFT, padx=(8, 0)
        )

        progress_frame = ttk.Frame(terminal)
        progress_frame.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(progress_frame, textvariable=self.progress_text).pack(side=tk.LEFT)
        self.progress = ttk.Progressbar(progress_frame, mode="determinate", length=220)
        self.progress.pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=(8, 0))

        ttk.Label(self.root, textvariable=self.status, anchor=tk.W, relief=tk.SUNKEN).pack(
            fill=tk.X, side=tk.BOTTOM
        )
        self._update_connection_fields()
        self._append("Введите AS-команду или !help для справки.\n")

    def _update_connection_fields(self, _event=None):
        is_tcp = self.connection_type.get() == "TCP"
        tcp_state = "normal" if is_tcp else "disabled"
        com_state = "disabled" if is_tcp else "normal"
        self.host_entry.configure(state=tcp_state)
        self.tcp_port_entry.configure(state=tcp_state)
        self.com_port_entry.configure(state=com_state)

    def _append(self, text):
        self.output.configure(state=tk.NORMAL)
        self.output.insert(tk.END, text)
        self.output.see(tk.END)
        self.output.configure(state=tk.DISABLED)

    def _read_timeout(self):
        try:
            timeout = int(self.timeout.get())
        except ValueError:
            raise ValueError("Тайм-аут должен быть целым числом.")
        if timeout < 1:
            raise ValueError("Тайм-аут должен быть положительным.")
        return timeout

    def _connection_string(self):
        if self.connection_type.get() == "COM":
            try:
                number = int(self.com_port.get())
            except ValueError:
                raise ValueError("Укажите номер COM-порта от 1 до 256.")
            if not 1 <= number <= 256:
                raise ValueError("Укажите номер COM-порта от 1 до 256.")
            return "COM%d" % number

        host = self.host.get().strip()
        if not host:
            raise ValueError("Укажите IP-адрес контроллера.")
        try:
            port = int(self.tcp_port.get())
        except ValueError:
            raise ValueError("Укажите TCP-порт от 1 до 65535.")
        if not 1 <= port <= 65535:
            raise ValueError("Укажите TCP-порт от 1 до 65535.")
        return "TCP as@%s %d" % (host, port)

    def connect(self):
        if self.connected or self.connecting:
            return
        try:
            port = self._connection_string()
            self._read_timeout()
        except ValueError as error:
            self._append("Ошибка настроек: %s\n" % error)
            return
        self.connecting = True
        self.connect_button.configure(state=tk.DISABLED)
        self.status.set("Подключение…")
        self.actions.put(("connect", port))

    def disconnect(self):
        if self.connected or self.connecting:
            self.actions.put(("disconnect",))

    def _choose_log(self):
        filename = filedialog.asksaveasfilename(
            title="Файл журнала", defaultextension=".log",
            filetypes=(("Журнал", "*.log"), ("Все файлы", "*.*")),
        )
        if filename:
            self.log_path.set(filename)

    def _browse_save(self):
        filename = filedialog.asksaveasfilename(
            title="Сохранить программу контроллера",
            defaultextension=".as",
            filetypes=(("Программы AS", "*.as"), ("Все файлы", "*.*")),
        )
        if filename:
            self.save_path.set(filename)

    def _browse_load(self):
        filename = filedialog.askopenfilename(
            title="Выбрать файл для загрузки",
            filetypes=(("Программы AS", "*.as"), ("Все файлы", "*.*")),
        )
        if filename:
            self.load_path.set(filename)

    def save_program(self):
        filename = self.save_path.get().strip()
        if not filename:
            self._append("Укажите файл для SAVE или нажмите «Обзор…».\n")
            return
        self.actions.put((
            "save", filename, self.save_programs.get().strip(),
            self.save_qualifiers.get().strip(),
        ))

    def load_program(self):
        filename = self.load_path.get().strip()
        if not filename:
            self._append("Укажите файл для LOAD или нажмите «Обзор…».\n")
            return
        self.actions.put(("load", filename, self.load_qualifier.get().strip()))

    def start_log(self):
        filename = self.log_path.get().strip()
        if not filename:
            self._append("Укажите имя файла журнала в настройках.\n")
            return
        self.actions.put(("start_log", filename))

    def stop_log(self):
        self.actions.put(("stop_log",))

    def _submit_event(self, _event):
        self._submit()
        return "break"

    def _submit(self):
        line = self.command_entry.get().strip()
        if not line:
            return
        self.command_entry.delete(0, tk.END)
        if line.startswith("!"):
            self._local_command(line)
            return
        if _SAVE_LOAD_RE.match(line):
            self._append("Команда заблокирована: используйте !save или !load.\n")
            return
        try:
            timeout = self._read_timeout()
        except ValueError as error:
            self._append("Ошибка настроек: %s\n" % error)
            return
        self._append("AS> %s\n" % line)
        self.actions.put(("command", line, timeout))

    def _local_command(self, line):
        try:
            args = shlex.split(line[1:], posix=False)
        except ValueError as error:
            self._append("Ошибка команды: %s\n" % error)
            return
        args = [
            item[1:-1] if len(item) >= 2 and item[0] == item[-1] and item[0] in ("'", '"')
            else item
            for item in args
        ]
        if not args:
            return
        command, values = args[0].lower(), args[1:]
        if command in ("quit", "q", "exit"):
            self.close()
        elif command == "help":
            self._append(
                "\nКоманды терминала:\n"
                "  !save <файл> [программы] [/P/R/L..]  сохранить программу\n"
                "  !load <файл> [/Q]                    загрузить программу\n"
                "  !log <файл> | !log off               включить/выключить журнал\n"
                "  !tmo <мс>                            тайм-аут ответа на команду\n"
                "  !help                                показать эту справку\n"
                "  !quit                                отключиться и выйти\n"
                "Обычные строки отправляются контроллеру как AS-команды.\n"
                "SAVE и LOAD заблокированы; используйте !save / !load.\n\n"
            )
        elif command == "tmo" and values:
            try:
                timeout = int(values[0])
            except ValueError:
                self._append("Укажите тайм-аут в миллисекундах.\n")
            else:
                if timeout < 1:
                    self._append("Тайм-аут должен быть положительным.\n")
                else:
                    self.timeout.set(str(timeout))
                    self._append("Тайм-аут: %d мс\n" % timeout)
        elif command == "save" and values:
            qualifiers = [item for item in values[1:] if item.startswith("/")]
            programs = [item for item in values[1:] if not item.startswith("/")]
            self.actions.put((
                "save", values[0], programs[0] if programs else "", "".join(qualifiers)
            ))
        elif command == "load" and values:
            self.actions.put(("load", values[0], values[1] if len(values) > 1 else ""))
        elif command == "log" and values:
            if values[0].lower() == "off":
                self.stop_log()
            else:
                self.actions.put(("start_log", " ".join(values)))
        else:
            self._append("Неизвестная команда или не хватает аргументов. Введите !help.\n")

    def _worker(self):
        com = None
        while True:
            try:
                action = self.actions.get(timeout=0.5)
            except queue.Empty:
                if com is not None:
                    if not com.IsConnected:
                        com.dispose()
                        com = None
                        self.events.put(("status", False, "Соединение потеряно"))
                    else:
                        _, text = com.command()
                        if text.strip():
                            self.events.put(("output", text.rstrip() + "\n"))
                continue

            operation = action[0]
            if operation == "shutdown":
                if com is not None:
                    com.dispose()
                return
            if operation == "connect":
                if com is not None:
                    com.dispose()
                try:
                    com = krcc.Commu(action[1])
                except krcc.CommuError as error:
                    com = None
                    self.events.put(("error", "Ошибка подключения: %s\n" % error))
                    self.events.put(("status", False, "Не подключено"))
                    continue
                com.progress = lambda done, total: self.events.put(
                    ("progress", done, total)
                )
                com.asInquiry = self._request_inquiry
                com.load_save_output = lambda msg: self.events.put(
                    ("load_save_output", msg)
                )
                self.events.put(("status", True, "Подключено к %s" % com.name()))
            elif operation == "disconnect":
                if com is not None:
                    com.dispose()
                    com = None
                self.events.put(("status", False, "Не подключено"))
            elif operation == "command":
                if com is None or not com.IsConnected:
                    self.events.put(("error", "[соединение не установлено]\n"))
                    continue
                code, text = com.command(action[1], action[2])
                if text:
                    self.events.put(("output", text.replace("\r\n", "\n")))
                    if not text.endswith(("\n", "\r")):
                        self.events.put(("output", "\n"))
                if code == -1:
                    self.events.put(("error", "[нет ответа]\n"))
                elif code == -2:
                    self.events.put(("error", "[ожидается ввод]\n"))
            elif operation in ("save", "load"):
                if com is None or not com.IsConnected:
                    self.events.put(("error", "[соединение не установлено]\n"))
                    continue
                if operation == "save":
                    _, filename, programs, qualifiers = action
                    result = com.save(filename, programs, qualifiers)
                else:
                    _, filename, qualifier = action
                    result = com.load(filename, qualifier)
                self.events.put(("output", "%s -> %s\n" % (operation, result)))
            elif operation == "start_log":
                if com is None or not com.IsConnected:
                    self.events.put(("error", "[соединение не установлено]\n"))
                elif com.startLog(action[1]):
                    self.events.put(("output", "Журнал включён: %s\n" % action[1]))
                else:
                    self.events.put(("error", "Не удалось открыть журнал: %s\n" % action[1]))
            elif operation == "stop_log":
                if com is not None:
                    com.stopLog()
                self.events.put(("output", "Журнал выключен.\n"))

    def _process_events(self):
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            if event[0] == "output":
                self._append(event[1])
            elif event[0] == "error":
                self._append(event[1])
            elif event[0] == "load_save_output":
                text = display_controller_text(event[1])
                self._append(text + ("" if text.endswith(("\n", "\r")) else "\n"))
            elif event[0] == "inquiry":
                _, msg, choices, response = event
                if self.closing:
                    response.put(choices[0][0])
                else:
                    self._show_inquiry_dialog(msg, choices, response)
            elif event[0] == "status":
                self.connected = event[1]
                self.connecting = False
                self.status.set(event[2])
                self.connect_button.configure(
                    state=tk.DISABLED if self.connected else tk.NORMAL
                )
                self.disconnect_button.configure(
                    state=tk.NORMAL if self.connected else tk.DISABLED
                )
            elif event[0] == "progress":
                done, total = event[1], event[2]
                if done < 0:
                    self.progress_text.set("")
                    self.progress.configure(value=0)
                else:
                    self.progress_text.set("%s / %s" % (done, total))
                    if total > 0:
                        self.progress.configure(maximum=total, value=done)
        if not self.closing:
            self.root.after(100, self._process_events)
        elif not self.worker.is_alive():
            self.root.destroy()
        else:
            self.root.after(50, self._process_events)

    def _request_inquiry(self, msg):
        choices = builtin_inquiry_options(msg)
        if choices is None:
            return None
        response = queue.Queue(maxsize=1)
        self.events.put(("inquiry", msg, choices, response))
        return response.get()

    def _show_inquiry_dialog(self, msg, choices, response):
        dialog = tk.Toplevel(self.root)
        dialog.title("Вопрос контроллера")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        self.pending_inquiry = (dialog, response, choices[0][0])

        text = display_controller_text(msg)
        ttk.Label(
            dialog, text=text, wraplength=480, justify=tk.LEFT, padding=16
        ).pack(fill=tk.X)
        buttons = ttk.Frame(dialog, padding=(8, 0, 8, 8))
        buttons.pack(fill=tk.X)

        def choose(value):
            if response.empty():
                response.put(value)
            self.pending_inquiry = None
            dialog.destroy()

        for value, label in choices:
            ttk.Button(
                buttons, text=label, command=lambda answer=value: choose(answer)
            ).pack(side=tk.LEFT, padx=4)

        dialog.protocol("WM_DELETE_WINDOW", lambda: choose(choices[0][0]))
        dialog.bind("<Escape>", lambda _event: choose(choices[0][0]))
        dialog.grab_set()
        dialog.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - dialog.winfo_width()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - dialog.winfo_height()) // 2
        dialog.geometry("+%d+%d" % (x, y))
        dialog.focus_set()

    def close(self):
        if self.closing:
            return
        self.closing = True
        if self.pending_inquiry is not None:
            dialog, response, default = self.pending_inquiry
            self.pending_inquiry = None
            if response.empty():
                response.put(default)
            dialog.destroy()
        self.status.set("Завершение…")
        self.actions.put(("shutdown",))


def main():
    parser = argparse.ArgumentParser(description="Графический терминал AS (pykrcc)")
    parser.add_argument(
        "port", nargs="?", help='строка подключения, например "TCP 192.168.0.2 23"'
    )
    args = parser.parse_args()
    root = tk.Tk()
    TerminalGui(root, args.port)
    root.mainloop()


if __name__ == "__main__":
    main()
