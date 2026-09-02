"""Website actor package (brief §26).

An independent actor: crawl ONE public website and extract publicly
available contact information. See README.md for details.
"""

from app.scrapers.actors.website.actor import WebsiteActor

__all__ = ["WebsiteActor"]
