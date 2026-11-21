from .base import Source
from .rtlsdr_source import SourceRtlSdr
from .simulation import SourceSimulation

# Sources proposées à l'utilisateur (menu Source).
SOURCES: dict[str, type[Source]] = {
    "rtlsdr": SourceRtlSdr,
}

# Source simulée : réservée aux tests automatiques (tests/), jamais proposée dans l'application.
SOURCES_ESSAI: dict[str, type[Source]] = {
    "simulation": SourceSimulation,
}
TOUTES_SOURCES = {**SOURCES, **SOURCES_ESSAI}

__all__ = ["Source", "SOURCES", "SOURCES_ESSAI", "TOUTES_SOURCES", "SourceRtlSdr", "SourceSimulation"]
