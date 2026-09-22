"""A very small S3 client: SigV4 signing plus put/get/delete/list (ticket 74).

Written rather than pulled in. ``boto3`` and ``botocore`` would add tens of MB of
service-description data to a PyInstaller build already at 125 MB, and this needs
four request shapes against one endpoint. Signing is the only hard part and it is
about sixty lines of :mod:`hmac` and :mod:`hashlib`.

Hand-rolled signing is normally a bad trade, so two things make it safe here.
:func:`authorization_header` signs whatever headers it is handed instead of a
fixed set, which means the tests can drive it with **Amazon's own published test
vector** rather than comparing our arithmetic to our arithmetic. And the network
call is injectable — the suite runs the whole client with a fake transport and
never opens a socket.

Cloudflare R2 speaks S3 with two quirks worth knowing: the region is always
``auto``, and the endpoint is per-account
(``<account_id>.r2.cloudflarestorage.com``). Path-style addressing is used
throughout; R2 accepts it and it keeps the bucket out of the hostname.

Transport is ``httpx``, never :mod:`urllib.request` — measured 2026-09-10,
``urllib`` died partway through a 10 MB upload with ``SSLEOFError`` while
``httpx`` moved 77 MB without complaint.

Qt-free.
"""

from __future__ import annotations

import datetime as dt
import functools
import hashlib
import hmac
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.parse import quote, urlparse

log = logging.getLogger(__name__)

ALGORITHM = "AWS4-HMAC-SHA256"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()

# (status, body). Anything that speaks this can stand in for the network.
Transport = Callable[[str, str, Mapping[str, str], bytes], tuple[int, bytes]]


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def _canonical_uri(path: str) -> str:
    """URI-encode the path, leaving ``/`` alone (S3 does not double-encode)."""
    return quote(path or "/", safe="/~")


def _canonical_query(query: str) -> str:
    """Sort the query string the way SigV4 wants it, encoding nothing.

    The caller percent-encodes parameter values when it builds the URL, and SigV4
    signs them **exactly as they travel**. Re-encoding here would double-encode
    an already-escaped value — ``prefix=abc%2Fx`` becoming ``abc%252Fx`` — and
    produce a signature the server cannot reproduce. Caught by cross-checking
    against botocore; the live round-trip missed it because ``list-type=2`` has
    nothing in it to escape.

    Empty-valued parameters keep their ``=``, so ``?delete`` signs as ``delete=``.
    """
    if not query:
        return ""
    pairs = [tuple(part.partition("=")[::2]) for part in query.split("&")]
    return "&".join(f"{k}={v}" for k, v in sorted(pairs))


def signing_key(secret: str, datestamp: str, region: str, service: str) -> bytes:
    """The four-step derived key. Separate so a test can pin it on its own."""
    key = _hmac(f"AWS4{secret}".encode(), datestamp)
    key = _hmac(key, region)
    key = _hmac(key, service)
    return _hmac(key, "aws4_request")


def canonical_request(
    method: str,
    url: str,
    headers: Mapping[str, str],
    payload_sha256: str,
) -> tuple[str, str]:
    """Build the canonical request and its ``SignedHeaders`` list.

    ``host`` is taken from ``url`` and always signed; every header passed in is
    signed too, lowercased and sorted, which is what lets the AWS vector (which
    signs ``range``) drive this function unchanged.
    """
    parts = urlparse(url)
    signed = {k.lower().strip(): " ".join(str(v).split()) for k, v in headers.items()}
    signed.setdefault("host", parts.netloc)
    names = sorted(signed)
    canonical_headers = "".join(f"{n}:{signed[n]}\n" for n in names)
    signed_headers = ";".join(names)
    request = (
        f"{method}\n"
        f"{_canonical_uri(parts.path)}\n"
        f"{_canonical_query(parts.query)}\n"
        f"{canonical_headers}\n"
        f"{signed_headers}\n"
        f"{payload_sha256}"
    )
    return request, signed_headers


def authorization_header(
    method: str,
    url: str,
    headers: Mapping[str, str],
    payload_sha256: str,
    *,
    access_key: str,
    secret_key: str,
    region: str,
    service: str = "s3",
    amzdate: str,
) -> str:
    """Return the full ``Authorization`` value for one request.

    ``amzdate`` is ``YYYYMMDDTHHMMSSZ``; the credential scope's datestamp is its
    first eight characters, so the two can never disagree.
    """
    datestamp = amzdate[:8]
    request, signed_headers = canonical_request(method, url, headers, payload_sha256)
    scope = f"{datestamp}/{region}/{service}/aws4_request"
    string_to_sign = (
        f"{ALGORITHM}\n{amzdate}\n{scope}\n"
        f"{hashlib.sha256(request.encode()).hexdigest()}"
    )
    signature = hmac.new(
        signing_key(secret_key, datestamp, region, service),
        string_to_sign.encode(),
        hashlib.sha256,
    ).hexdigest()
    return (
        f"{ALGORITHM} Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )


DEFAULT_TIMEOUT = 300.0  # a 15 MB art bundle on a slow uplink; uploads need room


def _send(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[int, bytes]:  # pragma: no cover - real network
    import httpx

    resp = httpx.request(
        method,
        url,
        content=body or None,
        headers=dict(headers),
        timeout=timeout,
    )
    return resp.status_code, resp.content


class S3Error(RuntimeError):
    """A request reached the store and it said no."""

    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status}: {body[:200].decode(errors='replace')}")


@dataclass(frozen=True)
class Credentials:
    account_id: str
    bucket: str
    access_key_id: str
    secret_access_key: str
    region: str = "auto"  # R2 signs as 'auto' regardless of where the data lives
    endpoint: str = ""  # derived from account_id when blank

    def base_url(self) -> str:
        endpoint = (
            self.endpoint or f"https://{self.account_id}.r2.cloudflarestorage.com"
        )
        return endpoint.rstrip("/")


class ObjectStore:
    """put/get/delete/list against one bucket.

    ``transport`` defaults to the real ``httpx`` call; tests pass a fake. Retries
    cover transport errors and 5xx only — a 403 will not become a 200 by asking
    again, and retrying it just delays a clear message.
    """

    def __init__(
        self,
        creds: Credentials,
        transport: Transport | None = None,
        *,
        retries: int = 3,
        backoff: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        """``timeout`` is per request, in seconds.

        The default suits a background upload. Anything a person is waiting on —
        the connect-and-verify check in Operations — passes something short, so
        a bad account id or a stalled DNS lookup fails in seconds, not minutes.
        """
        self._creds = creds
        # Bind the timeout here so injected transports keep the plain 4-argument
        # signature and never need to know about it.
        self._send = transport or functools.partial(_send, timeout=timeout)
        self._retries = max(1, retries)
        self._backoff = backoff
        self._sleep = sleep

    def _url(self, key: str = "", query: str = "") -> str:
        path = f"/{self._creds.bucket}" + (f"/{key}" if key else "")
        return self._creds.base_url() + path + (f"?{query}" if query else "")

    def _request(
        self,
        method: str,
        key: str = "",
        body: bytes = b"",
        query: str = "",
        extra: Mapping[str, str] | None = None,
    ) -> bytes:
        url = self._url(key, query)
        payload_sha = hashlib.sha256(body).hexdigest() if body else EMPTY_SHA256
        amzdate = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        headers = {
            "x-amz-date": amzdate,
            "x-amz-content-sha256": payload_sha,
            **(extra or {}),
        }
        headers["Authorization"] = authorization_header(
            method,
            url,
            headers,
            payload_sha,
            access_key=self._creds.access_key_id,
            secret_key=self._creds.secret_access_key,
            region=self._creds.region,
            amzdate=amzdate,
        )

        last: Exception | None = None
        for attempt in range(self._retries):
            try:
                status, content = self._send(method, url, headers, body)
                if status < 300:
                    return content
                if status < 500:
                    raise S3Error(status, content)
                last = S3Error(status, content)
            except S3Error:
                raise
            except Exception as exc:  # transport-level: DNS, TLS, reset
                last = exc
            if attempt < self._retries - 1:
                # The endpoint is briefly unreachable right after R2 is first
                # enabled on an account (observed 2026-09-10: TLS handshakes
                # refused outright for minutes). Backing off turns that into a
                # slow first publish instead of a scary error.
                delay = self._backoff * (2**attempt)
                log.warning(
                    "%s %s failed (%s); retrying in %.1fs", method, key, last, delay
                )
                self._sleep(delay)
        raise last if last else S3Error(0, b"no attempt was made")

    def put(
        self, key: str, body: bytes, content_type: str = "application/octet-stream"
    ) -> None:
        self._request("PUT", key, body, extra={"Content-Type": content_type})

    def get(self, key: str) -> bytes:
        return self._request("GET", key)

    def delete(self, key: str) -> None:
        self._request("DELETE", key)

    def list(self, prefix: str = "") -> list[str]:
        """Every key under ``prefix``, following continuation tokens.

        Parsed with the XML parser rather than a regex: a key may legally
        contain characters that would make a hand-rolled scan wrong, and
        ``list`` is what retention decides deletions from.
        """
        import xml.etree.ElementTree as ET

        ns = "{http://s3.amazonaws.com/doc/2006-03-01/}"
        keys: list[str] = []
        token = ""
        while True:
            query = "list-type=2"
            if prefix:
                query += f"&prefix={quote(prefix, safe='~')}"
            if token:
                query += f"&continuation-token={quote(token, safe='~')}"
            root = ET.fromstring(self._request("GET", query=query).decode())
            keys.extend(el.text for el in root.iter(f"{ns}Key") if el.text is not None)
            token_el = root.find(f"{ns}NextContinuationToken")
            if token_el is None or not token_el.text:
                return keys
            token = token_el.text
