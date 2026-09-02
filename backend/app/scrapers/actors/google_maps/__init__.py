"""Google Maps actor package (brief §28, §47).

Provider-based business listing actor. This package contains NO scraping of
Google and NO evasion of platform controls — see provider.py.
"""

from app.scrapers.actors.google_maps.actor import GoogleMapsActor

__all__ = ["GoogleMapsActor"]
