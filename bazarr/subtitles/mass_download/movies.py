# coding=utf-8
# fmt: off

import ast
import logging
import operator
import os

from functools import reduce

from utilities.path_mappings import path_mappings
from subtitles.indexer.movies import store_subtitles_movie, list_missing_subtitles_movies
from radarr.history import history_log_movie
from app.notifier import send_notifications_movie
from app.get_providers import get_providers
from app.database import (get_exclusion_clause, get_audio_profile_languages, TableMovies, database, select,
                          get_profile_id, get_subtitles)
from app.jobs_queue import jobs_queue
from app.event_handler import event_stream

from ..download import generate_subtitles
from ..requirements import requirement_from_token, format_requirement_token


def movies_download_subtitles(no, job_id=None, job_sub_function=False):
    if not job_sub_function and not job_id:
        jobs_queue.add_job_from_function(f"""Downloading missing subtitles for """
                                         f"""{database.scalar(select(TableMovies.title)
                                                              .where(TableMovies.radarrId == no))}"""
                                         f""" ({database.scalar(select(TableMovies.year)
                                                                .where(TableMovies.radarrId == no))})""",
                                         is_progress=True)
        return

    conditions = [(TableMovies.radarrId == no)]
    conditions += get_exclusion_clause('movie')
    stmt = select(TableMovies.path,
                  TableMovies.missing_subtitles,
                  TableMovies.audio_language,
                  TableMovies.radarrId,
                  TableMovies.sceneName,
                  TableMovies.title,
                  TableMovies.year,
                  TableMovies.tags,
                  TableMovies.monitored,
                  TableMovies.profileId) \
        .where(reduce(operator.and_, conditions))
    movie = database.execute(stmt).first()

    if not movie:
        logging.debug(f"BAZARR no movie with that radarrId can be found in database: {no}")
        jobs_queue.update_job_progress(job_id=job_id, progress_message="Movie not found in database.")
        return

    previously_indexed_subtitles = get_subtitles(radarr_id=movie.radarrId)

    if not len(previously_indexed_subtitles) or \
            any([x['embedded_track_id'] is None for x in previously_indexed_subtitles if not x['path']]):
        # subtitles indexing for this movie might be incomplete, we'll do it again
        store_subtitles_movie(no)
        movie = database.execute(stmt).first()
    elif movie.missing_subtitles is None:
        # missing subtitles calculation for this movie is incomplete, we'll do it again
        list_missing_subtitles_movies(no=no)
        movie = database.execute(stmt).first()

    moviePath = path_mappings.path_replace_movie(movie.path)

    if not os.path.exists(moviePath):
        logging.debug(f"BAZARR movie file not found. Path mapping issue?: {moviePath}")
        jobs_queue.update_job_progress(job_id=job_id, progress_message=f"Movie path doesn't exists: {moviePath}")
        raise OSError

    missing_subtitles = ast.literal_eval(movie.missing_subtitles or '[]')
    if missing_subtitles:
        count_movie = len(missing_subtitles)
    else:
        count_movie = 0

    audio_language_list = get_audio_profile_languages(movie.audio_language)
    if len(audio_language_list) > 0:
        audio_language = audio_language_list[0]['name']
    else:
        audio_language = 'None'

    jobs_queue.update_job_progress(job_id=job_id, progress_max=count_movie, progress_message=movie.title)

    providers_list = get_providers()
    languages = [
        requirement_from_token(language)
        for language in missing_subtitles
        if language is not None
    ]
    can_search = bool(providers_list) or any(
        language.content_type == 'bilingual' for language in languages
    )

    downloaded_count = 0
    if can_search:
        for result in generate_subtitles(moviePath,
                                         languages,
                                         audio_language,
                                         str(movie.sceneName),
                                         movie.title,
                                         'movie',
                                         movie.profileId,
                                         check_if_still_required=True,
                                         job_id=job_id):
            if result:
                if isinstance(result, tuple) and len(result):
                    result = result[0]
                store_subtitles_movie(no)
                history_log_movie(1, no, result)
                send_notifications_movie(no, result.message)
                downloaded_count += 1
        outcome_msg = (f"{downloaded_count} subtitle(s) downloaded"
                       if downloaded_count else "No subtitles found")
    else:
        logging.info("BAZARR All providers are throttled")
        outcome_msg = "All providers throttled"

    jobs_queue.update_job_progress(job_id=job_id, progress_value="max",
                                   progress_message=outcome_msg)
    jobs_queue.update_job_name(job_id=job_id, new_job_name=f"Downloaded missing subtitles for {movie.title} ({movie.year})")


def movie_download_specific_subtitles(radarr_id, language, hi, forced, job_id=None,
                                      content_type="single", secondary_language=None):
    if not job_id:
        return jobs_queue.add_job_from_function("Searching subtitles", progress_max=1, is_progress=False)

    movieInfo = database.execute(
        select(
            TableMovies.title,
            TableMovies.path,
            TableMovies.sceneName,
            TableMovies.audio_language)
        .where(TableMovies.radarrId == radarr_id)) \
        .first()

    if not movieInfo:
        return 'Movie not found', 404

    moviePath = path_mappings.path_replace_movie(movieInfo.path)

    if not os.path.exists(moviePath):
        return 'Movie file not found. Path mapping issue?', 500

    sceneName = movieInfo.sceneName or 'None'

    title = movieInfo.title

    requirement_token = f"{language}:bilingual={secondary_language}" if content_type == "bilingual" else language
    if forced == "True":
        requirement_token += ":forced"
    elif hi == "True":
        requirement_token += ":hi"
    requirement = requirement_from_token(requirement_token)
    language_str = format_requirement_token(requirement)

    jobs_queue.update_job_name(job_id=job_id, new_job_name=f"Searching {language_str.upper()} for {title}")

    audio_language_list = get_audio_profile_languages(movieInfo.audio_language)
    if len(audio_language_list) > 0:
        audio_language = audio_language_list[0]['name']
    else:
        audio_language = None

    try:
        result = list(generate_subtitles(moviePath, [requirement], audio_language,
                                         sceneName, title, 'movie', profile_id=get_profile_id(movie_id=radarr_id),
                                         job_id=job_id))
        if isinstance(result, list) and len(result):
            result = result[0]
            if isinstance(result, tuple) and len(result):
                result = result[0]
            store_subtitles_movie(radarr_id)
            history_log_movie(1, radarr_id, result)
            send_notifications_movie(radarr_id, result.message)
        else:
            event_stream(type='movie', payload=radarr_id)
            return '', 204
    except OSError:
        return 'Unable to save subtitles file. Permission or path mapping issue?', 409
    else:
        jobs_queue.update_job_name(job_id=job_id, new_job_name=f"Searched {language_str.upper()} for {title}")
        return '', 204
