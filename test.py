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
    commu.save('data1.as')
    #commu.load('data1.as', '/Q')
    #commu.save('data2.as')
    #commu.cmdInquiry = cmd_inq
    # while True:
    #     d = input('>')
    #     if d == 'exit':
    #         break
    #     response = commu.command(d)
    #     print(response[1])
    commu.disconnect()
    commu.stopLog()
    del commu
