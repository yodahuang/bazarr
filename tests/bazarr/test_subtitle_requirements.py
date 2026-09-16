# coding=utf-8

from types import SimpleNamespace

import pytest
import pysubs2

from subtitles.bilingual import (
    merge_subtitle_events,
    merge_subtitle_files,
    subtitle_has_bilingual_pair,
)
from subtitles.requirements import (
    SubtitleRequirement,
    artifact_satisfies_requirement,
    detect_bilingual_pair,
    missing_requirements,
    normalize_profile_item,
    requirement_from_token,
)


def test_legacy_profile_item_is_normalized_as_single_language():
    item = normalize_profile_item({
        "id": 1,
        "language": "ZH",
        "forced": "never",
        "hi": "also",
    })

    assert item["language"] == "zh"
    assert item["content_type"] == "single"
    assert item["secondary_language"] is None
    assert item["forced"] == "False"
    assert item["hi"] == "False"
    assert item["audio_exclude"] == "False"
    assert item["audio_only_include"] == "False"


def test_bilingual_requirement_token_round_trip_preserves_flags():
    requirement = requirement_from_token("zh:bilingual=en:hi")

    assert requirement == SubtitleRequirement(
        language="zh",
        content_type="bilingual",
        secondary_language="en",
        hi=True,
    )
    assert requirement.token == "zh:bilingual=en:hi"


def test_separate_tracks_do_not_satisfy_bilingual_requirement():
    requirement = SubtitleRequirement("zh", content_type="bilingual", secondary_language="en")
    artifacts = [
        {"code2": "zh", "content_type": "single", "forced": False, "hi": False},
        {"code2": "en", "content_type": "single", "forced": False, "hi": False},
    ]

    assert not any(artifact_satisfies_requirement(artifact, requirement) for artifact in artifacts)
    assert missing_requirements([requirement], artifacts) == [requirement]


def test_bilingual_artifact_satisfies_only_matching_pair():
    requirement = SubtitleRequirement("zh", content_type="bilingual", secondary_language="en")
    bilingual = {
        "code2": "zh",
        "content_type": "bilingual",
        "secondary_language": "en",
        "forced": False,
        "hi": False,
    }

    assert artifact_satisfies_requirement(bilingual, requirement)
    assert not artifact_satisfies_requirement(
        {**bilingual, "secondary_language": "ja"}, requirement
    )


@pytest.mark.parametrize(
    ("value", "primary", "secondary", "expected"),
    [
        ("Show.zh-Hans&English.srt", "zh", "en", ("zh", "en")),
        ("Show.繁体双语.ass", "zt", "en", ("zt", "en")),
        ("Show.zh-en.srt", None, None, ("zh", "en")),
        ("Show.English+Chinese.srt", "en", None, ("zh", "en")),
        ("Show.zh-en.srt", "zt", "en", ("zh", "en")),
        ("Show.zt-en.srt", "zh", "en", ("zt", "en")),
    ],
)
def test_detect_bilingual_pair(value, primary, secondary, expected):
    assert detect_bilingual_pair(value, primary, secondary) == expected


def test_explicit_non_english_pair_is_not_treated_as_chinese_english():
    value = "Show.中日双语.srt"

    assert detect_bilingual_pair(value) == ("zh", "ja")
    assert detect_bilingual_pair(value, "zh", "en") != ("zh", "en")


@pytest.mark.parametrize(
    ("value", "primary", "expected"),
    [
        ("Show.雙語.srt", "zh", ("zt", "en")),
        ("Show.双语.srt", "zt", ("zh", "en")),
        ("Show.繁体双语.srt", "zh", ("zt", "en")),
    ],
)
def test_bilingual_marker_keeps_chinese_script_identity(value, primary, expected):
    assert detect_bilingual_pair(value, primary, "en") == expected


def test_language_word_in_title_does_not_make_a_single_track_bilingual():
    assert detect_bilingual_pair("The English Patient.zh.srt", "zh", "en") is None


def test_generic_english_bilingual_marker_defaults_to_chinese_english():
    assert detect_bilingual_pair("Show.bilingual.srt") == ("zh", "en")


def test_formdata_empty_sentinel_is_not_a_language():
    with pytest.raises(ValueError):
        SubtitleRequirement("zh", content_type="bilingual", secondary_language="undefined")


def test_provider_metadata_fields_are_not_combined_for_detection():
    subtitle = SimpleNamespace(
        content_type="single",
        release_info="English",
        page_link="https://example.test/chinese",
        filename=None,
        version=None,
        name=None,
    )

    assert not subtitle_has_bilingual_pair(subtitle, "zh", "en")


def test_provider_bilingual_metadata_preserves_traditional_chinese():
    subtitle = SimpleNamespace(
        content_type="bilingual",
        secondary_language="en",
        language=SimpleNamespace(basename="zh-TW"),
    )

    assert subtitle_has_bilingual_pair(subtitle, "zt", "en")


def test_provider_bilingual_metadata_can_be_normalized_to_requested_primary():
    subtitle = SimpleNamespace(
        content_type="bilingual",
        secondary_language="en",
        language=SimpleNamespace(basename="en"),
        release_info=None,
        page_link=None,
        filename=None,
        version=None,
        name=None,
    )

    assert subtitle_has_bilingual_pair(subtitle, "zh", "en")


def test_merge_subtitle_events_keeps_primary_first_and_matches_overlapping_cues():
    primary = [pysubs2.SSAEvent(start=1000, end=3000, text="你好")]
    secondary = [pysubs2.SSAEvent(start=1100, end=2900, text="Hello")]

    merged = merge_subtitle_events(primary, secondary)

    assert len(merged) == 1
    assert merged[0].start == 1000
    assert merged[0].end == 3000
    assert merged[0].text == "你好\nHello"


def test_merge_subtitle_events_does_not_drop_unmatched_secondary_cues():
    primary = [pysubs2.SSAEvent(start=1000, end=2000, text="你好")]
    secondary = [
        pysubs2.SSAEvent(start=1000, end=2000, text="Hello"),
        pysubs2.SSAEvent(start=5000, end=6000, text="Goodbye"),
    ]

    merged = merge_subtitle_events(primary, secondary)

    assert [event.text for event in merged] == ["你好\nHello", "Goodbye"]


def test_merge_subtitle_files_reads_common_chinese_encoding(tmp_path):
    primary_path = tmp_path / "primary.srt"
    secondary_path = tmp_path / "secondary.srt"
    output_path = tmp_path / "merged.srt"

    primary_path.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n你好\n\n",
        encoding="gb18030",
    )
    secondary_path.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nHello\n\n",
        encoding="utf-8",
    )

    merge_subtitle_files(str(primary_path), str(secondary_path), str(output_path))

    assert "你好" in output_path.read_text(encoding="utf-8")
    assert "Hello" in output_path.read_text(encoding="utf-8")


def test_merge_subtitle_files_rejects_unparseable_component(tmp_path):
    primary_path = tmp_path / "primary.srt"
    secondary_path = tmp_path / "secondary.srt"
    output_path = tmp_path / "merged.srt"

    primary_path.write_text("not a subtitle file", encoding="utf-8")
    secondary_path.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nHello\n\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Unable to parse subtitle file"):
        merge_subtitle_files(str(primary_path), str(secondary_path), str(output_path))
