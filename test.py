import logging
logging.basicConfig()
logger = logging.getLogger()
logger.setLevel(logging.DEBUG)

from pykrcc.Commu import Commu

if __name__ == '__main__':
    commu = Commu(ip='127.0.0.1', port=9105, timeout=4)
    commu.startLog('commu.log')
    #commu.save('data2.as')
    commu.load('data2.as', '/Q')
    commu.disconnect()
    commu.stopLog()
    del commu

