"""Unit tests for mbtools.deploy.release.

Every test scripts the module's single ``fetch(url, headers) -> bytes``
seam (:class:`_ScriptedFetch` below) instead of touching the network --
see release.py's own module docstring ("HTTP seam"). Response bodies are
modeled on the real release pages named in the sprint-002 ticket:
League-Microbit/nezha-robot-template (``MICROBIT.hex``,
``MICROBIT.hex.txt``, and a versioned ``.hex`` on the same release --
``MICROBIT.hex`` must still win) and League-Robotics/microbit-radio-relay
(a moving tag literally named ``latest`` that is *not* GitHub's own
"Latest" release -- resolving via the ``releases/latest`` *endpoint*, not
a tag search, is what keeps those two apart).

One additional test (bottom of file, ``test_live_...``) is a real,
network-hitting smoke test against those actual repos; it is skipped by
default (see its own docstring) since unit correctness here never depends
on network access.
"""

from __future__ import annotations

import json
import urllib.error

import pytest

from mbtools.deploy import release as release_mod


class _ScriptedFetch:
    """A table-driven stand-in for :data:`release_mod.Fetcher`.

    ``script`` maps a URL suffix to either the bytes to return, or an
    exception instance to raise -- the same seam shape as ticket 005's
    injectable pyocd runner (``flash.py``'s tests monkeypatch
    ``subprocess.Popen`` with a fake that records every call; this fakes
    the one network entry point the same way). Every call is recorded in
    ``.calls`` so a cache-hit test can assert *zero* further calls.
    """

    def __init__(self, script: dict[str, object]):
        self.script = script
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, headers: dict[str, str]) -> bytes:
        self.calls.append((url, dict(headers)))
        for suffix, value in self.script.items():
            if url.endswith(suffix):
                if isinstance(value, BaseException):
                    raise value
                assert isinstance(value, bytes)
                return value
        raise AssertionError(f"unscripted fetch() call: {url}")


def _release_json(tag_name: str, asset_names: tuple[str, ...]) -> bytes:
    return json.dumps(
        {
            "tag_name": tag_name,
            "assets": [
                {
                    "name": name,
                    "browser_download_url": f"https://github.com/o/r/releases/download/{tag_name}/{name}",
                }
                for name in asset_names
            ],
        }
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# parse_repo_ref
# ---------------------------------------------------------------------------


class TestParseRepoRef:
    def test_no_tag(self):
        assert release_mod.parse_repo_ref("League-Microbit/nezha-robot-template") == (
            "League-Microbit",
            "nezha-robot-template",
            None,
        )

    def test_with_tag(self):
        assert release_mod.parse_repo_ref(
            "League-Microbit/nezha-robot-template@v0.20260919.7"
        ) == ("League-Microbit", "nezha-robot-template", "v0.20260919.7")

    @pytest.mark.parametrize("bad", ["no-slash", "/repo", "owner/", "", "@tag"])
    def test_invalid_raises_value_error(self, bad):
        with pytest.raises(ValueError):
            release_mod.parse_repo_ref(bad)


# ---------------------------------------------------------------------------
# resolving latest vs. a pinned tag -- the endpoint choice itself
# ---------------------------------------------------------------------------


class TestEndpointSelection:
    def test_no_tag_resolves_via_releases_latest(self, tmp_path):
        fetch = _ScriptedFetch(
            {
                "/repos/League-Microbit/nezha-robot-template/releases/latest": _release_json(
                    "v0.20260919.7", ("MICROBIT.hex",)
                ),
                "/MICROBIT.hex": b"HEXBYTES",
            }
        )
        result = release_mod.resolve_hex(
            "League-Microbit/nezha-robot-template",
            cache_dir=tmp_path,
            fetch=fetch,
        )
        assert result.tag == "v0.20260919.7"
        assert result.asset_name == "MICROBIT.hex"
        assert fetch.calls[0][0].endswith("/releases/latest")

    def test_moving_tag_named_latest_is_not_used_for_no_tag_ref(self, tmp_path):
        """League-Robotics/microbit-radio-relay's own counter-example: a
        tag literally named ``latest`` exists and moves, but is NOT
        GitHub's flagged "Latest" release. A bare ``OWNER/REPO`` (no
        ``@tag``) must resolve via the ``releases/latest`` *endpoint*
        and must never even request ``releases/tags/latest``.
        """
        fetch = _ScriptedFetch(
            {
                "/repos/League-Robotics/microbit-radio-relay/releases/latest": _release_json(
                    "v2.4.0", ("MICROBIT.hex",)
                ),
                "/MICROBIT.hex": b"REAL-LATEST-BYTES",
                # If this were ever requested, it would prove the bug the
                # ticket calls out by name -- deliberately scripted to a
                # different (wrong) tag so the test fails loudly if hit.
                "/repos/League-Robotics/microbit-radio-relay/releases/tags/latest": _release_json(
                    "latest", ("MICROBIT.hex",)
                ),
            }
        )
        result = release_mod.resolve_hex(
            "League-Robotics/microbit-radio-relay",
            cache_dir=tmp_path,
            fetch=fetch,
        )
        assert result.tag == "v2.4.0"
        assert result.hex_bytes == b"REAL-LATEST-BYTES"
        assert not any(
            url.endswith("releases/tags/latest") for url, _headers in fetch.calls
        )

    def test_explicit_tag_resolves_via_releases_tags(self, tmp_path):
        fetch = _ScriptedFetch(
            {
                "/repos/League-Microbit/nezha-robot-template/releases/tags/v0.20260919.7": _release_json(
                    "v0.20260919.7", ("MICROBIT.hex",)
                ),
                "/MICROBIT.hex": b"PINNED-BYTES",
            }
        )
        result = release_mod.resolve_hex(
            "League-Microbit/nezha-robot-template@v0.20260919.7",
            cache_dir=tmp_path,
            fetch=fetch,
        )
        assert result.tag == "v0.20260919.7"
        assert result.hex_bytes == b"PINNED-BYTES"
        assert fetch.calls[0][0].endswith("/releases/tags/v0.20260919.7")


# ---------------------------------------------------------------------------
# asset selection
# ---------------------------------------------------------------------------


class TestAssetSelection:
    def test_prefers_microbit_hex(self, tmp_path):
        """nezha-robot-template's real release shape: MICROBIT.hex,
        MICROBIT.hex.txt, and a versioned .hex all on the same release --
        MICROBIT.hex must win over both.
        """
        fetch = _ScriptedFetch(
            {
                "/releases/latest": _release_json(
                    "v1",
                    (
                        "MICROBIT.hex",
                        "MICROBIT.hex.txt",
                        "nezha-robot-template-v1.hex",
                    ),
                ),
                "/MICROBIT.hex": b"PREFERRED",
            }
        )
        result = release_mod.resolve_hex(
            "League-Microbit/nezha-robot-template", cache_dir=tmp_path, fetch=fetch
        )
        assert result.asset_name == "MICROBIT.hex"
        assert result.hex_bytes == b"PREFERRED"

    def test_falls_back_to_single_other_hex_asset(self, tmp_path):
        fetch = _ScriptedFetch(
            {
                "/releases/latest": _release_json(
                    "v1", ("firmware.hex", "README.md")
                ),
                "/firmware.hex": b"ONLY-HEX",
            }
        )
        result = release_mod.resolve_hex(
            "League-Microbit/Remote-Joystick-Student", cache_dir=tmp_path, fetch=fetch
        )
        assert result.asset_name == "firmware.hex"
        assert result.hex_bytes == b"ONLY-HEX"

    def test_ambiguous_multiple_hex_assets_errors_without_downloading(self, tmp_path):
        fetch = _ScriptedFetch(
            {
                "/releases/latest": _release_json(
                    "v1", ("left.hex", "right.hex")
                ),
            }
        )
        with pytest.raises(release_mod.AmbiguousAssetError):
            release_mod.resolve_hex(
                "League-Microbit/Remote-Joystick-Student",
                cache_dir=tmp_path,
                fetch=fetch,
            )
        # Only the release lookup happened -- never an asset download.
        assert len(fetch.calls) == 1

    def test_no_hex_asset_errors(self, tmp_path):
        fetch = _ScriptedFetch(
            {"/releases/latest": _release_json("v1", ("README.md",))}
        )
        with pytest.raises(release_mod.GithubReleaseError):
            release_mod.resolve_hex(
                "League-Microbit/Remote-Joystick-Student",
                cache_dir=tmp_path,
                fetch=fetch,
            )

    def test_asset_override_used_verbatim(self, tmp_path):
        """--asset overrides the automatic choice entirely, even when a
        preferred MICROBIT.hex is also present on the release.
        """
        fetch = _ScriptedFetch(
            {
                "/releases/latest": _release_json(
                    "v1", ("MICROBIT.hex", "nezha-robot-template-v1.hex")
                ),
                "/nezha-robot-template-v1.hex": b"OVERRIDE-BYTES",
            }
        )
        result = release_mod.resolve_hex(
            "League-Microbit/nezha-robot-template",
            asset_override="nezha-robot-template-v1.hex",
            cache_dir=tmp_path,
            fetch=fetch,
        )
        assert result.asset_name == "nezha-robot-template-v1.hex"
        assert result.hex_bytes == b"OVERRIDE-BYTES"

    def test_asset_override_not_found_on_release_errors(self, tmp_path):
        fetch = _ScriptedFetch(
            {"/releases/latest": _release_json("v1", ("MICROBIT.hex",))}
        )
        with pytest.raises(release_mod.GithubReleaseError):
            release_mod.resolve_hex(
                "League-Microbit/nezha-robot-template",
                asset_override="nope.hex",
                cache_dir=tmp_path,
                fetch=fetch,
            )


# ---------------------------------------------------------------------------
# caching
# ---------------------------------------------------------------------------


class TestCache:
    def test_second_call_for_same_pinned_repo_at_tag_makes_no_http_call(self, tmp_path):
        fetch = _ScriptedFetch(
            {
                "/releases/tags/v1": _release_json("v1", ("MICROBIT.hex",)),
                "/MICROBIT.hex": b"CACHE-ME",
            }
        )
        first = release_mod.resolve_hex(
            "League-Microbit/nezha-robot-template@v1", cache_dir=tmp_path, fetch=fetch
        )
        assert first.from_cache is False
        assert len(fetch.calls) == 2  # release lookup + asset download

        second = release_mod.resolve_hex(
            "League-Microbit/nezha-robot-template@v1", cache_dir=tmp_path, fetch=fetch
        )
        assert second.from_cache is True
        assert second.hex_bytes == b"CACHE-ME"
        assert second.tag == "v1"
        assert len(fetch.calls) == 2  # unchanged -- zero new calls

    def test_cache_is_keyed_by_owner_repo_tag_asset(self, tmp_path):
        fetch = _ScriptedFetch(
            {
                "/releases/tags/v1": _release_json("v1", ("MICROBIT.hex",)),
                "/MICROBIT.hex": b"BYTES",
            }
        )
        result = release_mod.resolve_hex(
            "League-Microbit/nezha-robot-template@v1", cache_dir=tmp_path, fetch=fetch
        )
        expected = (
            tmp_path
            / "League-Microbit"
            / "nezha-robot-template"
            / "v1"
            / "MICROBIT.hex"
        )
        assert result.hex_path == expected
        assert expected.read_bytes() == b"BYTES"

    def test_unpinned_ref_still_caches_under_resolved_tag_and_is_reused_by_pin(
        self, tmp_path
    ):
        fetch = _ScriptedFetch(
            {
                "/releases/latest": _release_json("v9", ("MICROBIT.hex",)),
                "/MICROBIT.hex": b"LATEST-THEN-CACHED",
            }
        )
        release_mod.resolve_hex(
            "League-Microbit/nezha-robot-template", cache_dir=tmp_path, fetch=fetch
        )
        calls_after_first = len(fetch.calls)

        # A later, explicitly pinned call to the tag "latest" resolved to
        # reuses the cache with zero network calls.
        second = release_mod.resolve_hex(
            "League-Microbit/nezha-robot-template@v9",
            cache_dir=tmp_path,
            fetch=fetch,
        )
        assert second.from_cache is True
        assert second.hex_bytes == b"LATEST-THEN-CACHED"
        assert len(fetch.calls) == calls_after_first


# ---------------------------------------------------------------------------
# error flows: not found / rate-limited
# ---------------------------------------------------------------------------


class TestErrorFlows:
    def test_repo_or_tag_not_found_raises_distinct_error(self, tmp_path):
        fetch = _ScriptedFetch(
            {
                "/releases/tags/vNOPE": release_mod.ReleaseNotFoundError(
                    "not found"
                )
            }
        )
        with pytest.raises(release_mod.ReleaseNotFoundError):
            release_mod.resolve_hex(
                "League-Microbit/nezha-robot-template@vNOPE",
                cache_dir=tmp_path,
                fetch=fetch,
            )

    def test_rate_limit_raises_distinct_error(self, tmp_path):
        fetch = _ScriptedFetch(
            {"/releases/latest": release_mod.GithubRateLimitedError("slow down")}
        )
        with pytest.raises(release_mod.GithubRateLimitedError):
            release_mod.resolve_hex(
                "League-Microbit/nezha-robot-template",
                cache_dir=tmp_path,
                fetch=fetch,
            )

    def test_not_found_and_rate_limited_are_not_the_same_exception_type(self):
        assert not issubclass(
            release_mod.ReleaseNotFoundError, release_mod.GithubRateLimitedError
        )
        assert not issubclass(
            release_mod.GithubRateLimitedError, release_mod.ReleaseNotFoundError
        )
        assert issubclass(release_mod.ReleaseNotFoundError, release_mod.GithubReleaseError)
        assert issubclass(
            release_mod.GithubRateLimitedError, release_mod.GithubReleaseError
        )


class TestDefaultFetchTranslatesHTTPErrors:
    """Exercises :func:`release_mod._default_fetch` itself -- the one
    place a real ``urllib.error.HTTPError`` is translated into this
    module's typed exceptions -- via a monkeypatched ``urlopen`` rather
    than a real network call.
    """

    def _patch_urlopen_to_raise(self, monkeypatch, code):
        def fake_urlopen(request, *a, **kw):
            raise urllib.error.HTTPError(
                request.full_url, code, "boom", hdrs=None, fp=None
            )

        monkeypatch.setattr(release_mod.urllib.request, "urlopen", fake_urlopen)

    def test_404_becomes_release_not_found(self, monkeypatch):
        self._patch_urlopen_to_raise(monkeypatch, 404)
        with pytest.raises(release_mod.ReleaseNotFoundError):
            release_mod._default_fetch("https://api.github.com/x", {})

    @pytest.mark.parametrize("code", [403, 429])
    def test_403_or_429_becomes_rate_limited(self, monkeypatch, code):
        self._patch_urlopen_to_raise(monkeypatch, code)
        with pytest.raises(release_mod.GithubRateLimitedError):
            release_mod._default_fetch("https://api.github.com/x", {})

    def test_other_status_becomes_generic_error(self, monkeypatch):
        self._patch_urlopen_to_raise(monkeypatch, 500)
        with pytest.raises(release_mod.GithubReleaseError):
            release_mod._default_fetch("https://api.github.com/x", {})


# ---------------------------------------------------------------------------
# GITHUB_TOKEN
# ---------------------------------------------------------------------------


class TestGithubToken:
    def test_token_env_var_sent_as_bearer_header(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "sekrit-token")
        fetch = _ScriptedFetch(
            {
                "/releases/latest": _release_json("v1", ("MICROBIT.hex",)),
                "/MICROBIT.hex": b"X",
            }
        )
        release_mod.resolve_hex(
            "League-Microbit/nezha-robot-template", cache_dir=tmp_path, fetch=fetch
        )
        for _url, headers in fetch.calls:
            assert headers["Authorization"] == "Bearer sekrit-token"

    def test_no_token_still_works_anonymously(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        fetch = _ScriptedFetch(
            {
                "/releases/latest": _release_json("v1", ("MICROBIT.hex",)),
                "/MICROBIT.hex": b"X",
            }
        )
        result = release_mod.resolve_hex(
            "League-Microbit/nezha-robot-template", cache_dir=tmp_path, fetch=fetch
        )
        assert result.hex_bytes == b"X"
        for _url, headers in fetch.calls:
            assert "Authorization" not in headers

    def test_explicit_token_param_overrides_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "env-token")
        fetch = _ScriptedFetch(
            {
                "/releases/latest": _release_json("v1", ("MICROBIT.hex",)),
                "/MICROBIT.hex": b"X",
            }
        )
        release_mod.resolve_hex(
            "League-Microbit/nezha-robot-template",
            cache_dir=tmp_path,
            fetch=fetch,
            token="explicit-token",
        )
        for _url, headers in fetch.calls:
            assert headers["Authorization"] == "Bearer explicit-token"


# ---------------------------------------------------------------------------
# resolve_hex() defaults to the real network fetcher when none is given
# ---------------------------------------------------------------------------


def test_resolve_hex_uses_default_fetch_when_none_injected(tmp_path, monkeypatch):
    calls: list[str] = []

    def fake_urlopen(request, *a, **kw):
        calls.append(request.full_url)

        class _Resp:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def read(self_inner):
                if request.full_url.endswith("/releases/latest"):
                    return _release_json("v1", ("MICROBIT.hex",))
                return b"REAL-DEFAULT-FETCH-BYTES"

        return _Resp()

    monkeypatch.setattr(release_mod.urllib.request, "urlopen", fake_urlopen)

    result = release_mod.resolve_hex(
        "League-Microbit/nezha-robot-template", cache_dir=tmp_path
    )
    assert result.hex_bytes == b"REAL-DEFAULT-FETCH-BYTES"
    assert any(u.endswith("/releases/latest") for u in calls)


# ---------------------------------------------------------------------------
# Optional network smoke test -- skipped by default.
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason=(
        "network smoke test -- hits the real GitHub API against "
        "League-Microbit/nezha-robot-template and "
        "League-Robotics/microbit-radio-relay. Run manually (remove this "
        "skip, or run with `pytest --no-skip` if configured) when "
        "sanity-checking against the real release pages; never part of "
        "the default/CI run."
    )
)
def test_live_nezha_and_radio_relay_asset_selection(tmp_path):
    nezha = release_mod.resolve_hex(
        "League-Microbit/nezha-robot-template", cache_dir=tmp_path
    )
    assert nezha.asset_name == "MICROBIT.hex"

    relay = release_mod.resolve_hex(
        "League-Robotics/microbit-radio-relay", cache_dir=tmp_path
    )
    # The real counter-example: GitHub's flagged "Latest" release must
    # never resolve to the repo's own moving tag literally named
    # "latest".
    assert relay.tag != "latest"
