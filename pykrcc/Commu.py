from gc import enable
import logging
logger = logging.getLogger()
logging.basicConfig()
logger.setLevel(logging.DEBUG)

import telnetlib as tlib
import time
import socket
import re

class Commu:
    '''
    Commu class provides communication functions with a robot controller.
    '''

    def __init__(self, port_str: str = None, login: str = 'as', ip: str = None, port: int = 23, timeout: int = 20, tcp_nodelay:bool = False):
        
        # Initialize parameters
        # TODO: Add compatibility with port_str (port = 'TCP 192.168.0.2 9000 3000 1') etc
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
        self.__cmd_terminators = [b'\x0a>', 
                                  b'Press SPACE key to continue.', 
                                  b'Yes:1, No:0']
        
        self.__as_terminators = [b'.as',
                                 b'LOAD in progress',
                                 b'1:Yes, 0:No / 2:Load all, 3:Exit',
                                 b'Delete program and abort',
                                 b'Are you sure ? \(Yes:1, No:0\)',
                                 b'E\x17',
                                 b'errors',
                                 b'\x02C\x17',
                                 b'Force load'
                                 b'Press ENTER.'
                                 ]
        
        
        # self.__load_terminators = [b'\x17', b'1:Yes, 0:No / 2:Load all, 3:Exit']
        # self.__as_terminators = [b'errors', 
        #                         b'1:Yes, 0:No / 2:Load all, 3:Exit', 
        #                         b'E\x17', 
        #                         b'Delete program and abort', 
        #                         b'Are you sure ? \(Yes:1, No:0\)',
        #                         b'.\x1b\[2D\x1b\[K>']
        self.cmdInquiry = self.default_cmd_inquiry
        self.asInquiry = self.default_as_inquiry

        # Autoconnect
        _ = self.__connect()

    def __del__(self):
        self.disconnect()
        if self.__logging_file is not None:
            self.stopLog()

    def __log(self, data) -> None:
        try:
            if type(data) is bytes or type(data) is bytearray:
                data = data.decode()
            if self.__logging:
                data = re.sub(r'\x17{0,1}\x05\x02[DE]{0,1}', '', data)
                self.__logging_file.write(data.replace('\r\n', '\n'))
                self.__logging_file.flush()
            logger.debug(data)
        except Exception as e:
            logger.error(e)

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
        else: logger.error('Unexpected telnet negotiation')

    def __write(self, data: bytearray) -> None:
        response = self.__telnet_connection.write(data)
        self.__log(data)
        return response

    def __read_until(self, match: bytearray, timeout: int = None) -> bytearray:
        if timeout is None:
            timeout = self.TimeoutValue
        response = self.__telnet_connection.read_until(match, timeout)
        self.__log(response)
        return response

    def default_cmd_inquiry(self, as_msg: bytearray) -> bytearray:
        if self.__cmd_terminators[0] in as_msg:
            return None
        if self.__cmd_terminators[1] in as_msg:
            return b' '
        if self.__cmd_terminators[2] in as_msg:
            return b'1'
        return b''

    def default_as_inquiry(self, as_msg: bytearray) -> bytearray:
        if b'errors' in as_msg:
            return b'break'
        if b'1:Yes, 0:No / 2:Load all, 3:Exit' in as_msg:
            return b'2\r\n'
        if b'E\x17' in as_msg:
            return None
        if b'Delete program and abort' in as_msg:
            return b'0\r\n'
        if b'Are you sure' in as_msg:
            return b'1\r\n'
        if b'Force load' in as_msg:
            return b'9\r\n'
        if b'Press ENTER.' in as_msg:
            return b'\r\n'
        return None

    def __read_until_many(self, matches: list, timeout: int = None) -> bytearray:
        if timeout is None:
            timeout = self.TimeoutValue
        response = self.__telnet_connection.expect(matches, timeout)
        self.__log(response[2])
        return response[2]

    def __read_eager(self) -> bytearray:
        response = self.__telnet_connection.read_eager()
        self.__log(response)
        return response

    def __get_savefile(self, prog: str = None, qual: str = None) -> bytes:
        _prog = ''
        if prog is not None:
            _prog = '=' + prog
        _qual = ''
        if qual is not None:
            _qual = qual
        
        self.__write(f'save{_qual} file.as{_prog}\r\n'.encode())
        self.__read_until(b'.as')
        self.__write(b'\x02B    0\x17')
        switch = True
        raw_data = b''

        while True:
            if switch:
                data_block = self.__read_until(b'\x05\x02')
                if data_block == b'':
                    break
                raw_data = raw_data + data_block
                data_block = self.__read_eager()
                if data_block == b'':
                    break
                raw_data = raw_data + data_block
                if b'E\x17' in data_block:
                    break
            else:
                data_block = self.__read_until(b'\x17')
                raw_data = raw_data + data_block
                if b'E\x17' in data_block:
                    break

            switch = not switch
        self.__write(b'\x02\x45' + b'    0' + b'\x17')
        self.__write(b'\r\n')
        self.__write(b'\x02' + b'E    0' + b'\x17')
        data_block = self.__read_until(b'>')

        return raw_data

    def __process_data(self, data: bytearray) -> list:
        data = re.sub(rb'\x17{0,1}\x05\x02[DE]{0,1}', b'', data)
        clean_data = []
        for line in data.splitlines():
            if line and not line.startswith(b'\x17') and not line.startswith(b'Bfile.as') and not line.startswith(b'='):
                clean_data.append(line)
            else:
                if line.startswith(b'Bfile.as'):
                    continue
                self.__log(line.replace(b'\x17', b'') + b'\n')
        return clean_data

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
            logger.error(f'Failed to connect to robot: {e}')
            return -1
        # Trying to login
        try:
            time.sleep(0.5)
            logger.debug('Trying to login') 
            self.__read_until(b'login: ')
            self.__write(self.__login.encode() + b'\r\n')
            self.__read_until(b'>')
        except TimeoutError:
            logger.error('Timeout while trying to login')
            return -2
        except Exception as e:
            logger.error(f'Unexpected error while trying to login: {e}')
            return -1
        self.IsConnected = True
        return 0

    def connect(self, port_str: str = None, login: str = 'khidl', ip: str = None, port: int = 23, timeout: int = 20000, tcp_nodelay:bool = False):
        # Initialize parameters
        # TODO: Add compatibility with port_str (port = 'TCP 192.168.0.2 9000 3000 1') etc
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
            logger.error(f'Failed to close connection: {e}')
            return False
        self.IsConnected = False
        return True

    def name(self) -> str:
        return f'TCP {self.__login}@{self.__ip}, {self.__port}, {self.TimeoutValue}{"," if not self.__tcp_nodelay else "TCP_NODELAY"}'

    def startLog(self, log_fname: str) -> bool:
        try:
            self.__logging_file = open(log_fname, 'a')
            self.__logging = True
            return True
        except Exception as e:
            logger.error(f'Failed to open logging file: {e}')
            return False

    def stopLog(self) -> bool:
        try:
            self.__logging_file.close()
            self.__logging_file = None
            self.__logging = False
            return True
        except Exception as e:
            logger.error(f'Failed to close logging file: {e}')
            return False

    def command(self, cmd: str = None, timeout: int = None) -> list:
        if not self.IsConnected:
            return (-2, 'Not connected')
        try:
            self.__write(cmd.encode() + b'\r\n')
            response = self.__read_until_many(self.__cmd_terminators, 5)
            while True:
                request = self.cmdInquiry(response)
                if request is None:
                    break
                self.__write(request)
                response = self.__read_until_many(self.__cmd_terminators, 5)
            return (0, response)    
        
        except TimeoutError:
            logger.warning('Timeout while reading')
            return (-1, 'Timeout while reading')
        except Exception as e:
            logger.error(f'Unexpected error: {e}')
            return (-2, 'Unexpected error')

    # def __parse_file_to_blocks(self, content) -> list:
    #     content_lines = content.splitlines()
    #     content_blocks = []
    #     block = ''
    #     for line in content_lines:
    #         block = block + line + '\r\n'
    #         if line.strip().upper() == '.END':
    #             content_blocks.append(block)
    #             block = ''
    #     return content_blocks
       
    # def __load_block(self, block: str, qual: str) -> int:
    #     _qual = b''
    #     if qual is not None:
    #         if qual != '/Q':
    #             logger.error('Invalid qualifier')
    #             return -4
    #         _qual = b'/Q'
    #     if not self.IsConnected:
    #         return -3
    #     max_chars = 492
    #     content_blocks = [block[i:i+max_chars] for i in range(0, len(block), max_chars)]
    #     self.__write(b'load'+ _qual + b' file'  + b'\r\n')
    #     response = self.__read_until(b'.as')
    #     if b'LOAD in progress' in response:
    #         logger.error('SAVE/LOAD in progress')
    #         if self.__logging_file:
    #             self.__logging_file.write(response.decode())
    #         return -2
    #     self.__write(b'\x02A    0\x17')
    #     d = self.__read_until(b'\x17')
    #     for i in range(0, len(content_blocks), 1):
    #         self.__write(b'\x02C    0' + content_blocks[i].encode() + b'\r\n\x17')
    #         response = self.__read_until_many(self.__load_terminators, 1)
    #         request = self.asInquiry(response)
    #         if request is not None:
    #             self.__write(request)
    #     empty_breaks = 0
    #     while True:
    #         response = self.__read_until_many(self.__load_terminators, 1)
    #         request = self.asInquiry(response)
    #         if response == b'':
    #             empty_breaks += 1
    #             if empty_breaks > 2:
    #                 break
    #         if request is not None:
    #             if request == b'break':
    #                 break
    #             self.__write(request)

    #     self.__write(b'\x02' + b'C    0' + b'\x1a\x17')
    #     self.__write(b'\r\n')
    #     response = self.__read_until(b'E\x17')
    #     self.__write(b'\x02' + b'E    0' + b'\x17')
    #     response = self.__read_until(b'>')
    #     return 0

    # def load(self, fname: str, qual: str = None) -> int:


    #     # Do not write raw data to logging file
    #     enable_later = False
    #     if self.__logging:
    #         enable_later = True
    #         self.__logging = False

    #     try:
    #         max_chars = 492
    #         content = open(fname, 'r').read()
    #         content_blocks = self.__parse_file_to_blocks(content)
    #         for block in content_blocks:
    #             self.__load_block(block, qual)
    #         return 0
    #     except FileNotFoundError:
    #         logger.error(f'File not found: {fname}')
    #         return -3
    #     except TimeoutError:
    #         logger.warning('Timeout while reading')
    #         return -1
    #     except Exception as e:
    #         logger.error(f'Unexpected error: {e}')
    #         return -4
    #     finally:
    #         self.__logging = enable_later
  
    def __split_content_to_blocks(self, content: list) -> list:
        
        max_chars = 492
        
        content_blocks = []
        block = ''

        for line in content:
            if len(block) + len(line) + 2 >= max_chars:
                content_blocks.append(block)
                block = ''
            block = block + line 
        if block != '':
            content_blocks.append(block)
        return content_blocks

    def load(self, fname: str, qual: str = None) -> int:
        # Check connection
        if not self.IsConnected:
            logger.error('Not connected')
            return -3
        # Disable logging while loading
        enable_later = False
        if self.__logging:
            enable_later = True
            self.__logging = False
        # Get qualifier
        _qual = b''
        if qual is not None:
            _qual = qual.encode()
        # Load file
        try:
            max_chars = 470
            with open(fname, 'r') as f:
                content = f.readlines()
            content_blocks = self.__split_content_to_blocks(content)
        except FileNotFoundError:
            logger.error(f'File not found: {fname}')
            return -3
        except Exception as e:
            logger.error(f'Unexpected error: {e}')
            return -4
        finally:
            self.__logging = enable_later
        # Load data to controller
        try:
            self.__write(b'load'+ _qual + b' file'  + b'\r\n')
            # Add to logging file
            if self.__logging_file is not None:
                self.__logging_file.write(f'load{_qual} file')
            response = self.__read_until_many(self.__as_terminators, 2)
            # Check if load is in progress
            if b'LOAD in progress' in response:
                logger.error('SAVE/LOAD in progress')
                if self.__logging_file:
                    self.__logging_file.write('SAVE/LOAD in progress')
                return -2
            self.__write(b'\x02A    0\x17')
            response = self.__read_until(b'\x17')
            logger.debug(response)
            
            empty_counter = 0
            for block in content_blocks:
                self.__write(b'\x02C    0' + block.encode() + b'\r\n\x17')
                response = self.__read_until_many(self.__as_terminators, 1)
                logger.debug(response)
                request = self.asInquiry(response)
                if request is not None:
                    self.__write(request)
                if response == b'':
                    empty_counter += 1
                    if empty_counter > 2:
                        break
            empty_counter = 0
            while True:
                response = self.__read_until_many(self.__as_terminators, 1)
                logger.debug(response)
                request = self.asInquiry(response)
                if request is not None:
                    self.__write(request)
                if response == b'':
                    empty_counter += 1
                    if empty_counter > 2:
                        break
            self.__write(b'\x02' + b'C    0' + b'\x1a\x17')
            self.__write(b'\r\n')
            response = self.__read_until(b'E\x17')
            self.__write(b'\x02' + b'E    0' + b'\x17')
            response = self.__read_until(b'>')
            return 0
            # for block in content_blocks:
            #     self.__write(f'load{_qual} file.as{_prog}\r\n'.encode())
            #     self.__read_until(b'.as')
            #     self.__write(block.encode() + b'\x17')
        except TimeoutError:
            logger.warning('Timeout while reading')
            return -1
        except Exception as e:
            logger.error(f'Unexpected error: {e}')
            return -4
        finally:
            self.__logging = enable_later

    def save(self, fname: str, prog: str = None, qual: str = None) -> int:
        enable_later = False
        if self.__logging:
            enable_later = True
            self.__logging = False
        try:
            with open(fname, 'w') as f:
                raw_data = self.__get_savefile(prog, qual)
                self.__logging = enable_later
                data_list = self.__process_data(raw_data)
                f.writelines([line.decode() + '\n' for line in data_list])
        except TimeoutError:
            logger.warning('Timeout while reading')
            return -1
        except FileExistsError:
            logger.error(f'File already exists: {fname}')
            return -3
        except Exception as e:
            logger.error(f'Unexpected error: {e}')
            return -4
        finally:
            self.__logging = enable_later

