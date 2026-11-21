from .base import Source
from .rtlsdr_source import SourceRtlSdr
from .simulation import SourceSimulation

SOURCES: dict[str, type[Source]] = {
    "rtlsdr": SourceRtlSdr,
    "simulation": SourceSimulation,
}

__all__ = ["Source", "SOURCES", "SourceRtlSdr", "SourceSimulation"]
