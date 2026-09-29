"""Property tests for the two places arbitrary text decides who Cortex talks to.

* ``_parse_host_header`` reduces a request's ``Host`` header to a hostname before
  the loopback allow-list is consulted, on every route, before any credential is
  read. It must never raise (an unauthenticated 500), must say the same thing on
  every supported Python (the ``urlsplit`` it wraps does not), and must never let
  an ordinary hostname read as a loopback one.
* ``resolve_download_url`` and ``_validate_download_url`` (``llamacpp/download.py``)
  turn a user's URL or repository id into the address Cortex fetches and the file
  name it writes. They must refuse with their typed error and never produce a
  file name that leaves the models folder, a non-https address, or an address that
  is loopback or private.

Name resolution is replaced with a fixed public answer: nothing here touches the
network, and no result depends on what a resolver says.
"""

from __future__ import annotations

from collections.abc import Iterator
import ipaddress
import os
import re
import socket
from urllib.parse import urlsplit

from hypothesis import example, given, strategies as st
import pytest

from cortex_backend.api.security import _parse_host_header
from cortex_backend.llamacpp import download
from cortex_backend.llamacpp.download import (
    DownloadSource,
    GGUFDownloadError,
    _normalize_huggingface_blob_url,
    _validate_download_url,
    resolve_download_url,
)

LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost", "::1"})
_PUBLIC_ADDRESS = "93.184.216.34"

# Text that is mostly the punctuation an authority or a URL is built from, so the
# interesting parses are reached; plus every character Python can represent.
_URL_PIECES = (
    "https", "http", "://", ":", "/", "//", "@", "?", "#", "\\", "[", "]", "::1", "127.0.0.1", "localhost",
    "0", "80", "99999", ".", "..", "-", "%2e", "%40", " ", "\t", "\n", "\x00", "model.gguf", ".gguf", "a", "b",
    "huggingface.co", "blob", "resolve", "main", "evil.com", "LOCALHOST", "xn--", "K", "．",
)
_ANY_TEXT = st.text(alphabet=st.characters(exclude_categories=()), max_size=80)
_URL_SOUP = st.builds(
    lambda pieces, separator: separator.join(pieces),
    st.lists(st.sampled_from(_URL_PIECES), max_size=14),
    st.sampled_from(("", "", "/")),
)
_ARBITRARY_TEXT = st.one_of(_ANY_TEXT, _URL_SOUP)



def _is_ip_literal(name: str) -> bool:
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True


# Ordinary DNS names: letters, digits and hyphens in up to three labels, never an
# IP literal in disguise ("10.0.0.1" is four numeric labels).
_HOSTNAMES = st.from_regex(
    r"[a-z0-9]([a-z0-9-]{0,10}[a-z0-9])?(\.[a-z0-9]{1,8}){0,2}", fullmatch=True
).filter(lambda name: not _is_ip_literal(name))
_PORTS = st.one_of(st.none(), st.integers(0, 65_535))


# -- The Host header ----------------------------------------------------------------


@given(raw=_ARBITRARY_TEXT)
@example(raw="[")
@example(raw="[::1")
@example(raw="[::1]evil.com")
@example(raw="::1")
@example(raw="ｌocalhost")
def test_a_host_header_never_raises_and_always_reduces_to_lowercase(raw: str) -> None:
    host = _parse_host_header(raw)

    assert isinstance(host, str)
    assert host == host.lower()


_BRACKETED_AUTHORITY = re.compile(r"^\[(?P<address>[^\[\]]*)\](?::\d{1,5})?$")


@given(raw=st.one_of(_ARBITRARY_TEXT, st.builds(lambda address, tail: f"[{address}]{tail}", _ANY_TEXT, _ARBITRARY_TEXT)))
def test_a_bracketed_host_is_read_by_one_rule_on_every_python(raw: str) -> None:
    """Whatever urlsplit does with brackets, on any version, a header that opens or
    closes with one is a bracketed authority: an address, then at most a port."""

    if not (raw.startswith("[") or raw.endswith("]")):
        return
    match = _BRACKETED_AUTHORITY.match(raw)

    assert _parse_host_header(raw) == ("" if match is None else match.group("address").lower())


@given(host=_HOSTNAMES, port=_PORTS, shout=st.booleans())
def test_an_ordinary_host_reads_as_itself_and_only_loopback_names_pass_the_allow_list(
    host: str, port: int | None, shout: bool
) -> None:
    header = host.upper() if shout else host
    if port is not None:
        header += f":{port}"

    parsed = _parse_host_header(header)

    assert parsed == host
    assert (parsed in LOOPBACK_NAMES) == (host in LOOPBACK_NAMES)


@given(port=_PORTS)
def test_the_ipv6_loopback_reads_as_itself_only_when_bracketed(port: int | None) -> None:
    suffix = "" if port is None else f":{port}"

    assert _parse_host_header(f"[::1]{suffix}") == "::1"
    assert _parse_host_header(f"::1{suffix}") == ""


# -- Download URLs ------------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def _no_dns() -> Iterator[None]:
    """Every name resolves to one public address; the suite never asks a resolver.

    Module-scoped, unlike ``monkeypatch``, because Hypothesis refuses a
    function-scoped fixture around a test that runs many examples.
    """

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            download.socket,
            "getaddrinfo",
            lambda host, port, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (_PUBLIC_ADDRESS, port))],
        )
        yield


_SOURCES = st.one_of(
    st.builds(DownloadSource, source=st.just("url"), url=_ARBITRARY_TEXT),
    st.builds(
        DownloadSource,
        source=st.just("huggingface"),
        repo_id=_ARBITRARY_TEXT,
        filename=_ARBITRARY_TEXT,
    ),
    st.builds(
        DownloadSource,
        source=st.just("huggingface"),
        repo_id=st.from_regex(r"[A-Za-z0-9._-]{1,12}/[A-Za-z0-9._-]{1,12}", fullmatch=True),
        filename=st.from_regex(r"[A-Za-z0-9._-]{1,12}", fullmatch=True).map(lambda stem: f"{stem}.gguf"),
    ),
    st.builds(
        DownloadSource,
        source=st.just("url"),
        url=st.builds(
            lambda scheme, host, tail, name: f"{scheme}://{host}/{tail}{name}.gguf",
            st.sampled_from(("https", "https", "HTTPS", "http", "ftp", "file")),
            _HOSTNAMES,
            st.from_regex(r"([a-z0-9]{1,6}/){0,3}", fullmatch=True),
            st.from_regex(r"[A-Za-z0-9._-]{1,12}", fullmatch=True),
        ),
    ),
    st.builds(DownloadSource, source=st.sampled_from(("ftp", "", "URL")), url=_ARBITRARY_TEXT),
)
_SAFE_FILENAME = re.compile(r"^[\w.\-]+\.gguf$", re.IGNORECASE)


@given(request=_SOURCES)
@example(request=DownloadSource(source="url", url="https://example.com/a/model.gguf"))
@example(request=DownloadSource(source="huggingface", repo_id="owner/name", filename="model.gguf"))
@example(request=DownloadSource(source="url", url="https://huggingface.co/owner/name/blob/main/model.gguf"))
def test_a_download_request_resolves_to_a_safe_address_and_file_name_or_is_refused(
    request: DownloadSource,
) -> None:
    try:
        url, filename = resolve_download_url(request)
    except GGUFDownloadError as refusal:
        assert str(refusal)
        return

    assert urlsplit(url).scheme == "https"
    assert not any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in url)
    # The name is written into the models folder: one plain component, never a path.
    assert _SAFE_FILENAME.fullmatch(filename)
    assert filename not in {".", ".."}
    assert os.path.basename(filename) == filename
    assert "/" not in filename and "\\" not in filename and os.sep not in filename
    if request.source == "huggingface":
        assert url == f"https://huggingface.co/{request.repo_id}/resolve/main/{filename}"
        assert url.count("/") == 7
    else:
        assert os.path.basename(urlsplit(url).path) == filename


@given(
    scheme=st.sampled_from(("http", "HTTP", "ftp", "file", "ws", "wss", "data", "javascript", "gopher", "")),
    host=_HOSTNAMES,
)
def test_only_an_https_url_is_ever_resolved(scheme: str, host: str) -> None:
    with pytest.raises(GGUFDownloadError):
        resolve_download_url(DownloadSource(source="url", url=f"{scheme}://{host}/model.gguf"))


@given(
    head=st.from_regex(r"[a-z0-9]{1,8}", fullmatch=True),
    separator=st.sampled_from(("/", "\\", "../", "..\\", "/../", "%2f", "\n", "\x00", " ", ":", "*")),
    tail=st.from_regex(r"[a-z0-9]{1,8}", fullmatch=True),
)
def test_a_huggingface_file_name_is_one_plain_component(head: str, separator: str, tail: str) -> None:
    """A name that could name a place other than the models folder is refused, not trimmed."""

    request = DownloadSource(source="huggingface", repo_id="owner/name", filename=f"{head}{separator}{tail}.gguf")

    with pytest.raises(GGUFDownloadError):
        resolve_download_url(request)


@given(request=_SOURCES)
def test_resolving_a_download_request_is_deterministic(request: DownloadSource) -> None:
    def outcome() -> object:
        try:
            return resolve_download_url(request)
        except GGUFDownloadError as refusal:
            return ("refused", str(refusal))

    assert outcome() == outcome()


@given(url=_ARBITRARY_TEXT)
@example(url="https://huggingface.co/a/b/blob/blob/x.gguf")
def test_rewriting_a_blob_link_is_idempotent_and_leaves_no_blob_behind(url: str) -> None:
    once = _normalize_huggingface_blob_url(url)

    assert _normalize_huggingface_blob_url(once) == once
    if once != url:
        assert once.startswith("https://huggingface.co/")
        assert "/resolve/" in once


@given(url=_ARBITRARY_TEXT)
@example(url="https://example.com/model.gguf")
@example(url="https://[::1/model.gguf")
@example(url="https://example.com:99999/model.gguf")
@example(url="https://example.com:x/model.gguf")
def test_a_download_url_is_accepted_unchanged_or_refused_with_the_typed_error(url: str) -> None:
    try:
        accepted = _validate_download_url(url)
    except GGUFDownloadError as refusal:
        assert str(refusal)
        return

    parts = urlsplit(accepted)
    assert accepted == url
    assert parts.scheme.casefold() == "https"
    assert parts.hostname
    assert parts.username is None and parts.password is None and not parts.fragment
    assert parts.port is None or 1 <= parts.port <= 65_535
    host = parts.hostname.rstrip(".").casefold()
    assert host not in {"localhost", "localhost.localdomain"}
    assert not host.endswith((".localhost", ".local", ".internal"))
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        return
    assert literal.is_global and not (literal.is_private or literal.is_loopback or literal.is_link_local)


_NON_PUBLIC_NETWORKS = (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
    "192.0.2.0/24", "192.168.0.0/16", "198.18.0.0/15", "224.0.0.0/4", "240.0.0.0/4",
    "::/128", "::1/128", "fc00::/7", "fe80::/10", "ff00::/8",
)


@st.composite
def _non_public_hosts(draw) -> str:
    network = ipaddress.ip_network(draw(st.sampled_from(_NON_PUBLIC_NETWORKS)))
    address = draw(st.ip_addresses(v=network.version, network=network))
    return f"[{address}]" if network.version == 6 else str(address)


@given(host=_non_public_hosts(), port=_PORTS, name=st.sampled_from(("model.gguf", "a/b/model.gguf")))
def test_an_address_that_is_loopback_private_or_reserved_is_never_a_download_target(
    host: str, port: int | None, name: str
) -> None:
    suffix = "" if port is None else f":{port}"

    with pytest.raises(GGUFDownloadError):
        _validate_download_url(f"https://{host}{suffix}/{name}")


_LOCAL_HOSTS = st.one_of(
    st.sampled_from(("localhost", "LOCALHOST", "LocalHost.", "localhost.localdomain", "LOCALHOST.LOCALDOMAIN.")),
    st.builds(
        lambda label, suffix: f"{label}.{suffix}",
        st.from_regex(r"[a-z0-9]{1,8}(\.[a-z0-9]{1,8}){0,2}", fullmatch=True),
        st.sampled_from(("localhost", "LocalHost", "local", "LOCAL", "internal", "Internal.", "local.")),
    ),
)


@given(host=_LOCAL_HOSTS, port=_PORTS)
def test_local_host_names_are_never_a_download_target(host: str, port: int | None) -> None:
    suffix = "" if port is None else f":{port}"

    with pytest.raises(GGUFDownloadError):
        _validate_download_url(f"https://{host}{suffix}/model.gguf")


_PUBLIC_HOSTNAMES = _HOSTNAMES.filter(
    lambda name: name != "localhost" and not name.endswith((".local", ".internal", ".localhost"))
)


@given(host=_PUBLIC_HOSTNAMES, port=st.one_of(st.none(), st.integers(1, 65_535)))
def test_an_ordinary_public_host_is_accepted(host: str, port: int | None) -> None:
    url = f"https://{host}{'' if port is None else f':{port}'}/model.gguf"

    assert _validate_download_url(url) == url
