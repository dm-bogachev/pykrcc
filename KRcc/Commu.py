import logging
logger = logging.getLogger()
logging.basicConfig()
logger.setLevel(logging.DEBUG)

#from telnetlib import IAC, DO, DONT, WILL, WONT, SB, SE, TTYPE, NAOFFD, ECHO
import telnetlib as tlib
import time
import socket

class Commu:
    '''
    Commu class provides communication functions with a robot controller.
    '''

    def __init__(self, port_str: str = None, login: str = "as", ip: str = None, port: int = 23, timeout: int = 20000, tcp_nodelay:bool = False):
        
        # Initialize parameters
        # TODO: Add compatibility with port_str (port = "TCP 192.168.0.2 9000 3000 1") etc
        self.__port_str = port_str
        self.__login = login
        self.__ip = ip
        self.__port = port
        self.TimeoutValue = timeout
        self.__tcp_nodelay = tcp_nodelay
        
        # Initialize internal data
        self.IsConnected = False
        self.__logging = False
        self.__logging_file = None

        # Autoconnect
        _ = self.__connect()


    def __process_options(self, socket, cmd, opt):
        IS = b'\00'
        if cmd == tlib.WILL and opt == tlib.ECHO:
            socket.sendall(tlib.IAC + tlib.DO + opt) 
        elif cmd == tlib.DO and opt == tlib.TTYPE:
            socket.sendall(tlib.IAC + tlib.WILL + tlib.TTYPE)
        elif cmd == tlib.SB:
            socket.sendall(tlib.IAC + tlib.SB + tlib.TTYPE + IS + 'VT100'.encode() + IS + tlib.IAC + tlib.SE)
        elif cmd == tlib.SE: 
            pass 
        else: logger.error("Unexpected telnet negotiation")

    def __write(self, data: str) -> None:
        data = data + "\r\n"
        self.__telnet_connection.write(data.encode())
        if self.__logging_file is not None:
            self.__logging_file.write(data)
            self.__logging_file.flush()

    def __read_until(self, match: str, timeout: int = None, decode: str = None) -> str:

        if timeout is None:
            timeout = self.TimeoutValue

        response = self.__telnet_connection.read_until(match.encode(), timeout)
        if decode is not None:
            response = response.decode(decode)
        else:
            response = response.decode()
        if self.__logging_file is not None:
            self.__logging_file.write(response)
            self.__logging_file.flush()
        logger.debug(response)
        return response

    def __connect(self) -> int: 
        # Trying to establish connection
        try: 
            logger.debug(f'Connecting to robot with {self.__ip}:{self.__port}')
            self.__telnet_connection = tlib.Telnet()
            self.__telnet_connection.set_option_negotiation_callback(self.__process_options)
            self.__telnet_connection.open(self.__ip, self.__port, self.TimeoutValue)
            self.__telnet_connection.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, self.__tcp_nodelay)
            logger.debug('Connected successfully')
        except Exception as e:
            logger.error(f"Failed to connect to robot: {e}")
            return -1
        # Trying to login
        try:
            time.sleep(0.5)
            logger.debug('Trying to login') 
            self.__read_until("login: ")
            self.__write(self.__login)
            self.__read_until(">")
        except TimeoutError:
            logger.error("Timeout while trying to login")
            return -2
        except Exception as e:
            logger.error(f"Unexpected error while trying to login: {e}")
            return -1
        self.IsConnected = True
        return 0

    def __del__(self):
        self.disconnect()

    def connect(self, port_str: str = None, login: str = "khidl", ip: str = None, port: int = 23, timeout: int = 20000, tcp_nodelay:bool = False):
        # Initialize parameters
        # TODO: Add compatibility with port_str (port = "TCP 192.168.0.2 9000 3000 1") etc
        self.__port_str = port_str
        self.__login = login
        self.__ip = ip
        self.__port = port
        self.TimeoutValue = timeout
        self.__tcp_nodelay = tcp_nodelay

        return self.__connect()

    def disconnect(self) -> bool: 
        if not self.IsConnected:
            return True
        try:
            self.__telnet_connection.close()
        except Exception as e:
            logger.error(f"Failed to close connection: {e}")
            return False
        self.IsConnected = False
        return True

    def name(self) -> str:
        return f"TCP {self.__login}@{self.__ip}, {self.__port}, {self.TimeoutValue}{',' if not self.__tcp_nodelay else 'TCP_NODELAY'}"

    def startLog(self, log_fname: str) -> bool:
        try:
            self.__logging_file = open(log_fname, "a")
            self.__logging = True
            return True
        except Exception as e:
            logger.error(f"Failed to open logging file: {e}")
            return False

    def stopLog(self) -> bool:
        try:
            self.__logging_file.close()
            self.__logging_file = None
            self.__logging = False
            return True
        except Exception as e:
            logger.error(f"Failed to close logging file: {e}")
            return False

    def command(self, cmd: str = None, timeout: int = None) -> list:
        if not self.IsConnected:
            return (-2, 'Not connected')
        try:
            self.__write(cmd)
            response = self.__read_until('>', timeout)
            return (0, response)    
        except TimeoutError:
            logger.warning('Timeout while reading')
            return (-1, 'Timeout while reading')
        except Exception as e:
            logger.error(f'Unexpected error: {e}')
            return (-2, 'Unexpected error')

    def __prepare_content(self, content, qual: str = None) -> str:
        #TODO: Implement qualifier handling
        if qual is None:
            return content
        else:
            return content

    def load(self, fname: str, qual: str = None) -> int:
        if not self.IsConnected:
            return -3
        try:
            max_chars = 492
            content = open(fname, 'r').read()
            content = self.__prepare_content(content, qual)
            content_blocks = [content[i:i+max_chars] for i in range(0, len(content), max_chars)]
            self.__write(f'load file')
            self.__read_until('.as', decode='ascii')
            self.__write('\x02A    0\x17')
            self.__read_until("\x17")
            for i in range(0, len(content_blocks), 1):
                self.__write('\x02C    0' + content_blocks[i] + '\x17')
                self.__read_until("\x17")
            self.__write('\x02' + 'C    0' + '\x1a\x17')
            #self.__read_until("Confirm !")
            self.__write('\r\n')
            self.__read_until("E\x17")
            self.__write('\x02' + 'E    0' + '\x17')
            self.__read_until(">")
            return 0
        except FileNotFoundError:
            logger.error(f'File not found: {fname}')
            return -3
        except TimeoutError:
            logger.warning('Timeout while reading')
            return -1
        except Exception as e:
            logger.error(f'Unexpected error: {e}')
            return -4

if __name__ == "__main__":
    commu = Commu(ip="127.0.0.1", port=9105)
    commu.startLog("commu.log")
    logger.debug(commu.name())
    retval = commu.command("WHERE")
    commu.load("file.as")
    commu.disconnect()
    commu.stopLog()
    del commu


# >>> while True: #really you should set some sort of a timeout here.
# ...    r = tn.read_some()
# ...    if any(x in r for x in ["#", ">"]):
# ...        break