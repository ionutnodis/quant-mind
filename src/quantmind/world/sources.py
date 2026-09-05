from __future__ import annotations

from dataclasses import dataclass

from .models import WorldConfig


@dataclass(frozen=True, slots=True)
class Source:
    id: str
    name: str
    category: str
    homepage: str
    url: str
    kind: str
    topics: tuple[str, ...]
    regions: tuple[str, ...]
    access: str
    description: str
    interval_seconds: int = 900


SOURCES: tuple[Source, ...] = (
    Source("fed", "Federal Reserve", "central-bank", "https://www.federalreserve.gov/", "https://www.federalreserve.gov/feeds/press_all.xml", "rss", ("monetary policy", "banking"), ("US",), "free", "Federal Reserve press releases"),
    Source("ecb", "European Central Bank", "central-bank", "https://www.ecb.europa.eu/", "https://www.ecb.europa.eu/rss/press.html", "rss", ("monetary policy", "banking"), ("EU",), "free", "ECB press releases and speeches"),
    Source("boe", "Bank of England", "central-bank", "https://www.bankofengland.co.uk/", "https://www.bankofengland.co.uk/rss/news", "rss", ("monetary policy", "banking"), ("GB",), "free", "Bank of England news"),
    Source("bls", "US Bureau of Labor Statistics", "economy", "https://www.bls.gov/", "https://www.bls.gov/feed/bls_latest.rss", "rss", ("inflation", "employment"), ("US",), "free", "Latest US labour statistics", 1800),
    Source("bea", "US Bureau of Economic Analysis", "economy", "https://www.bea.gov/", "https://apps.bea.gov/rss/rss.xml", "rss", ("growth", "trade"), ("US",), "free", "US economic releases", 1800),
    Source("bis", "Bank for International Settlements", "central-bank", "https://www.bis.org/", "https://www.bis.org/doclist/all_pressrels.rss", "rss", ("banking", "financial stability"), ("GLOBAL",), "free", "BIS press releases", 1800),
    Source("eia", "US Energy Information Administration", "energy", "https://www.eia.gov/", "https://www.eia.gov/rss/todayinenergy.xml", "rss", ("energy", "commodities"), ("US", "GLOBAL"), "free", "Today in Energy", 1800),
    Source("un", "UN News", "geopolitics", "https://news.un.org/", "https://news.un.org/feed/subscribe/en/news/all/rss.xml", "rss", ("geopolitics", "humanitarian"), ("GLOBAL",), "free", "United Nations global news"),
    Source("gdacs", "Global Disaster Alert and Coordination System", "disasters", "https://www.gdacs.org/", "https://www.gdacs.org/xml/rss.xml", "rss", ("disasters",), ("GLOBAL",), "free", "Official global disaster alerts", 300),
    Source("sec", "US Securities and Exchange Commission", "regulation", "https://www.sec.gov/", "https://www.sec.gov/news/pressreleases.rss", "rss", ("regulation", "markets"), ("US",), "contact", "SEC press releases; requires an identifying user agent"),
    Source("usgs", "US Geological Survey", "disasters", "https://earthquake.usgs.gov/", "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson", "usgs_geojson", ("earthquakes", "disasters"), ("GLOBAL",), "free", "Magnitude 4.5+ earthquakes recorded in the past day", 300),
    Source("gdelt", "GDELT Project", "news-index", "https://www.gdeltproject.org/", "https://api.gdeltproject.org/api/v2/doc/doc?query=(economy%20OR%20markets%20OR%20geopolitics)&mode=artlist&maxrecords=100&format=json&sort=datedesc", "gdelt", ("markets", "geopolitics"), ("GLOBAL",), "free", "Machine-indexed global news discovery", 900),
    Source("imf", "International Monetary Fund SDMX", "economy", "https://data.imf.org/", "https://sdmxcentral.imf.org/rss.xml", "rss", ("economic data",), ("GLOBAL",), "free", "IMF statistical data updates", 3600),
    Source("ukgov", "UK Government", "geopolitics", "https://www.gov.uk/search/news-and-communications", "https://www.gov.uk/search/news-and-communications.atom", "atom", ("government", "economy"), ("GB",), "free", "UK government news and communications"),
    Source("who", "World Health Organization", "health", "https://www.who.int/", "https://www.who.int/rss-feeds/news-english.xml", "rss", ("health",), ("GLOBAL",), "free", "WHO news releases"),
    Source("x", "X API", "social", "https://developer.x.com/", "https://api.x.com/2/tweets/search/recent", "x", ("markets",), ("GLOBAL",), "paid opt-in", "Optional recent-post search through the official paid X API", 900),
    Source("reddit", "Reddit API", "social", "https://www.reddit.com/dev/api/", "https://oauth.reddit.com/r/investing+stocks+Economics/new", "reddit", ("markets",), ("GLOBAL",), "approved OAuth opt-in", "Optional posts through Reddit's approved OAuth API", 900),
)


def source_enabled(source: Source, config: WorldConfig) -> bool:
    if source.id == "sec":
        return bool(config.sec_user_agent.strip())
    if source.kind == "x":
        return config.x_enabled and bool(config.x_bearer_token.get_secret_value()) and bool(config.x_query.strip())
    if source.kind == "reddit":
        return config.reddit_enabled and all((config.reddit_client_id.strip(), config.reddit_client_secret.get_secret_value(), config.reddit_refresh_token.get_secret_value(), config.reddit_user_agent.strip()))
    return True


def source_setup_note(source: Source, config: WorldConfig) -> str | None:
    if source.id == "sec" and not source_enabled(source, config):
        return "Set an identifying SEC user agent with contact information."
    if source.kind == "x" and not source_enabled(source, config):
        return "X recent search is a paid API: explicitly enable it and set a bearer token and query."
    if source.kind == "reddit" and not source_enabled(source, config):
        return "Reddit requires explicit enablement and approved OAuth credentials."
    return None
