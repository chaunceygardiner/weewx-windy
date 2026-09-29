# Copyright 2019-2026 Matthew Wall
# Copyright 2026 John A Kline (Windy Stations API v2)

"""
This is a weewx extension that uploads data to windy.com through the Windy
Stations API v2.

https://stations.windy.com/api-reference

Each archive record goes out as an HTTPS GET of

    https://stations.windy.com/api/v2/observation/update?id=<station_id>&...

with the observations as query parameters in metric units, and the station
password in an Authorization: Bearer header, so it never appears in a URL or
a log.  Windy's original API (pws/update/<api_key>, used through 0.8) stops
accepting data on 31 December 2026.

Minimal configuration

[StdRESTful]
    [[Windy]]
        station_id = STATION_ID
        password = STATION_PASSWORD

Both come from the station's page under My Stations at
https://stations.windy.com/stations.  Each station has its own ID and
password; the account API key is for managing stations and cannot upload.

Windy accepts one upload per station every 5 minutes, refuses observations
more than 2 hours old, and keeps only the latest, so an old record is worth
nothing to it.  Hence the defaults: post_interval = 300, max_backlog = 0
(post only the newest record waiting) and stale = 1800.

To check a station password without uploading anything, run this file with
--check (see the end of the file).
"""

import argparse
import getpass
import json
import logging
import queue
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode, urlsplit

import weewx
import weewx.manager
import weewx.restx
import weewx.units
from weeutil.weeutil import to_bool

VERSION = "1.0"

REQUIRED_WEEWX = (4, 1)


def weewx_version_at_least(minimum, version=None):
    """Is WeeWX `version` (default: the running one) at least `minimum`?

    Compared part by part as integers: as text, "4.10" sorts below "4.6".
    weeutil's own version_compare cannot do this check, because it first
    shipped in WeeWX 5.0 and so is missing from the versions between the
    floor and 5.0 that this has to accept.
    """
    if version is None:
        version = weewx.__version__
    running = []
    for chunk in version.split('.')[:len(minimum)]:
        digits = ''
        for char in chunk:
            if not char.isdigit():
                break
            digits += char
        running.append(int(digits) if digits else 0)
    running += [0] * (len(minimum) - len(running))
    return tuple(running) >= tuple(minimum)


if not weewx_version_at_least(REQUIRED_WEEWX):
    raise weewx.UnsupportedFeature(
        "weewx-windy requires WeeWX %s or later, found %s"
        % ('.'.join(str(part) for part in REQUIRED_WEEWX), weewx.__version__))

log = logging.getLogger(__name__)


def logdbg(msg):
    log.debug(msg)


def loginf(msg):
    log.info(msg)


def logerr(msg):
    log.error(msg)


# The options that configured the retired API: the account key, and an
# integer that told one key's stations apart.
LEGACY_OPTIONS = ('api_key', 'station')


class Windy(weewx.restx.StdRESTbase):
    DEFAULT_URL = 'https://stations.windy.com/api/v2/observation/update'

    def __init__(self, engine, cfg_dict):
        super(Windy, self).__init__(engine, cfg_dict)
        loginf("version is %s" % VERSION)
        site_dict = weewx.restx.get_site_dict(
            cfg_dict, 'Windy', 'station_id', 'password')
        if site_dict is None:
            if Windy.needs_v2_credentials(cfg_dict):
                logerr("Nothing will be uploaded.  api_key is the credential "
                       "for Windy's retired API; the current one needs "
                       "station_id and password in [StdRESTful] [[Windy]], "
                       "both on the station's page at "
                       "https://stations.windy.com/stations")
            return

        legacy = [key for key in LEGACY_OPTIONS if key in site_dict]
        for key in legacy:
            site_dict.pop(key)
        if legacy:
            loginf("%s %s no longer used and can be deleted from "
                   "[StdRESTful] [[Windy]]"
                   % (' and '.join(legacy), 'is' if len(legacy) == 1 else 'are'))

        # 0.8 and earlier uploaded to <host>/pws/update/<api_key>.  A
        # server_url still pointing there would send every v2 upload to a
        # path that no longer takes them.
        server_url = site_dict.get('server_url')
        if server_url and urlsplit(server_url).path.rstrip('/') == '/pws/update':
            site_dict.pop('server_url')
            loginf("server_url %s is Windy's retired API and is ignored; "
                   "delete it from [StdRESTful] [[Windy]]" % server_url)

        try:
            site_dict['manager_dict'] = weewx.manager.get_manager_dict_from_config(cfg_dict, 'wx_binding')
        except weewx.UnknownBinding:
            pass

        self.archive_queue = queue.Queue()
        self.archive_thread = WindyThread(self.archive_queue, **site_dict)

        self.archive_thread.start()
        self.bind(weewx.NEW_ARCHIVE_RECORD, self.new_archive_record)
        loginf("Data for station %s will be uploaded to %s"
               % (site_dict['station_id'], self.archive_thread.server_url))

    @staticmethod
    def needs_v2_credentials(cfg_dict):
        """Is this an enabled station still configured for the retired API?"""
        try:
            windy_dict = cfg_dict['StdRESTful']['Windy']
        except KeyError:
            return False
        return (to_bool(windy_dict.get('enable', True))
                and 'api_key' in windy_dict)

    def new_archive_record(self, event):
        self.archive_queue.put(event.record)


# What Windy is sent: (WeeWX type, Windy parameter, multiplier from METRICWX
# units, decimal places, lowest and highest value Windy accepts).  Windy
# rejects the WHOLE observation when one value is out of range, so a value
# outside its range is left out and the rest still go.
OBSERVATIONS = [
    ('outTemp',     'temp',           1.0,   1, -100, 100),     # degree_C
    ('dewpoint',    'dewpoint',       1.0,   1, -60, 50),       # degree_C
    ('outHumidity', 'humidity',       1.0,   1, 0, 100),        # percent
    ('windSpeed',   'wind',           1.0,   1, 0, 200),        # m/s
    ('windGust',    'gust',           1.0,   1, 0, 300),        # m/s
    ('windDir',     'winddir',        1.0,   0, 0, 360),        # degree
    ('barometer',   'pressure',       100.0, 0, 50000, 150000),  # mbar to Pa
    ('hourRain',    'precip',         1.0,   2, 0, 2500),       # mm, past hour
    ('UV',          'uv',             1.0,   1, 0, 70),         # index
    ('radiation',   'solarradiation', 1.0,   0, 0, 2000),       # W/m^2
]


class WindyThread(weewx.restx.RESTThread):

    def __init__(self, q, station_id, password, server_url=Windy.DEFAULT_URL,
                 skip_upload=False, manager_dict=None,
                 post_interval=300, max_backlog=0, stale=1800,
                 log_success=True, log_failure=True,
                 timeout=60, max_tries=3, retry_wait=5, retry_login=3600):
        super(WindyThread, self).__init__(q,
                                          protocol_name='Windy',
                                          manager_dict=manager_dict,
                                          post_interval=post_interval,
                                          max_backlog=max_backlog,
                                          stale=stale,
                                          log_success=log_success,
                                          log_failure=log_failure,
                                          max_tries=max_tries,
                                          timeout=timeout,
                                          retry_wait=retry_wait,
                                          retry_login=retry_login)
        self.station_id = station_id
        self.password = password
        self.server_url = server_url
        self.skip_upload = to_bool(skip_upload)

    def format_url(self, record):
        """Return the URL for a GET that uploads the record to windy"""
        record_m = weewx.units.to_METRICWX(record)
        params = [('id', self.station_id),
                  ('ts', int(record_m['dateTime']))]
        for obs_type, param, multiplier, places, lowest, highest in OBSERVATIONS:
            value = record_m.get(obs_type)
            if value is None:
                continue
            value = round(value * multiplier, places)
            # Range first: int() raises on NaN and infinity, and an exception
            # here would end the upload thread.  NaN fails every comparison,
            # so a NaN reading is dropped here like any other bad value.
            if not lowest <= value <= highest:
                logdbg("%s %s is outside the %s to %s windy accepts; not sent"
                       % (obs_type, value, lowest, highest))
                continue
            params.append((param, int(value) if places == 0 else value))
        params.append(('softwaretype', 'weewx-windy/%s' % VERSION))
        url = '%s?%s' % (self.server_url, urlencode(params))
        if weewx.debug >= 2:
            logdbg("url: %s" % url)
        return url

    def get_request(self, url):
        """Add the station password, which windy takes as a bearer token"""
        request = super(WindyThread, self).get_request(url)
        request.add_header('Authorization', 'Bearer %s' % self.password)
        return request

    def get_post_body(self, record):
        """No body: the observations are in the URL, so this is a GET"""
        return None

    def handle_exception(self, e, count):
        """Stop at once on the answers a retry cannot change.

        Windy answers 400 for a wrong password as well as for a rejected
        observation, 401 for a missing password, 409 for an observation it
        already has, and 429 for an upload less than 5 minutes after the
        last one.  Anything else (a 5xx, a timeout, no network) is retried.
        """
        code = getattr(e, 'code', None)
        if code not in (400, 401, 409, 429):
            super(WindyThread, self).handle_exception(e, count)
            return
        body = read_error_body(e)
        message = error_message(body) or getattr(e, 'reason', '') or str(e)
        if code == 401 or (code == 400 and 'password' in message.lower()):
            # RESTThread logs only that the login failed, not why.
            logerr("Station %s: %s (HTTP %d)" % (self.station_id, message, code))
            raise weewx.restx.BadLogin(message)
        if code == 400:
            raise weewx.restx.FailedPost("Windy rejected the observation: %s"
                                         % message)
        if code == 409:
            raise weewx.restx.AbortedPost("Windy already has this observation")
        raise weewx.restx.AbortedPost(
            "Windy allows one upload every 5 minutes; next one allowed at %s"
            % body.get('retry_after', 'an unstated time'))


def read_error_body(e):
    """The JSON body of an HTTP error response, or {} if there is none"""
    try:
        body = json.loads(e.read().decode('utf-8', 'replace'))
    except (AttributeError, OSError, ValueError):
        return {}
    return body if isinstance(body, dict) else {}


def error_message(body):
    """Windy's explanation from an error body: one string, or a list of them
    when an observation fails validation on several counts"""
    message = body.get('message', '')
    if isinstance(message, list):
        message = '; '.join(str(item) for item in message)
    return str(message)


def check_password(password, url=None, timeout=30):
    """Ask windy which station a password belongs to.  Uploads nothing.

    Returns the station's metadata (id, name, last_observation_time, ...).
    Raises urllib.error.HTTPError when windy refuses the password, and
    ValueError when its answer names no station.
    """
    if url is None:
        url = Windy.DEFAULT_URL.rsplit('/', 1)[0] + '?latestLimit=1'
    request = urllib.request.Request(url)
    request.add_header('User-Agent', 'weewx-windy/%s' % VERSION)
    request.add_header('Authorization', 'Bearer %s' % password)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.loads(response.read().decode('utf-8'))
    header = body.get('header') if isinstance(body, dict) else None
    if not isinstance(header, dict) or not header.get('id'):
        raise ValueError("unexpected answer from windy: %.200s" % body)
    return header


# Use this hook to test the uploader.  With no arguments it prints the URL a
# sample record would be uploaded with, and sends nothing.  With --check it
# asks for a station password and shows which windy station it belongs to --
# the station_id to put in weewx.conf -- again without uploading anything:
#   PYTHONPATH=bin python bin/user/windy.py [--check]

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Check windy credentials without uploading anything.")
    parser.add_argument('--check', action='store_true',
                        help="ask for a station password and show the "
                             "station it belongs to")
    args = parser.parse_args(argv)

    if args.check:
        password = getpass.getpass("Windy station password: ").strip()
        try:
            station = check_password(password)
        except urllib.error.HTTPError as e:
            print("Windy refused it: %s (HTTP %d)"
                  % (error_message(read_error_body(e)) or e.reason, e.code))
            return 1
        except (urllib.error.URLError, OSError) as e:
            print("Could not reach windy: %s" % getattr(e, 'reason', e))
            return 1
        except ValueError as e:
            # Not JSON, or JSON that names no station.
            print("Could not check the password: %s" % e)
            return 1
        print("station_id = %s" % station.get('id'))
        print("name: %s" % station.get('name'))
        print("last observation: %s"
              % station.get('last_observation_time', 'none yet'))
        return 0

    t = WindyThread(queue.Queue(), station_id='STATION_ID',
                    password='STATION_PASSWORD')
    r = {'dateTime': int(time.time() + 0.5),
         'usUnits': weewx.US,
         'outTemp': 32.5,
         'inTemp': 75.8,
         'outHumidity': 24,
         'windSpeed': 10,
         'windDir': 32}
    print(t.format_url(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
