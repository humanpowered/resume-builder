"""
Reading a job posting from a link the person pastes, so a resume can be
written for it without copying the text by hand.

The approach follows the pipeline's link reader (pipeline/src/score_urls.py)
but lives here, so the builder never imports or runs the pipeline: Greenhouse's
public feed first, then the posting data employers build into their pages for
search engines (schema.org JobPosting), then the page's plain text.

A link is fetched by our server, so only public web addresses are read: http
or https, and never a host that resolves to a private, loopback, link-local or
reserved address, checked again at every redirect.
"""
import ipaddress
import json
import re
import socket
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlparse

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")
TIMEOUT = 15
MAX_BYTES = 3_000_000
MAX_REDIRECTS = 5
MIN_TEXT = 300          # less than this is a login wall or an empty shell

LINK = re.compile(r"^https?://\S+$", re.I)


class Unreadable(ValueError):
    """The link couldn't be read; the person is asked to paste the text."""


def is_link(text: str) -> bool:
    return bool(LINK.match((text or "").strip()))


def host(url: str) -> str:
    return (urlparse(url).hostname or "").removeprefix("www.")


def _ask_for_text(url: str) -> Unreadable:
    return Unreadable(f"We couldn't read the posting at {host(url) or 'that link'}. Some sites, "
                      "like LinkedIn and Indeed, don't let other programs read their pages. "
                      "Copy the posting's text and paste it here instead.")


def _public(url: str) -> None:
    """Refuse anything but a public web address."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise Unreadable("That doesn't look like a web link. Paste the posting's text instead.")
    try:
        infos = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80))
    except OSError:
        raise _ask_for_text(url)
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            raise Unreadable("That link points to a private address, so we can't read it. "
                             "Paste the posting's text instead.")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def _get(url: str) -> tuple:
    """The page's text and final address, following redirects one at a time
    so each new host is checked before it is contacted."""
    opener = urllib.request.build_opener(_NoRedirect)
    for _ in range(MAX_REDIRECTS + 1):
        _public(url)
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,application/json"})
        try:
            resp = opener.open(req, timeout=TIMEOUT)
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308) and exc.headers.get("Location"):
                url = urljoin(url, exc.headers["Location"])
                continue
            raise _ask_for_text(url)
        except (urllib.error.URLError, OSError, ValueError):
            raise _ask_for_text(url)
        with resp:
            body = resp.read(MAX_BYTES)
            charset = resp.headers.get_content_charset() or "utf-8"
        return body.decode(charset, errors="replace"), url
    raise _ask_for_text(url)


# ---------------------------------------------------------------- pulling the posting out

def strip_tags(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|nav|header|footer)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?is)<br\s*/?>|</p>|</div>|</li>|</h\d>", "\n", html)
    text = re.sub(r"<[^>]+>", " ", html)
    text = (text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
                .replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " "))
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n\s*(\n\s*)+", "\n\n", text).strip()


def from_jsonld(html: str) -> dict | None:
    """Employers publish schema.org JobPosting for search engines; when it's
    there it is far cleaner than the rendered page."""
    for m in re.finditer(r'(?is)<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html):
        try:
            data = json.loads(m.group(1).strip())
        except ValueError:
            continue
        nodes = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for node in nodes:
            if not isinstance(node, dict) or node.get("@type") != "JobPosting":
                continue
            org = node.get("hiringOrganization") or {}
            return {"title": str(node.get("title") or "").strip(),
                    "company": str(org.get("name") if isinstance(org, dict) else org or "").strip(),
                    "text": strip_tags(str(node.get("description") or ""))}
    return None


def from_greenhouse(url: str) -> dict | None:
    """Greenhouse pages are drawn in the browser, so a plain read gets an empty
    shell; the same posting is on their public feed."""
    m = re.search(r"greenhouse\.io/(?:embed/job_app\?for=)?([^/?]+)/jobs/(\d+)", url)
    if not m:
        return None
    try:
        body, _ = _get(f"https://boards-api.greenhouse.io/v1/boards/{m.group(1)}/jobs/{m.group(2)}")
        j = json.loads(body)
    except (Unreadable, ValueError):
        return None
    import html as _html
    return {"title": j.get("title", ""), "company": m.group(1),
            "text": strip_tags(_html.unescape(j.get("content", "")))}


def read(url: str) -> dict:
    """The posting at a link: {"title", "company", "text", "url"}. Raises
    Unreadable, with words for the person, when there isn't enough to use."""
    url = url.strip()
    got = from_greenhouse(url)
    if not got or len(got["text"]) < MIN_TEXT:
        html, url = _get(url)
        got = from_jsonld(html)
        if not got or len(got["text"]) < MIN_TEXT:
            m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
            title = strip_tags(m.group(1)) if m else ""
            got = {"title": (got or {}).get("title") or title.split("|")[0].split(" at ")[0].strip(),
                   "company": (got or {}).get("company", ""), "text": strip_tags(html)}
    if len(got["text"]) < MIN_TEXT:
        raise _ask_for_text(url)
    return {**got, "url": url}


def as_posting(got: dict) -> str:
    """The posting as the model reads it."""
    head = "\n".join(x for x in (got.get("title"), got.get("company")) if x)
    return f"{head}\n\n{got['text']}".strip()
