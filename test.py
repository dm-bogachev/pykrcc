import logging
logging.basicConfig()
logger = logging.getLogger()
logger.setLevel(logging.DEBUG)

from pykrcc import pykrcc

def cmd_inq(as_msg: bytearray) -> bytearray:
    print(as_msg)
    return input('>')

if __name__ == '__main__':
    commu = pykrcc.pykrcc(ip='127.0.0.1', port=9105, timeout=4)
    
    commu.startLog('commu.log')
    while True:
        cmd = input('> ')
        if cmd == 'quit':
            break
        if 'save' in cmd:
            parts = cmd.split(' ')
            filename = parts[1]
            qual = ''
            if '/' in parts[0]:
                qual = parts[0][4:]
            commu.save(filename, qual=qual)            
        if 'load' in cmd:
            parts = cmd.split(' ')
            filename = parts[1]
            qual = ''
            if '/' in parts[0]:
                qual = parts[0][4:]
            commu.load(filename, qual=qual)  
        response = commu.command(input('> '))
        
    commu.disconnect()
    commu.stopLog()
    del commu
