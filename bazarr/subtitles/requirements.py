# coding=utf-8

"""Shared language-profile and subtitle-requirement primitives.

Language profiles historically represented a subtitle requirement as a small
dictionary containing a language and the forced/HI flags.  Keep that shape at
the API boundary, but give the requirement a canonical identity internally so
that a bilingual subtitle cannot be confused with an ordinary subtitle in one
of its component languages.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable, Mapping


CONTENT_TYPE_SINGLE = "single"
CONTENT_TYPE_BILINGUAL = "bilingual"
SUPPORTED_CONTENT_TYPES = frozenset((CONTENT_TYPE_SINGLE, CONTENT_TYPE_BILINGUAL))


_LANGUAGE_ALIASES = {
    "en": "en",
    "eng": "en",
    "english": "en",
    "chinese": "zh",
    "simplified chinese": "zh",
    "traditional chinese": "zt",
    "英文": "en",
    "英语": "en",
    "中文": "zh",
    "汉语": "zh",
    "zh": "zh",
    "zho": "zh",
    "chi": "zh",
    "chs": "zh",
    "zhs": "zh",
    "zh-cn": "zh",
    "zh-hans": "zh",
    "zh-sg": "zh",
    "hans": "zh",
    "simplified": "zh",
    "简体": "zh",
    "简中": "zh",
    "zt": "zt",
    "zht": "zt",
    "cht": "zt",
    "zh-tw": "zt",
    "zh-hant": "zt",
    "zh-hk": "zt",
    "zh-mo": "zt",
    "hant": "zt",
    "big5": "zt",
    "traditional": "zt",
    "繁体": "zt",
    "繁體": "zt",
}

_BILINGUAL_MARKERS = (
    "bilingual",
    "dual language",
    "dual-language",
    "双语",
    "雙語",
    "中英",
    "简英",
    "簡英",
    "繁英",
)

# A generic ``双语`` label is intentionally treated as Chinese-English, which
# is the convention used by the Chinese subtitle providers we support.  When
# a filename names another pair explicitly, however, it must not be silently
# relabelled as English.  Keep these markers before the generic detection
# below so ``中日双语`` is recognized as Chinese-Japanese and cannot satisfy a
# Chinese-English profile.
_EXPLICIT_BILINGUAL_PAIRS = (
    ("中文雙語", ("zt", "en")),
    ("中文双语", ("zh", "en")),
    ("中英雙語", ("zt", "en")),
    ("中英双语", ("zh", "en")),
    ("繁體雙語", ("zt", "en")),
    ("繁體双语", ("zt", "en")),
    ("繁体雙語", ("zt", "en")),
    ("繁体双语", ("zt", "en")),
    ("中文+英文", ("zh", "en")),
    ("简体&英文", ("zh", "en")),
    ("繁体&英文", ("zt", "en")),
    ("chs.eng", ("zh", "en")),
    ("chs&eng", ("zh", "en")),
    ("cht.eng", ("zt", "en")),
    ("cht&eng", ("zt", "en")),
    ("中英", ("zh", "en")),
    ("简英", ("zh", "en")),
    ("簡英", ("zh", "en")),
    ("繁英", ("zt", "en")),
    ("日中", ("ja", "zh")),
    ("中日", ("zh", "ja")),
    ("簡日", ("zh", "ja")),
    ("简日", ("zh", "ja")),
    ("繁日", ("zt", "ja")),
    ("中韓", ("zh", "ko")),
    ("中韩", ("zh", "ko")),
    ("中法", ("zh", "fr")),
    ("中德", ("zh", "de")),
    ("中俄", ("zh", "ru")),
    ("中西", ("zh", "es")),
)

_CHINESE_LANGUAGE_PATTERN = (
    r"(?:simplified[._ -]+chinese|traditional[._ -]+chinese|chinese|汉语|中文|"
    r"简体|简中|繁体|繁體|chs|cht|zhs|zht|zho|chi|"
    r"zh(?:[-_](?:hans|hant|cn|tw|sg|hk|mo))?|zt)"
)
_ENGLISH_LANGUAGE_PATTERN = r"(?:english|英文|英语|eng|en)"
_CONNECTED_BILINGUAL_RE = re.compile(
    rf"(?:{_CHINESE_LANGUAGE_PATTERN}[-._&+/\s]+{_ENGLISH_LANGUAGE_PATTERN}|"
    rf"{_ENGLISH_LANGUAGE_PATTERN}[-._&+/\s]+{_CHINESE_LANGUAGE_PATTERN})"
)


def normalize_language_code(value: Any) -> str | None:
    """Normalize common subtitle filename/provider language labels to alpha-2."""

    if value is None:
        return None
    value = str(value).strip().casefold()
    if value in {"", "null", "none", "undefined"}:
        return None
    return _LANGUAGE_ALIASES.get(value, value)


def _language_aliases_in_text(text: str) -> list[str]:
    found = []
    for alias, normalized in sorted(_LANGUAGE_ALIASES.items(), key=lambda item: -len(item[0])):
        if any(ord(character) > 127 for character in alias):
            present = alias in text
        else:
            present = re.search(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", text) is not None
        if present and normalized not in found:
            found.append(normalized)
    return found


def _has_text_language_marker(text: str, marker: str) -> bool:
    if any(ord(character) > 127 for character in marker):
        return marker in text
    return re.search(rf"(?<![a-z]){re.escape(marker)}(?![a-z])", text) is not None


def _explicit_bilingual_pair(text: str) -> tuple[str, str] | None:
    for marker, pair in sorted(_EXPLICIT_BILINGUAL_PAIRS, key=lambda item: -len(item[0])):
        if marker in text:
            return pair
    return None


def detect_bilingual_pair(
    value: Any,
    primary_language: Any = None,
    secondary_language: Any = None,
) -> tuple[str, str] | None:
    """Detect a bilingual language pair from a filename, track title, or label.

    Profile hints fill in ambiguous provider labels.  An explicit filename
    pair, including its simplified/traditional marker, wins over a conflicting
    hint so a ``zh-en`` file cannot satisfy a traditional-Chinese profile.
    Chinese subtitle sites often label Chinese-English files only as
    ``双语``/``雙語``, so a Chinese primary hint plus that marker still defaults
    the secondary language to English.
    """

    text = str(value or "").casefold()
    if not text:
        return None

    primary = normalize_language_code(primary_language)
    secondary = normalize_language_code(secondary_language)
    explicit_pair = _explicit_bilingual_pair(text)
    if explicit_pair:
        return explicit_pair

    # The two Chinese forms of the generic bilingual marker also carry a
    # useful script hint. Respect it even when the caller supplies the other
    # Chinese variant as a profile hint; otherwise a traditional-only file
    # could incorrectly satisfy a simplified-Chinese requirement (and vice
    # versa).
    if "雙語" in text:
        return "zt", "en"
    if "双语" in text:
        return "zh", "en"

    aliases = _language_aliases_in_text(text)
    has_marker = any(marker in text for marker in _BILINGUAL_MARKERS)
    has_connected_pair = _CONNECTED_BILINGUAL_RE.search(text) is not None

    if primary and secondary and has_marker and not has_connected_pair:
        if primary != secondary:
            return primary, secondary

    if primary and primary in {"zh", "zt"} and not (has_connected_pair and "en" in aliases):
        if has_marker:
            return primary, secondary or "en"

    if has_marker:
        # Chinese subtitle sites commonly omit the English language label and
        # use only a simplified/traditional bilingual marker. Preserve the
        # distinction when it is present; a generic marker defaults to
        # simplified Chinese.
        if "繁英" in text:
            return "zt", "en"
        if "中英" in text or "简英" in text:
            return "zh", "en"
        if "zt" in aliases or "繁体" in text or "繁體" in text or "雙語" in text:
            return "zt", "en"
        if "zh" in aliases or "简体" in text or "简中" in text or "双语" in text:
            return "zh", "en"

        # An unqualified English ``bilingual`` label is the convention used
        # by the Chinese subtitle sources supported here.  Profile hints have
        # already taken precedence above when a different pair is requested.
        return "zh", "en"

    # Treat Chinese-English files as a canonical Chinese-first pair even when
    # a provider labels the stream itself as English or lists English first.
    # This lets an existing ``English + Chinese`` track satisfy the same
    # profile as a ``Chinese + English`` track while keeping generated output
    # consistently ordered for the user.
    if has_connected_pair and "en" in aliases and ("zh" in aliases or "zt" in aliases):
        traditional_markers = (
            "zh-tw", "zh_hant", "zh-hant", "zht", "cht", "hant", "big5",
            "traditional", "繁体", "繁體",
        )
        simplified_markers = (
            "zh-cn", "zh_hans", "zh-hans", "zhs", "chs", "hans", "simplified",
            "简体", "简中",
        )
        if any(_has_text_language_marker(text, marker) for marker in traditional_markers):
            chinese = "zt"
        elif any(_has_text_language_marker(text, marker) for marker in simplified_markers):
            chinese = "zh"
        elif _has_text_language_marker(text, "zt"):
            chinese = "zt"
        elif _has_text_language_marker(text, "zh") or _has_text_language_marker(text, "zho"):
            chinese = "zh"
        else:
            chinese = primary if primary in {"zh", "zt"} else ("zt" if "zt" in aliases else "zh")
        return chinese, "en"

    if has_connected_pair and len(aliases) >= 2:
        if primary in aliases:
            other = next((language for language in aliases if language != primary), None)
            if other:
                return primary, other
        if "zh" in aliases and "en" in aliases:
            return "zh", "en"
        return aliases[0], aliases[1]

    return None


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value

    return str(value).lower() in {"true", "1", "yes", "only"}


def _as_bool_string(value: Any) -> str:
    return "True" if _as_bool(value) else "False"


def normalize_profile_item(item: Mapping[str, Any]) -> dict[str, Any]:
    """Return a profile item with the current, backward-compatible shape.

    Existing profiles do not have ``content_type`` or ``secondary_language``.
    They are ordinary single-language requirements and are normalized here so
    every caller can use the same representation.
    """

    if not isinstance(item, Mapping):
        raise ValueError("Language profile items must be objects")

    normalized = dict(item)
    language = normalize_language_code(normalized.get("language"))
    if not language:
        raise ValueError("Language profile items must define a language")

    content_type = str(normalized.get("content_type") or CONTENT_TYPE_SINGLE).strip().lower()
    if content_type in {"", "null", "none", "undefined"}:
        content_type = CONTENT_TYPE_SINGLE
    if content_type not in SUPPORTED_CONTENT_TYPES:
        raise ValueError(f"Unsupported subtitle content type: {content_type}")

    secondary_language = normalized.get("secondary_language")
    if content_type == CONTENT_TYPE_BILINGUAL:
        secondary_language = normalize_language_code(secondary_language)
        if not secondary_language:
            raise ValueError("Bilingual profile items must define a secondary language")
        if secondary_language == language:
            raise ValueError("Bilingual profile items must use two different languages")
    else:
        secondary_language = None

    normalized.update(
        {
            "language": language,
            "content_type": content_type,
            "secondary_language": secondary_language,
            "forced": _as_bool_string(normalized.get("forced", False)),
            "hi": _as_bool_string(normalized.get("hi", False)),
            "audio_exclude": _as_bool_string(normalized.get("audio_exclude", False)),
            "audio_only_include": _as_bool_string(normalized.get("audio_only_include", False)),
        }
    )
    return normalized


def normalize_profile_items(items: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [normalize_profile_item(item) for item in items]


@dataclass(frozen=True)
class SubtitleRequirement:
    """Canonical identity of one language-profile requirement."""

    language: str
    forced: bool = False
    hi: bool = False
    content_type: str = CONTENT_TYPE_SINGLE
    secondary_language: str | None = None

    def __post_init__(self) -> None:
        language = normalize_language_code(self.language)
        content_type = str(self.content_type).strip().lower()
        if content_type in {"", "null", "none", "undefined"}:
            content_type = CONTENT_TYPE_SINGLE
        secondary_language = normalize_language_code(self.secondary_language)

        if not language:
            raise ValueError("Subtitle requirements must define a language")
        if content_type not in SUPPORTED_CONTENT_TYPES:
            raise ValueError(f"Unsupported subtitle content type: {content_type}")
        if content_type == CONTENT_TYPE_BILINGUAL:
            if not secondary_language:
                raise ValueError("Bilingual requirements must define a secondary language")
            if secondary_language == language:
                raise ValueError("Bilingual requirements must use two different languages")
        else:
            secondary_language = None

        object.__setattr__(self, "language", language)
        object.__setattr__(self, "content_type", content_type)
        object.__setattr__(self, "secondary_language", secondary_language)
        object.__setattr__(self, "forced", _as_bool(self.forced))
        object.__setattr__(self, "hi", _as_bool(self.hi))

    @property
    def identity(self) -> tuple[str, str | None, str, bool, bool]:
        return self.language, self.secondary_language, self.content_type, self.forced, self.hi

    @property
    def token(self) -> str:
        if self.content_type == CONTENT_TYPE_BILINGUAL:
            result = f"{self.language}:bilingual={self.secondary_language}"
        else:
            result = self.language

        if self.forced:
            result += ":forced"
        elif self.hi:
            result += ":hi"
        return result


def requirement_from_profile_item(item: Mapping[str, Any]) -> SubtitleRequirement:
    normalized = normalize_profile_item(item)
    return SubtitleRequirement(
        language=normalized["language"],
        forced=_as_bool(normalized["forced"]),
        hi=_as_bool(normalized["hi"]),
        content_type=normalized["content_type"],
        secondary_language=normalized["secondary_language"],
    )


def requirements_from_profile_items(items: Iterable[Mapping[str, Any]]) -> list[SubtitleRequirement]:
    return [requirement_from_profile_item(item) for item in items]


def requirement_from_token(token: str) -> SubtitleRequirement:
    """Parse both legacy tokens and composite bilingual tokens."""

    if not isinstance(token, str) or not token.strip():
        raise ValueError("Subtitle requirement token must be a non-empty string")

    parts = token.strip().split(":")
    language = parts.pop(0).strip().lower()
    content_type = CONTENT_TYPE_SINGLE
    secondary_language = None
    forced = False
    hi = False

    for part in parts:
        part = part.strip().lower()
        if part.startswith("bilingual="):
            content_type = CONTENT_TYPE_BILINGUAL
            secondary_language = part.split("=", 1)[1]
        elif part == "forced":
            forced = True
        elif part == "hi":
            hi = True
        elif part:
            raise ValueError(f"Unsupported subtitle requirement token: {token}")

    return SubtitleRequirement(
        language=language,
        forced=forced,
        hi=hi,
        content_type=content_type,
        secondary_language=secondary_language,
    )


def format_requirement_token(item: Mapping[str, Any] | SubtitleRequirement) -> str:
    if isinstance(item, SubtitleRequirement):
        return item.token
    return requirement_from_profile_item(item).token


def _artifact_content_type(artifact: Mapping[str, Any]) -> str:
    content_type = str(artifact.get("content_type") or CONTENT_TYPE_SINGLE).strip().lower()
    return CONTENT_TYPE_SINGLE if content_type in {"", "null", "none", "undefined"} else content_type


def _artifact_is_hi(artifact: Mapping[str, Any]) -> bool:
    return _as_bool(artifact.get("hi", False))


def _artifact_is_forced(artifact: Mapping[str, Any]) -> bool:
    return _as_bool(artifact.get("forced", False))


def artifact_satisfies_requirement(
    artifact: Mapping[str, Any],
    requirement: SubtitleRequirement,
) -> bool:
    """Return whether one indexed subtitle artifact satisfies a requirement.

    HI subtitles retain Bazarr's historical behavior of satisfying a normal
    subtitle requirement.  Forced subtitles do not satisfy normal subtitles,
    and a single-language artifact never satisfies a bilingual requirement.
    """

    artifact_language = normalize_language_code(artifact.get("language") or artifact.get("code2"))
    if artifact_language != requirement.language:
        return False

    if requirement.content_type == CONTENT_TYPE_BILINGUAL:
        if _artifact_content_type(artifact) != CONTENT_TYPE_BILINGUAL:
            return False
        if normalize_language_code(artifact.get("secondary_language")) != requirement.secondary_language:
            return False
    elif _artifact_content_type(artifact) != CONTENT_TYPE_SINGLE:
        return False

    artifact_forced = _artifact_is_forced(artifact)
    artifact_hi = _artifact_is_hi(artifact)
    if requirement.forced:
        return artifact_forced
    if requirement.hi:
        return artifact_hi and not artifact_forced
    return not artifact_forced


def missing_requirements(
    requirements: Iterable[SubtitleRequirement],
    artifacts: Iterable[Mapping[str, Any]],
) -> list[SubtitleRequirement]:
    artifacts = list(artifacts)
    return [
        requirement
        for requirement in requirements
        if not any(artifact_satisfies_requirement(artifact, requirement) for artifact in artifacts)
    ]


def requirements_to_tokens(requirements: Iterable[SubtitleRequirement]) -> list[str]:
    return [requirement.token for requirement in requirements]


def has_bilingual_requirement_tokens(tokens: Iterable[str | SubtitleRequirement]) -> bool:
    """Return whether a missing-subtitle token list needs local composition.

    This is used by scheduled callers to decide whether they should still run
    when every provider is throttled: an already-indexed Chinese and English
    track can still be composed into the requested bilingual sidecar.
    """

    return any(
        (token if isinstance(token, SubtitleRequirement) else requirement_from_token(token)).content_type ==
        CONTENT_TYPE_BILINGUAL
        for token in tokens
        if token is not None
    )
