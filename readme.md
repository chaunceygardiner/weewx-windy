# weewx-windy

A WeeWX extension that uploads your station's observations to
[windy.com](https://www.windy.com) after every archive interval, through the
[Windy Stations API v2](https://stations.windy.com/api-reference).

**Upgrading from 0.8 or earlier?  You must act before 31 December 2026.**
Windy shuts down its original API, the one that took an `api_key`, at the
end of 2026.  This release uploads through API v2 instead, which identifies
a station by its own station ID and password.  Until you enter them, nothing
is uploaded.  See [Upgrading](#upgrading-from-08-or-earlier).

## Requirements

- WeeWX 4.1 or later, on Python 3.
- A station registered at [stations.windy.com](https://stations.windy.com/stations).

## Installation

1. Register your station at
   [stations.windy.com](https://stations.windy.com/stations) if you have not
   already.  Its page there shows the two things the extension needs: the
   station ID (a short code such as `YZjgOxm`) and the station password.

1. Download the
   [latest release from GitHub](https://github.com/chaunceygardiner/weewx-windy/releases/latest/download/weewx-windy.zip).

1. Install the extension.  On WeeWX 5:

   ```
   weectl extension install weewx-windy.zip
   ```

   On a pip install, activate the virtual environment first
   (`source ~/weewx-venv/bin/activate`).  On WeeWX 4:

   ```
   sudo wee_extension --install weewx-windy.zip
   ```

1. Edit `weewx.conf`.  The installer has added this, with comments:

   ```
   [StdRESTful]
       [[Windy]]
           enable = true
           station_id = replace_me
           password = replace_me
   ```

   Replace the two `replace_me` values with your station ID and password.
   The password is the station's, not your account's API key: the API key is
   for managing stations and cannot upload.

1. Restart WeeWX.

After the next archive interval the log shows a line like

```
INFO weewx.restx: Windy: Published record 2026-09-29 11:30:00 PDT (1790706600)
```

and the observation appears on your station's page at stations.windy.com.

## Upgrading from 0.8 or earlier

Run the same install command, then restart WeeWX.  The install adds
`station_id = replace_me` and `password = replace_me` to your existing
`[[Windy]]` section and changes nothing else in it.  Until you replace them,
the log says, at every startup:

```
ERROR user.windy: Nothing will be uploaded.  api_key is the credential for Windy's retired API; ...
```

Fill in the two values from your station's page at
[stations.windy.com](https://stations.windy.com/stations) and restart
WeeWX.  `api_key`, and `station` if you had set it, are no longer used.  You
may delete them, and until you do the log says so once at startup.  The
same goes for a `server_url` you had pointed at the original API
(`.../pws/update`): it is ignored, and uploads go to API v2.

## Checking a password

To find out which station a password belongs to, without uploading
anything, run the extension's check.  From the directory that holds
`user/windy.py` (on WeeWX 5, `~/weewx-data/bin` for a pip install and
`/etc/weewx/bin` for a package install), with the Python that runs WeeWX
(on a pip install, activate its virtual environment first):

```
PYTHONPATH=. python3 user/windy.py --check
```

(A package install keeps WeeWX itself in `/usr/share/weewx`, so there it
is `PYTHONPATH=.:/usr/share/weewx`.)  It asks for the password and prints
the station's ID, name and last observation time, or Windy's reason for
refusing it.

## Configuration

Only `station_id` and `password` are required.  The rest are there if you
need them:

| Option | Default | Meaning |
|---|---|---|
| `enable` | `true` | Set to `false` to stop uploading. |
| `station_id` | | The station ID from stations.windy.com. |
| `password` | | The station password from stations.windy.com.  Sent in a request header, never in the URL, so it does not appear in logs. |
| `post_interval` | `300` | Seconds between uploads.  Windy accepts one per station every 5 minutes. |
| `stale` | `1800` | A record older than this many seconds is not sent.  Windy refuses anything over 2 hours old. |
| `max_backlog` | `0` | Records waiting to be sent beyond the newest.  Windy keeps only the latest observation, so the default sends only the newest. |
| `timeout` | `60` | Seconds to wait for Windy to answer. |
| `max_tries` | `3` | Attempts per record when the network or Windy's server fails. |
| `retry_wait` | `5` | Seconds between those attempts. |
| `retry_login` | `3600` | Seconds to wait after Windy refuses the password before trying again. |
| `log_success`, `log_failure` | `true` | Log each upload, and each failure. |
| `skip_upload` | `false` | Do everything but send (for testing). |

## What is sent

| WeeWX | Windy | Units |
|---|---|---|
| `outTemp` | `temp` | °C |
| `dewpoint` | `dewpoint` | °C |
| `outHumidity` | `humidity` | % |
| `windSpeed` | `wind` | m/s |
| `windGust` | `gust` | m/s |
| `windDir` | `winddir` | degrees, whole |
| `barometer` | `pressure` | Pa |
| `hourRain` | `precip` | mm over the past hour |
| `UV` | `uv` | index |
| `radiation` | `solarradiation` | W/m² |

A value your station does not report is left out.  So is a value outside the
range Windy accepts (a humidity sensor reading 101%, say): Windy refuses the
whole observation when one value is out of range, so leaving out the one
value lets the rest through.

## When something goes wrong

| Log line | What it means |
|---|---|
| `ERROR user.windy: Nothing will be uploaded.  api_key is the credential ...` | The station is still configured for the retired API.  See [Upgrading](#upgrading-from-08-or-earlier). |
| `ERROR user.windy: Station ...: Provided password is invalid (HTTP 400)` | Windy refused the password.  `--check` shows which station a password belongs to.  The extension tries again after `retry_login` seconds. |
| `ERROR weewx.restx: Windy: Failed to publish record ...: Windy rejected the observation: ...` | Windy refused the observation itself, and says why. |
| `INFO weewx.restx: Windy: Skipped record ...: Windy allows one upload every 5 minutes ...` | An upload came too soon after the last one, usually just after WeeWX catches up at startup.  Nothing to do. |

## License & Copyright

Copyright (c) 2019-2026 Matthew Wall; Windy Stations API v2 support
copyright (c) 2026 John A Kline.

Distributed under the terms of the GNU Public License (GPLv3).  See the
file `license` for your full rights.
