"""Tests for weewx-windy.  Hermetic: no test touches the network.

Every HTTP call goes through a patched urlopen, so these run the real
upload path -- format_url, get_request, RESTThread's retry loop,
handle_exception -- and fake only windy's answer.

    /home/weewx/weewx-venv/bin/python -m pytest tests

The installer tests merge into WeeWX 5's shipped weewx.conf; everything
else also runs against WeeWX 4 (-k 'not TestTheInstaller').
"""

import importlib.util
import io
import os
import queue
import time
import unittest
import urllib.error
from unittest import mock
from urllib.parse import parse_qsl, urlsplit

import configobj

import weecfg.extension
import weewx
import weewx.restx

import user.windy
from user.windy import Windy, WindyThread

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

STATION_ID = 'YZjgOxm'
PASSWORD = 's3cret-pw'

# 11:25 PDT on 2026-09-29, in US units, as a station's archive record has it.
US_RECORD = {
    'dateTime': 1790707500,
    'usUnits': weewx.US,
    'outTemp': 50.0,          # 10.0 C
    'dewpoint': 32.0,         # 0.0 C
    'outHumidity': 55.0,
    'windSpeed': 10.0,        # 4.4704 m/s
    'windGust': 22.3694,      # 10.0 m/s
    'windDir': 225.4,
    'barometer': 29.92,       # 1013.207 mbar
    'hourRain': 0.1,          # 2.54 mm
    'UV': 3.24,
    'radiation': 540.4,
    'inTemp': 75.8,           # not something windy takes
}


def make_thread(**kwargs):
    kwargs.setdefault('retry_wait', 0)
    return WindyThread(queue.Queue(), station_id=STATION_ID,
                       password=PASSWORD, **kwargs)


def query(url):
    return dict(parse_qsl(urlsplit(url).query))


def ok_response():
    response = mock.MagicMock()
    response.code = 200
    return response


def http_error(code, body):
    return urllib.error.HTTPError(Windy.DEFAULT_URL, code, 'error', {},
                                  io.BytesIO(body.encode('utf-8')))


class TestVersionFloor(unittest.TestCase):

    def test_versions_compare_as_numbers(self):
        at_least = user.windy.weewx_version_at_least
        self.assertTrue(at_least((4, 1), '4.1.0'))
        self.assertTrue(at_least((4, 1), '4.10.2'))    # "4.10" < "4.6" as text
        self.assertTrue(at_least((4, 1), '5.5.2'))
        self.assertTrue(at_least((4, 1), '5.0.0b17'))
        self.assertTrue(at_least((4, 1), '10.0'))      # "10" < "4" as text
        self.assertFalse(at_least((4, 1), '4.0.9'))
        self.assertFalse(at_least((4, 1), '4'))
        self.assertFalse(at_least((4, 1), '3.9.2'))

    def load_module_under(self, version):
        spec = importlib.util.spec_from_file_location(
            'windy_under_test', os.path.join(REPO, 'bin', 'user', 'windy.py'))
        with mock.patch.object(weewx, '__version__', version):
            spec.loader.exec_module(importlib.util.module_from_spec(spec))

    def test_the_module_refuses_to_load_below_the_floor(self):
        with self.assertRaises(weewx.UnsupportedFeature) as caught:
            self.load_module_under('4.0.1')
        self.assertIn('4.1', str(caught.exception))
        self.assertIn('4.0.1', str(caught.exception))

    def test_the_module_loads_on_the_last_weewx_4(self):
        self.load_module_under('4.10.2')


class TestTheRequest(unittest.TestCase):

    def test_observations_go_in_metric_with_windy_names(self):
        self.assertEqual(query(make_thread().format_url(US_RECORD)), {
            'id': STATION_ID,
            'ts': '1790707500',
            'temp': '10.0',
            'dewpoint': '0.0',
            'humidity': '55.0',
            'wind': '4.5',
            'gust': '10.0',
            'winddir': '225',
            'pressure': '101321',
            'precip': '2.54',
            'uv': '3.2',
            'solarradiation': '540',
            'softwaretype': 'weewx-windy/%s' % user.windy.VERSION,
        })

    def test_it_goes_to_the_v2_endpoint(self):
        url = make_thread().format_url(US_RECORD)
        self.assertEqual(url.split('?')[0],
                         'https://stations.windy.com/api/v2/observation/update')

    def test_the_password_is_never_in_the_url(self):
        self.assertNotIn(PASSWORD, make_thread().format_url(US_RECORD))

    def test_missing_and_none_values_are_left_out(self):
        record = dict(US_RECORD, outTemp=None)
        del record['UV']
        params = query(make_thread().format_url(record))
        self.assertNotIn('temp', params)
        self.assertNotIn('uv', params)
        self.assertEqual(params['humidity'], '55.0')

    def test_a_value_windy_would_refuse_is_dropped_not_the_record(self):
        # Windy answers 400 to the whole observation when one value is out
        # of range; a humidity sensor reading 101 must not cost the rest.
        record = dict(US_RECORD, outHumidity=101.0, windDir=359.7)
        params = query(make_thread().format_url(record))
        self.assertNotIn('humidity', params)
        self.assertEqual(params['winddir'], '360')
        self.assertEqual(params['temp'], '10.0')

    def test_nan_and_infinity_are_dropped_not_fatal(self):
        # int() raises on both; in format_url that would end the thread.
        record = dict(US_RECORD, windDir=float('nan'), radiation=float('inf'),
                      outTemp=float('nan'))
        params = query(make_thread().format_url(record))
        self.assertNotIn('winddir', params)
        self.assertNotIn('solarradiation', params)
        self.assertNotIn('temp', params)
        self.assertEqual(params['humidity'], '55.0')

    def test_a_metric_station_is_converted_too(self):
        record = {'dateTime': 1790707500, 'usUnits': weewx.METRIC,
                  'outTemp': 10.0, 'windSpeed': 36.0, 'rain': 0.1,
                  'hourRain': 0.254, 'barometer': 1013.2}  # km/h, cm
        params = query(make_thread().format_url(record))
        self.assertEqual(params['wind'], '10.0')
        self.assertEqual(params['precip'], '2.54')
        self.assertEqual(params['pressure'], '101320')


class TestTheUpload(unittest.TestCase):
    """process_record runs the whole path; only windy's answer is faked."""

    def upload(self, thread, answers):
        # Patched where restx looks it up: on WeeWX 4 that is six.moves,
        # which caches the first urlopen it sees, so patching urllib.request
        # would leave every test after the first talking to a stale mock.
        with mock.patch.object(weewx.restx.urllib.request, 'urlopen',
                               side_effect=answers) as urlopen:
            try:
                thread.process_record(US_RECORD, None)
            finally:
                self.calls = urlopen.call_args_list

    def test_a_get_with_the_password_as_a_bearer_token(self):
        self.upload(make_thread(), [ok_response()])
        self.assertEqual(len(self.calls), 1)
        request = self.calls[0].args[0]
        self.assertEqual(request.get_method(), 'GET')
        self.assertIsNone(self.calls[0].kwargs.get('data'))
        self.assertEqual(request.get_header('Authorization'), 'Bearer %s' % PASSWORD)
        self.assertEqual(query(request.full_url)['id'], STATION_ID)

    def test_a_wrong_password_stops_at_once_and_says_why(self):
        answer = http_error(400, '{"message":"Provided password is invalid",'
                                 '"error":"Bad Request","statusCode":400}')
        with self.assertLogs('user.windy', 'ERROR') as logged:
            with self.assertRaises(weewx.restx.BadLogin):
                self.upload(make_thread(), [answer])
        self.assertEqual(len(self.calls), 1)       # no retries
        self.assertIn('Provided password is invalid', logged.output[0])
        self.assertIn(STATION_ID, logged.output[0])

    def test_a_missing_password_is_a_bad_login(self):
        answer = http_error(401, '{"message":"Provide a valid password via the '
                                 'PASSWORD query parameter or a Bearer token.",'
                                 '"statusCode":401}')
        with self.assertLogs('user.windy', 'ERROR'):
            with self.assertRaises(weewx.restx.BadLogin):
                self.upload(make_thread(), [answer])
        self.assertEqual(len(self.calls), 1)

    def test_a_rejected_observation_is_not_retried(self):
        answer = http_error(400, '{"message":["winddir must be an integer",'
                                 '"temp must not be greater than 100"],'
                                 '"statusCode":400}')
        with self.assertRaises(weewx.restx.FailedPost) as caught:
            self.upload(make_thread(), [answer])
        self.assertEqual(len(self.calls), 1)
        self.assertIn('winddir must be an integer; temp must not be greater '
                      'than 100', str(caught.exception))

    def test_a_duplicate_is_skipped(self):
        with self.assertRaises(weewx.restx.AbortedPost):
            self.upload(make_thread(), [http_error(409, '{}')])
        self.assertEqual(len(self.calls), 1)

    def test_too_soon_is_skipped_with_windy_s_next_time(self):
        answer = http_error(429, '{"retry_after":"2026-09-29T18:40:00.000Z"}')
        with self.assertRaises(weewx.restx.AbortedPost) as caught:
            self.upload(make_thread(), [answer])
        self.assertEqual(len(self.calls), 1)
        self.assertIn('2026-09-29T18:40:00.000Z', str(caught.exception))

    def test_a_server_error_is_retried(self):
        answers = [http_error(503, ''), http_error(503, ''), ok_response()]
        self.upload(make_thread(), answers)
        self.assertEqual(len(self.calls), 3)

    def test_a_server_error_that_persists_fails_after_max_tries(self):
        with self.assertRaises(weewx.restx.FailedPost):
            self.upload(make_thread(), [http_error(503, '')] * 3)
        self.assertEqual(len(self.calls), 3)

    def test_skip_upload_sends_nothing(self):
        with self.assertRaises(weewx.restx.AbortedPost):
            self.upload(make_thread(skip_upload='true'), [])
        self.assertEqual(self.calls, [])


class TestWhichRecordsGo(unittest.TestCase):
    """RESTThread's own loop, with windy's defaults: newest record only."""

    class DrainedQueue(queue.Queue):
        """Answers None -- RESTThread's signal to stop -- once empty.  A
        None put on the queue would count as backlog and cost the newest
        record, which a running station never sees."""
        def get(self, block=True, timeout=None):
            return None if self.empty() else super().get(block, timeout)

    def run_loop_over(self, records, **kwargs):
        thread = make_thread(**kwargs)
        thread.queue = self.DrainedQueue()
        for record in records:
            thread.queue.put(record)
        with mock.patch.object(thread, 'process_record') as process:
            thread.run_loop()
        return [call.args[0]['dateTime'] for call in process.call_args_list]

    def test_a_backlog_posts_only_the_newest_record(self):
        now = int(time.time())
        records = [dict(US_RECORD, dateTime=now - 600 + 300 * n) for n in range(3)]
        self.assertEqual(self.run_loop_over(records), [now])

    def test_a_record_older_than_half_an_hour_is_not_sent(self):
        now = int(time.time())
        self.assertEqual(
            self.run_loop_over([dict(US_RECORD, dateTime=now - 1900)]), [])
        self.assertEqual(
            self.run_loop_over([dict(US_RECORD, dateTime=now - 1700)]), [now - 1700])

    def test_records_closer_than_five_minutes_are_not_both_sent(self):
        now = int(time.time())
        records = [dict(US_RECORD, dateTime=now - 60), dict(US_RECORD, dateTime=now)]
        self.assertEqual(self.run_loop_over(records, max_backlog=10), [now - 60])


def stdrestful(windy_lines):
    text = '[StdRESTful]\n    [[Windy]]\n' + ''.join(
        '        %s\n' % line for line in windy_lines)
    return configobj.ConfigObj(io.StringIO(text))


class TestTheService(unittest.TestCase):
    """Windy.__init__ against weewx.conf stanzas, old and new."""

    def start(self, config):
        engine = mock.MagicMock()
        with mock.patch.object(user.windy, 'WindyThread') as thread_class, \
                mock.patch('weewx.manager.get_manager_dict_from_config',
                           return_value={'manager': 'dict'}):
            Windy(engine, config)
        return thread_class, engine

    def test_a_v2_stanza_starts_the_uploader(self):
        thread_class, engine = self.start(stdrestful(
            ['enable = true', 'station_id = %s' % STATION_ID,
             'password = %s' % PASSWORD]))
        thread_class.assert_called_once()
        kwargs = thread_class.call_args.kwargs
        self.assertEqual(kwargs['station_id'], STATION_ID)
        self.assertEqual(kwargs['password'], PASSWORD)
        engine.bind.assert_called_once_with(weewx.NEW_ARCHIVE_RECORD, mock.ANY)

    def test_the_real_thread_accepts_what_the_service_passes(self):
        # With WindyThread itself (only start() stubbed), a stanza still
        # carrying the retired options must not reach its signature.
        config = stdrestful(['enable = true', 'api_key = OLDKEY', 'station = 1',
                             'station_id = %s' % STATION_ID,
                             'password = %s' % PASSWORD])
        thread, logged = self.start_real_thread(config)
        self.assertEqual(thread.station_id, STATION_ID)
        self.assertTrue(any('api_key and station are no longer used' in line
                            for line in logged))

    def start_real_thread(self, config):
        with mock.patch.object(WindyThread, 'start'), \
                mock.patch('weewx.manager.get_manager_dict_from_config',
                           return_value=None), \
                self.assertLogs('user.windy', 'INFO') as logged:
            service = Windy(mock.MagicMock(), config)
        return service.archive_thread, logged.output

    def test_a_server_url_left_on_the_old_api_falls_back_to_v2(self):
        thread, logged = self.start_real_thread(stdrestful(
            ['server_url = http://stations.windy.com/pws/update/',
             'station_id = %s' % STATION_ID, 'password = %s' % PASSWORD]))
        self.assertEqual(thread.server_url, Windy.DEFAULT_URL)
        self.assertTrue(any("server_url http://stations.windy.com/pws/update/ "
                            "is Windy's retired API" in line for line in logged))

    def test_any_other_server_url_is_kept(self):
        # Only a PATH of exactly /pws/update is the old API: not v2's own
        # path, and not a URL that merely mentions it elsewhere.
        for proxy in ('http://proxy.local:8080/pws/v2/observation/update',
                      'http://proxy.local:8080/relay?from=/pws/update'):
            thread, logged = self.start_real_thread(stdrestful(
                ['server_url = %s' % proxy,
                 'station_id = %s' % STATION_ID, 'password = %s' % PASSWORD]))
            self.assertEqual(thread.server_url, proxy)
            self.assertFalse(any('retired' in line for line in logged), proxy)

    def test_one_leftover_option_is_named_in_the_singular(self):
        with self.assertLogs('user.windy', 'INFO') as logged:
            self.start(stdrestful(['api_key = OLDKEY',
                                   'station_id = %s' % STATION_ID,
                                   'password = %s' % PASSWORD]))
        self.assertTrue(any('api_key is no longer used' in line
                            for line in logged.output))

    def test_a_stanza_with_only_the_old_api_key_says_what_to_do(self):
        with self.assertLogs('user.windy', 'ERROR') as logged:
            thread_class, _ = self.start(stdrestful(['enable = true',
                                                     'api_key = OLDKEY']))
        thread_class.assert_not_called()
        self.assertIn('station_id and password', logged.output[0])
        self.assertIn('https://stations.windy.com/stations', logged.output[0])

    def test_an_upgraded_stanza_still_on_placeholders_says_what_to_do(self):
        with self.assertLogs('user.windy', 'ERROR'):
            thread_class, _ = self.start(stdrestful(
                ['enable = true', 'api_key = OLDKEY',
                 'station_id = replace_me', 'password = replace_me']))
        thread_class.assert_not_called()

    def test_a_disabled_old_stanza_stays_quiet(self):
        with mock.patch.object(user.windy.log, 'error') as error:
            thread_class, _ = self.start(stdrestful(['enable = false',
                                                     'api_key = OLDKEY']))
        thread_class.assert_not_called()
        error.assert_not_called()


def load_installer():
    spec = importlib.util.spec_from_file_location(
        'windy_install', os.path.join(REPO, 'install.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def weewx_conf(upgrade_from=None):
    """WeeWX's own shipped weewx.conf, parsed from text as weectl does, with
    an old [[Windy]] stanza added when upgrade_from is given"""
    import weewx_data  # WeeWX 5 only
    with open(os.path.join(os.path.dirname(weewx_data.__file__),
                           'weewx.conf')) as f:
        text = f.read()
    if upgrade_from:
        text = text.replace('[StdRESTful]\n', '[StdRESTful]\n    [[Windy]]\n'
                            + ''.join('        %s\n' % line for line in upgrade_from), 1)
    config = configobj.ConfigObj(io.StringIO(text), encoding='utf-8',
                                 interpolation=False)
    config['WEEWX_ROOT'] = '/nonexistent'
    return config


def install_into(config):
    """Merge the installer's stanza the way weectl extension install does"""
    engine = weecfg.extension.ExtensionEngine('/nonexistent/weewx.conf', config)
    engine._inject_config(load_installer().loader().get('config'), 'windy')
    out = io.BytesIO()
    config.write(out)
    return out.getvalue().decode('utf-8').splitlines()


def windy_block(lines):
    """[[Windy]] with the comment block above it, down to the next section"""
    header = next(n for n, line in enumerate(lines) if line.strip() == '[[Windy]]')
    start = header
    while lines[start - 1].lstrip().startswith('#'):
        start -= 1
    end = header + 1
    while end < len(lines) and lines[end].strip() and not lines[end].lstrip().startswith('['):
        end += 1
    while lines[end - 1].lstrip().startswith('#'):  # a block above the NEXT key
        end -= 1
    return lines[start:end]


class TestTheInstaller(unittest.TestCase):

    def test_the_stanza_s_values(self):
        stanza = load_installer().loader().get('config')
        self.assertEqual(stanza.keys(), ['StdRESTful'])
        self.assertEqual(stanza['StdRESTful'].scalars, [])
        self.assertEqual(dict(stanza['StdRESTful']['Windy']), {
            'enable': 'true', 'station_id': 'replace_me',
            'password': 'replace_me'})

    def test_every_key_carries_a_comment_and_placeholders_say_so(self):
        windy = load_installer().loader().get('config')['StdRESTful']['Windy']
        self.assertTrue(windy.parent.comments['Windy'])
        for key in windy.scalars:
            self.assertTrue(windy.comments[key], key)
        for key in ('station_id', 'password'):
            self.assertTrue(windy.comments[key][0].lstrip('# ').startswith(
                'PLACEHOLDER --'), key)

    def test_a_fresh_install_gets_the_stanza_byte_for_byte(self):
        installer = load_installer()
        expected = installer.CONFIG.strip('\n').splitlines()[1:]  # less [StdRESTful]
        self.assertEqual(windy_block(install_into(weewx_conf())), expected)

    def test_an_upgrade_adds_the_new_options_and_keeps_the_old_values(self):
        block = windy_block(install_into(weewx_conf(
            upgrade_from=['enable = true', 'api_key = OLDKEY'])))
        installer_lines = load_installer().CONFIG.splitlines()
        added = installer_lines[installer_lines.index(
            '        station_id = replace_me') - 2:]
        self.assertEqual(block, ['    [[Windy]]', '        enable = true',
                                 '        api_key = OLDKEY'] + added)

    def test_an_upgrade_leaves_filled_in_credentials_alone(self):
        block = windy_block(install_into(weewx_conf(upgrade_from=[
            'enable = true', 'station_id = %s' % STATION_ID,
            'password = %s' % PASSWORD])))
        self.assertEqual([line.strip() for line in block], [
            '[[Windy]]', 'enable = true', 'station_id = %s' % STATION_ID,
            'password = %s' % PASSWORD])

    def test_the_service_reads_what_the_installer_writes(self):
        config = weewx_conf()
        install_into(config)
        with mock.patch.object(user.windy, 'WindyThread') as thread_class:
            Windy(mock.MagicMock(), config)
        thread_class.assert_not_called()           # placeholders: nothing sent
        windy = config['StdRESTful']['Windy']
        windy['station_id'], windy['password'] = STATION_ID, PASSWORD
        with mock.patch.object(user.windy, 'WindyThread') as thread_class, \
                mock.patch('weewx.manager.get_manager_dict_from_config',
                           return_value=None):
            Windy(mock.MagicMock(), config)
        thread_class.assert_called_once()


class TestTheCheck(unittest.TestCase):
    """bin/user/windy.py --check, which reads and never uploads."""

    ANSWER = (b'{"header":{"id":"YZjgOxm","name":"Palo Alto",'
              b'"last_observation_time":"2026-09-29T18:25:00Z"},"data":{"ts":[]}}')

    def test_it_asks_windy_which_station_the_password_belongs_to(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = self.ANSWER
        with mock.patch('urllib.request.urlopen', return_value=response) as urlopen, \
                mock.patch('getpass.getpass', return_value=PASSWORD), \
                mock.patch('builtins.print') as printed:
            self.assertEqual(user.windy.main(['--check']), 0)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_method(), 'GET')
        self.assertEqual(request.full_url,
                         'https://stations.windy.com/api/v2/observation?latestLimit=1')
        self.assertEqual(request.get_header('Authorization'), 'Bearer %s' % PASSWORD)
        self.assertIn(mock.call('station_id = YZjgOxm'), printed.call_args_list)

    def test_a_refused_password_is_reported(self):
        answer = http_error(400, '{"message":"Provided password is invalid"}')
        with mock.patch('urllib.request.urlopen', side_effect=answer), \
                mock.patch('getpass.getpass', return_value='wrong'), \
                mock.patch('builtins.print') as printed:
            self.assertEqual(user.windy.main(['--check']), 1)
        self.assertIn('Provided password is invalid', printed.call_args.args[0])

    def check_answering(self, answer):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = answer
        with mock.patch('urllib.request.urlopen', return_value=response), \
                mock.patch('getpass.getpass', return_value=PASSWORD), \
                mock.patch('builtins.print') as printed:
            rc = user.windy.main(['--check'])
        return rc, printed.call_args.args[0]

    def test_an_answer_naming_no_station_is_not_a_success(self):
        rc, said = self.check_answering(b'{"data":{"ts":[]}}')
        self.assertEqual(rc, 1)
        self.assertIn('Could not check the password', said)

    def test_an_answer_that_is_not_an_object_is_reported_not_a_traceback(self):
        for answer in (b'["YZjgOxm"]', b'<html>Bad Gateway</html>'):
            rc, said = self.check_answering(answer)
            self.assertEqual(rc, 1, answer)
            self.assertIn('Could not check the password', said)

    def test_no_network_is_reported_not_a_traceback(self):
        failure = urllib.error.URLError('[Errno -3] Temporary failure in name resolution')
        with mock.patch('urllib.request.urlopen', side_effect=failure), \
                mock.patch('getpass.getpass', return_value=PASSWORD), \
                mock.patch('builtins.print') as printed:
            self.assertEqual(user.windy.main(['--check']), 1)
        self.assertIn('Could not reach windy', printed.call_args.args[0])

    def test_with_no_arguments_it_prints_a_url_and_sends_nothing(self):
        with mock.patch('urllib.request.urlopen') as urlopen, \
                mock.patch('builtins.print') as printed:
            self.assertEqual(user.windy.main([]), 0)
        urlopen.assert_not_called()
        self.assertTrue(printed.call_args.args[0].startswith(Windy.DEFAULT_URL))
