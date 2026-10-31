"""Shared helpers for versions.py and update-readme.py (Python standard library only)."""

import datetime
import json
import urllib.error
import urllib.parse
import urllib.request

IMAGE = "ghcr.io/megathorx/nginx-php"
BASE_IMAGE = "docker.io/library/php"
ARCHES = ("amd64", "arm64")
LABEL_PREFIX = "io.github.megathorx.nginx-php."

ENDOFLIFE_URL = "https://endoflife.date/api/php.json"
PECL_STABLE_URL = "https://pecl.php.net/rest/r/{}/stable.txt"

_MANIFEST_TYPES = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])

_REGISTRIES = {
    "docker.io": ("registry-1.docker.io", "https://auth.docker.io/token?service=registry.docker.io&scope={}"),
    "ghcr.io": ("ghcr.io", "https://ghcr.io/token?scope={}"),
}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def http_get(url, headers=None, method="GET"):
    """Returns (status, headers, body) and follows redirects without leaking auth headers."""
    for _ in range(5):
        request = urllib.request.Request(url, headers=headers or {}, method=method)
        try:
            with _opener.open(request, timeout=30) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as error:
            if error.code in (301, 302, 303, 307, 308):
                # Blob downloads redirect to a CDN with a signed URL that rejects our token
                url = urllib.parse.urljoin(url, error.headers["Location"])
                headers = {}
                continue
            return error.code, error.headers, error.read()
    raise RuntimeError(f"too many redirects for {url}")


def get_json(url):
    status, _, body = http_get(url, {"Accept": "application/json", "User-Agent": "nginx-php-builder"})
    if status != 200:
        raise RuntimeError(f"GET {url} returned HTTP {status}")
    return json.loads(body)


def get_text(url):
    status, _, body = http_get(url, {"User-Agent": "nginx-php-builder"})
    if status != 200:
        raise RuntimeError(f"GET {url} returned HTTP {status}")
    return body.decode().strip()


class Registry:
    """Anonymous, read-only access to a public image repository such as ghcr.io/megathorx/nginx-php."""

    def __init__(self, image):
        host, _, self.repo = image.partition("/")
        self.host, token_url = _REGISTRIES[host]
        self.token_url = token_url.format(urllib.parse.quote(f"repository:{self.repo}:pull"))
        self._refresh_token()

    def _refresh_token(self):
        self.token = get_json(self.token_url)["token"]

    def _get(self, path, accept=None, method="GET"):
        for attempt in range(2):
            headers = {"Authorization": f"Bearer {self.token}"}
            if accept:
                headers["Accept"] = accept
            status, response_headers, body = http_get(f"https://{self.host}/v2/{self.repo}/{path}", headers, method)
            # Docker Hub tokens expire after five minutes
            if status != 401 or attempt:
                return status, response_headers, body
            self._refresh_token()

    def manifest(self, reference):
        """Returns (manifest, digest), or (None, None) if the tag does not exist. Any other error raises."""
        status, headers, body = self._get(f"manifests/{reference}", _MANIFEST_TYPES)
        if status == 404:
            return None, None
        if status != 200:
            raise RuntimeError(f"{self.host}/{self.repo}:{reference}: HTTP {status}")
        return json.loads(body), headers["Docker-Content-Digest"]

    def digest(self, reference):
        status, headers, _ = self._get(f"manifests/{reference}", _MANIFEST_TYPES, method="HEAD")
        if status == 404:
            return None
        if status != 200:
            raise RuntimeError(f"{self.host}/{self.repo}:{reference}: HTTP {status}")
        return headers["Docker-Content-Digest"]

    def config(self, manifest_digest):
        manifest, _ = self.manifest(manifest_digest)
        status, _, body = self._get(f"blobs/{manifest['config']['digest']}")
        if status != 200:
            raise RuntimeError(f"{self.host}/{self.repo}@{manifest_digest} config: HTTP {status}")
        return json.loads(body)


def platform_digests(index):
    """Maps "amd64"/"arm64" to the linux manifest digests of a multi-platform index."""
    return {
        entry["platform"]["architecture"]: entry["digest"]
        for entry in index.get("manifests", [])
        if entry.get("platform", {}).get("os") == "linux" and entry["platform"].get("architecture") in ARCHES
    }


def env_value(config, name):
    for item in config.get("config", {}).get("Env") or []:
        key, _, value = item.partition("=")
        if key == name:
            return value
    return None


def labels(config):
    return config.get("config", {}).get("Labels") or {}


def version_key(cycle):
    return tuple(int(part) for part in cycle.split("."))


def is_eol(entry, today):
    eol = entry["eol"]
    if isinstance(eol, bool):
        return eol
    return datetime.date.fromisoformat(eol) <= today


def is_released(entry, today):
    return datetime.date.fromisoformat(entry["releaseDate"]) <= today


def extra_tags(cycles):
    """`latest` for the newest cycle and `<major>` for the newest cycle of each major."""
    tags = {cycle: [] for cycle in cycles}
    newest_per_major = {}
    for cycle in sorted(cycles, key=version_key):
        newest_per_major[cycle.split(".")[0]] = cycle
    for major, cycle in newest_per_major.items():
        tags[cycle].append(major)
    if cycles:
        tags[max(cycles, key=version_key)].append("latest")
    return tags
