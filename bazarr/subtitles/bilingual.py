# coding=utf-8

"""Create a readable bilingual subtitle from two timed text tracks.

The first input is the primary language and is kept as the left/top part of
each cue.  The second input is matched by overlapping timestamps and is
placed below it.  Unmatched cues from either input are retained so a slightly
different cue segmentation does not silently lose dialogue.
"""

from __future__ import annotations

from copy import deepcopy
import os
from typing import Iterable

import pysubs2
from subliminal_patch.subtitle import Subtitle


DEFAULT_MATCH_TOLERANCE_MS = 750


def _load_subtitle(path: str) -> pysubs2.SSAFile:
    try:
        return pysubs2.load(path, encoding="utf-8")
    except (UnicodeError, ValueError, pysubs2.Pysubs2Error):
        # Chinese subtitle files are frequently encoded as GB18030/Big5 even
        # when their filename has no encoding hint.
        with open(path, "rb") as subtitle_file:
            content = subtitle_file.read()
        for encoding in ("gb18030", "big5", "utf-16", "cp1252"):
            try:
                return pysubs2.SSAFile.from_string(content.decode(encoding))
            except (UnicodeDecodeError, ValueError, pysubs2.Pysubs2Error):
                continue
        raise ValueError(f"Unable to parse subtitle file: {path}")


def _dialogue_events(subtitles: pysubs2.SSAFile) -> list[pysubs2.SSAEvent]:
    return [event for event in subtitles.events if not event.is_comment and event.text.strip()]


def _overlap_ms(left: pysubs2.SSAEvent, right: pysubs2.SSAEvent) -> int:
    return max(0, min(left.end, right.end) - max(left.start, right.start))


def _matching_secondary_events(
    primary: pysubs2.SSAEvent,
    secondary: Iterable[pysubs2.SSAEvent],
    used: set[int],
    tolerance_ms: int,
) -> list[tuple[int, pysubs2.SSAEvent]]:
    candidates = []
    for index, event in enumerate(secondary):
        if index in used:
            continue

        overlap = _overlap_ms(primary, event)
        distance = abs(primary.start - event.start)
        if overlap:
            candidates.append((index, event, 2, overlap, -distance))
        elif distance <= tolerance_ms:
            candidates.append((index, event, 1, 0, -distance))

    if not candidates:
        return []

    # Prefer actual overlap, then the largest overlap, then the closest cue.
    candidates.sort(key=lambda item: (item[2], item[3], item[4]), reverse=True)
    best_rank = candidates[0][2:]
    return [(index, event) for index, event, *rank in candidates if tuple(rank) == best_rank]


def merge_subtitle_events(
    primary_events: Iterable[pysubs2.SSAEvent],
    secondary_events: Iterable[pysubs2.SSAEvent],
    tolerance_ms: int = DEFAULT_MATCH_TOLERANCE_MS,
) -> list[pysubs2.SSAEvent]:
    """Merge two event streams with primary-language text first."""

    primary = list(primary_events)
    secondary = list(secondary_events)
    used_secondary: set[int] = set()
    merged = []

    for primary_event in primary:
        matches = _matching_secondary_events(
            primary_event,
            secondary,
            used_secondary,
            tolerance_ms,
        )
        secondary_text = "\n".join(event.text.strip() for _, event in matches if event.text.strip())
        if matches:
            used_secondary.update(index for index, _ in matches)

        text = primary_event.text.strip()
        if secondary_text:
            text = f"{text}\n{secondary_text}" if text else secondary_text

        merged.append(
            pysubs2.SSAEvent(
                start=primary_event.start,
                end=primary_event.end,
                text=text,
                marked=primary_event.marked,
                layer=primary_event.layer,
                style=primary_event.style,
                name=primary_event.name,
                marginl=primary_event.marginl,
                marginr=primary_event.marginr,
                marginv=primary_event.marginv,
                effect=primary_event.effect,
                type=primary_event.type,
            )
        )

    # Preserve secondary-only dialogue rather than dropping it when the two
    # subtitle files have different segmentation or timings.
    for index, secondary_event in enumerate(secondary):
        if index in used_secondary:
            continue
        merged.append(deepcopy(secondary_event))

    merged.sort(key=lambda event: (event.start, event.end))
    return merged


def merge_subtitle_files(
    primary_path: str,
    secondary_path: str,
    output_path: str,
    tolerance_ms: int = DEFAULT_MATCH_TOLERANCE_MS,
    chmod: int | None = None,
) -> str:
    """Merge two subtitle files into a UTF-8 SRT file and return its path."""

    if not os.path.isfile(primary_path):
        raise FileNotFoundError(primary_path)
    if not os.path.isfile(secondary_path):
        raise FileNotFoundError(secondary_path)

    primary = _dialogue_events(_load_subtitle(primary_path))
    secondary = _dialogue_events(_load_subtitle(secondary_path))
    if not primary or not secondary:
        raise ValueError("Both subtitle files must contain dialogue cues")

    result = pysubs2.SSAFile()
    result.events = merge_subtitle_events(primary, secondary, tolerance_ms=tolerance_ms)
    output_directory = os.path.dirname(output_path)
    if output_directory:
        os.makedirs(output_directory, exist_ok=True)
    result.save(output_path, encoding="utf-8", format_="srt")
    if chmod is not None:
        os.chmod(output_path, chmod)
    return output_path


def subtitle_srt_content(subtitle):
    """Return a downloaded subtitle as UTF-8 SRT bytes.

    Providers may return ASS, SSA, or WebVTT even when the caller requests a
    different output format. Pair-generated files have an explicit ``.srt``
    extension, so normalize the downloaded content before merging or saving.
    """

    try:
        content = subtitle.get_modified_content(format="srt")
    except (AttributeError, LookupError, OSError, UnicodeError, ValueError):
        return None
    if not content:
        return None
    if isinstance(content, str):
        content = content.encode("utf-8")

    encodings = ["utf-8"]
    try:
        encoding = subtitle.get_encoding()
    except (AttributeError, LookupError):
        encoding = None
    if encoding and encoding.lower() not in encodings:
        encodings.append(encoding)

    for encoding in encodings:
        try:
            text = content.decode(encoding)
            parsed = pysubs2.SSAFile.from_string(text)
            return parsed.to_string(format_="srt").encode("utf-8")
        except (UnicodeDecodeError, ValueError, pysubs2.Pysubs2Error):
            continue

    # Keep the original bytes as a last resort; the normal provider validity
    # check has already rejected completely unparseable subtitle content.
    return content


def subtitle_has_bilingual_pair(subtitle, primary_language: str, secondary_language: str) -> bool:
    """Inspect provider metadata without downloading or parsing the file."""

    from .requirements import detect_bilingual_pair, normalize_language_code

    primary_language = normalize_language_code(primary_language)
    secondary_language = normalize_language_code(secondary_language)
    if not primary_language or not secondary_language:
        return False

    if str(getattr(subtitle, "content_type", "single")).lower() == "bilingual":
        subtitle_language = getattr(subtitle, "language", None)
        subtitle_primary = normalize_language_code(
            getattr(subtitle_language, "basename", None) or
            getattr(subtitle_language, "alpha3", None)
        )
        subtitle_secondary = normalize_language_code(getattr(subtitle, "secondary_language", None))
        # Some providers expose the English side as the object's language
        # while still recording the requested pair as ``zh + en``. Treat
        # that known reverse ordering as the same provider-labelled result;
        # an unrelated primary language remains rejected.
        if subtitle_secondary == secondary_language and \
                (not subtitle_primary or subtitle_primary in {primary_language, secondary_language}):
            return True

    metadata_values = (
        getattr(subtitle, "release_info", None),
        getattr(subtitle, "page_link", None),
        getattr(subtitle, "filename", None),
        getattr(subtitle, "version", None),
        getattr(subtitle, "name", None),
    )
    return any(
        detect_bilingual_pair(
            value,
            primary_language=primary_language,
            secondary_language=secondary_language,
        ) == (primary_language, secondary_language)
        for value in metadata_values
    )


def bilingual_subtitle_path(
    video_path: str,
    primary_language: str,
    secondary_language: str,
    forced: bool = False,
    hi: bool = False,
    directory: str | None = None,
) -> str:
    """Build a stable sidecar name that the indexer can recognize as bilingual."""

    from .requirements import normalize_language_code

    primary_language = normalize_language_code(primary_language) or str(primary_language).lower()
    secondary_language = normalize_language_code(secondary_language) or str(secondary_language).lower()
    root = os.path.splitext(os.path.basename(video_path))[0]
    suffix = ".forced" if forced else ".hi" if hi else ""
    filename = f"{root}.{primary_language}-{secondary_language}{suffix}.srt"
    return os.path.join(directory or os.path.dirname(video_path), filename)


def save_bilingual_subtitle(
    subtitle,
    video_path: str,
    primary_language: str,
    secondary_language: str,
    directory: str | None = None,
    chmod: int | None = None,
) -> str | None:
    """Save a provider subtitle under a pair-aware sidecar name."""

    from .requirements import normalize_language_code

    primary_language = normalize_language_code(primary_language) or str(primary_language).lower()
    secondary_language = normalize_language_code(secondary_language) or str(secondary_language).lower()
    output_path = bilingual_subtitle_path(
        video_path,
        primary_language,
        secondary_language,
        forced=bool(getattr(subtitle.language, "forced", False)),
        hi=bool(getattr(subtitle.language, "hi", False)),
        directory=directory,
    )
    output_directory = os.path.dirname(output_path)
    if output_directory:
        os.makedirs(output_directory, exist_ok=True)

    content = subtitle_srt_content(subtitle)
    if not content:
        return None

    with open(output_path, "wb") as output_file:
        output_file.write(content)
    if chmod is not None:
        os.chmod(output_path, chmod)

    subtitle.storage_path = output_path
    subtitle.content_type = "bilingual"
    subtitle.secondary_language = secondary_language
    return output_path


class GeneratedBilingualSubtitle(Subtitle):
    """Small Subliminal-compatible wrapper for a generated sidecar."""

    provider_name = "bilingual-generator"

    def __init__(self, language, secondary_language: str, storage_path: str, score: float = 0):
        super().__init__(language)
        self.content_type = "bilingual"
        self.secondary_language = secondary_language
        self.storage_path = storage_path
        self.score = score
        self.release_info = f"Generated from {language.basename} + {secondary_language}"
        self.matches = {"bilingual", "generated"}
        self.uploader = None
        self._subtitle_id = f"generated:{storage_path}"

    @property
    def id(self):
        return self._subtitle_id
