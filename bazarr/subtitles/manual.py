# coding=utf-8
# fmt: off

import os
import sys
import logging
import subliminal

from subzero.language import Language
from subliminal_patch.core import save_subtitles
from subliminal_patch.core_persistent import list_all_subtitles, download_subtitles
from subliminal_patch.score import compute_score, DEFAULT_SCORES

from languages.get_languages import alpha2_from_alpha3, alpha3_from_alpha2
from app.config import settings, get_array_from
from utilities.helper import get_target_folder, force_unicode
from utilities.path_mappings import path_mappings
from app.database import (database, get_profiles_list, select, TableEpisodes, TableShows, get_audio_profile_languages,
                          get_profile_id, TableMovies)
from app.jobs_queue import jobs_queue
from app.notifier import send_notifications, send_notifications_movie
from sonarr.history import history_log
from radarr.history import history_log_movie
from subtitles.indexer.series import store_subtitles
from subtitles.indexer.movies import store_subtitles_movie
from subtitles.processing import ProcessSubtitlesResult
from subtitles.bilingual import save_bilingual_subtitle, subtitle_has_bilingual_pair
from subtitles.requirements import normalize_language_code, requirements_from_profile_items

from bazarr.subtitles.cache import subtitle_cache
from .pool import update_pools, _get_pool
from .utils import get_video, _get_lang_obj, _get_scores, _set_forced_providers
from .processing import process_subtitle


@update_pools
def manual_search(path, profile_id, providers, sceneName, title, media_type):
    logging.debug(f'BAZARR Manually searching subtitles for this file: {path}')

    final_subtitles = []

    pool = _get_pool(media_type, profile_id)

    language_set, original_format = _get_language_obj(profile_id=profile_id)
    profile = get_profiles_list(profile_id=int(profile_id))
    profile_requirements = requirements_from_profile_items(profile['items']) if profile else []
    bilingual_requirements = [x for x in profile_requirements if x.content_type == "bilingual"]
    also_forced = any([x.forced for x in language_set])
    forced_required = all([x.forced for x in language_set])
    normal = not also_forced and not forced_required and all([not x.hi for x in language_set])
    search_language_set = set(language_set)
    for requirement in bilingual_requirements:
        secondary_language = _get_lang_obj(alpha3_from_alpha2(requirement.secondary_language))
        if requirement.forced:
            secondary_language = Language.rebuild(secondary_language, forced=True)
        if requirement.hi:
            secondary_language = Language.rebuild(secondary_language, hi=True)
        search_language_set.add(secondary_language)
    _set_forced_providers(pool=pool, also_forced=also_forced, forced_required=forced_required)

    if providers:
        video = get_video(force_unicode(path), title, sceneName, providers=providers, media_type=media_type)
    else:
        logging.info("BAZARR All providers are throttled")
        return 'All providers are throttled'
    if video:
        try:
            if providers:
                if bilingual_requirements:
                    # ``list_all_subtitles`` subtracts languages already
                    # present in the video. That is correct for ordinary
                    # requirements, but two separate embedded tracks do not
                    # satisfy a bilingual requirement, so search both sides
                    # explicitly in this case.
                    subtitles = {video: pool.list_subtitles(video, search_language_set)}
                else:
                    subtitles = list_all_subtitles([video], language_set, pool)
            else:
                logging.info("BAZARR All providers are throttled")
                return 'All providers are throttled'
        except Exception as e:
            logging.exception(f"BAZARR Error trying to get Subtitle list from provider for this file {path}: {repr(e)}")
        else:
            subtitles_list = []
            minimum_score = settings.general.minimum_score
            minimum_score_movie = settings.general.minimum_score_movie
            score_handler = DEFAULT_SCORES['episode'] if media_type == "series" else DEFAULT_SCORES['movie']

            for s in subtitles[video]:
                bilingual_requirement = next(
                    (
                        requirement for requirement in bilingual_requirements
                        if subtitle_has_bilingual_pair(
                            s,
                            requirement.language,
                            requirement.secondary_language,
                        )
                    ),
                    None,
                )

                if (not normal or bilingual_requirements) and s.language not in language_set and not bilingual_requirement:
                    logging.debug(f"Skipping subtitle {s.language} because it's not requested")
                    continue

                try:
                    matches = s.matches if hasattr(s, 'matches') and isinstance(s.matches, set) and len(s.matches) \
                        else s.get_matches(video)
                    matches = {match for match in matches if match in score_handler.keys()}  # cleanup unwanted criterion
                except AttributeError:
                    continue

                # skip wrong season/episodes
                if media_type == "series":
                    can_verify_series = True
                    if not s.hash_verifiable and "hash" in matches:
                        can_verify_series = False

                    if can_verify_series and not {"series", "season", "episode"}.issubset(matches):
                        try:
                            logging.debug(f"BAZARR Skipping {s}, because it doesn't match our series/episode")
                        except TypeError:
                            logging.debug("BAZARR Ignoring invalid subtitles")
                        continue

                if s.hearing_impaired or normal:
                    matches.add('hearing_impaired')

                _, max_score, scores = _get_scores(media_type, minimum_score_movie, minimum_score)
                score, score_without_hash = compute_score(matches, s, video, hearing_impaired=s.hearing_impaired,)

                if 'hash' not in matches:
                    not_matched = scores - matches
                    not_matched = {match for match in not_matched if match in score_handler.keys()}
                    s.score = score_without_hash
                else:
                    matches = s.matches = {match for match in matches if match in ("hash", "hearing_impaired")}
                    s.score = score
                    not_matched = set()

                releases = []
                if hasattr(s, 'release_info'):
                    if s.release_info is not None:
                        for s_item in s.release_info.split(','):
                            if s_item.strip():
                                releases.append(s_item)

                if s.uploader and s.uploader.strip():
                    s_uploader = s.uploader.strip()
                else:
                    s_uploader = None

                if original_format in (1, "1", "True", True):
                    s.use_original_format = True

                if bilingual_requirement:
                    # Preserve the classification in the cache as well as in
                    # the response. The selected object is reused by the
                    # manual download endpoint, which must not have to infer
                    # the pair a second time after the provider search.
                    s.content_type = "bilingual"
                    s.secondary_language = bilingual_requirement.secondary_language

                subtitles_list.append(
                    dict(score=round((score / max_score * 100), 2),
                         orig_score=score,
                         score_without_hash=score_without_hash,
                         forced=str(s.language.forced),
                         language=(bilingual_requirement.language
                                   if bilingual_requirement else str(s.language.basename)),
                         hearing_impaired=str(s.hearing_impaired),
                         provider=s.provider_name,
                         subtitle=subtitle_cache.store(s),
                         url=s.page_link,
                         original_format=s.use_original_format,
                         matches=list(matches),
                         dont_matches=list(not_matched),
                         release_info=releases,
                         uploader=s_uploader,
                         content_type="bilingual" if bilingual_requirement else "single",
                         secondary_language=(bilingual_requirement.secondary_language
                                             if bilingual_requirement else None)))

            final_subtitles = sorted(subtitles_list, key=lambda x: (x['orig_score'], x['score_without_hash']),
                                     reverse=True)
            logging.debug(f'BAZARR {len(final_subtitles)} Subtitles have been found for this file: {path}')
            logging.debug(f'BAZARR Ended searching Subtitles for this file: {path}')

    subliminal.region.backend.sync()

    return final_subtitles


@update_pools
def manual_download_subtitle(path, audio_language, hi, forced, subtitle, provider, sceneName, title, media_type,
                             use_original_format, profile_id, job_id=None, content_type="single",
                             secondary_language=None, language=None):
    logging.debug(f'BAZARR Manually downloading Subtitles for this file: {path}')

    if settings.general.utf8_encode:
        os.environ["SZ_KEEP_ENCODING"] = ""
    else:
        os.environ["SZ_KEEP_ENCODING"] = "True"

    subtitle = subtitle_cache.get(subtitle)
    if subtitle is None:
        logging.error("BAZARR Subtitle not found in cache (expired or invalid ID)")
        return 'Subtitle not found in cache. Please search again.'
    if hi == 'True':
        subtitle.language.hi = True
    else:
        subtitle.language.hi = False
    if forced == 'True':
        subtitle.language.forced = True
    else:
        subtitle.language.forced = False
    if use_original_format in (1, "1", "True", True):
        subtitle.use_original_format = True

    subtitle.mods = get_array_from(settings.general.subzero_mods)
    video = get_video(force_unicode(path), title, sceneName, providers={provider}, media_type=media_type)
    if video:
        try:
            if provider:
                download_subtitles([subtitle], _get_pool(media_type, profile_id))
                logging.debug(f'BAZARR Subtitles file downloaded for this file: {path}')
            else:
                logging.info("BAZARR All providers are throttled")
                return 'All providers are throttled'
        except Exception:
            logging.exception(f'BAZARR Error downloading Subtitles for this file {path}')
            return 'Error downloading Subtitles'
        else:
            if not subtitle.is_valid():
                logging.error(f"BAZARR Downloaded subtitles isn't valid for this file: {path}")
                return "Downloaded subtitles isn't valid. Check log."
            try:
                chmod = int(settings.general.chmod, 8) if not sys.platform.startswith(
                    'win') and settings.general.chmod_enabled else None
                if content_type == "bilingual":
                    subtitle_primary = normalize_language_code(
                        getattr(subtitle.language, "basename", None)
                    )
                    # ``alpha2_from_alpha3('zho')`` is ``zh`` even when the
                    # subtitle carries a TW/Hant region. Preserve Bazarr's
                    # custom ``zt`` code for traditional Chinese results.
                    if subtitle_primary not in {"zh", "zt"}:
                        subtitle_primary = alpha2_from_alpha3(subtitle.language.alpha3)
                    requested_primary = normalize_language_code(language) or subtitle_primary
                    if not secondary_language or not subtitle_has_bilingual_pair(
                            subtitle,
                            requested_primary or str(subtitle.language.basename),
                            secondary_language):
                        return "Selected subtitle is not identified as a bilingual provider result."
                    normalized_primary = alpha3_from_alpha2(requested_primary)
                    if normalized_primary:
                        primary_language = _get_lang_obj(normalized_primary)
                        primary_language = Language.rebuild(primary_language, hi=hi, forced=forced)
                        subtitle.language = primary_language
                    subtitle.content_type = "bilingual"
                    subtitle.secondary_language = secondary_language
                    saved_path = save_bilingual_subtitle(
                        subtitle,
                        video.original_path,
                        requested_primary or str(subtitle.language.basename),
                        secondary_language,
                        directory=get_target_folder(path),
                        chmod=chmod,
                    )
                    saved_subtitles = [subtitle] if saved_path else []
                else:
                    saved_subtitles = save_subtitles(video.original_path, [subtitle],
                                                     single=settings.general.single_language,
                                                     tags=None,  # fixme
                                                     directory=get_target_folder(path),
                                                     chmod=chmod,
                                                     formats=(subtitle.format,),
                                                     path_decoder=force_unicode)
            except Exception as e:
                logging.exception(f'BAZARR Error saving Subtitles file to disk for this file {path}: {repr(e)}')
                return 'Error saving Subtitles file to disk'
            else:
                if saved_subtitles:
                    _, max_score, _ = _get_scores(media_type)
                    for saved_subtitle in saved_subtitles:
                        processed_subtitle = process_subtitle(subtitle=saved_subtitle, media_type=media_type,
                                                              audio_language=audio_language, is_upgrade=False,
                                                              is_manual=True, path=path, max_score=max_score,
                                                              job_id=job_id, content_type=content_type,
                                                              secondary_language=secondary_language,
                                                              primary_language=requested_primary
                                                              if content_type == "bilingual" else None)
                        if processed_subtitle:
                            return processed_subtitle
                        else:
                            logging.debug(f"BAZARR unable to process this subtitles: {subtitle}")
                            continue
                else:
                    logging.error(
                        f"BAZARR Tried to manually download a Subtitles for file: {path} but we weren't able to do "
                        f"(probably throttled by {subtitle.provider_name}. Please retry later or select a Subtitles "
                        f"from another provider.")
                    return 'Something went wrong, check the logs for error'

    subliminal.region.backend.sync()

    logging.debug(f'BAZARR Ended manually downloading Subtitles for file: {path}')


def episode_manually_download_specific_subtitle(sonarr_series_id, sonarr_episode_id, hi, forced, use_original_format,
                                                selected_provider, subtitle, job_id=None, content_type="single",
                                                secondary_language=None, language=None):
    if not job_id:
        return jobs_queue.add_job_from_function("Manually downloading Subtitles",is_progress=False)

    episodeInfo = database.execute(
        select(
            TableEpisodes.audio_language,
            TableEpisodes.path,
            TableEpisodes.sceneName,
            TableEpisodes.season,
            TableEpisodes.episode,
            TableEpisodes.title.label('episodeTitle'),
            TableShows.title)
        .select_from(TableEpisodes)
        .join(TableShows)
        .where(TableEpisodes.sonarrEpisodeId == sonarr_episode_id)) \
        .first()

    if not episodeInfo:
        return 'Episode not found', 404

    title = episodeInfo.title
    jobs_queue.update_job_name(job_id=job_id, new_job_name=f"Manually downloading Subtitles for {title} - "
                                                           f"S{episodeInfo.season:02d}E{episodeInfo.episode:02d} - "
                                                           f"{episodeInfo.episodeTitle}")
    episodePath = path_mappings.path_replace(episodeInfo.path)
    sceneName = episodeInfo.sceneName or "None"

    audio_language_list = get_audio_profile_languages(episodeInfo.audio_language)
    if len(audio_language_list) > 0:
        audio_language = audio_language_list[0]['name']
    else:
        audio_language = 'None'

    try:
        result = manual_download_subtitle(episodePath, audio_language, hi, forced, subtitle, selected_provider,
                                          sceneName, title, 'series', use_original_format,
                                          profile_id=get_profile_id(episode_id=sonarr_episode_id), job_id=job_id,
                                          content_type=content_type, secondary_language=secondary_language,
                                          language=language)
    except OSError:
        return 'Unable to save subtitles file', 500
    else:
        if isinstance(result, tuple) and len(result):
            result = result[0]
        if isinstance(result, str):
            return result, 500
        elif result:
            store_subtitles(sonarr_episode_id)
            history_log(2, sonarr_series_id, sonarr_episode_id, result)
            if not settings.general.dont_notify_manual_actions:
                send_notifications(sonarr_series_id, sonarr_episode_id, result.message)
            return '', 204
    finally:
        jobs_queue.update_job_name(job_id=job_id, new_job_name=f"Manually downloaded Subtitles for {title} - "
                                                               f"S{episodeInfo.season:02d}E{episodeInfo.episode:02d} - "
                                                               f"{episodeInfo.episodeTitle}")


def movie_manually_download_specific_subtitle(radarr_id, hi, forced, use_original_format, selected_provider, subtitle,
                                              job_id=None, content_type="single", secondary_language=None, language=None):
    if not job_id:
        return jobs_queue.add_job_from_function("Manually downloading Subtitles", is_progress=False)

    movieInfo = database.execute(
        select(TableMovies.title,
               TableMovies.year,
               TableMovies.path,
               TableMovies.sceneName,
               TableMovies.audio_language)
        .where(TableMovies.radarrId == radarr_id)) \
        .first()

    if not movieInfo:
        return 'Movie not found', 404

    title = movieInfo.title
    jobs_queue.update_job_name(job_id=job_id, new_job_name=f"Manually downloading Subtitles for {title} "
                                                           f"({movieInfo.year})")
    moviePath = path_mappings.path_replace_movie(movieInfo.path)
    sceneName = movieInfo.sceneName or "None"

    audio_language_list = get_audio_profile_languages(movieInfo.audio_language)
    if len(audio_language_list) > 0:
        audio_language = audio_language_list[0]['name']
    else:
        audio_language = 'None'

    try:
        result = manual_download_subtitle(moviePath, audio_language, hi, forced, subtitle, selected_provider,
                                          sceneName, title, 'movie', use_original_format,
                                          profile_id=get_profile_id(movie_id=radarr_id), job_id=job_id,
                                          content_type=content_type, secondary_language=secondary_language,
                                          language=language)
    except OSError:
        return 'Unable to save subtitles file', 500
    else:
        if isinstance(result, tuple) and len(result):
            result = result[0]
        if isinstance(result, str):
            return result, 500
        elif result:
            store_subtitles_movie(radarr_id)
            history_log_movie(2, radarr_id, result)
            if not settings.general.dont_notify_manual_actions:
                send_notifications_movie(radarr_id, result.message)
            return '', 204
    finally:
        jobs_queue.update_job_name(job_id=job_id, new_job_name=f"Manually downloaded Subtitles for {title} "
                                                               f"({movieInfo.year})")


def _get_language_obj(profile_id):
    language_set = set()

    profile = get_profiles_list(profile_id=int(profile_id))
    language_items = profile['items']
    original_format = profile['originalFormat']

    for language in language_items:
        forced = language['forced']
        hi = language['hi']
        language = language['language']

        lang = alpha3_from_alpha2(language)

        lang_obj = _get_lang_obj(lang)

        if forced == "True":
            lang_obj = Language.rebuild(lang_obj, forced=True)

        if hi == "True":
            lang_obj = Language.rebuild(lang_obj, hi=True)

        language_set.add(lang_obj)

    return language_set, original_format
