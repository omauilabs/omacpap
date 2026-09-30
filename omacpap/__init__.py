"""OmaCPAP — local-first CPAP data analyzer for Omarchy.

Pulls nightly summaries from ResMed myAir (unofficial API) and full-detail
data from an AirSense 10/11 SD card into one private SQLite database, then
serves a themed dashboard as an Omarchy web app.
"""

__version__ = "0.1.0"
APP_NAME = "OmaCPAP"
APP_ID = "omacpap"
