"""Tunable parameters for the data pipeline, in one place.

Changing a number here changes it everywhere: the backend computation and the
properties written into the GeoJSON that the frontend reads back.
"""
from datetime import datetime


def clamp_future_date(date: str | None, today: datetime | None = None) -> tuple[str | None, bool]:
    """Pull a future-dated sighting back to today. Returns (date, was_clamped).

    A date later than today is a typo in the published data, but the location
    it carries is presumably real. Dropping the record would erase a genuine
    risk zone from the map, so the point is kept and only its date is adjusted
    — erring toward showing risk rather than hiding it.

    Clamping rather than leaving it alone matters because a far-future typo
    (a mistyped year, say) would otherwise sit inside the "recent" window for
    years, never ageing out. Every clamp is still reported, never silent.
    """
    if not date:
        return date, False
    today = today or datetime.now()
    try:
        parsed = datetime.strptime(date, "%Y/%m/%d")
    except ValueError:
        return date, False
    if parsed <= today:
        return date, False
    return f"{today.year}/{today.month}/{today.day}", True

# How long a sighting counts as "current" for risk purposes. Points inside this
# window generate the 2km risk buffers subtracted from the dark-sky area.
# The boundary is inclusive: a sighting exactly this many days old still counts
# as current, which is why the display tags below all read "より前" (strictly
# older than) rather than "以上" — otherwise a 90-day-old sighting would fall
# into both the risk calculation and the "excluded" tag at once.
RISK_WINDOW_DAYS = 90

# How much history to keep at all. Sightings older than RISK_WINDOW_DAYS are
# still shown on the map (greyed out, tagged with their age) because "no
# sighting recently" is not the same as "bears have never been here", but
# anything past this limit is dropped so the published files stay small.
RETENTION_YEARS = 3

# Age tags for sightings that no longer feed the risk calculation. Each entry is
# (days older than, label). A point is tagged with the oldest threshold it
# exceeds. Note that with RETENTION_YEARS = 3 the "3年より前" tag is never
# reached, since those records are discarded; raise RETENTION_YEARS to 4 to
# make that tag appear.
AGE_TAGS = [
    (90, "90日より前"),
    (365, "1年より前"),
    (730, "2年より前"),
    (1095, "3年より前"),
]

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
