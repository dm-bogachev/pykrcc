import logging
logging.basicConfig()
logger = logging.getLogger()
logger.setLevel(logging.DEBUG)
from pykrcc.Commu import Commu

if __name__ == '__main__':
    commu = Commu(ip='127.0.0.1', port=9105, timeout=4)
    commu.startLog('commu.log')
    #commu.save('data2.as')
    commu.load('data2.as')
    # commu.save("lol.as", qual='/S')
    # commu.save("lol2.as", qual='/STG')
    # commu.save("lol3.as", qual='/SYS')
    # logger.debug(commu.name())
    # retval = commu.command('sw')
    #commu.load('load.as')
    # commu.save('file2.as', qual='/P')
    # commu.save('pg.as', prog='calibrate,motion')
    #commu.save('llpgr.as', qual='/L')
    # commu.load('load.as')#, '/Q')
    # ----------------------------------------
    # while True:
    #     req = input()
    #     if req == 'quit':
    #         break
    #     response = commu.command(req)
    #     print(response[1].decode())
    commu.disconnect()
    commu.stopLog()
    del commu

