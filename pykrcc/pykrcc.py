"""
pykrcc -- чистая Python-реализация класса ``KRcc.Commu`` (Kawasaki Robot Control Class Library).

Модуль общается с контроллером робота Kawasaki (язык AS) по TCP/IP (telnet-порт 23,
либо PC-AS/ROSET) и, в экспериментальном режиме, по последовательному порту. Нужна
только стандартная библиотека Python 3.8+, работает на Windows, Linux и macOS;
``krcc64.dll``, .NET и pythonnet **не требуются**.

Что реализовано
===============

* :class:`Commu` -- соединение с контроллером: ``command``, ``load``, ``save``,
  ``connect``/``disconnect``, ``startLog``/``stopLog``, ``name``, свойства
  ``IsConnected``, ``TimeoutValue``, ``LoadSaveMsg`` и делегаты ``asInquiry``,
  ``cmdInquiry``, ``progress``, ``load_save_output``.
* Делегаты :data:`ASInquiry`, :data:`CmdInquiry`, :data:`Progress` и
  :data:`LoadSaveOutput`
  (в Python это обычные вызываемые объекты).
* Исключения :class:`CommuError`, :class:`ConnectError`, :class:`LoginError`,
  :class:`NoResponseError` с теми же текстами, что и у .NET-библиотеки
  (``"can't connect TCP/IP"``, ``"can't login"``).

Имена как в оригинале и в стиле Python
======================================

Каждый член оригинального API доступен под двумя именами::

    com.IsConnected   == com.is_connected
    com.TimeoutValue  =  com.timeout_value = 3000
    com.LoadSaveMsg   == com.load_save_msg
    com.asInquiry     == com.as_inquiry
    com.cmdInquiry    == com.cmd_inquiry
    com.startLog(..)  == com.start_log(..)
    com.stopLog()     == com.stop_log()
    com.Dispose()     == com.dispose()

Класс также доступен под именем :class:`Ccommu` (синоним :class:`Commu`) и
поддерживает ``with``::

    import pykrcc

    with pykrcc.Commu("TCP 192.168.0.2") as com:
        code, text = com.command("where", 3000)

Делегаты в Python
=================

``ASInquiry(msg: str) -> Optional[str]``
    Вызывается при ``load``/``save``, когда контроллер прислал вопрос. Верните
    строку-ответ (например ``"0\\n"``) или ``None`` -- тогда будет использован ответ
    по умолчанию из встроенной таблицы (см. :data:`DEFAULT_RESPONSES`).

``CmdInquiry(msg: str) -> Optional[str]``
    Вызывается из ``command`` для каждой строки ответа контроллера (когда делегат
    задан, строки *не* накапливаются в результате). Верните строку, чтобы отправить
    её контроллеру (например ``" "`` на ``"Press SPACE key continue."``), иначе
    ``None``.  Сигнатуру из примера документации ``(msg, ref resp) -> bool``
    в Python выразить нельзя, поэтому ответ возвращается как значение.

``Progress(done: int, total: int) -> None``
    Ход ``load``/``save``: ``done`` -- число переданных байт (``-1`` по окончании),
    ``total`` -- размер загружаемого файла (при ``save`` всегда ``-1``).

``LoadSaveOutput(msg: str) -> None``
    Вызывается для каждой текстовой строки контроллера во время ``load``/``save``,
    в том числе для вопросов до вызова :data:`ASInquiry`.

Протокол (восстановлен по krcc64.dll)
=====================================

* Логин: после TCP-соединения отправляются 5 байт ``FF FA 18 F0 0A``, пауза 1 с,
  имя ``khidl`` (или ``as``) + ``CR LF``; контроллер отвечает приглашением ``>``
  в начале строки.
* Команда: ``<текст>\n``; контроллер возвращает эхо, строки ответа и снова ``>``.
* ``SAVE``/``LOAD`` отправляют служебное имя ``using.rcc``; реальные данные
  переносятся кадрами ``ENQ STX <A..F> ... ETB`` (open/read/write/close).

.. note::
   Протокол восстановлен дизассемблированием DLL и проверен на собственной
   имитации контроллера (``mock_controller.py``), но **не** на реальном роботе.
   Если на вашем контроллере что-то ведёт себя иначе -- включите отладочный журнал
   (``pykrcc.DEBUG = True``) и сравните байты.
"""

from __future__ import annotations

import os
import select
import socket
import sys
import threading
import time
from collections import deque
from typing import Callable, List, Optional, Tuple

__all__ = [
    "Commu", "Ccommu", "ASInquiry", "CmdInquiry", "Progress",
    "CommuError", "ConnectError", "LoginError", "NoResponseError",
    "LoadSaveOutput",
    "DEFAULT_RESPONSES", "DEBUG",
]
__version__ = "2.0"

#: Печатать в stderr отладочную информацию о принятых/отправленных байтах.
DEBUG = False

# --------------------------------------------------------------------------- #
# Делегаты (в Python -- обычные callable)
# --------------------------------------------------------------------------- #

#: ``delegate string ASInquiry(string)`` -- вопрос контроллера при load/save.
ASInquiry = Callable[[str], Optional[str]]
#: ``delegate string CmdInquiry(string)`` -- строка ответа контроллера в command().
CmdInquiry = Callable[[str], Optional[str]]
#: ``delegate void Progress(int, int)`` -- ход load/save.
Progress = Callable[[int, int], None]
#: Полученная текстовая строка во время SAVE/LOAD.
LoadSaveOutput = Callable[[str], None]


# --------------------------------------------------------------------------- #
# Исключения
# --------------------------------------------------------------------------- #

class CommuError(Exception):
    """Базовое исключение библиотеки; ``str(e)`` совпадает с ``e.Message`` из .NET."""

    @property
    def Message(self) -> str:  # совместимость с примерами на C#/VB
        return str(self)


class ConnectError(CommuError):
    """Не удалось установить соединение: ``"can't connect TCP/IP"``."""


class LoginError(CommuError):
    """Соединение есть, но контроллер не принял логин: ``"can't login"``."""


class NoResponseError(CommuError):
    """Контроллер не ответил на проверочную команду: ``"no response TCP port"``."""


# --------------------------------------------------------------------------- #
# Константы протокола
# --------------------------------------------------------------------------- #

_ENQ, _STX, _ETB, _SUB, _DLE, _BEL, _SYN = 5, 2, 23, 26, 16, 7, 22
#: Управляющие символы, которые контроллер вставляет в текст сообщений.
_CTRL = (0x10, 0x07, 0x12, 0x14, 0x16, 0x19)
_FLUSH = 0xF000            # маркер «конец порции» в кольцевом буфере
_DUMMY_NAME = "using.rcc"  # служебное имя файла в командах SAVE/LOAD
_CHUNK = 2910              # размер порции данных при LOAD (кадр 'C')
_TELNET_GREETING = b"\xff\xfa\x18\xf0\n"
_E_OPEN = -3               # внутренняя ошибка: файл не открывается

#: Встроенные ответы на вопросы контроллера: (образец в байтах/latin-1, ответ).
#: Таблица извлечена из krcc64.dll (секции [builtin-SaveMsg] и [builtin-LoadMsg]);
#: сообщения на японском хранятся в исходной кодировке (cp932), поэтому образцы
#: сопоставляются как latin-1-представление сырых байтов.
_BUILTIN = [
    (b'\x8am\x94F\x81I\x81i\x83\x8a\x83^\x81[\x83\x93\x93\xfc\x97\xcd\x81j', '\n'),
    (b'Confirm! (Press ENTER.)', '\n'),
    (b'Confermare! (Schiacciare ENTER.)', '\n'),
    (b'Vahvista! (paina ENTER.)', '\n'),
    (b'Best\xe4tigen! (Dr\xfccken Sie ENTER.)', '\n'),
    (b'\x82O\x81F\x8fI\x97\xb9\x81A\x82X\x81F\x8d\xc4\x8e\x8e\x8ds', '0\n'),
    (b'0:abort, 9:retry', '0\n'),
    (b'0:interromp., 9:riprovare.', '0\n'),
    (b'\x82P\x81F\x83v\x83\x8d\x83O\x83\x89\x83\x80\x82\xf0\x8d\xed\x8f\x9c\x82\xb5\x8fI\x97\xb9', '1\n'),
    (b'1:Delete program and abort', '1\n'),
    (b'1:Elimina pg e interrompe', '1\n'),
    (b'1:Poista ohjelma ja poistu', '1\n'),
    (b'1:Programm l\xf6schen und abbrechen', '1\n'),
    (b'\x8am\x94F! \x8e\xc0\x8ds\x82\xb5\x82\xdc\x82\xb7\x82\xa9\x81H (\x82\xcd\x82\xa2:\x82P, \x82\xa2\x82\xa2\x82\xa6:\x82O)', '0\n'),
    (b'Are you sure ? (Yes:1, No:0)', '0\n'),
    (b'Sei sicuro? (S\xec: 1, No:0)', '0\n'),
    (b'Oletko varma? (Kylla:1, Ei:0)', '0\n'),
    (b'Sind Sie sicher ? (Ja:1, Nein:0)', '0\n'),
    (b'\x82P\x81F\x83X\x83L\x83b\x83v, \x82X\x81F\x8b\xad\x90\xa7\x83\x8d\x81[\x83h', '1\n'),
    (b'1:Skip, 9:Force load', '1\n'),
    (b'1:Saltare, 9:Caricare forza', '1\n'),
    (b'\x8d\xed\x8f\x9c\x82\xb5\x82\xdc\x82\xb5\x82\xbd\x81B \x8am\x94F\x81I', '\n'),
    (b'deleted. Confirm !', '\n'),
    (b'fallito. Confermare!', '\n'),
    (b'poistettu. Vahvista!', '\n'),
    (b'gel\xf6scht. Best\xe4tigen!', '\n'),
    (b'\x8am\x94F\x81I\x81i\x83\x8a\x83^\x81[\x83\x93\x93\xfc\x97\xcd\x81j', '\n'),
    (b'Confirm! (Press ENTER.)', '\n'),
    (b'Confermare! (Schiacciare ENTER.)', '\n'),
    (b'Vahvista! (paina ENTER.)', '\n'),
    (b'Best\xe4tigen! (Dr\xfccken Sie ENTER.)', '\n'),
    (b'\x81i\x82P\x81F\x82\xcd\x82\xa2\x81C\x82O\x81F\x82\xa2\x82\xa2\x82\xa6\x81^\x82Q\x81F\x82\xa0\x82\xc6\x91S\x95\x94\x93\xc7\x82\xdd\x8d\x9e\x82\xdd\x81C\x82R\x81F\x8fI\x97\xb9\x81j', '0\n'),
    (b'1:Yes, 0:No / 2:Load all, 3:Exit)', '0\n'),
    (b'1:Si, 0:No / 2:Caricare tutti, 3:Exit)', '0\n'),
    (b'1:Kylla, 0:Ei / 2:Lataa kaikki, 3:Pois)', '0\n'),
    (b'1:Ja, 0:Nein / 2:Alles laden, 3:Exit)', '0\n'),
    (b'\x8f\x88\x97\x9d\x82\xf0\x91I\x82\xf1\x82\xc5\x89\xba\x82\xb3\x82\xa2\x81B', '0\n'),
    (b'Select procedure.', '0\n'),
    (b'Seleziona procedura desiderata.', '0\n'),
    (b'Valitse toiminto.', '0\n'),
    (b'Gewuenschtes Verfahren auswaehlen', '0\n'),
]
DEFAULT_RESPONSES = [(p.decode("latin-1"), r) for p, r in _BUILTIN]


def _dbg(*a):
    if DEBUG:
        print("[pykrcc]", *a, file=sys.stderr)


# --------------------------------------------------------------------------- #
# Кольцевой буфер «слов» (аналог TRingBuff)
# --------------------------------------------------------------------------- #

class _Ring:
    """Очередь принятых символов.

    Элемент -- целое: ``0..255`` обычный байт, ``0xFFxx`` -- байт-терминатор строки
    (LF, ETB, DLE, ``>`` в начале строки), ``0xF000`` -- принудительный конец порции.
    Каждый терминатор «отпускает» семафор, поэтому ``get`` возвращает по одной
    строке (вместе с её терминатором).
    """

    def __init__(self):
        self._q = deque()
        self._lock = threading.Lock()
        self._sem = threading.Semaphore(0)

    def push(self, v: int) -> None:
        with self._lock:
            self._q.append(v)

    def signal(self) -> None:
        self._sem.release()

    def clear(self) -> None:
        with self._lock:
            self._q.clear()
        while self._sem.acquire(False):
            pass

    def empty(self) -> bool:
        with self._lock:
            return not self._q

    def get(self, tmo_ms: int, maxlen: int = 4095) -> Tuple[Optional[bytes], bool]:
        """Вернуть ``(строка|None, timed_out)``.

        ``tmo_ms == 0`` -- не ждать; отрицательное значение -- ждать до 60 с (как в
        оригинале). По таймауту отдаётся недочитанный остаток строки (так приходят
        приглашения и вопросы без перевода строки).
        """
        if tmo_ms < 0:
            tmo_ms = 60000
        if tmo_ms == 0:
            self._sem.acquire(False)
            timed_out = False
        else:
            timed_out = not self._sem.acquire(timeout=tmo_ms / 1000.0)
        with self._lock:
            if not self._q:
                return None, timed_out
            if timed_out and self._q[0] == _ENQ:
                return None, timed_out
            out = bytearray()
            while self._q:
                v = self._q.popleft()
                if v == _FLUSH:
                    break
                out.append(v & 0xFF)
                if v & 0xFF00:           # терминатор
                    break
                if len(out) >= maxlen:
                    break
            return bytes(out), timed_out


# --------------------------------------------------------------------------- #
# Транспорты
# --------------------------------------------------------------------------- #

class _TcpTransport:
    def __init__(self, host: str, port: int, conn_tmo_ms: int, nodelay: bool):
        try:
            self.sock = socket.create_connection((host, port), timeout=conn_tmo_ms / 1000.0)
        except (OSError, socket.timeout):
            raise ConnectError("can't connect TCP/IP")
        self.sock.settimeout(None)
        if nodelay:
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def send(self, data: bytes) -> None:
        self.sock.sendall(data)

    def recv(self) -> bytes:
        try:
            return self.sock.recv(4096)
        except OSError:
            return b""

    def close(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class _SerialTransport:
    """Последовательный порт без pyserial (termios на POSIX, kernel32 на Windows).

    ЭКСПЕРИМЕНТАЛЬНО: проверено только по документации ОС, не на оборудовании.
    """

    _BAUDS = (110, 300, 600, 1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200)

    def __init__(self, device: str, baud: int):
        self._closed = False
        if os.name == "nt":
            self._open_win(device, baud)
        else:
            self._open_posix(device, baud)

    # -- POSIX ------------------------------------------------------------ #
    def _open_posix(self, device, baud):
        import termios
        self._win = False
        name = "B%d" % baud
        if not hasattr(termios, name):
            raise ConnectError("can't connect COM port")
        try:
            self.fd = os.open(device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            attrs = termios.tcgetattr(self.fd)
            iflag, oflag, cflag, lflag, ispeed, ospeed, cc = attrs
            iflag = 0
            oflag = 0
            lflag = 0
            cflag = (cflag & ~(termios.CSIZE | termios.PARENB | termios.CSTOPB)) | \
                termios.CS8 | termios.CLOCAL | termios.CREAD
            speed = getattr(termios, name)
            cc[termios.VMIN] = 0
            cc[termios.VTIME] = 0
            termios.tcsetattr(self.fd, termios.TCSANOW,
                              [iflag, oflag, cflag, lflag, speed, speed, cc])
        except (OSError, termios.error):
            raise ConnectError("can't connect COM port")

    # -- Windows ---------------------------------------------------------- #
    def _open_win(self, device, baud):
        import ctypes
        from ctypes import wintypes
        self._win = True
        k32 = ctypes.windll.kernel32
        self._k32 = k32
        k32.CreateFileW.restype = wintypes.HANDLE
        h = k32.CreateFileW("\\\\.\\" + device, 0xC0000000, 0, None, 3, 0, None)
        if h in (None, wintypes.HANDLE(-1).value, -1):
            raise ConnectError("can't connect COM port")
        self.h = h

        class DCB(ctypes.Structure):
            _fields_ = [("DCBlength", wintypes.DWORD), ("BaudRate", wintypes.DWORD),
                        ("flags", wintypes.DWORD), ("wReserved", wintypes.WORD),
                        ("XonLim", wintypes.WORD), ("XoffLim", wintypes.WORD),
                        ("ByteSize", ctypes.c_ubyte), ("Parity", ctypes.c_ubyte),
                        ("StopBits", ctypes.c_ubyte), ("XonChar", ctypes.c_char),
                        ("XoffChar", ctypes.c_char), ("ErrorChar", ctypes.c_char),
                        ("EofChar", ctypes.c_char), ("EvtChar", ctypes.c_char),
                        ("wReserved1", wintypes.WORD)]

        class TMO(ctypes.Structure):
            _fields_ = [("ReadIntervalTimeout", wintypes.DWORD),
                        ("ReadTotalTimeoutMultiplier", wintypes.DWORD),
                        ("ReadTotalTimeoutConstant", wintypes.DWORD),
                        ("WriteTotalTimeoutMultiplier", wintypes.DWORD),
                        ("WriteTotalTimeoutConstant", wintypes.DWORD)]
        dcb = DCB()
        dcb.DCBlength = ctypes.sizeof(DCB)
        ok = k32.GetCommState(h, ctypes.byref(dcb))
        dcb.BaudRate = baud
        dcb.ByteSize = 8
        dcb.Parity = 0
        dcb.StopBits = 0
        dcb.flags = 0x1 | 0x10  # fBinary | fDtrControl=ENABLE
        tm = TMO(0, 0, 100, 0, 1000)  # чтение возвращается не позже, чем через 100 мс
        if not (ok and k32.SetCommState(h, ctypes.byref(dcb)) and
                k32.SetCommTimeouts(h, ctypes.byref(tm))):
            k32.CloseHandle(h)
            raise ConnectError("can't connect COM port")

    def send(self, data: bytes) -> None:
        if self._win:
            import ctypes
            from ctypes import wintypes
            n = wintypes.DWORD(0)
            if not self._k32.WriteFile(self.h, data, len(data), ctypes.byref(n), None):
                raise OSError("serial write failed")
        else:
            view = memoryview(data)
            while view:
                try:
                    n = os.write(self.fd, view)
                    view = view[n:]
                except BlockingIOError:
                    select.select([], [self.fd], [], 1.0)

    def recv(self) -> bytes:
        while not self._closed:
            if self._win:
                import ctypes
                from ctypes import wintypes
                buf = ctypes.create_string_buffer(1024)
                n = wintypes.DWORD(0)
                if not self._k32.ReadFile(self.h, buf, 1024, ctypes.byref(n), None):
                    return b""
                if n.value:
                    return buf.raw[:n.value]
            else:
                try:
                    r, _, _ = select.select([self.fd], [], [], 0.1)
                    if r:
                        d = os.read(self.fd, 1024)
                        if d:
                            return d
                except OSError:
                    return b""
        return b""

    def close(self) -> None:
        self._closed = True
        try:
            if self._win:
                self._k32.CloseHandle(self.h)
            else:
                os.close(self.fd)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# Разбор строки порта
# --------------------------------------------------------------------------- #

class _PortSpec:
    """Разобранная строка порта.

    ``"TCP [[login@]host] [port] [timeout_ms] [nodelay]"`` или ``"COMn [baud]"``.
    """

    def __init__(self, text: str):
        self.text = text
        toks = (text or "").split()
        if not toks:
            raise CommuError("bad commu_port")
        kind = toks[0]
        if kind.upper() == "TCP":
            self.kind = "tcp"
            self.login = "khidl"
            self.host = "localhost"
            self.port = 23
            self.timeout = 10000
            self.nodelay = False
            rest = toks[1:]
            if rest:
                host = rest[0]
                if "@" in host:
                    self.login, host = host.split("@", 1)
                    self.login = self.login or "khidl"
                self.host = host or "localhost"
            ints = []
            for t in rest[1:4]:
                try:
                    ints.append(int(t))
                except ValueError:
                    break
            if len(ints) > 0:
                self.port = ints[0]
            if len(ints) > 1:
                self.timeout = ints[1]
            if len(ints) > 2:
                self.nodelay = bool(ints[2])
        elif kind.upper().startswith("COM") or kind.startswith("/dev/") or kind.startswith("tty"):
            self.kind = "com"
            self.device = kind
            self.baud = 9600
            self.timeout = 10000
            if len(toks) > 1:
                try:
                    self.baud = int(toks[1])
                except ValueError:
                    raise CommuError("bad commu_port")
        else:
            raise CommuError("bad commu_port")


# --------------------------------------------------------------------------- #
# Commu
# --------------------------------------------------------------------------- #

def _alias(name: str, doc: str = ""):
    """Свойство-синоним другого атрибута (для имён в стиле .NET)."""
    return property(lambda self: getattr(self, name),
                    lambda self, v: setattr(self, name, v), doc=doc)


class Commu:
    """Связь с контроллером робота Kawasaki (аналог ``KRcc.Commu``).

    Конструктору передаётся строка порта::

        "TCP [[login@]host] [port] [timeout_ms] [nodelay]"
        "COM1 [baud]"                     (экспериментально; только ``command``)

    * ``login`` -- ``"as"`` или ``"khidl"`` (по умолчанию ``"khidl"``);
    * ``port`` -- по умолчанию 23; ``timeout_ms`` -- ожидание соединения/логина
      (по умолчанию 10000 мс; в справке написано 20 с, но в DLL зашито 10 с);
    * ``nodelay`` -- ``1`` включает ``TCP_NODELAY``.

    Примеры строк: ``"TCP 192.168.0.2"``, ``"TCP as@192.168.0.2 9000 3000 1"``,
    ``"TCP localhost 9105"`` (PC-AS / PC-ROSET).

    Исключения конструктора: :class:`ConnectError` (``"can't connect TCP/IP"``),
    :class:`LoginError` (``"can't login"``), :class:`NoResponseError`.

    Атрибуты-делегаты (по умолчанию ``None``): :attr:`as_inquiry`,
    :attr:`cmd_inquiry`, :attr:`progress`, :attr:`load_save_output`. Один
    экземпляр безопасно использовать из нескольких потоков: команды выполняются
    строго по очереди.
    """

    #: Пауза после приветствия telnet перед отправкой логина (в оригинале 1 с).
    LOGIN_DELAY = 1.0
    #: Кодировка текста на проводе. latin-1 без потерь отображает любые байты.
    encoding = "latin-1"
    #: Перевод строки при записи файлов в ``save``. ``None`` -- как в оригинале
    #: (текстовый режим C: ``\r\n`` на Windows, ``\n`` на Linux/macOS); можно
    #: задать ``"\n"`` или ``"\r\n"``, чтобы получать одинаковые файлы везде.
    newline: Optional[str] = None

    def __init__(self, port: str):
        self.asInquiry: Optional[ASInquiry] = None
        self.progress: Optional[Progress] = None
        self.cmdInquiry: Optional[CmdInquiry] = None
        self.load_save_output: Optional[LoadSaveOutput] = None
        self._timeout_value = 5000
        self._load_save_msg = ""
        self._name = port
        self._ring = _Ring()
        self._cmd_lock = threading.RLock()
        self._transport = None
        self._rx_thread: Optional[threading.Thread] = None
        self._closed = True
        self._login_ok = False
        self._log = None
        self._log_lock = threading.Lock()
        self._log_on = False
        # состояние save/load
        self._fname = ""
        self._fp = None
        self._total = -1
        self._written = 0
        self._open(port)

    # -- синонимы имён ---------------------------------------------------- #
    as_inquiry = _alias("asInquiry", "Делегат :data:`ASInquiry`.")
    cmd_inquiry = _alias("cmdInquiry", "Делегат :data:`CmdInquiry`.")

    # -- контекстный менеджер -------------------------------------------- #
    def __enter__(self) -> "Commu":
        return self

    def __exit__(self, *exc) -> None:
        self.dispose()

    def __del__(self):
        try:
            self.dispose()
        except Exception:
            pass

    # ===================================================================== #
    # Свойства
    # ===================================================================== #
    @property
    def is_connected(self) -> bool:
        """``True``, если соединение установлено (``False`` после обрыва)."""
        return self._transport is not None and not self._closed

    IsConnected = is_connected

    @property
    def timeout_value(self) -> int:
        """Тайм-аут ``load``/``save`` в мс (запись). По умолчанию 5000."""
        return self._timeout_value

    @timeout_value.setter
    def timeout_value(self, ms: int) -> None:
        self._timeout_value = int(ms)

    TimeoutValue = timeout_value

    @property
    def load_save_msg(self) -> str:
        """Сообщения контроллера, накопленные за последний ``load``/``save``.

        В них имя ``using.rcc`` заменено реальным именем файла. Вопросы, на
        которые был дан ответ, сюда не попадают.
        """
        return self._load_save_msg

    LoadSaveMsg = load_save_msg

    # ===================================================================== #
    # Соединение
    # ===================================================================== #
    def name(self) -> str:
        """Имя порта в том виде, как оно было передано (``"TCP 192.168.0.2"``)."""
        return self._name

    def connect(self, port: str) -> int:
        """(Пере)подключиться к контроллеру.

        Возвращает ``0`` -- успех, ``-1`` -- ошибка соединения (таймаут),
        ``-2`` -- ошибка логина.
        """
        self._shutdown()
        try:
            self._open(port)
        except LoginError:
            return -2
        except CommuError:
            return -1
        return 0

    def disconnect(self) -> bool:
        """Закрыть соединение. ``True`` -- успех."""
        try:
            self._shutdown()
            return True
        except Exception:
            return False

    def dispose(self) -> None:
        """Освободить ресурсы: закрыть соединение и журнал."""
        self._shutdown()
        self.stop_log()

    Dispose = dispose

    # ===================================================================== #
    # AS-команды
    # ===================================================================== #
    def command(self, cmd: Optional[str] = None, tmo: Optional[int] = None) -> List:
        """Отправить AS-команду и вернуть ``[код, ответ]``.

        * ``command()`` -- ничего не отправляет, забирает уже принятые сообщения
          (например ``"(E1135) Motor power OFF."``);
        * ``command(cmd)`` -- то же, что ``command(cmd, 500)``;
        * ``command(cmd, tmo)`` -- ``tmo`` в мс -- время ожидания каждой строки/
          приглашения. Отрицательное значение -- ждать до 60 с.

        Код: ``0`` -- успех (получено приглашение ``>``); ``-1`` -- таймаут без
        ответа; ``-2`` -- таймаут, но часть ответа получена (например контроллер
        ждёт ответа на вопрос -- его можно отправить следующим ``command``).

        Если задан :attr:`cmd_inquiry`, строки ответа передаются ему, а во втором
        элементе возвращается пустая строка.
        """
        if cmd is not None and tmo is None:
            tmo = 500
        if cmd is None:
            tmo = 0
        if not self.is_connected and self._ring.empty():
            return [-1, ""]
        try:
            with self._cmd_lock:
                code, text = self._command(cmd, int(tmo))
        except (OSError, CommuError):
            self._closed = True
            return [-2, ""]
        return [code, text]

    # ===================================================================== #
    # SAVE / LOAD
    # ===================================================================== #
    def load(self, fname: str, qual: str = "") -> int:
        """Загрузить файл ``fname`` в контроллер (``LOAD [qual] file``).

        ``qual`` -- модификатор, например ``"/Q"`` (контроллер будет задавать
        вопросы -- см. :attr:`as_inquiry`).

        Возвращает ``0`` -- норма, ``-1`` -- таймаут, ``-2`` -- ошибка контроллера,
        ``-3`` -- внутренняя ошибка (файл не открывается), ``>0`` -- число
        интерактивных вопросов, на которые был дан ответ.
        """
        if not self.is_connected:
            return -1
        if not fname:
            return -3
        try:
            with open(fname, "rb"):
                pass
        except OSError:
            return -3
        with self._cmd_lock:
            try:
                self._fname = fname
                self._fp = None
                self._total = -1
                self._written = 0
                cmd = "LOAD " + ((qual + " ") if qual else "") + _DUMMY_NAME
                if self._send_command(cmd) < 0:
                    return -2
                code = self._svld_seq()
                self._notify_progress(-1, self._total)
                return code
            except OSError:
                self._closed = True
                return -1
            finally:
                self._close_file()

    def save(self, fname: str, prog: str = "", qual: str = "") -> int:
        """Сохранить данные контроллера в файл (``SAVE[qual] file[=prog]``).

        * ``save("foo.as")`` -- всё содержимое;
        * ``save("foo", "pg1,pg2")`` -- только перечисленные программы;
        * ``save("foo", "pg1", "/P")`` -- с модификатором (``/P``, ``/R``, ``/L`` ...).

        Значения возврата -- как у :meth:`load`.
        """
        if not self.is_connected:
            return -1
        if prog.startswith("/"):          # save(fname, "/P") == save(fname, "", "/P")
            prog, qual = qual, prog
        with self._cmd_lock:
            try:
                self._fname = fname
                self._fp = None
                self._total = -1
                self._written = 0
                cmd = "SAVE" + qual + " " + _DUMMY_NAME + (("=" + prog) if prog else "")
                if self._send_command(cmd) < 0:
                    return -2
                code = self._svld_seq()
                self._notify_progress(-1, -1)
                return code
            except OSError:
                self._closed = True
                return -1
            finally:
                self._close_file()

    # ===================================================================== #
    # Журнал
    # ===================================================================== #
    def start_log(self, log_fname: str) -> bool:
        """Начать запись всего, что присылает контроллер, в файл. ``False`` -- ошибка."""
        self.stop_log()
        try:
            f = open(log_fname, "w", encoding=self.encoding, newline="\n", errors="replace")
            f.write("======== RCC LOG =======\n%s\n========================\n" % time.asctime())
            f.flush()
        except OSError:
            return False
        with self._log_lock:
            self._log = f
        return True

    startLog = start_log

    def stop_log(self) -> bool:
        """Остановить запись журнала."""
        with self._log_lock:
            f, self._log = self._log, None
        if f is None:
            return True
        try:
            f.close()
            return True
        except OSError:
            return False

    stopLog = stop_log

    # ===================================================================== #
    # Внутренности: соединение
    # ===================================================================== #
    def _open(self, port: str) -> None:
        spec = _PortSpec(port)
        self._name = port
        self._ring = _Ring()
        self._login_ok = False
        self._closed = False
        if spec.kind == "tcp":
            tr = _TcpTransport(spec.host, spec.port, spec.timeout, spec.nodelay)
        else:
            tr = _SerialTransport(spec.device, spec.baud)
        self._transport = tr
        self._rx_thread = threading.Thread(target=self._rx_main, name="krcc-rx", daemon=True)
        self._rx_thread.start()
        try:
            if spec.kind == "tcp":
                tr.send(_TELNET_GREETING)
                time.sleep(self.LOGIN_DELAY)
                tr.send(spec.login.encode("latin-1") + b"\r\n")
            else:
                tr.send(b"\n")
            waited = 0
            while not self._login_ok:
                if self._closed or waited >= spec.timeout:
                    raise LoginError("can't login")
                time.sleep(0.05)
                waited += 50
            code, _ = self._command('type ""', 5000)
            if code < 0:
                raise NoResponseError("no response %s port" % ("TCP" if spec.kind == "tcp" else "COM"))
        except OSError:
            self._shutdown()
            raise ConnectError("can't connect TCP/IP")
        except CommuError:
            self._shutdown()
            raise

    def _shutdown(self) -> None:
        self._closed = True
        tr, self._transport = self._transport, None
        if tr is not None:
            tr.close()
        t = self._rx_thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2.0)
        self._rx_thread = None
        self._close_file()

    # -- поток приёма ----------------------------------------------------- #
    def _rx_main(self) -> None:
        tr = self._transport
        ring = self._ring
        buf = b""
        pos = 0

        def next_byte() -> int:
            nonlocal buf, pos
            while pos >= len(buf):
                if self._closed:
                    return -1
                buf = tr.recv()
                pos = 0
                if not buf:
                    return -1
                _dbg("rx", buf)
            b = buf[pos]
            pos += 1
            return b

        # Фаза 1: ждём приглашение '>' в начале строки.
        cnt = 0
        while True:
            c = next_byte()
            if c < 0:
                self._closed = True
                return
            self._log_byte(c)
            if c in (10, _DLE):
                cnt = 0
            elif c == 0x3E and cnt == 0:
                break
            else:
                cnt += 1
        self._login_ok = True
        self._log_on = True

        # Фаза 2: разбор потока на строки.
        term = 10
        n = 0           # символов в текущей (ещё не законченной) строке
        unput = -1
        while True:
            if unput >= 0:
                ring.push(unput & 0xFF)
                unput = -1
                n = 1
            c = next_byte()
            if c < 0:
                self._closed = True
                return
            if c == _STX:
                term = _ETB
                self._log_on = False
            elif c == _ENQ:
                self._log_on = False
                if n > 1:                       # закончить недописанную строку
                    unput = _ENQ
                    ring.push(_FLUSH)
                    ring.signal()
                    n = 0
                    continue
            if self._log_on and c != 13:
                self._log_byte(c)
            if c == term:
                ring.push(0xFF00 | c)
                ring.signal()
                self._log_on = True
                term = 10
                n = 0
            elif c == _DLE:
                ring.push(0xFF00 | c)
                ring.signal()
                n = 0
            elif c == 0x3E and n == 0:
                ring.push(0xFF00 | c)
                ring.signal()
            else:
                ring.push(c)
                n += 1

    def _log_byte(self, c: int) -> None:
        f = self._log
        if f is not None and c != 13:
            with self._log_lock:
                if self._log is not None:
                    try:
                        self._log.write(chr(c))
                        if c == 10 or c == 0x3E:
                            self._log.flush()
                    except (OSError, ValueError):
                        pass

    # -- отправка --------------------------------------------------------- #
    def _raw_send(self, data: bytes) -> None:
        tr = self._transport
        if tr is None:
            raise OSError("not connected")
        _dbg("tx", data)
        tr.send(data)

    def _enc(self, s: str) -> bytes:
        return s.encode(self.encoding, "replace")

    def _dec(self, b: bytes) -> str:
        return b.decode(self.encoding, "replace")

    # ===================================================================== #
    # Внутренности: command()
    # ===================================================================== #
    @staticmethod
    def _strip_ctrl(text: str) -> Tuple[str, List[int]]:
        """Заменить по одному управляющему символу каждого вида пробелом."""
        found = []
        for ctrl in _CTRL:
            ch = chr(ctrl)
            i = text.find(ch)
            if i >= 0:
                text = text[:i] + " " + text[i + 1:]
                found.append(ctrl)
        return text, found

    def _send_command(self, cmd: str) -> int:
        """Отправить команду, не дожидаясь ответа (для SAVE/LOAD)."""
        code, _ = self._command(cmd, 0)
        return code

    def _command(self, cmd: Optional[str], tmo: int) -> Tuple[int, str]:
        ring = self._ring
        cb = self.cmdInquiry
        resp: List[str] = []

        if cmd is None:                              # забрать накопленное
            while True:
                line, _ = ring.get(0)
                if not line:
                    break
                text = self._dec(line)
                if text != ">":
                    self._deliver(text, cb, resp)
            return 0, "".join(resp)

        pos = 0
        while True:
            nl = cmd.find("\n", pos)
            seg = cmd[pos:] if nl < 0 else cmd[pos:nl]
            pos = -1 if nl < 0 else nl + 1

            while True:                              # выбросить устаревшее
                line, _ = ring.get(0)
                if not line:
                    break
            self._raw_send(self._enc(seg))
            self._raw_send(b"\n")

            if tmo != 0:
                echo_pending = True
                while True:
                    line, timed_out = ring.get(tmo)
                    if line is None:
                        if timed_out or self._closed:
                            return (-1 if not resp else -2), "".join(resp)
                        continue
                    if not line:
                        continue
                    text = self._dec(line)
                    if echo_pending:
                        for ctrl in _CTRL:
                            if chr(ctrl) in text and ctrl == _SYN:
                                self._raw_send(b"\r\n")
                        first = text
                        for sep in ("\r", "\n"):
                            k = first.find(sep)
                            if k >= 0:
                                first = first[:k]
                        if first == seg:
                            echo_pending = False
                        else:
                            _dbg("garbage:", text)
                        continue
                    if text == ">":
                        break
                    done = self._deliver(text, cb, resp)
                    if done:
                        break
            if pos < 0:
                break
        return 0, "".join(resp)

    def _deliver(self, text: str, cb: Optional[CmdInquiry], resp: List[str]) -> bool:
        """Обработать строку ответа. ``True`` -- конец ответа на команду."""
        clean, found = self._strip_ctrl(text)
        if _SYN in found:
            self._raw_send(b"\r\n")
        if cb is not None:
            reply = cb(clean)
            if reply:
                self._raw_send(self._enc(reply))
            return False
        if found:                    # строка с управляющим кодом -- контроллер ждёт ввода
            return True
        resp.append(text)
        return False

    # ===================================================================== #
    # Внутренности: SAVE/LOAD
    # ===================================================================== #
    def _notify_progress(self, done: int, total: int) -> None:
        cb = self.progress
        if cb is not None:
            cb(done, total)

    def _close_file(self) -> None:
        fp, self._fp = self._fp, None
        if fp is not None:
            try:
                fp.close()
            except OSError:
                pass

    def _interactive(self, msg: str) -> Optional[str]:
        """Ответ на вопрос контроллера: сначала делегат, затем таблица."""
        cb = self.asInquiry
        if cb is not None:
            r = cb(msg)
            if r is not None:
                return r
        for pat, resp in DEFAULT_RESPONSES:
            if pat in msg:
                return resp
        return None

    def _svld_seq(self) -> int:
        """Главный цикл SAVE/LOAD: обслуживает кадры файла и вопросы контроллера."""
        ring = self._ring
        result = 0
        count = 0
        self._load_save_msg = ""
        tmo = self._timeout_value
        converted = "(" + _DUMMY_NAME + ")"
        while True:
            line, timed_out = ring.get(250)
            if line is None:
                if not timed_out:
                    continue
                tmo -= 250
                if tmo <= 0 or self._closed:
                    return -1
                continue
            tmo = self._timeout_value
            if not line:
                continue
            _dbg("line", line)
            if line[:1] == b">" or line[:2] == b"\x07>" or line[:3] == b"\x10\x07>":
                return result if result < 0 else count
            if len(line) >= 3 and line[0] == _ENQ and line[1] == _STX and 0x41 <= line[2] <= 0x46:
                code, reply = self._frame(line)
                if code < 0 and result >= 0:
                    result = code
                if reply:
                    self._raw_send(reply)
                continue
            text = self._dec(line).replace(converted, "(" + self._fname + ")")
            msg, _ = self._strip_ctrl(text.rstrip("\r\n"))
            output_cb = self.load_save_output
            if output_cb is not None:
                output_cb(msg)
            answer = self._interactive(msg)
            if answer is not None:
                count += 1
                self._raw_send(self._enc(answer))
            else:
                self._load_save_msg += text

    # -- обработчики кадров ENQ STX <cmd> ... ETB ------------------------- #
    @staticmethod
    def _reply(cmd: int, code: int, data: bytes = b"") -> bytes:
        return bytes([_STX, cmd]) + ("%5d" % code).encode("ascii") + data + bytes([_ETB])

    def _frame(self, frame: bytes) -> Tuple[int, bytes]:
        cmd = frame[2]
        if cmd == 0x41:                                   # 'A' -- открыть для чтения
            code = 0
            try:
                self._close_file()
                self._fp = open(self._fname, "rb")
                self._fp.seek(0, 2)
                self._total = self._fp.tell()
                self._fp.seek(0)
            except OSError:
                code = _E_OPEN
            return code, self._reply(cmd, code)
        if cmd == 0x42:                                   # 'B' -- открыть для записи
            code = 0
            try:
                self._close_file()
                self._fp = open(self._fname, "w", encoding=self.encoding, errors="replace",
                                newline=self.newline)
                self._written = 0
            except OSError:
                code = _E_OPEN
            return code, self._reply(cmd, code)
        if cmd == 0x43:                                   # 'C' -- прочитать порцию
            if self._fp is None:
                return _E_OPEN, self._reply(cmd, _E_OPEN)
            data = self._fp.read(_CHUNK)
            data = data.split(b"\0", 1)[0]                # C-строка
            if not data:
                data = bytes([_SUB])
            self._notify_progress(self._fp.tell(), self._total)
            return 0, self._reply(cmd, 0, data)
        if cmd == 0x44:                                   # 'D' -- записать строку
            if self._fp is None or _ETB not in frame:
                return _E_OPEN, self._reply(cmd, _E_OPEN)
            data = frame[3:]
            if data[:1] == bytes([_SUB]):
                return 0, b""
            end = len(data) - 1                           # без ETB
            for sep in (b"\r", b"\n"):
                k = frame.find(sep)
                if k >= 0:
                    end = min(end, k - 3)
            line = self._dec(data[:end])
            self._fp.write(line + "\n")
            self._written += len(line) + 1
            self._notify_progress(self._written, -1)
            return 0, b""                                 # при успехе ответа нет
        if cmd == 0x45:                                   # 'E' -- закрыть файл
            self._close_file()
            return 0, self._reply(cmd, 0)
        return 0, self._reply(cmd, 0)                     # 'F' -- удалить (не делаем)


#: Синоним имени класса (``Ccommu`` из постановки задачи).
Ccommu = Commu
