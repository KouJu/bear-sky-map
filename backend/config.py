"""Tunable parameters for the data pipeline, in one place.

Changing a number here changes it everywhere: the backend computation and the
properties written into the GeoJSON that the frontend reads back.
"""

# How long a sighting counts as "current" for risk purposes. Points inside this
# window generate the 2km risk buffers subtracted from the dark-sky area.
RISK_WINDOW_DAYS = 365

# How much history to keep at all. Sightings between RISK_WINDOW_DAYS and this
# limit are still shown on the map (greyed out, labelled "1年以上前") because
# "no sighting this year" is not the same as "bears have never been here", but
# anything older is dropped so the published files stay small.
RETENTION_YEARS = 3

# Radius drawn around each recent sighting, in metres.
BUFFER_METERS = 2000.0

# Sky darkness threshold: SQM (mag/arcsec^2) at or above this counts as dark.
SQM_THRESHOLD = 20.0

# Light Pollution Atlas edition used for the SQM grid.
LP_YEAR = 2025

# Which prefecture each data source lives in. Dark-sky polygons are clipped to
# these boundaries, which keeps the result on land and stops the analysis
# bounding box showing up as a rectangle.
REGION_PREFECTURE = {
    "tokyo": "東京都",
    "miyagi": "宮城県",
}
