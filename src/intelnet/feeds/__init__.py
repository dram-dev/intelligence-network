"""Reference feeds — official sources that join the network as trusted sensors.

Every feed here produces the same `Signal` shape a person does; the only
difference is a fixed high trust and the `reference` quality. Import the
modules so they self-register in `FEEDS`.
"""
from intelnet.feeds.base import FEEDS, FeedResult, ReferenceFeed  # noqa: F401
from intelnet.feeds import iem_asos, iem_lsr, nws_alerts  # noqa: F401  (registration)

__all__ = ["FEEDS", "FeedResult", "ReferenceFeed"]
