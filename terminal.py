"""Интерактивный терминал AS для контроллеров Kawasaki."""
import argparse
import re
import sys
import threading
import time
from pykrcc import pykrcc as krcc


_COLORS = {
    "cyan": "\033[96m",
    "green": "\033[92m",
    "yellow": "\033[93m",
    "red": "\033[91m",
    "bold": "\033[1m",
    "reset": "\033[0m",
}


def paint(text, color):
    if not sys.stdout.isatty():
        return text
    return _COLORS[color] + text + _COLORS["reset"]


def print_banner():
    print(paint("╭──────────────────────────────────────╮", "cyan"))
    print(paint("│       KRCC  •  терминал AS            │", "cyan"))
    print(paint("╰──────────────────────────────────────╯", "cyan"))


def print_help():
    print(paint("\nКоманды терминала", "bold"))
    print("  !save <файл> [программы] [/P/R/L..]  сохранить программу")
    print("  !load <файл> [/Q]                    загрузить программу")
    print("  !log <файл> | !log off               включить/выключить журнал")
    print("  !tmo <мс>                            тайм-аут ответа на команду")
    print("  !help                                показать эту справку")
    print("  !quit                                отключиться и выйти")
    print("  Обычные строки отправляются контроллеру как AS-команды.")
    print("  SAVE и LOAD как обычные AS-команды заблокированы; используйте !save / !load.\n")


def read_number(prompt, default, minimum, maximum):
    while True:
        value = input(prompt).strip()
        if not value:
            return default
        try:
            number = int(value)
        except ValueError:
            number = None
        if number is not None and minimum <= number <= maximum:
            return number
        print(paint("Введите число от %s до %s." % (minimum, maximum), "red"))


def prompt_connection():
    print_banner()
    while True:
        kind = input("Тип соединения: 1 — TCP, 2 — COM [1]: ").strip() or "1"
        if kind in ("1", "2"):
            break
        print(paint("Выберите 1 (TCP) или 2 (COM).", "red"))

    if kind == "2":
        port_number = read_number("Номер COM-порта [1]: ", 1, 1, 256)
        return "COM%d" % port_number

    host = input("IP-адрес контроллера [192.168.0.2]: ").strip() or "192.168.0.2"
    tcp_port = read_number("TCP-порт [23]: ", 23, 1, 65535)
    return "TCP as@%s %d" % (host, tcp_port)


def is_manual_save_load(line):
    return bool(re.match(r"^\s*(?:save|load)(?:\s|/|$)", line, re.IGNORECASE))


def watcher(com, stop):
    """Фоновый вывод самопроизвольных сообщений контроллера."""
    while not stop.is_set():
        time.sleep(0.5)
        if com.IsConnected:
            _, text = com.command()
            if text.strip():
                print("\r" + text.rstrip(), flush=True)


def local(com, line, state):
    p = line[1:].split()
    if not p:
        return True
    c, a = p[0].lower(), p[1:]
    if c in ("quit", "q", "exit"):
        return False
    if c == "help":
        print(__doc__)
    elif c == "save" and a:
        quals = [x for x in a[1:] if x.startswith("/")]
        progs = [x for x in a[1:] if not x.startswith("/")]
        print("save ->", com.save(a[0], progs[0] if progs else "", "".join(quals)))
        print(com.LoadSaveMsg, end="")
    elif c == "load" and a:
        print("load ->", com.load(a[0], a[1] if len(a) > 1 else ""))
        print(com.LoadSaveMsg, end="")
    elif c == "log" and a:
        if a[0] == "off":
            com.stopLog()
            print("журнал выключен")
        else:
            print("журнал ->", a[0], "ok" if com.startLog(a[0]) else "ошибка")
    elif c == "tmo" and a:
        try:
            timeout = int(a[0])
        except ValueError:
            print("Укажите тайм-аут в миллисекундах.")
        else:
            if timeout < 1:
                print("Тайм-аут должен быть положительным.")
            else:
                state["tmo"] = timeout
                print("Тайм-аут: %d мс" % timeout)
    else:
        print("?  (!help)")
    return True


def main():
    ap = argparse.ArgumentParser(description="Интерактивный терминал AS (pykrcc)")
    ap.add_argument("port", nargs="?", help='строка подключения, например "TCP 192.168.0.2 23"')
    ap.add_argument("--timeout", type=int, default=1000, help="ожидание ответа на команду, мс")
    ap.add_argument("--no-progress", action="store_true")
    args = ap.parse_args()

    try:
        port = args.port or prompt_connection()
        com = krcc.Commu(port)
    except KeyboardInterrupt:
        print("\nВыход.")
        return
    except krcc.CommuError as e:
        sys.exit(paint("Ошибка подключения: %s" % e, "red"))
    if not args.no_progress:
        com.progress = lambda v, t: print(
            "\r%s / %s" % (v, t), end="" if v >= 0 else "\n", flush=True
        )

    state = {"tmo": args.timeout}
    stop = threading.Event()
    threading.Thread(target=watcher, args=(com, stop), daemon=True).start()
    print(paint("✓ Подключено к %s" % com.name(), "green"))
    print_help()
    try:
        while True:
            if not com.IsConnected:
                print(paint("[соединение потеряно]", "red"))
                break
            try:
                line = input(paint("AS> ", "cyan"))
            except EOFError:
                break
            if line.startswith("!"):
                if not local(com, line, state):
                    break
                continue
            if is_manual_save_load(line):
                print(paint("Команда заблокирована: используйте !save или !load.", "yellow"))
                continue
            code, text = com.command(line, state["tmo"])
            print(text.replace("\r\n", "\n"), end="" if text.endswith(("\n", "\r")) or not text else "\n")
            if code == -1:
                print(paint("[нет ответа]", "yellow"))
            elif code == -2:
                print(paint("[ожидается ввод]", "yellow"))
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        com.dispose()


if __name__ == "__main__":
    main()
