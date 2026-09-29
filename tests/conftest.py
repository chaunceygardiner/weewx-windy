import os
import sys

# The extension lives in bin/user, as it does in a WeeWX install.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'bin'))
