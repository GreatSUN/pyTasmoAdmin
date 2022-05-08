#!/usr/bin/python3
import json
from lxml import etree
from netaddr import IPNetwork
from os.path import exists
from packaging import version
import pathlib
from threading import Thread
from queue import Queue
import re
import requests
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
import sys
import urllib.parse


## configuration base
configDict = {}
configFile = str(pathlib.Path(__file__).parent.resolve()) + '/config.json'

## load config from file
if exists(configFile):
    dataFile = open(configFile, 'r')
    dataFileStr = dataFile.read()
    dataFile.close()
    try:
        configDict = json.loads(dataFileStr)
    except Exception as ex:
        print(str(ex))
        sys.exit('Error while loading configuration file ' + configFile)
else:
    sys.exit('Config file ' + configFile + ' not found. Please create one.')


## gather/initialize base information/configuration
ipArr = IPNetwork(configDict['tasmotaNetwork']).iter_hosts()
retry_strategy = Retry(total=configDict['maxRetriesTasmotaOTAUrl'], backoff_factor=0, status_forcelist=[408,429,503])
adapter = HTTPAdapter(max_retries=retry_strategy)
http = requests.Session()
http.mount('https://', adapter)
http.mount('http://', adapter)
tasmotaDevDataFile = configDict['deviceDataFilePath'];
tasmotaBaseTypeTableDict = {
    'tasmota': {
            'tasmotaName': 4,
            'tasmotaVersion': 6
        },
    'tasmota32': {
            'tasmotaName': 1,
            'tasmotaVersion': 3
        }
    }
tasmotaNameVersionDict = {}
for tasmotaBaseType in tasmotaBaseTypeTableDict.keys():
    print('gathering versions for base type ' + tasmotaBaseType)
    tasmotalatestVersionSource = http.get(
        'http://ota.tasmota.com/' + tasmotaBaseType + '/release/', 
        timeout=(configDict['connectTimeoutOTAUrl'],configDict['readTimeoutOTAUrl'])
    ).text
    htmlTableObj = etree.HTML(tasmotalatestVersionSource).find('body/table')
    htmlRowsObj = iter(htmlTableObj)
    rowCount = 0
    for row in htmlRowsObj:
        colCount = 0
        tasmotaFWDict = {}
        for col in row:
            if colCount == tasmotaBaseTypeTableDict[tasmotaBaseType]['tasmotaName']:
                if col.text == None or not ('tasmota' in str(col.text)):
                    break
                tmpName = col.text.split('/').pop().replace('.bin', '')
                if tmpName.startswith('tasmota'):
                    tasmotaFWDict['tasmotaName'] = tmpName
                else:
                    break
            elif colCount == tasmotaBaseTypeTableDict[tasmotaBaseType]['tasmotaVersion']:
                if col.text == None:
                    break
                tasmotaFWDict['tasmotaVersion'] = col.text
            colCount+=1
        if len(tasmotaFWDict.keys()) == 2:
            tasmotaNameVersionDict[tasmotaFWDict['tasmotaName']] = tasmotaFWDict['tasmotaVersion']
            if tasmotaFWDict['tasmotaName'] in configDict['firmwareTranslationDict']:
                for firmwareTranslationName in configDict['firmwareTranslationDict'][tasmotaFWDict['tasmotaName']]:
                    tasmotaNameVersionDict[firmwareTranslationName] = tasmotaFWDict['tasmotaVersion']
        rowCount+=1


## worker method (executor)
def checkTasmotaAtIp(ipAddr, tasmotaNameVersionDict, configDict):
    ## only needed if you are debugging thread stuff
    ## print('working on: ' + ipAddr)
    deviceTypeRegex = re.compile(r'^([^-]+).*$', re.M)
    versionTypeRegex = re.compile(r'\(([^\)]*)\)')
    timeoutMatchRegex = re.compile(r'HTTPConnectionPool.*Max.retries.exceeded.*Connection.to.\d+.\d+.\d+.\d+.timed.out')
    retry_strategy = Retry(total=configDict['maxRetriesDev'], backoff_factor=0, status_forcelist=[408,429,503])
    adapter = HTTPAdapter(max_retries=retry_strategy)
    http = requests.Session()
    http.mount('https://', adapter)
    http.mount('http://', adapter)
    tasmotaUrlPrefix = 'http://' if configDict['tasmotaUrlSSL'] == False else 'https://'
    try:
        ## gather device data
        response = http.get(
            tasmotaUrlPrefix + ipAddr + '/cm?cmnd=STATUS0', 
            timeout=(configDict['connectTimeoutDev'],configDict['readTimeoutDev'])
        )
        tasmotaDevDataSource = response.text;
        try:
            ## parse json response int python dict
            tasmotaDevData = json.loads(tasmotaDevDataSource)
        except Exception as ex:
            ## something went wrong, probably we requested some other device?
            ## the output is just for debugging (in case we miss a tasmota device here)
            print(ipAddr + ': No Tasmota device detected: connect success, but no proper return.')
            print(ex)
            print(tasmotaDevDataSource)
            return None
        for configItem in ['StatusFWR', 'StatusMQT', 'StatusPRM']:
            ## in case we got a json response, we need to ensure that we really have a
            ## tasmota device and that we have all the needed data entries
            ## the output in case this fails is for debugging reasons (we might have
            ## a different setup from an old tasmota version which we do not have
            ## implemented up to now -> TODO:)
            if configItem not in tasmotaDevData:
                print('response of ' + ipAddr + ' was not expected, moving to next.')
                print(ipAddr + ' got response: ' + json.dumps(tasmotaDevData))
                return None
    ## handle exceptions and output the info in case we have a tasmota device here
    ## the device might be behind a firewall or currently not online...
    except Exception as ex:
        ## connection refused error
        if 'Errno 111' in str(ex):
            print(ipAddr + ' refused the connection')
        ## not reachable (most probably no device on the other side)
        elif 'Errno 113' in str(ex):
            print(ipAddr + ' is not reachable')
        ## timed out (device did not respond in time, but is existing)
        elif timeoutMatchRegex.match(str(ex)):
            print(ipAddr + ' connection timed out')
        ## handle any other error
        else:
            print(ipAddr + ' Error occured during request: ' + str(ex))
        ## fall back to existing configuration -> device might be offline temporary
        ## TODO: handle these kind of results different (device might have changed 
        ## IP and is though no more existing with this IP address)
        if ipAddr in tasmotaDevDict:
            print(ipAddr + '> keeping old (existing) data.')
            return tasmotaDevDict[ipAddr]
        return None
    ## gather device info to be able to properly handle a firmware upgrade
    ## tasmota32 devices are also named Version(tasmota-releasetype) and
    ## not (tasmota32-releasetype)
    if 'Hardware' in tasmotaDevData['StatusFWR']:
        try:
            deviceType = deviceTypeRegex.search(tasmotaDevData['StatusFWR']['Hardware']).groups(0)[0]
        except Exception as ex:
            print(ipAddr + ' Exception occured: ' + str(ex))
            print(ipAddr + ' Debug data: ' + json.dumps(tasmotaDevData))
            ## fallback to existing data (see TODO above)
            if ipAddr in tasmotaDevDict:
                print(ipAddr + '> keeping old (existing) data.')
                return tasmotaDevDict[ipAddr]
            return None
    else:
        ## unsure if we really have a tasmota device or if tasmota device is just delivering stuff
        ## stuff in a different way
        print(ipAddr + ' did not get proper hardware information. Device cannot be handled!')
        print(ipAddr + ' Debug-Data: ' + json.dumps(tasmotaDevData))
        ## fallback to existing data (see TODO above)
        if ipAddr in tasmotaDevDict:
            print(ipAddr + '> keeping old (existing) data.')
            return tasmotaDevDict[ipAddr]
        return None
    ## we found a tasmota device!!!
    ## give information about the device's relevant config
    print(ipAddr + '> Device Type: ' + deviceType)
    print(ipAddr + '> Firmware: ' + tasmotaDevData['StatusFWR']['Version'])
    print(ipAddr + '> MQTT-Server: ' + tasmotaDevData['StatusMQT']['MqttHost'] + ':' + str(tasmotaDevData['StatusMQT']['MqttPort']))
    ## check if auto update of MQTT configuration is wanted and do it
    if configDict['autoUpdateMQTT'] == True:
        if str(tasmotaDevData['StatusMQT']['MqttHost']) != configDict['MQTTHost']:
            print(ipAddr + '> >> updating MQTT host to desired ' + configDict['MQTTHost'])
            http.get(
                tasmotaUrlPrefix + ipAddr + '/cm?cmnd=' + urllib.parse.quote_plus('MqttHost ' + configDict['MQTTHost']),
                timeout=(configDict['connectTimeoutDev'],configDict['readTimeoutDev'])
            )
            ## write back new MQTT host due we issued automated update
            ## we could implement some check to ensure we have successfully done the update and otherwise leave the old version
            tasmotaDevData['StatusMQT']['MqttHost'] = configDict['MQTTHost']
        if str(tasmotaDevData['StatusMQT']['MqttPort']) != configDict['MQTTPort']:
            print(ipAddr + '> >> updating MQTT port to desired ' + configDict['MQTTPort'])
            http.get(
                tasmotaUrlPrefix + ipAddr + '/cm?cmnd=' + urllib.parse.quote_plus('MqttPort ' + configDict['MQTTPort']),
                timeout=(configDict['connectTimeoutDev'],configDict['readTimeoutDev'])
            )
            ## write back new MQTT port due we issued automated update
            ## we could implement some check to ensure we have successfully done the update and otherwise leave the old version
            tasmotaDevData['StatusMQT']['MqttPort'] = configDict['MQTTPort']
    ## check for current firmware type (to be able to properly guess the new firmware type)
    firmwareType = re.search(r'\(([^\)]*)\)', tasmotaDevData['StatusFWR']['Version']).groups(0)[0]
    firmwareVersion = tasmotaDevData['StatusFWR']['Version'].replace('(' + firmwareType + ')', '')
    ## handle special for tasmota32 devices
    firmwarePrefix = 'tasmota32' if deviceType == 'ESP32' else 'tasmota'
    ## complete firmware name
    if firmwarePrefix not in firmwareType:
        firmwareType = firmwarePrefix + '-' + str(firmwareType)
    if firmwareType not in tasmotaNameVersionDict:
        resFWTypeArr = [fwTypeKey for fwTypeKey, fwTypeTransValArr in configDict['firmwareTranslationDict'].items() if firmwareType in fwTypeTransValArr]
        if resFWTypeArr == 1:
            firmwareType = resFWTypeArr[0]
        else:
            print(ipAddr + ': did not find translation/match for ' + firmwareType)
    if firmwareType not in tasmotaNameVersionDict:
        print(ipAddr + ': Found firmware version that is not a default firmware: ' + firmwareType + ' -> are we missing a translation?')
    else:
        ## check if we should/have to upgrade the device firmware
        if version.parse(tasmotaNameVersionDict[firmwareType]) > version.parse(firmwareVersion):
            ## we want automatic update -> do it
            ## TODO: implement default and per device setting of auto update
            if configDict['autoUpdateFW'] == True:
                print('> >> issueing firmware update of ' + firmwareType + ' to ' + tasmotaNameVersionDict[firmwareType])
                newOtaUrl = 'http://ota.tasmota.com/' + firmwarePrefix + '/release/' + firmwareType + '.bin'
                if newOtaUrl != tasmotaDevData['StatusPRM']['OtaUrl']:
                    http.get(
                        tasmotaUrlPrefix + ipAddr + '/cm?cmnd=' + urllib.parse.quote_plus('OtaUrl ' + newOtaUrl),
                        timeout=(configDict['connectTimeoutDev'],configDict['readTimeoutDev'])
                    )
                response = http.get(
                    tasmotaUrlPrefix + ipAddr + '/cm?cmnd=' + urllib.parse.quote_plus('Upgrade ' + tasmotaNameVersionDict[firmwareType]),
                    timeout=(configDict['connectTimeoutDev'],configDict['readTimeoutDev'])
                ).text
                print(ipAddr + '> >> >> ' + response)
                ## write back new tasmota version due we issued automated update
                ## we could implement some check to ensure we have successfully done the update and otherwise leave the old version
                tasmotaDevData['StatusFWR']['Version'] = tasmotaNameVersionDict[firmwareType]
            ## just give information onto old firmware and new firmware version
            else:
                print(ipAddr + ' has old firmware: ' + firmwareVersion + ' / ' + firmwareType + ' - current version is: ' + tasmotaNameVersionDict[firmwareType])
    ## finally return gathered data into stock database
    return {
                'firmware': tasmotaDevData['StatusFWR']['Version'],
                'mqttServer': tasmotaDevData['StatusMQT']
            }

## worker implementation
def worker(workQueue, tasmotaDevDict, configDict):
    while not workQueue.empty():
        workData = workQueue.get()
        resDict = checkTasmotaAtIp(workData[0], workData[1], configDict)
        if resDict:
            tasmotaDevDict[workData[0]] = resDict
        workQueue.task_done()
    return True

## initialize variable for holding the devices
tasmotaDevDict = {}

## load existing base data
if exists(tasmotaDevDataFile):
    dataFile = open(tasmotaDevDataFile, 'r')
    dataFileStr = dataFile.read()
    dataFile.close()
    tasmotaDevDict = json.loads(dataFileStr)

## initialize work queue to scan network
workQueue = Queue(maxsize=0)
### setup work queue with all ip addresses and the tasmota image dict
for ipNetAddr in ipArr:
    ipAddr = str(ipNetAddr)
    if ipAddr not in configDict['excludeIpArr']:
        workQueue.put((ipAddr, tasmotaNameVersionDict))

## start the worker threads
print('starting scan with ' + str(configDict['maxThreads']) + ' threads')
for i in range(configDict['maxThreads']):
    threadWorker = Thread(target=worker, args=(workQueue, tasmotaDevDict, configDict))
    threadWorker.setDaemon(True)
    threadWorker.start()

## wait for all workers to finish
workQueue.join()

## write final results to file
dataFile = open(tasmotaDevDataFile, 'w')
dataFile.write(json.dumps(tasmotaDevDict))
dataFile.close()
