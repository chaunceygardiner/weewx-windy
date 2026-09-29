# installer for windy
# Copyright 2019-2026 Matthew Wall
# Copyright 2026 John A Kline (Windy Stations API v2)
# Distributed under the terms of the GNU Public License (GPLv3)

from io import StringIO

import configobj

from weecfg.extension import ExtensionInstaller

# Written as weewx.conf text rather than a dict so that the stanza weectl
# merges into weewx.conf arrives with its comments: ConfigObj keeps them, a
# dict has nowhere to put them.  Nothing goes above [StdRESTful], and no
# comment block is attached to it: every weewx.conf already has that
# section, and the merge drops the comments of a section that exists.
CONFIG = """
[StdRESTful]
    # Upload each archive record to windy.com.  The station ID and password
    # below are on the station's page under My Stations at
    # https://stations.windy.com/stations (register the station there
    # first).  Nothing is uploaded while either still reads replace_me.
    [[Windy]]
        # Set to false to stop uploading.
        enable = true
        # PLACEHOLDER -- replace with the station ID from the station's
        # page, a short code such as YZjgOxm.
        station_id = replace_me
        # PLACEHOLDER -- replace with the station password from the same
        # page.  Not the account's API key, which cannot upload.
        password = replace_me
"""


def loader():
    return WindyInstaller()


class WindyInstaller(ExtensionInstaller):
    def __init__(self):
        super(WindyInstaller, self).__init__(
            version="1.0",
            name='windy',
            description='Upload weather data to Windy.',
            author="Matthew Wall",
            author_email="mwall@users.sourceforge.net",
            restful_services='user.windy.Windy',
            config=configobj.ConfigObj(StringIO(CONFIG)),
            files=[('bin/user', ['bin/user/windy.py'])]
            )
