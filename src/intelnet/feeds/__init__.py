"""Reference feeds — official sources that join the network as trusted sensors.

Every feed here produces the same `Signal` shape a person does; the only
difference is a fixed high trust and the `reference` quality. Import the
modules so they self-register in `FEEDS`.
"""
from intelnet.feeds.base import FEEDS, FeedResult, ReferenceFeed  # noqa: F401
from intelnet.feeds import (  # noqa: F401  (registration)
    iem_asos,
    iem_lsr,
    nrcs_scan,
    nws_alerts,
    usdm_drought,
    usgs_water,
)

__all__ = ["FEEDS", "FeedResult", "ReferenceFeed"]
