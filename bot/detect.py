import re
from urllib.parse import urlparse

TERA_HOSTS = (
    "terabox.com",
    "terabox.app",
    "1024terabox.com",
    "1024tera.com",
    "freeterabox.com",
    "nephobox.com",
    "4funbox.com",
    "4funbox.co",
    "mirrobox.com",
    "momerybox.com",
    "teraboxapp.com",
    "terasharelink.com",
    "terafileshare.com",
    "teraboxlink.com",
    "teraboxshare.com",
    "terasharefile.com",
    "teraboxurl.com",
    "gibibox.com",
    "goaibox.com",
    "dubox.com",
)

IG_HOSTS = ("instagram.com", "instagr.am", "ig.me", "ddinstagram.com")
URL_RE = re.compile(r"https?://[^\s<>]+", re.I)
IG_ITEM = re.compile(r"/(?:share/)?(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)", re.I)


def _host(url: str) -> str:
    host = urlparse(url).netloc.lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host


def _matches(host: str, names: tuple[str, ...]) -> bool:
    return any(host == name or host.endswith("." + name) for name in names)


def find_urls(text: str) -> list[str]:
    found = []
    for raw in URL_RE.findall(text or ""):
        url = raw.rstrip(").,]}>\"'")
        if url not in found:
            found.append(url)
    return found


def classify(url: str) -> str | None:
    host = _host(url)
    if _matches(host, IG_HOSTS):
        return "instagram"
    if _matches(host, TERA_HOSTS):
        return "terabox"
    return None


def instagram_shortcode(url: str) -> tuple[str, str] | None:
    path = urlparse(url).path
    match = IG_ITEM.search(path)
    if not match:
        return None
    kind = "reel" if "/reel" in path.lower() else "p"
    if "/tv/" in path.lower():
        kind = "tv"
    return match.group(1), kind
