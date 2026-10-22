"""Operator-selected URL origins for published text; stored evidence stays intact."""

import re
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

import yaml


def _origin(value: str) -> str:
    if not isinstance(value, str) or re.search(r"[\s\\]", value):
        raise ValueError("invalid link origins: expected HTTP(S) origins")
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and re.fullmatch(r"[a-zA-Z0-9._:-]+", parsed.hostname)
            and not parsed.netloc.endswith(":")
            and parsed.username is None and parsed.password is None
            and parsed.path in {"", "/"}
            and not parsed.query and not parsed.fragment
            and "?" not in value and "#" not in value
        )
        parsed.port  # Reject malformed or out-of-range ports.
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("invalid link origins: expected HTTP(S) origins")
    return f"{parsed.scheme}://{parsed.netloc.lower()}"


def validate_link_origins(origins: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(origins, Mapping):
        raise ValueError("invalid link origins: expected an origin mapping")
    normalized = {}
    for source, target in origins.items():
        source, target = _origin(source), _origin(target)
        if source in normalized and normalized[source] != target:
            raise ValueError("invalid link origins: conflicting source origins")
        normalized[source] = target
    normalized = {source: target for source, target in normalized.items() if source != target}
    if any(target in normalized for target in normalized.values()):
        raise ValueError("invalid link origins: chains and cycles are not supported")
    return normalized


def load_link_origins(config_dir: Path) -> dict[str, str]:
    path = config_dir / "link-origins.yaml"
    if not path.exists():
        return {}
    try:
        config = yaml.safe_load(path.read_text())
    except yaml.YAMLError as error:
        raise ValueError("invalid link origins configuration") from error
    if not isinstance(config, dict) or set(config) != {"origins"}:
        raise ValueError("invalid link origins: expected an origins mapping")
    return validate_link_origins(config["origins"])


def rewrite_links(text: str, origins: Mapping[str, str]) -> str:
    origins = validate_link_origins(origins)
    if not origins:
        return text
    alternatives = "|".join(re.escape(origin) for origin in sorted(origins, key=len, reverse=True))
    origin_pattern = re.compile(
        r"(?:" + alternatives + r")"
        r"(?=$|[/#?\s<>\"'`(){}\[\]]|[.,;!](?=$|\s))",
        re.IGNORECASE,
    )
    # Consume the whole URL so embedded URLs in its path, query, or fragment
    # cannot be rewritten as separate origins. Only the leading origin changes.
    def replace_url(match: re.Match) -> str:
        url = match.group()
        origin = origin_pattern.match(url)
        if origin is None:
            return url
        return origins[origin.group().lower()] + url[origin.end():]

    # Markdown closers and table separators end a token; IPv6 authority
    # brackets are kept together, including in nested URL suffixes.
    return re.sub(
        r"(?<![\w+./:@%-])https?://(?:\[[0-9a-f:.]+\]|[^\s<>\"'`\])|])+",
        replace_url, text, flags=re.IGNORECASE,
    )
