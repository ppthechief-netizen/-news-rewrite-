import hashlib
import urllib.parse


def url_key(u: str) -> str:
    u = urllib.parse.urlsplit(u)._replace(fragment="", query="").geturl().rstrip("/")
    return hashlib.sha1(u.encode("utf-8")).hexdigest()[:12]
