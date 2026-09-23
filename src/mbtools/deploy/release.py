"""deploy.release — GitHub latest-release hex fetch, tag pin, asset
selection, cache.

Turns a ``--repo OWNER/REPO[@TAG]`` reference into a local hex file path.
New for this sprint — no existing code to port, per the issue's own text
("Details learned from the real release pages").

Boundary (sprint.md's Architecture, module ``deploy.release``): GitHub
``releases/latest`` (never a tag literally named ``latest`` —
``League-Robotics/microbit-radio-relay`` has a moving ``latest``-named tag
that is *not* its GitHub "Latest" release, the issue's own counter-
example) and ``releases/tags/<tag>``, ``MICROBIT.hex``-preferred asset
selection with an ``--asset`` override and an ambiguity error, a
``~/.cache/mbtools/hex/<owner>/<repo>/<tag>/`` cache, and an optional
``GITHUB_TOKEN`` for rate-limit headroom. Uses the standard library's
``urllib.request`` only (Design Rationale: no new HTTP dependency for two
GET requests and one asset download). This module knows nothing about
flashing or the registry — it returns a path (and the raw bytes fetched
or read from cache, per sprint.md's Migration Concerns forward note for
sprint 003's remote flash) and prints which release/asset it picked.

**HTTP seam**: every network call goes through a single injectable
``fetch(url, headers) -> bytes`` callable (same seam shape as ticket
005's injectable pyocd runner -- one narrow point real hardware/network
access happens, replaceable wholesale in tests). The default
implementation (:func:`_default_fetch`) wraps ``urllib.request.urlopen``
and translates its ``HTTPError`` into this module's own typed exceptions
(:class:`ReleaseNotFoundError`, :class:`GithubRateLimitedError`) so a
caller never has to know urllib was involved at all.

**Cache short-circuit**: an explicit ``@TAG`` pin is, by definition,
immutable -- once ``owner/repo@tag``'s selected asset has been downloaded
once, a later call for the exact same ``repo@tag`` (and, if given, the
same ``--asset``) never needs to ask GitHub anything: the cache directory
alone is enough to answer. An un-pinned ``OWNER/REPO`` reference cannot
take this shortcut (GitHub's own "Latest" release can change between two
calls), so it always resolves via ``releases/latest`` first -- but the
asset it downloads is still cached under the *resolved* tag, so a
subsequent pinned call for that same tag gets the fast path.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

__all__ = [
    "GITHUB_TOKEN_ENV_VAR",
    "DEFAULT_CACHE_DIR",
    "ReleaseAsset",
    "ReleaseResult",
    "GithubReleaseError",
    "ReleaseNotFoundError",
    "GithubRateLimitedError",
    "AmbiguousAssetError",
    "Fetcher",
    "parse_repo_ref",
    "resolve_hex",
]

#: Optional bearer token for GitHub API rate-limit headroom. Its absence
#: must never prevent anonymous access from working for a public repo --
#: see the module docstring and this ticket's own acceptance criteria.
GITHUB_TOKEN_ENV_VAR = "GITHUB_TOKEN"

#: ``~/.cache/mbtools/hex`` -- overridable by callers (mainly tests, via
#: the ``cache_dir`` parameter) so nothing here ever touches a real
#: user's home directory during a test run.
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "mbtools" / "hex"

_GITHUB_API = "https://api.github.com"

#: The one asset name every nezha-family firmware repo is expected to
#: publish, and the one this module prefers whenever present -- see
#: League-Microbit/nezha-robot-template's own release assets
#: (``MICROBIT.hex``, ``MICROBIT.hex.txt``, a versioned ``.hex`` too) for
#: the concrete shape this preference exists to resolve unambiguously.
_PREFERRED_ASSET_NAME = "MICROBIT.hex"


class GithubReleaseError(Exception):
    """Base for every release-resolution failure this module raises.

    Distinct from a flash failure -- a caller (ticket 007's ``mbdeploy
    deploy --repo ...``) must never report one of these as a flash error
    for a hex file that was never actually fetched (SUC-002's own error-
    flow requirement).
    """


class ReleaseNotFoundError(GithubReleaseError):
    """The referenced repo, or the pinned tag, does not exist (GitHub's
    API answered 404)."""


class GithubRateLimitedError(GithubReleaseError):
    """GitHub's API refused the request as rate-limited (403 or 429),
    distinct from "not found" so a caller can suggest setting
    ``GITHUB_TOKEN`` rather than implying the repo/tag is wrong."""


class AmbiguousAssetError(GithubReleaseError):
    """More than one ``*.hex`` asset exists on the release and none is
    named ``MICROBIT.hex`` -- this module refuses to guess (per this
    ticket's own acceptance criteria); the caller must pass ``--asset``.
    """


@dataclass(frozen=True)
class ReleaseAsset:
    """One selected release asset: its name and GitHub's own
    ``browser_download_url`` for it."""

    name: str
    download_url: str


@dataclass(frozen=True)
class ReleaseResult:
    """What :func:`resolve_hex` hands back: both a local file path and
    the raw bytes it came from (sprint.md's Migration Concerns forward
    note -- sprint 003's remote flash needs the raw bytes to send over
    the wire, not just a local path), plus which release/asset were
    actually selected so a caller can report it (this ticket's own
    acceptance criterion: "return value, not just a printed line").
    """

    owner: str
    repo: str
    tag: str
    asset_name: str
    hex_path: Path
    hex_bytes: bytes
    from_cache: bool


#: A ``fetch(url, headers) -> bytes`` callable -- the one seam every
#: network access in this module goes through. See the module docstring.
Fetcher = Callable[[str, dict[str, str]], bytes]


def parse_repo_ref(repo_ref: str) -> tuple[str, str, str | None]:
    """Split ``OWNER/REPO`` or ``OWNER/REPO@TAG`` into ``(owner, repo,
    tag)``, ``tag`` being ``None`` when no ``@TAG`` was given.

    Raises :class:`ValueError` for anything that isn't ``OWNER/REPO`` (an
    empty owner or repo segment) -- a usage error, not a network one, so
    it is never one of :class:`GithubReleaseError`'s subclasses.
    """
    ref, _, tag = repo_ref.partition("@")
    owner, _, repo = ref.partition("/")
    if not owner or not repo:
        raise ValueError(
            f"invalid --repo value {repo_ref!r}: expected OWNER/REPO[@TAG]"
        )
    return owner, repo, (tag or None)


def _auth_headers(token: str | None) -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _default_fetch(url: str, headers: dict[str, str]) -> bytes:
    """The real network implementation of :data:`Fetcher` -- the only
    place ``urllib.request`` is invoked in this module. Translates
    ``urllib.error.HTTPError`` into this module's own typed exceptions so
    every other function (and every caller) only ever sees those.
    """
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request) as response:  # noqa: S310 (github/asset URLs only)
            return response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ReleaseNotFoundError(f"{url}: not found (404)") from exc
        if exc.code in (403, 429):
            raise GithubRateLimitedError(
                f"{url}: rate-limited (HTTP {exc.code}) -- set {GITHUB_TOKEN_ENV_VAR} "
                "for higher limits"
            ) from exc
        raise GithubReleaseError(f"{url}: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise GithubReleaseError(f"{url}: {exc.reason}") from exc


def _select_asset(
    assets: list[dict[str, Any]], asset_override: str | None
) -> ReleaseAsset:
    """Pick the release asset to flash. ``--asset NAME`` (``asset_
    override``) wins outright and is used verbatim -- no other rule in
    this function applies once it's given. Otherwise: ``MICROBIT.hex`` if
    present; the single other ``*.hex`` asset if there's exactly one;
    else an :class:`AmbiguousAssetError` (more than one, none preferred)
    or a plain :class:`GithubReleaseError` (none at all) -- this module
    never guesses.
    """
    by_name = {asset["name"]: asset for asset in assets}

    if asset_override is not None:
        if asset_override not in by_name:
            raise GithubReleaseError(
                f"--asset {asset_override!r} not found on this release "
                f"(available: {sorted(by_name)})"
            )
        chosen = by_name[asset_override]
        return ReleaseAsset(chosen["name"], chosen["browser_download_url"])

    if _PREFERRED_ASSET_NAME in by_name:
        chosen = by_name[_PREFERRED_ASSET_NAME]
        return ReleaseAsset(chosen["name"], chosen["browser_download_url"])

    hex_assets = [a for a in assets if a["name"].endswith(".hex")]
    if len(hex_assets) == 1:
        chosen = hex_assets[0]
        return ReleaseAsset(chosen["name"], chosen["browser_download_url"])
    if not hex_assets:
        raise GithubReleaseError("release has no .hex asset")
    raise AmbiguousAssetError(
        "multiple .hex assets and none named "
        f"{_PREFERRED_ASSET_NAME!r}: {sorted(a['name'] for a in hex_assets)} "
        "-- pass --asset NAME to choose one"
    )


def _cache_dir_for(cache_dir: str | Path, owner: str, repo: str, tag: str) -> Path:
    return Path(cache_dir) / owner / repo / tag


def _cache_lookup(
    cache_dir: str | Path,
    owner: str,
    repo: str,
    tag: str,
    asset_override: str | None,
) -> tuple[str, bytes] | None:
    """Return ``(asset_name, hex_bytes)`` from the on-disk cache for a
    pinned ``owner/repo@tag``, or ``None`` if nothing usable is cached
    yet. Never makes a network call -- see the module docstring's Cache
    short-circuit note.

    With ``--asset`` given, only that exact filename counts. Without it,
    a single cached file is trusted as "the asset a previous call already
    selected for this tag" (this module only ever writes one file per
    ``resolve_hex`` call into a tag's cache directory); zero or more than
    one file is treated as "nothing conclusively cached" and falls
    through to a real resolution instead of guessing.
    """
    tag_dir = _cache_dir_for(cache_dir, owner, repo, tag)
    if not tag_dir.is_dir():
        return None
    if asset_override is not None:
        candidate = tag_dir / asset_override
        if candidate.is_file():
            return asset_override, candidate.read_bytes()
        return None
    cached_files = [p for p in tag_dir.iterdir() if p.is_file()]
    if len(cached_files) == 1:
        return cached_files[0].name, cached_files[0].read_bytes()
    return None


def _write_cache(
    cache_dir: str | Path, owner: str, repo: str, tag: str, asset_name: str, data: bytes
) -> Path:
    tag_dir = _cache_dir_for(cache_dir, owner, repo, tag)
    tag_dir.mkdir(parents=True, exist_ok=True)
    path = tag_dir / asset_name
    path.write_bytes(data)
    return path


def resolve_hex(
    repo_ref: str,
    asset_override: str | None = None,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
    token: str | None = None,
    fetch: Fetcher | None = None,
) -> ReleaseResult:
    """Turn ``repo_ref`` (``OWNER/REPO`` or ``OWNER/REPO@TAG``) into a
    local hex file, downloading and caching it if needed.

    ``token`` defaults to the ``GITHUB_TOKEN`` environment variable
    (:data:`GITHUB_TOKEN_ENV_VAR`) when not given explicitly; its absence
    never blocks anonymous access to a public repo. ``fetch`` defaults to
    :func:`_default_fetch` (real network access via ``urllib.request``);
    tests inject a fake to script ``releases/latest``/``releases/tags/
    <tag>``/asset-download responses without ever touching the network.

    Prints which release (tag) and asset were selected, per this
    ticket's acceptance criteria, in addition to returning them on the
    :class:`ReleaseResult`.
    """
    owner, repo, tag = parse_repo_ref(repo_ref)
    fetch = fetch if fetch is not None else _default_fetch
    if token is None:
        token = os.environ.get(GITHUB_TOKEN_ENV_VAR)
    headers = _auth_headers(token)

    # A pinned tag is immutable -- a previous download for this exact
    # repo@tag (and --asset, if given) can be reused with zero network
    # calls. An un-pinned reference has no such shortcut: GitHub's own
    # "Latest" release can change between two calls, so it must always
    # be resolved fresh.
    if tag is not None:
        cached = _cache_lookup(cache_dir, owner, repo, tag, asset_override)
        if cached is not None:
            asset_name, hex_bytes = cached
            print(
                f"mbdeploy: using cached {owner}/{repo}@{tag} "
                f"asset {asset_name}"
            )
            cache_path = _cache_dir_for(cache_dir, owner, repo, tag) / asset_name
            return ReleaseResult(
                owner, repo, tag, asset_name, cache_path, hex_bytes, True
            )

    if tag is None:
        url = f"{_GITHUB_API}/repos/{owner}/{repo}/releases/latest"
    else:
        url = f"{_GITHUB_API}/repos/{owner}/{repo}/releases/tags/{tag}"

    release = json.loads(fetch(url, headers))
    resolved_tag = release["tag_name"]
    asset = _select_asset(release.get("assets", []), asset_override)

    # Now that an un-pinned reference's actual tag is known, a second
    # cache lookup can still save the asset download (not the release
    # lookup above -- that one network call for "what is latest right
    # now" is unavoidable for an un-pinned reference).
    cached = _cache_lookup(cache_dir, owner, repo, resolved_tag, asset.name)
    if cached is not None:
        asset_name, hex_bytes = cached
        print(
            f"mbdeploy: using cached {owner}/{repo}@{resolved_tag} "
            f"asset {asset_name}"
        )
        cache_path = _cache_dir_for(cache_dir, owner, repo, resolved_tag) / asset_name
        return ReleaseResult(
            owner, repo, resolved_tag, asset_name, cache_path, hex_bytes, True
        )

    hex_bytes = fetch(asset.download_url, headers)
    cache_path = _write_cache(
        cache_dir, owner, repo, resolved_tag, asset.name, hex_bytes
    )
    print(f"mbdeploy: fetched {owner}/{repo}@{resolved_tag} asset {asset.name}")
    return ReleaseResult(
        owner, repo, resolved_tag, asset.name, cache_path, hex_bytes, False
    )
