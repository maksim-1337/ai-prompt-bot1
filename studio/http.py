"""Small stdlib HTTP client: bounded requests and secret-safe network errors."""
from __future__ import annotations

import json as jsonlib
import mimetypes
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


class RequestException(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Response:
    def __init__(self, response):
        self.raw = response
        self.status_code = response.code
        self.ok = 200 <= response.code < 300
        self._content = None

    @property
    def content(self):
        if self._content is None:
            try:
                self._content = self.raw.read(40_000_001)
            except (OSError, TimeoutError):
                raise RequestException("HTTP response read failed") from None
            finally:
                self.raw.close()
            if len(self._content) > 40_000_000:
                raise RequestException("HTTP response exceeds size limit")
        return self._content

    def json(self):
        return jsonlib.loads(self.content)

    def iter_content(self, size):
        try:
            while chunk := self.raw.read(size):
                yield chunk
        except (OSError, TimeoutError):
            raise RequestException("HTTP download interrupted") from None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.raw.close()


def request(method, url, headers=None, params=None, json=None, data=None, files=None,
            timeout=30, stream=False, allow_redirects=True):
    headers = dict(headers or {})
    headers.setdefault("User-Agent", "NewsLightStudio/1.0")
    if params:
        url += ("&" if "?" in url else "?") + urlencode(params)
    body = None
    if files:
        boundary = "----newslight" + uuid.uuid4().hex
        parts = []
        for name, value in (data or {}).items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        for name, (filename, handle, mime) in files.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\nContent-Type: {mime or mimetypes.guess_type(filename)[0]}\r\n\r\n'.encode())
            parts.extend([handle.read(), b"\r\n"])
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)
        headers["Content-Type"] = "multipart/form-data; boundary=" + boundary
    elif json is not None:
        body = jsonlib.dumps(json).encode()
        headers["Content-Type"] = "application/json"
    elif data is not None:
        body = urlencode(data).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    opener = build_opener() if allow_redirects else build_opener(NoRedirect())
    try:
        response = opener.open(Request(url, data=body, headers=headers, method=method), timeout=timeout)
    except HTTPError as e:
        response = e
    except (URLError, OSError, TimeoutError):
        raise RequestException("HTTP connection failed") from None
    return Response(response)


def get(url, **kwargs):
    return request("GET", url, **kwargs)


def post(url, **kwargs):
    return request("POST", url, **kwargs)


def put(url, **kwargs):
    return request("PUT", url, **kwargs)
