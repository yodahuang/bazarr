# coding=utf-8
# fmt: off

import os
import sys
import logging
import subliminal
import ast
import subprocess
import tempfile

from subzero.language import Language
from subliminal_patch.core import save_subtitles
from subliminal_patch.core_persistent import download_best_subtitles

from app.config import settings, get_array_from
from app.database import TableEpisodes, TableMovies, database, select, get_profiles_list, get_subtitles
from utilities.path_mappings import path_mappings
from utilities.helper import get_target_folder, force_unicode
from utilities.binaries import get_binary
from languages.get_languages import alpha3_from_alpha2

from .pool import update_pools, _get_pool
from .utils import get_video, _get_lang_obj, _get_scores, _set_forced_providers
from .processing import process_subtitle
from .requirements import (
    CONTENT_TYPE_BILINGUAL,
    CONTENT_TYPE_SINGLE,
    SubtitleRequirement,
    artifact_satisfies_requirement,
    requirement_from_profile_item,
    requirement_from_token,
)
from .bilingual import (
    GeneratedBilingualSubtitle,
    bilingual_subtitle_path,
    merge_subtitle_files,
    save_bilingual_subtitle,
    subtitle_srt_content,
    subtitle_has_bilingual_pair,
)


@update_pools
def generate_subtitles(path, languages, audio_language, sceneName, title, media_type, profile_id,
                       forced_minimum_score=None, is_upgrade=False, check_if_still_required=False,
                       previous_subtitles_to_delete=None, job_id=None, fallback_allowed=False):
    requirements = [_coerce_requirement(language) for language in (languages or [])]
    bilingual_requirements = [x for x in requirements if x.content_type == CONTENT_TYPE_BILINGUAL]
    if bilingual_requirements:
        for requirement in bilingual_requirements:
            yield from generate_bilingual_subtitles(
                path,
                requirement,
                audio_language,
                sceneName,
                title,
                media_type,
                profile_id,
                forced_minimum_score=forced_minimum_score,
                is_upgrade=is_upgrade,
                check_if_still_required=check_if_still_required,
                previous_subtitles_to_delete=previous_subtitles_to_delete,
                job_id=job_id,
                fallback_allowed=fallback_allowed,
            )

        requirements = [x for x in requirements if x.content_type == CONTENT_TYPE_SINGLE]

    # The historical provider downloader still accepts three-value tuples.
    # Normalize every ordinary requirement here, including calls that contain
    # no bilingual item, so the new canonical representation is safe at this
    # boundary as well.
    languages = [(x.language, "True" if x.hi else "False", "True" if x.forced else "False")
                 for x in requirements]

    if not languages:
        return None

    logging.debug(f'BAZARR Searching subtitles for this file: {path}')

    if settings.general.utf8_encode:
        os.environ["SZ_KEEP_ENCODING"] = ""
    else:
        os.environ["SZ_KEEP_ENCODING"] = "True"

    pool = _get_pool(media_type, profile_id)
    providers = pool.providers

    language_set = _get_language_obj(languages=languages)
    profile = get_profiles_list(profile_id=profile_id)
    original_format = profile['originalFormat']
    hi_required = "force HI" if all([x.hi for x in language_set]) else "don't prefer"
    also_forced = any([x.forced for x in language_set])
    forced_required = all([x.forced for x in language_set])
    _set_forced_providers(pool=pool, also_forced=also_forced, forced_required=forced_required)

    try:
        video = get_video(force_unicode(path), title, sceneName, providers=providers, media_type=media_type)
    except ValueError as e:
        logging.exception(f'BAZARR Unable to get video object for {path}: {e}')
        return None

    if video:
        minimum_score = settings.general.minimum_score
        minimum_score_movie = settings.general.minimum_score_movie
        min_score, max_score, scores = _get_scores(media_type, minimum_score_movie, minimum_score)

        subz_mods = get_array_from(settings.general.subzero_mods)
        saved_any = False

        if providers:
            if forced_minimum_score:
                min_score = int(forced_minimum_score) + 1
            for language in language_set:
                # confirm if language is still missing or if cutoff has been reached
                if check_if_still_required and language not in check_missing_languages(path, media_type):
                    # cutoff has been reached
                    logging.debug(f"BAZARR this language ({parse_language_object(language)}) is ignored because cutoff "
                                  f"has been reached during this search.")
                    continue
                else:
                    try:
                        downloaded_subtitles = download_best_subtitles(videos={video},
                                                                       languages={language},
                                                                       pool_instance=pool,
                                                                       min_score=int(min_score),
                                                                       hearing_impaired=hi_required,
                                                                       use_original_format=original_format in (1, "1", "True", True),
                                                                       fallback_allowed=fallback_allowed)
                    except Exception as e:
                        logging.exception(f'BAZARR Error downloading Subtitles for this file {path}: {repr(e)}')
                        return None

                if downloaded_subtitles:
                    for video, subtitles in downloaded_subtitles.items():
                        if not subtitles:
                            continue

                        subtitle_formats = set()
                        for s in subtitles:
                            s.mods = subz_mods
                            subtitle_formats.add(s.format)

                        try:
                            fld = get_target_folder(path)
                            chmod = int(settings.general.chmod, 8) if not sys.platform.startswith(
                                'win') and settings.general.chmod_enabled else None
                            if is_upgrade and previous_subtitles_to_delete:
                                try:
                                    # delete previously downloaded subtitles in case of an upgrade to prevent edge loop
                                    # issue.
                                    os.remove(previous_subtitles_to_delete)
                                except (OSError, FileNotFoundError):
                                    pass
                            saved_subtitles = save_subtitles(video.original_path, subtitles,
                                                             single=settings.general.single_language,
                                                             tags=None,  # fixme
                                                             directory=fld,
                                                             chmod=chmod,
                                                             formats=subtitle_formats,
                                                             path_decoder=force_unicode
                                                             )
                        except Exception as e:
                            logging.exception(
                                f'BAZARR Error saving Subtitles file to disk for this file {path}: {repr(e)}')
                            pass
                        else:
                            saved_any = True
                            for subtitle in saved_subtitles:
                                if "hash" in subtitle.matches:
                                    # make matches set cleaner for history purpose when hash matches
                                    subtitle.matches = {match for match in subtitle.matches
                                                        if match in ("hash", "hearing_impaired")}
                                processed_subtitle = process_subtitle(subtitle=subtitle, media_type=media_type,
                                                                      audio_language=audio_language,
                                                                      is_upgrade=is_upgrade, is_manual=False,
                                                                      path=path, max_score=max_score, job_id=job_id)
                                if not processed_subtitle:
                                    logging.debug(f"BAZARR unable to process this subtitles: {subtitle}")
                                    continue
                                yield processed_subtitle
        else:
            logging.info("BAZARR All providers are throttled")
            return None

        if not saved_any:
            logging.debug(f'BAZARR No Subtitles were found for this file: {path}')
            return None

    subliminal.region.backend.sync()

    logging.debug(f'BAZARR Ended searching Subtitles for file: {path}')


def _coerce_requirement(value):
    if isinstance(value, SubtitleRequirement):
        return value
    if isinstance(value, str):
        return requirement_from_token(value)
    if isinstance(value, dict):
        return requirement_from_profile_item(value)
    if isinstance(value, (tuple, list)) and len(value) >= 3:
        return SubtitleRequirement(
            language=value[0],
            hi=value[1],
            forced=value[2],
            content_type=value[3] if len(value) > 3 else CONTENT_TYPE_SINGLE,
            secondary_language=value[4] if len(value) > 4 else None,
        )
    raise ValueError(f"Unsupported subtitle requirement: {value!r}")


def _language_object_for_requirement(requirement):
    language = _get_lang_obj(alpha3_from_alpha2(requirement.language))
    if requirement.forced:
        language = Language.rebuild(language, forced=True)
    if requirement.hi:
        language = Language.rebuild(language, hi=True)
    return language


def _download_provider_subtitle(video, pool, requirement, min_score, original_format, fallback_allowed=False):
    """Download one component without saving it as a normal sidecar."""

    language = _language_object_for_requirement(requirement)
    try:
        candidates = pool.list_subtitles(video, {language})
        candidates = candidates or []
        for candidate in candidates:
            candidate.mods = get_array_from(settings.general.subzero_mods)
        downloaded = pool.download_best_subtitles(
            candidates,
            video,
            {language},
            min_score=int(min_score),
            hearing_impaired="force HI" if requirement.hi else "don't prefer",
            only_one=True,
            use_original_format=original_format in (1, "1", "True", True),
            fallback_allowed=fallback_allowed,
        )
    except Exception as error:
        logging.exception(f"BAZARR Error downloading a bilingual component: {repr(error)}")
        return None
    return downloaded[0] if downloaded else None


def _download_provider_bilingual_subtitle(video, pool, requirement, min_score, original_format,
                                          fallback_allowed=False):
    """Try provider-labelled bilingual results before creating a merged file."""

    language = _language_object_for_requirement(requirement)
    secondary_language = _language_object_for_requirement(
        SubtitleRequirement(
            requirement.secondary_language,
            forced=requirement.forced,
            hi=requirement.hi,
        )
    )
    try:
        # Some providers expose a pair under the secondary side's language
        # (for example English) even though the user requested Chinese first.
        # Search both sides, then normalize accepted candidates back to the
        # profile's primary language before downloading.
        candidates = pool.list_subtitles(video, {language, secondary_language}) or []
        bilingual_candidates = [
            candidate for candidate in candidates
            if subtitle_has_bilingual_pair(candidate, requirement.language, requirement.secondary_language)
        ]
        for candidate in bilingual_candidates:
            candidate.mods = get_array_from(settings.general.subzero_mods)
            # The provider may expose the same pair with English as the
            # object's language. Normalize it before the shared downloader's
            # language filter and before processing/history see the result.
            candidate.language = Language.rebuild(language)

        downloaded = pool.download_best_subtitles(
            bilingual_candidates,
            video,
            {language},
            min_score=int(min_score),
            hearing_impaired="force HI" if requirement.hi else "don't prefer",
            only_one=True,
            use_original_format=original_format in (1, "1", "True", True),
            fallback_allowed=fallback_allowed,
        )
    except Exception as error:
        logging.exception(f"BAZARR Error looking for provider bilingual subtitles: {repr(error)}")
        return None
    return downloaded[0] if downloaded else None


def _write_component_to_temp(subtitle, directory, prefix):
    content = subtitle_srt_content(subtitle)
    if not content:
        return None
    path = os.path.join(directory, f"{prefix}.srt")
    with open(path, "wb") as output_file:
        output_file.write(content)
    return path


def _remove_previous_upgrade(previous_path, new_path):
    if not previous_path or not new_path:
        return
    try:
        if os.path.abspath(previous_path) == os.path.abspath(new_path):
            return
        os.remove(previous_path)
    except (OSError, FileNotFoundError):
        pass


def _indexed_subtitles_for_path(path, media_type):
    if media_type == "series":
        item = database.execute(
            select(TableEpisodes.sonarrEpisodeId)
            .where(TableEpisodes.path == path_mappings.path_replace_reverse(path))
        ).first()
        return get_subtitles(sonarr_episode_id=item.sonarrEpisodeId) if item else []

    item = database.execute(
        select(TableMovies.radarrId)
        .where(TableMovies.path == path_mappings.path_replace_reverse_movie(path))
    ).first()
    return get_subtitles(radarr_id=item.radarrId) if item else []


def _extract_embedded_component(path, track_id, directory, prefix):
    output_path = os.path.join(directory, f"{prefix}.srt")
    try:
        subprocess.run(
            [get_binary("ffmpeg"), "-v", "error", "-y", "-i", path,
             "-map", f"0:{track_id}", "-f", "srt", output_path],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=600,
        )
    except (OSError, subprocess.SubprocessError) as error:
        logging.warning(f"BAZARR unable to extract embedded subtitle track {track_id}: {error}")
        return None
    return output_path if os.path.isfile(output_path) else None


def _find_indexed_component(path, media_type, requirement, directory, prefix):
    artifacts = _indexed_subtitles_for_path(path, media_type)
    for artifact in artifacts:
        if not artifact_satisfies_requirement(artifact, requirement):
            continue
        if artifact.get("embedded_track_id") is not None and not settings.general.use_embedded_subs:
            continue
        if artifact.get("path") and os.path.isfile(artifact["path"]):
            return artifact["path"], 100
        if artifact.get("embedded_track_id") is not None:
            extracted = _extract_embedded_component(
                path,
                artifact["embedded_track_id"],
                directory,
                prefix,
            )
            if extracted:
                return extracted, 100
    return None, None


def _missing_requirements(path, media_type):
    if media_type == "series":
        row = database.execute(
            select(TableEpisodes.missing_subtitles)
            .where(TableEpisodes.path == path_mappings.path_replace_reverse(path))
        ).first()
    else:
        row = database.execute(
            select(TableMovies.missing_subtitles)
            .where(TableMovies.path == path_mappings.path_replace_reverse_movie(path))
        ).first()

    if not row or not row.missing_subtitles:
        return []
    return [requirement_from_token(token) for token in ast.literal_eval(row.missing_subtitles) if token is not None]


@update_pools
def generate_bilingual_subtitles(path, requirement, audio_language, sceneName, title, media_type, profile_id,
                                 forced_minimum_score=None, is_upgrade=False, check_if_still_required=False,
                                 previous_subtitles_to_delete=None, job_id=None, fallback_allowed=False):
    requirement = _coerce_requirement(requirement)
    if requirement.content_type != CONTENT_TYPE_BILINGUAL:
        return None

    if check_if_still_required and requirement not in _missing_requirements(path, media_type):
        logging.debug(f"BAZARR bilingual requirement {requirement.token} is no longer missing")
        return None

    logging.debug(f"BAZARR Searching bilingual subtitles for this file: {path}: {requirement.token}")
    pool = _get_pool(media_type, profile_id)

    profile = get_profiles_list(profile_id=profile_id)
    original_format = profile["originalFormat"] if profile else False
    minimum_score = settings.general.minimum_score
    minimum_score_movie = settings.general.minimum_score_movie
    min_score, max_score, _ = _get_scores(media_type, minimum_score_movie, minimum_score)
    if forced_minimum_score:
        min_score = int(forced_minimum_score) + 1

    video = None
    if pool.providers:
        _set_forced_providers(pool=pool, also_forced=requirement.forced, forced_required=requirement.forced)
        try:
            video = get_video(force_unicode(path), title, sceneName, providers=pool.providers, media_type=media_type)
        except ValueError as error:
            logging.exception(f"BAZARR Unable to get video object for {path}: {error}")

        if video:
            direct_subtitle = _download_provider_bilingual_subtitle(
                video,
                pool,
                requirement,
                min_score,
                original_format,
                fallback_allowed=fallback_allowed,
            )
            if direct_subtitle:
                direct_subtitle.content_type = CONTENT_TYPE_BILINGUAL
                direct_subtitle.secondary_language = requirement.secondary_language
                try:
                    saved_path = save_bilingual_subtitle(
                        direct_subtitle,
                        video.original_path,
                        requirement.language,
                        requirement.secondary_language,
                        directory=get_target_folder(path),
                        chmod=int(settings.general.chmod, 8) if not sys.platform.startswith("win") and
                        settings.general.chmod_enabled else None,
                    )
                except Exception as error:
                    logging.exception(f"BAZARR Error saving provider bilingual subtitles: {repr(error)}")
                    saved_path = None
                if saved_path:
                    processed = process_subtitle(
                        subtitle=direct_subtitle,
                        media_type=media_type,
                        audio_language=audio_language,
                        is_upgrade=is_upgrade,
                        is_manual=False,
                        path=path,
                        max_score=max_score,
                        job_id=job_id,
                        content_type=CONTENT_TYPE_BILINGUAL,
                        secondary_language=requirement.secondary_language,
                        primary_language=requirement.language,
                    )
                    if processed:
                        _remove_previous_upgrade(previous_subtitles_to_delete, saved_path)
                        yield processed
                    return
    else:
        logging.info("BAZARR All providers are throttled; checking indexed subtitle tracks for bilingual fallback")

    with tempfile.TemporaryDirectory(prefix="bazarr-bilingual-") as temp_directory:
        primary_requirement = SubtitleRequirement(
            requirement.language,
            forced=requirement.forced,
            hi=requirement.hi,
        )
        secondary_requirement = SubtitleRequirement(
            requirement.secondary_language,
            forced=requirement.forced,
            hi=requirement.hi,
        )
        primary_path, primary_score = _find_indexed_component(
            path, media_type, primary_requirement, temp_directory, "primary-embedded"
        )
        secondary_path, secondary_score = _find_indexed_component(
            path, media_type, secondary_requirement, temp_directory, "secondary-embedded"
        )

        provider_components = (
            (
                ("primary", primary_requirement, "primary-provider"),
                ("secondary", secondary_requirement, "secondary-provider"),
            )
            if video else ()
        )
        for component, component_requirement, prefix in provider_components:
            if (component == "primary" and primary_path) or (component == "secondary" and secondary_path):
                continue

            downloaded = _download_provider_subtitle(
                video,
                pool,
                component_requirement,
                min_score,
                original_format,
                fallback_allowed=fallback_allowed,
            )
            if not downloaded:
                continue

            downloaded_pair = subtitle_has_bilingual_pair(
                downloaded,
                requirement.language,
                requirement.secondary_language,
            )
            if downloaded_pair:
                downloaded.language = _language_object_for_requirement(requirement)
                downloaded.content_type = CONTENT_TYPE_BILINGUAL
                downloaded.secondary_language = requirement.secondary_language
                saved_path = save_bilingual_subtitle(
                    downloaded,
                    video.original_path,
                    requirement.language,
                    requirement.secondary_language,
                    directory=get_target_folder(path),
                    chmod=int(settings.general.chmod, 8) if not sys.platform.startswith("win") and
                    settings.general.chmod_enabled else None,
                )
                if saved_path:
                    processed = process_subtitle(
                        subtitle=downloaded,
                        media_type=media_type,
                        audio_language=audio_language,
                        is_upgrade=is_upgrade,
                        is_manual=False,
                        path=path,
                        max_score=max_score,
                        job_id=job_id,
                        content_type=CONTENT_TYPE_BILINGUAL,
                        secondary_language=requirement.secondary_language,
                        primary_language=requirement.language,
                    )
                    if processed:
                        _remove_previous_upgrade(previous_subtitles_to_delete, saved_path)
                        yield processed
                    return

            component_path = _write_component_to_temp(downloaded, temp_directory, prefix)
            if component_path:
                if component == "primary":
                    primary_path, primary_score = component_path, getattr(downloaded, "score", 0)
                else:
                    secondary_path, secondary_score = component_path, getattr(downloaded, "score", 0)

        if not primary_path or not secondary_path:
            logging.debug(f"BAZARR Unable to build bilingual subtitles; missing {requirement.token} components")
            return None

        output_path = bilingual_subtitle_path(
            video.original_path if video else path,
            requirement.language,
            requirement.secondary_language,
            forced=requirement.forced,
            hi=requirement.hi,
            directory=get_target_folder(path),
        )
        try:
            chmod = int(settings.general.chmod, 8) if not sys.platform.startswith("win") and \
                settings.general.chmod_enabled else None
            merge_subtitle_files(primary_path, secondary_path, output_path, chmod=chmod)
        except (OSError, ValueError) as error:
            logging.warning(f"BAZARR Unable to merge bilingual subtitles for {path}: {error}")
            return None

        generated = GeneratedBilingualSubtitle(
            _language_object_for_requirement(requirement),
            requirement.secondary_language,
            output_path,
            score=min(
                primary_score if primary_score is not None else max_score,
                secondary_score if secondary_score is not None else max_score,
            ),
        )
        processed = process_subtitle(
            subtitle=generated,
            media_type=media_type,
            audio_language=audio_language,
            is_upgrade=is_upgrade,
            is_manual=False,
            path=path,
            max_score=max_score,
            job_id=job_id,
            content_type=CONTENT_TYPE_BILINGUAL,
            secondary_language=requirement.secondary_language,
            primary_language=requirement.language,
        )
        if processed:
            _remove_previous_upgrade(previous_subtitles_to_delete, output_path)
            yield processed


def _get_language_obj(languages):
    language_set = set()

    if not isinstance(languages, (set, list)):
        languages = [languages]

    for language in languages:
        lang, hi_item, forced_item = language

        # Always use alpha2 in API Request
        lang = alpha3_from_alpha2(lang)

        lang_obj = _get_lang_obj(lang)

        if forced_item == "True":
            lang_obj = Language.rebuild(lang_obj, forced=True)
        if hi_item == "True":
            lang_obj = Language.rebuild(lang_obj, hi=True)

        language_set.add(lang_obj)

    return language_set


def parse_language_object(language):
    if isinstance(language, Language):
        hi = ":hi" if language.hi else ""
        forced = ":forced" if language.forced else ""
        return language.basename + hi + forced
    else:
        return language


def check_missing_languages(path, media_type):
    requirements = _missing_requirements(path, media_type)
    return _get_language_obj(
        languages=[
            (requirement.language, "True" if requirement.hi else "False",
             "True" if requirement.forced else "False")
            for requirement in requirements
            if requirement.content_type == CONTENT_TYPE_SINGLE
        ]
    )
