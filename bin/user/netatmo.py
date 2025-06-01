#!/usr/bin/python
# Copyright 2015 Matthew Wall
# Distributed under the terms of the GNU Public License (GPLv3)
#
# Thanks to phillippe larduinat for publishing lnetatmo.py
#   https://github.com/philippelt/netatmo-api-python
# The netatmo API has changed quite a bit since phillippe wrote the code, but
# the implementation helps.
#
# Shame on netatmo for making it very difficult to get data from the hardware
# without going through their servers.
#
# More shame on netatmo for encrypting the communication between the netatmo
# station and the netatmo servers, without providing end-users a way to access
# their data locally.  They used to send data in the clear up to firmware 101,
# then instead of fixing a stupid decision to send the wifi network password
# to the netatmo servers, they chose to encrypt the communication altogether.
# Beware that netatmo might still be receiving your network passwords.

"""To use cloud mode you must obtain a client_id and client_secret from
the netatmo development web servers https://dev.netatmo.com

This driver supports multiple devices, and all known modules on each device.
As of april 2016 that means the base station, 'outside' T/H, additional T/H,
rain, and wind.
"""
import sys

try:
    import queue as Queue  # Python 3
except:
    import Queue  # Python 2

import json
import re
import socket
import syslog
import threading
# import datetime
import time

try:
    from urllib.parse import urlencode
except:
    from urllib import urlencode

try:
    import urllib.request, urllib.error, urllib.parse
except:
    import urllib2

import weewx.drivers
import weewx.engine
import weewx.units
import weewx.wxformulas
import weeutil.weeutil

pvers = sys.version_info.major

DRIVER_NAME = 'netatmo'
DRIVER_VERSION = "0.17"
DEBUG = False

INHG_PER_MBAR = 0.0295299830714
MPH_TO_KPH = 1.60934
MPS_TO_KPH = 3.6
BEAFORT_TO_KPH = {0: 1, 1: 3.0, 2: 9.0, 3: 15.0, 4: 24.0, 5: 34.0, 6: 43.0,
                  7: 55.0, 8: 68.0, 9: 82.0, 10: 95.0, 11: 110.0, 12: 120.0}
KNOT_TO_KPH = 1.852


def logmsg(level, msg):
    syslog.syslog(level, 'netatmo: %s: %s' %
                  (threading.current_thread().name, msg))

def logdbg(msg):
    logmsg(syslog.LOG_DEBUG, msg)


def loginf(msg):
    logmsg(syslog.LOG_INFO, msg)


def logerr(msg):
    logmsg(syslog.LOG_ERR, msg)


def loader(config_dict, engine):
    return NetatmoDriver(**config_dict[DRIVER_NAME])


def confeditor_loader():
    return NetatmoConfEditor()


class NetatmoConfEditor(weewx.drivers.AbstractConfEditor):
    @property
    def default_stanza(self):
        return """
[netatmo]
    # This section is for the netatmo station.

    # The mode specifies how driver should obtain data.  The 'cloud' mode will
    # retrieve data from the netatmo.com servers.  The 'sniff' mode will parse
    # packets from the netatmo station on the local network.
    mode = cloud

    # The cloud mode requires credentials:
    refresh_token = INSERT_REFRESH_TOKEN_HERE
    client_id = INSERT_CLIENT_ID_HERE
    client_secret = INSERT_CLIENT_SECRET_HERE

    # The driver itself
    driver = user.netatmo
"""

    def prompt_for_settings(self):
        settings = dict()
        print("Specify the mode for obtaining data, either 'cloud' or 'sniff'")
        settings['mode'] = self._prompt('mode', 'cloud', ['cloud', 'sniff'])
        if settings['mode'] == 'cloud':
            print("Specify the client id for netatmo.com")
            self._prompt('client_id')
            print("Specify the client secret for netatmo.com")
            self._prompt('client_secret')
            print("Specify the tokens persistence file")
            self._prompt('tokens_persistence_file')
        return settings


class NetatmoDriver(weewx.drivers.AbstractDevice):
    DEFAULT_PORT = 80
    DEFAULT_HOST = ''
    # map from netatmo names to database schema names
    # apparently battery_vp is in older firmware, whereas battery_percent is
    # in newer firmware.
    DEFAULT_SENSOR_MAP = {
        'pressure': '*.NAMain.AbsolutePressure',
        'inTemp': '*.NAMain.Temperature',
        'inHumidity': '*.NAMain.Humidity',
        'co2': '*.NAMain.CO2',
        'noise': '*.NAMain.Noise',
        'wifi_status': '*.NAMain.wifi_status',
        'outTemp': '*.NAModule1.Temperature',
        'outHumidity': '*.NAModule1.Humidity',
        'out_rf_status': '*.NAModule1.rf_status',
        'out_battery_vp': '*.NAModule1.battery_vp',
        'outTempBatteryStatus': '*.NAModule1.battery_percent',
        'extraTemp1': '*.NAModule4.Temperature',
        'extraHumid1': '*.NAModule4.Humidity',
        'extra_rf_status_1': '*.NAModule4.rf_status',
        'extra_battery_vp_1': '*.NAModule4.battery_vp',
        'extra1BatteryStatus': '*.NAModule4.battery_percent',
        'windSpeed': '*.NAModule2.WindStrength',
        'windDir': '*.NAModule2.WindAngle',
        'windGust': '*.NAModule2.GustStrength',
        'windGustDir': '*.NAModule2.GustAngle',
        'wind_rf_status': '*.NAModule2.rf_status',
        'wind_battery_vp': '*.NAModule2.battery_vp',
        'windBatteryStatus': '*.NAModule2.battery_percent',
        'rain': '*.NAModule3.Rain',
        'rain_total': '*.NAModule3.sum_rain_24',
        'rain_rf_status': '*.NAModule3.rf_status',
        'rain_battery_vp': '*.NAModule3.battery_vp',
        'rainBatteryStatus': '*.NAModule3.battery_percent'}

    def __init__(self, **stn_dict):
        loginf("driver version is %s" % DRIVER_VERSION)
        self.sensor_map = dict(NetatmoDriver.DEFAULT_SENSOR_MAP)
        if 'sensor_map' in stn_dict:
            self.sensor_map.update(stn_dict['sensor_map'])
        loginf('sensor map is %s' % self.sensor_map)
        device_id = stn_dict.get('device_id', None)
        mode = stn_dict.get('mode', 'cloud')
        if mode.lower() == 'sniff':
            port = int(stn_dict.get('port', NetatmoDriver.DEFAULT_PORT))
            addr = stn_dict.get('host', NetatmoDriver.DEFAULT_HOST)
            self.collector = PacketSniffer((addr, port))
        elif mode.lower() == 'cloud':
            max_tries = int(stn_dict.get('max_tries', 5))
            retry_wait = int(stn_dict.get('retry_wait', 10))  # seconds
            poll_interval = int(stn_dict.get('poll_interval', 300))  # seconds
            tokens_persistence_file = stn_dict['tokens_persistence_file']
            client_id = stn_dict['client_id']
            client_secret = stn_dict['client_secret']
            gm_device_id = stn_dict.get('gm_device_id', None)
            gm_node_id = stn_dict.get('gm_node_id', None)
            self.collector = CloudClient(
                tokens_persistence_file, client_id, client_secret,
                device_id=device_id, poll_interval=poll_interval,
                max_tries=max_tries, retry_wait=retry_wait)
        else:
            raise ValueError("unsupported mode '%s'" % mode)
        self.collector.startup()

    def closePort(self):
        self.collector.shutdown()

    @property
    def hardware_name(self):
        return DRIVER_NAME

    def genLoopPackets(self):
        while True:
            try:
                data = self.collector.queue.get(True, 10)
                logdbg('data: %s' % data)
                pkt = self.data_to_packet(data)
                logdbg('packet: %s' % pkt)
                if pkt:
                    yield pkt
            except Queue.Empty:
                pass

    def data_to_packet(self, data):
        # convert netatmo data to format for database
        packet = dict()
        packet['dateTime'] = int(time.time() + 0.5)
        packet['usUnits'] = weewx.METRIC
        for n in self.sensor_map:
            label = self._find_match(self.sensor_map[n], list(data.keys()))
            if label:
                packet[n] = data.get(label)
        return packet

    @staticmethod
    def _find_match(pattern, keylist):
        pparts = pattern.split('.')
        if len(pparts) != 3:
            return None
        for k in keylist:
            kparts = k.split('.')
            if len(kparts) != 3:
                return None
            if (NetatmoDriver._part_match(pparts[0], kparts[0]) and
                    NetatmoDriver._part_match(pparts[1], kparts[1]) and
                    NetatmoDriver._part_match(pparts[2], kparts[2])):
                return k
        return None

    @staticmethod
    def _part_match(pattern, value):
        if pattern == value:
            return True
        if pattern == '*' and value:
            return True
        return False


class Collector(object):
    queue = Queue.Queue()

    def startup(self):
        pass

    def shutdown(self):
        pass


class CloudClient(Collector):
    """Poll the netatmo servers for data.  Put the result on the queue.

    noise is measured in dB
    co2 is measured in ppm
    rain is measured in mm
    temperatures are measured in C or F

    the user object indicates the units of the download
      unit: 0: metric, 1: imperial
      windunit: 0: kph, 1: mph, 2: m/s, 3: beafort, 4: knot
      pressureunit: 0: mbar, 1: inHg, 2: mmHg
      lang: user locale
      reg_locale: regional preferences for date
      feel_like_algo: 0: humidex, 1: heat-index

    rf_status is a mapping to rssi (+dB)
      0: 90, 1: 80, 2: 70, 3: 60
    wifi_status is a mapping to rssi (+dB)
      0: 86, 1: 71, 2: 56
    battery_vp measures battery capacity
             indoor: 6000-4200; 0: 5640, 1: 5280, 2: 4920, 3: 4560
       rain/outdoor: 6000-3600; 0: 5500, 1: 5000, 2: 4500, 3: 4000
               wind: 6000-3950; 0: 5590, 1: 5180, 2: 4770, 3: 4360
    """

    # endpoints for the cloud queries
    NETATMO_URL = 'https://api.netatmo.com'
    AUTH_URL = '/oauth2/token'
    DATA_URL = '/api/getstationsdata'
    GETM_URL = '/api/getmeasure'

    # mapping between observation name and function used to convert it
    CONVERSIONS = {
        #        'Temperature': '_cvt_temperature',
        #        'AbsolutePressure': '_cvt_pressure',
        #        'Pressure': '_cvt_pressure',
        #        'WindStrength': '_cvt_speed',
        #        'GustStrength': '_cvt_speed',
        #    'Temperature': '_cvt_temperature',
        #    'AbsolutePressure': '_cvt_pressure',
        #    'Pressure': '_cvt_pressure',
        #    'WindStrength': '_cvt_speed',
        #    'GustStrength': '_cvt_speed',
        'Rain': '_cvt_rain',
        'sum_rain_24': '_cvt_rain',
        'sum_rain_1': '_cvt_rain'}
    # list of source units we need to watch for
    UNITS = ['unit', 'windunit', 'pressureunit']

    # these items are tracked from every module and every device
    DASHBOARD_ITEMS = [
        'Temperature', 'Humidity', 'AbsolutePressure', 'Pressure',
        'CO2', 'Noise', 'Rain', 'sum_rain_24', 'sum_rain_1',
        'WindStrength', 'WindAngle', 'GustStrength', 'GustAngle']
    META_ITEMS = [
        'wifi_status', 'rf_status', 'battery_vp', 'co2_calibrating',
        '_id', 'module_name', 'last_status_store', 'last_seen',
        'battery_percent',
        'firmware', 'last_setup', 'last_upgrade', 'date_setup']

    def __init__(self, tokens_persistence_file, client_id, client_secret,
                 device_id=None, poll_interval=300, max_tries=3, retry_wait=30):
        self._poll_interval = poll_interval
        self._max_tries = max_tries
        self._retry_wait = retry_wait
        self._device_id = device_id
        self._auth = CloudClient.GrantTypeAuth(
            tokens_persistence_file, client_id, client_secret)
        self._sd = CloudClient.StationData(self._auth)
        self._gm = CloudClient.StationMeasure(self._auth)
        self._thread = None
        self._collect_data = False
        self._gm_info = {}

    def collect_data(self):
        """Loop forever, wake up periodically to see if it is time to quit."""
        last_poll = 0
        while self._collect_data:
            now = int(time.time())
            if now - last_poll > self._poll_interval:
                for tries in range(self._max_tries):
                    try:
                        CloudClient.get_data(self._sd, self._gm, self._device_id, self._gm_info)
                        break
                    except (socket.error, socket.timeout,
                            urllib.error.HTTPError if pvers == 3 else urllib2.HTTPError,
                            urllib.error.URLError if pvers == 3 else urllib2.URLError) as e:
                        logerr("failed attempt %s of %s to get data: %s" %
                               (tries + 1, self._max_tries, e))
                        logdbg("waiting %s seconds before retry" %
                               self._retry_wait)
                        time.sleep(self._retry_wait)
                    except Exception as e:
                        logerr("exception in netatmo-client: %s" % e)
                        weeutil.weeutil.log_traceback('*** ', syslog.LOG_DEBUG)
                else:
                    logerr("failed to get data after %d attempts" %
                           self._max_tries)
                last_poll = now
                logdbg('next update in %s seconds' % self._poll_interval)
            time.sleep(1)

    @staticmethod
    def get_data(sd, gm, device_id, gm_info):
        """Query the server for each device and module, put data on queue"""
        raw_data = sd.get_data(device_id)
        units_dict = dict((x, raw_data['user']['administrative'][x])
                          for x in CloudClient.UNITS)
        logdbg('cloud units: %s' % units_dict)
        # i would prefer to do partial packets, but there is no guarantee that
        # the timestamps will not align.  so aggregate into a single packet,
        # and let the driver figure out what timestamp it wants to put on it.
        alldata = dict()  # single dict with all devices and modules
        for d in raw_data['devices']:
            data = CloudClient.extract_data(d, units_dict)
            data = CloudClient.apply_labels(data, d['_id'], d['type'])
            alldata.update(data)
            # Collector.queue.put(data)
            for m in d['modules']:
                data = CloudClient.extract_data(m, units_dict)
                if m['type'] == 'NAModule3' and data.get('time_utc', None):
                    # is it rain Module and was the time returned?
                    curr_station = d['_id']
                    if not curr_station in gm_info:
                        gm_info[curr_station] = {'module': m['_id'], 'type': m['type'], 'lastp': 0, 'lasta': 0}
                        print('Found Rain Module %s for correction' % gm_info[curr_station]['module'])
                    actrain = data['time_utc']  # actual time of measurement
                    if gm_info[curr_station]['lastp'] == actrain:  # remove rain data if already posted
                        data['Rain'] = 0.0  # data already written, reset/set to zero
                        logdbg('Duplicate detected. Modified rain to 0.0')
                    gm_info[curr_station]['lastp'] = actrain  # save last posted raindata time
                data = CloudClient.apply_labels(data, m['_id'], m['type'])
                alldata.update(data)

        # Collector.queue.put(alldata)
        """Query the server for rain data with getmeasurement."""
        for station in gm_info:
            rain_data = gm.get_data(station, gm_info[station]['module'])
            logdbg('getmeasurement Resp: %s' % rain_data)
            rain_data_times = [int(x) for x in rain_data.keys()]
            rain_data_times.sort(reverse=True)

            if len(rain_data_times) > 1 and len(rain_data[str(rain_data_times[1])]) != 0:
                if rain_data_times[0] == gm_info[station]['lastp']:  # last measurement is the same time, OK
                    if rain_data_times[1] == gm_info[station]['lasta']:  # data already written?
                        pass  # yes, do nothing
                    else:  # no, prepare for adding rain amount
                        # Rain Data is statically converted from mm -> cm (as WEEWX needs it) by multiplying with 0.1
                        # add the additional rain data to the entry "Rain" in collected data
                        rainindex = gm_info[station]['module'] + "." + gm_info[station]['type'] + ".Rain"
                        logdbg('Modified rain data for %s' % rainindex)
                        alldata[rainindex] += (rain_data[str(rain_data_times[1])][0]) * 0.1
                        gm_info[station]['lasta'] = rain_data_times[1]  # save last written date
            else:
                print("Lacking data for rain fix. Skipping.")
        logdbg('Alldata: %s' % alldata)
        Collector.queue.put(alldata)  # now write the modified record

    @staticmethod
    def extract_data(x, units_dict):
        """Extract data we care about from a device or module"""
        data = dict()
        # if contact with sensors is lost, then there will be no dashboard_data
        if 'dashboard_data' not in x:
            return data
        if 'time_utc' in x['dashboard_data']:
            data['time_utc'] = x['dashboard_data']['time_utc']
        for n in CloudClient.META_ITEMS:
            if n in x:
                data[n] = x[n]
        for n in CloudClient.DASHBOARD_ITEMS:
            if n in x['dashboard_data']:
                data[n] = x['dashboard_data'][n]
        # do any unit conversions - everything converts to weewx.METRIC
        for n in data:
            if n in CloudClient.CONVERSIONS:
                try:
                    func = CloudClient.CONVERSIONS.get(n)
                    data[n] = getattr(CloudClient, func)(data[n], units_dict)
                except ValueError as e:
                    logerr("unit conversion failed for %s: %s" % (data[n], e))
                    data[n] = None
        return data

    @staticmethod
    def apply_labels(data, xid, xtype):
        """Copy the data dict but use fully-qualified keys"""
        return dict(("%s.%s.%s" % (xid, xtype, n), data[n]) for n in data)

    #        new_data = dict()
    #        for n in data:
    #            new_data["%s.%s.%s" % (xid, xtype, n)] = data[n]
    #        return new_data

    @staticmethod
    def _cvt_pressure(x, from_unit_dict):
        # pressureunit: 0: mbar, 1: inHg, 2: mmHg
        if from_unit_dict['pressureunit'] == 1:
            x /= INHG_PER_MBAR
        elif from_unit_dict['pressureunit'] == 2:
            x /= INHG_PER_MBAR * 25.4
        return x

    @staticmethod
    def _cvt_speed(x, from_unit_dict):
        # windunit: 0: kph, 1: mph, 2: m/s, 3: beafort, 4: knot
        if from_unit_dict['windunit'] == 1:
            x *= MPH_TO_KPH
        elif from_unit_dict['windunit'] == 2:
            x *= MPS_TO_KPH
        elif from_unit_dict['windunit'] == 3:
            x = BEAFORT_TO_KPH.get(x)
        elif from_unit_dict['windunit'] == 4:
            x *= KNOT_TO_KPH
        return x

    @staticmethod
    def _cvt_temperature(x, from_unit_dict):
        if from_unit_dict['unit'] == 1:
            x = weewx.wxformulas.FtoC(x)
        return x

    @staticmethod
    def _cvt_rain(x, from_unit_dict):
        # convert mm to cm since weewx METRIC wants cm
        return x * 0.1

    def startup(self):
        """Start a thread that collects data from the netatmo servers."""
        self._thread = CloudClient.CollectorThread(self)
        self._collect_data = True
        self._thread.start()

    def shutdown(self):
        """Tell the thread to stop, then wait for it to finish."""
        if self._thread:
            self._collect_data = False
            self._thread.join()
            self._thread = None

    class CollectorThread(threading.Thread):
        def __init__(self, client):
            threading.Thread.__init__(self)
            self.client = client
            self.name = 'netatmo-client'

        def run(self):
            self.client.collect_data()

    class GrantTypeAuth(object):
        """Encapsulate the authentication data and protocols.  This
        object contains the refresh token, client_id, and client_secret
        that are required to authenticate to the api.

        The access token will be fetched on the first time so that the class can keep track of the expiration date

        It will re-query the netatmo server whenever the tokens have expired"""

        def __init__(self, tokens_persistence_file, client_id, client_secret):
            self._tokens_persistence_file = tokens_persistence_file
            self._client_id = client_id
            self._client_secret = client_secret

            self._access_token = None
            self._scope = None
            self._expiration = None

        @property
        def refresh_token(self):
            data = {}
            with open(self._tokens_persistence_file, 'r') as f:
                try:
                    data = json.load(f)
                except json.JSONDecodeError as e:
                    raise Exception(
                        "Could not decode {} content into a JSON format, "
                        "you should also provide a refresh_token key: {}".format(self._tokens_persistence_file, e)
                    )

            refresh_token = data.get("refresh_token", None)
            if not refresh_token:
                raise ValueError("Missing refresh_token in file {}".format(self._tokens_persistence_file))
            return refresh_token

        @refresh_token.setter
        def refresh_token(self, value):
            data = {}
            with open(self._tokens_persistence_file, 'r') as f:
                try:
                    data = json.load(f)
                except json.JSONDecodeError:
                    data = {}
            data["refresh_token"] = value
            with open(self._tokens_persistence_file, 'w') as f:
                json.dump(data, f)

        @property
        def access_token(self):
            if self._expiration is None or self._expiration < time.time():
                params = {
                    'grant_type': 'refresh_token',
                    'refresh_token': self.refresh_token,
                    'client_id': self._client_id,
                    'client_secret': self._client_secret}
                resp = CloudClient.post_request(CloudClient.AUTH_URL, params)
                self._access_token = resp['access_token']
                self.refresh_token = resp['refresh_token']
                self._expiration = int(resp['expire_in'] + time.time())
            return self._access_token

    class StationData(object):
        def __init__(self, auth):
            self._auth = auth
            self._last_update = 0
            self._raw_data = dict()

        def get_data(self, device_id=None, stale=60):  # changed to 60 from 300 to avoid missing data
            if int(time.time()) - self._last_update > stale:
                params = {}
                headers = {"Authorization": "Bearer " + self._auth.access_token}
                if device_id:
                    params['device_id'] = device_id
                resp = CloudClient.post_request(CloudClient.DATA_URL, params, headers=headers)
                self._raw_data = dict(resp['body'])
                self._last_update = int(time.time())
            return self._raw_data

    class StationMeasure(object):
        """ Get full rain data through a get measurement call."""

        def __init__(self, auth):
            self._auth = auth
            self._last_update = 0
            self._raw_data = dict()

        def get_data(self, device_id, module_id, stale=60):  # changed to 60 from 300 to avoid missing data
            if int(time.time()) - self._last_update > stale:
                # date_begin = int(datetime.datetime.now().timestamp()) - 30 * 60
                date_begin = int(time.time()) - 30 * 60
                params = {'access_token': self._auth.access_token}
                params['device_id'] = device_id
                params['module_id'] = module_id
                params['scale'] = 'max'
                params['type'] = 'rain'
                params['date_begin'] = date_begin
                #  "&date_end=" + date_end +
                #  "&limit=" + limit +
                params['optimize'] = 'false'
                params['real_time'] = 'true'
                resp = CloudClient.post_request(CloudClient.GETM_URL, params)
                self._raw_data = dict(resp['body'])
                self._last_update = int(time.time())
            return self._raw_data

    @staticmethod
    def post_request(url, params, headers=None):
        # netatmo response body size is limited to 64K
        url = CloudClient.NETATMO_URL + url
        params = urlencode(params).encode("utf-8")
        if headers is None:
            headers = {}
        headers.update({
            "Content-Type": "application/x-www-form-urlencoded;charset=utf-8"})
        logdbg("url: %s data: %s hdr: %s" % (url, params, headers))
        req = urllib.request.Request(url=url, data=params, headers=headers) if pvers == 3 else \
            urllib2.Request(url=url, data=params, headers=headers)
        resp = urllib.request.urlopen(req).read(65535) if pvers == 3 else \
            urllib2.urlopen(req).read(65535)
        resp_obj = json.loads(resp)
        logdbg("resp_obj: %s" % resp_obj)
        return resp_obj


class PacketSniffer(Collector):
    """listen for incoming packets then parse them.  put result on queue."""

    def startup(self):
        pass

    def shutdown(self):
        pass

    class Packet(object):
        _HDR = re.compile('(\d+).(\d+) IP (\S+) > (\S+):')
        _DATA = re.compile('0x00\d0: (.*)')

        def lines2packets(lines):
            pkts = []
            ts = None
            src = None
            dst = None
            data = []
            for line in lines:
                line = line.strip()
                PacketSniffer.Packet._HDR.search(line)
                if m:
                    ts = m.group(1)
                    src = m.group(3)
                    dst = m.group(4)
                    data = []
                    pkts.append({'dateTime': ts, 'src': src, 'dst': dst,
                                 'data': ''.join(data)})
                    continue
                PacketSniffer.Packet._DATA.search(line)
                if m:
                    data.append(m.group(1))
                    continue
            return pkts

        @staticmethod
        def parse_data(data):
            pkt = dict()
            return pkt


# To test this driver, do the following:
#   PYTHONPATH=bin python user/netatmo.py
if __name__ == "__main__":
    usage = """%prog [options] [--help]"""


    def main():
        import optparse
        syslog.openlog('wee_netatmo', syslog.LOG_PID | syslog.LOG_CONS)
        parser = optparse.OptionParser(usage=usage)
        parser.add_option('--version', dest='version', action='store_true',
                          help='display driver version')
        parser.add_option('--debug', dest='debug', action='store_true',
                          help='display diagnostic information while running')
        parser.add_option('--run-sniff-driver', dest='ts', action='store_true',
                          help='run the driver in packet sniff mode')
        parser.add_option('--run-cloud-driver', dest='tc', action='store_true',
                          help='run the driver in cloud client mode')
        parser.add_option('--test-parse', dest='tp', metavar='FILENAME',
                          help='test the tcp packet parser')
        parser.add_option('--tokens_persistence_file', dest='tokens_persistence_file',
                          metavar='TOKENS_PERSISTENCE_FILE',
                          help='tokens persistence file for cloud mode')
        parser.add_option('--client-id', dest='ci', metavar='CLIENT_ID',
                          help='client_id for cloud mode')
        parser.add_option('--client-secret', dest='cs', metavar='CLIENT_SECRET',
                          help='client_secret for cloud mode')
        parser.add_option('--get-stn-data', dest='sdata', action='store_true',
                          help='get formatted station data from cloud')
        parser.add_option('--get-json-data', dest='jdata', action='store_true',
                          help='get all cloud data as json response')
        (opts, args) = parser.parse_args()

        if opts.debug:
            syslog.setlogmask(syslog.LOG_UPTO(syslog.LOG_DEBUG))

        if opts.ts:
            run_packet_driver()
        if opts.tc:
            run_cloud_driver(opts.tokens_persistence_file, opts.ci, opts.cs)
        if opts.tp:
            test_parse(options.tp)
        if opts.sdata:
            get_station_data(opts.tokens_persistence_file, opts.ci, opts.cs)
        if opts.jdata:
            get_json_data(opts.tokens_persistence_file, opts.ci, opts.cs)


    def run_sniff_driver():
        import weeutil.weeutil
        driver = NetatmoDriver({'mode': 'sniff'})
        for pkt in driver.genLoopPackets():
            print(weeutil.weeutil.timestamp_to_string(pkt['dateTime']), pkt)


    def run_cloud_driver(tokens_persistence_file, c_id, c_secret):
        import weeutil.weeutil
        driver = None
        try:
            driver = NetatmoDriver(mode='cloud',
                                   tokens_persistence_file=tokens_persistence_file,
                                   client_id=c_id, client_secret=c_secret)
            for pkt in driver.genLoopPackets():
                print(weeutil.weeutil.timestamp_to_string(pkt['dateTime']), pkt)
        except KeyboardInterrupt:
            driver.closePort()


    def get_station_data(tokens_persistence_file, c_id, c_secret):
        auth = CloudClient.GrantTypeAuth(tokens_persistence_file, c_id, c_secret)
        sd = CloudClient.StationData(auth)
        ppv('station data', sd.get_data())


    def get_json_data(tokens_persistence_file, c_id, c_secret):
        auth = CloudClient.GrantTypeAuth(tokens_persistence_file, c_id, c_secret)
        params = {'app_type': 'app_station'}
        headers = {"Authorization": "Bearer " + auth.access_token}
        reply = CloudClient.post_request(CloudClient.DATA_URL, params, headers=headers)
        print(json.dumps(reply, sort_keys=True, indent=2))


    def test_parse(filename):
        lines = []
        with open(filename, "r") as f:
            while f:
                lines.append(f.readline())
        print(PacketSniffer.Packet.lines2packets(''.join(lines)))


    def ppv(label, x, level=0):
        """pretty-print a variable, recursing if it is a dict"""
        indent = '  '
        if type(x) is dict:
            print("%s%s" % (indent * level, label))
            for n in x:
                ppv(n, x[n], level=level + 1)
        elif type(x) is list:
            print("%s[" % (indent * level))
            for i, y in enumerate(x):
                ppv("%s %s" % (label, i), y, level=level + 1)
            print("%s]" % (indent * level))
        else:
            print("%s%s=%s" % (indent * level, label, x))


    main()
