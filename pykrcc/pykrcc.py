from gc import enable
import logging
logger = logging.getLogger()
logging.basicConfig()
logger.setLevel(logging.INFO)

import telnetlib as tlib
import time
import socket
import re
import os

#TODO: Add code comments

class pykrcc:
    """
    Class for communication with Kawasaki robot controllers.

    This class allows you to connect to a Kawasaki robot controller, send commands and receive responses.
    It also provides functions for saving and loading programs to/from the controller.
    """

    def __init__(self, login: str = 'as', ip: str = None, port: int = 23, timeout: int = 20, tcp_nodelay:bool = False) -> None:
        """
        Initializes a new instance of the pykrcc class.

        Args:
            login (str, optional): Login string. Defaults to 'as'.
            ip (str, optional): IP address of the robot. Defaults to None.
            port (int, optional): Port number. Defaults to 23.
            timeout (int, optional): Timeout in milliseconds. Defaults to 20.
            tcp_nodelay (bool, optional): TCP_NODELAY option. Defaults to False.
        """
        
        # Initialize parameters
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
        
        self.cmdInquiry = self.default_cmd_inquiry
        self.asInquiry = self.default_as_inquiry
        self.progress = self.default_progress

        # Autoconnect
        _ = self.__connect()

    def __del__(self) -> None:
        """
        Destructor. Disconnects from the robot and stops logging if needed.
        """
        self.disconnect()
        if self.__logging_file is not None:
            self.stopLog()

    def __log(self, data) -> None:
        """
        Logs the data to the log file and debug output.

        Args:
            data (bytearray or bytes or str): The data to log.
        """
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

    def __process_options(self, socket, cmd, opt) -> None:
        """
        Handles telnet options. 

        We need to respond to some options to make the telnet connection work.
        """
        IS = b'\00'
        if cmd == tlib.WILL and opt == tlib.ECHO:
            # We want to echo the characters we type
            socket.sendall(tlib.IAC + tlib.DO + opt) 
        elif cmd == tlib.DO and opt == tlib.TTYPE:
            # We want to send the terminal type
            socket.sendall(tlib.IAC + tlib.WILL + tlib.TTYPE)
        elif cmd == tlib.SB:
            # This is a suboption
            socket.sendall(tlib.IAC + tlib.SB + tlib.TTYPE + IS + 'VT100'.encode() + IS + tlib.IAC + tlib.SE)
        elif cmd == tlib.SE: 
            # This is a suboption end
            pass 
        else: logger.error('Unexpected telnet negotiation')

    def __write(self, data: bytearray) -> int:
        """
        Writes the data to the connection.

        Args:
            data (bytearray): The data to write.

        Returns:
            int: The number of bytes written.
        """
        bytes_written = self.__telnet_connection.write(data)
        self.__log(data)
        return bytes_written

    def __read_until(self, match: bytearray, timeout: int = None) -> bytearray:
        """
        Reads from the connection until a match is found.

        Args:
            match (bytearray): The match to search for.
            timeout (int, optional): The timeout in milliseconds. Defaults to None.

        Returns:
            bytearray: The data read from the connection.
        """
        if timeout is None:
            timeout = self.TimeoutValue
        response = self.__telnet_connection.read_until(match, timeout)
        self.__log(response)
        return response

    def __read_until_many(self, matches: list, timeout: int = None) -> bytearray:
        """
        Reads from the connection until one of the matches is found.

        Args:
            matches (list): A list of matches to search for.
            timeout (int, optional): The timeout in milliseconds. Defaults to None.

        Returns:
            bytearray: The data read from the connection.
        """
        if timeout is None:
            timeout = self.TimeoutValue
        response = self.__telnet_connection.expect(matches, timeout)
        self.__log(response[2])
        return response[2]

    def __read_eager(self) -> bytearray:
        """
        Reads all the available data from the connection.

        Returns:
            bytearray: The data read from the connection.
        """
        response = self.__telnet_connection.read_eager()
        self.__log(response)
        return response

    def __get_savefile(self, prog: str = None, qual: str = None) -> bytes:
        """
        Reads the source code of the program from the controller and returns it.

        Args:
            prog (str, optional): Program name. Defaults to None.
            qual (str, optional): Qualifier. Defaults to None.

        Returns:
            bytes: The source code of the program.
        """
        _prog = ''
        if prog is not None:
            _prog = '=' + prog
        _qual = ''
        if qual is not None:
            _qual = qual
        
        if self.__logging_file:
            self.__logging_file.write(f'save{_qual} file.as{_prog}\n')
        self.__write(f'save{_qual} file.as{_prog}\r\n'.encode())
        response = self.__read_until(b'.as', 1)
        if self.__logging_file:
            self.__logging_file.write('Saving...(file.as)')
        # Check if save/load is in progress
        if b'LOAD in progress' in response:
            logger.error('SAVE/LOAD in progress')
            if self.__logging_file:
                self.__logging_file.write('SAVE/LOAD in progress')
            return -2
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
        """
        Processes the data from the robot after save command.

        Args:
            data (bytearray): Raw data from the robot after save command.

        Returns:
            list: List of strings which are the actual source code of the program.
        """
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

    def __split_content_to_blocks(self, content: list) -> list:
        """
        Splits the content into blocks which are acceptable by the robot.

        Args:
            content (list): The content to split.

        Returns:
            list: A list of blocks.
        """
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

    def __connect(self) -> int: 
        """ 
        Tries to establish connection to the robot and login.

        Returns:
            int: 0 if connected successfully, 
            -1 if connection could not be established, 
            -2 if timeout occurred while trying to login, 
            -3 if an unexpected error occurred while trying to login.
        """ 
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
            return -3
        self.IsConnected = True
        return 0

    def connect(self, port_str: str = None, login: str = 'khidl', ip: str = None, port: int = 23, timeout: int = 20000, tcp_nodelay:bool = False):
        """
        Connects to the robot.

        Args:
            port_str (str, optional): Port string (e.g. 'COM1'). Defaults to None.
            login (str, optional): Login string. Defaults to 'khidl'.
            ip (str, optional): IP address of the robot. Defaults to None.
            port (int, optional): Port number. Defaults to 23.
            timeout (int, optional): Timeout in milliseconds. Defaults to 20000.
            tcp_nodelay (bool, optional): TCP_NODELAY option. Defaults to False.

        Returns:
            int: 0 if connected successfully, 
            -1 if failed to connect, 
            -2 if failed to login.
        """
        # Initialize parameters
        self.__port_str = port_str
        self.__login = login
        self.__ip = ip
        self.__port = port
        self.TimeoutValue = timeout
        self.__tcp_nodelay = tcp_nodelay

        return self.__connect()

    def disconnect(self) -> bool:
        """
        Disconnects from the robot.

        Returns:
            bool: True if disconnected successfully, False if not.
        """
        if not self.IsConnected:
            logger.info('Robot is already disconnected')
            return True
        try:
            self.__telnet_connection.close()
        except Exception as e:
            logger.error(f'Failed to close connection: {e}')
            return False
        self.IsConnected = False
        logger.info('Robot disconnected')
        return True

    def name(self) -> str:
        """
        Name of the connection.

        Returns:
            str: Name of the connection in format 'TCP <login>@<ip>, <port>, <timeout>{,TCP_NODELAY}'.
        """
        return f'TCP {self.__login}@{self.__ip}, {self.__port}, {self.TimeoutValue}{"," if not self.__tcp_nodelay else "TCP_NODELAY"}'

    def startLog(self, log_fname: str) -> bool:
        """
        Starts logging to file.

        Args:
            log_fname (str): Name of the log file.

        Returns:
            bool: True if logging started successfully, False if not.
        """
        try:
            self.__logging_file = open(log_fname, 'a')
            self.__logging = True
            return True
        except Exception as e:
            logger.error(f'Failed to open logging file: {e}')
            return False

    def stopLog(self) -> bool:
        """
        Stop logging to file.

        Returns:
            bool: True if logging stopped successfully, False if not.
        """
        try:
            self.__logging_file.close()
            self.__logging_file = None
            self.__logging = False
            return True
        except Exception as e:
            logger.error(f'Failed to close logging file: {e}')
            return False

    def command(self, cmd: str = None, timeout: int = None) -> list:
        """
        Sends a command to the robot controller and returns the response.

        Args:
            cmd (str, optional): Command string. Defaults to None.
            timeout (int, optional): Timeout in milliseconds. Defaults to None.

        Returns:
            list: A list containing the return code and the response string. 
            Return code is 0 if the command was sent successfully, 
            -1 if timeout occurred, 
            -2 if not connected, 
            -3 if the command was invalid, 
            -4 if the request was denied, 
            -5 if an error occurred.
        """
        if not self.IsConnected:
            return (-2, 'Not connected')
        response_ret = b''
        try:
            self.__write(cmd.encode() + b'\r\n')
            response = self.__read_until_many(self.__cmd_terminators, 5)
            response_ret = response_ret + response
            while True:
                request = self.cmdInquiry(response)
                if request is None:
                    break
                self.__write(request)
                response = self.__read_until_many(self.__cmd_terminators, 5)
                response_ret = response_ret + response
            return (0, response_ret.decode())    
        
        except TimeoutError:
            logger.warning('Timeout while reading')
            return (-1, 'Timeout while reading')
        except Exception as e:
            logger.error(f'Unexpected error: {e}')
            return (-2, 'Unexpected error')

    def default_cmd_inquiry(self, as_msg: bytearray) -> bytearray:
        """
        Default inquiry function for commands.

        Checks if the response contains any inquiry strings and returns the appropriate response.
        """
        if b'\x0a>' in as_msg:
            return None
        if b'Press SPACE key to continue.' in as_msg:
            return b' '
        if b'Yes:1, No:0' in as_msg:
            return b'1'
        return b''

    def default_as_inquiry(self, as_msg: bytearray) -> bytearray:
        """
        Default inquiry function for save/load commands.

        Checks if the response contains any inquiry strings and returns the appropriate response.
        """
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

    def default_progress(self, val: int, total: int) -> None:
        """
        Default progress function.

        Simply logs the progress.

        Args:
            val (int): Current progress value.
            total (int): Total progress value.
        """
        logger.info(f'Progress: {val}/{total}')
        return

    def load(self, fname: str, qual: str = None) -> int:
        """
        Loads a file into the controller.

        Args:
            fname (str): Name of the file to load.
            qual (str, optional): Qualifier string. Defaults to None.

        Returns:
            int: Return code. 0 if the file was loaded successfully, 
            -1 if timeout occurred, 
            -2 if not connected, 
            -3 if the file does not exist, 
            -4 if an error occurred.
        """
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
            file_size = os.path.getsize(fname)
            logger.debug(f'File size: {file_size}')
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
                self.__logging_file.write(f'load{_qual.decode()} file\r\n')
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
            loaded_size = 0
            for block in content_blocks:
                loaded_size = loaded_size + len(block.encode())
                self.progress(loaded_size, file_size)
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
            self.progress(file_size, file_size)
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
        except TimeoutError:
            logger.warning('Timeout while reading')
            return -1
        except Exception as e:
            logger.error(f'Unexpected error: {e}')
            return -4
        finally:
            self.__logging = enable_later

    def save(self, fname: str, prog: str = None, qual: str = None) -> int:
        """
        Saves the source code of the program to file.

        Args:
            fname (str): Name of the file to save the data to.
            prog (str, optional): Program name. Defaults to None.
            qual (str, optional): Qualifier. Defaults to None.

        Returns:
            int: 0 if saved successfully, 
            -1 if timeout occurred, 
            -2 if not connected, 
            -3 if file already exists, 
            -4 if an error occurred.
        """
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
