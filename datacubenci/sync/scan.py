import dawg
import logging
import time
from itertools import chain
from pathlib import Path
from typing import Iterable, Any, Mapping

import structlog
from boltons import fileutils
from boltons import strutils

from datacube.index._api import Index
from datacube.utils import uri_to_local_path, InvalidDocException
from datacubenci import paths
from datacubenci.collections import Collection
from .differences import ArchivedDatasetOnDisk, Mismatch, LocationMissingOnDisk, LocationNotIndexed, \
    DatasetNotIndexed
from .index import DatasetPathIndex, DatasetLite, AgdcDatasetPathIndex

_LOG = structlog.get_logger()

# 12 hours (roughly the same workday)
CACHE_TIMEOUT_SECS = 60 * 60 * 12


def cache_is_too_old(path):
    if not path.exists():
        return True

    oldest_valid_time = time.time() - CACHE_TIMEOUT_SECS
    return path.stat().st_mtime < oldest_valid_time


def _build_pathset(
        log: logging.Logger,
        path_search_root: Path,
        path_offset_glob: str,
        path_index: DatasetPathIndex,
        cache_path: Path = None) -> dawg.CompletionDAWG:
    """
    Build a combined set (in dawg form) of all dataset paths in the given index and filesystem.

    Optionally use the given cache directory to cache repeated builds.
    """
    locations_cache = cache_path.joinpath('locations.dawg') if cache_path else None
    if locations_cache and not cache_is_too_old(locations_cache):
        path_set = dawg.CompletionDAWG()
        log.debug("paths.trie.cache.load", file=locations_cache)
        path_set.load(str(locations_cache))
    else:
        log.info("paths.trie.build")
        path_set = dawg.CompletionDAWG(
            chain(
                path_index.iter_all_uris(),
                (path.absolute().as_uri() for path in path_search_root.glob(path_offset_glob))
            )
        )
        log.info("paths.trie.done")
        if locations_cache is not None:
            log.debug("paths.trie.cache.create", file=locations_cache)
            with fileutils.atomic_save(str(locations_cache)) as f:
                path_set.write(f)
    return path_set


def find_index_disk_mismatches(log,
                               path_index: DatasetPathIndex,
                               root_folder: Path,
                               dataset_glob: str,
                               cache_path: Path = None) -> Iterable[Mismatch]:
    """
    Compare the given index and filesystem contents, yielding Mismatches of any differences.
    """
    pathset = _build_pathset(log, root_folder, dataset_glob, path_index, cache_path=cache_path)
    yield from _find_uri_mismatches(pathset.iterkeys('file://'), path_index)


def _find_uri_mismatches(all_file_uris: Iterable[str], index: DatasetPathIndex) -> Iterable[Mismatch]:
    """
    Compare the index and filesystem contents for the given uris,
    yielding Mismatches of any differences.
    """
    for uri in all_file_uris:

        def ids(datasets):
            return [d.id for d in datasets]

        path = uri_to_local_path(uri)
        log = _LOG.bind(path=path)
        log.debug("index.get_dataset_ids_for_uri")
        indexed_datasets = set(index.get_datasets_for_uri(uri))
        try:
            datasets_in_file = set(map(DatasetLite, paths.get_path_dataset_ids(path) if path.exists() else []))
        except InvalidDocException:
            log.exception("invalid_path")
            continue

        log.info("dataset_ids",
                 indexed_dataset_ids=ids(indexed_datasets),
                 file_ids=ids(datasets_in_file))

        for indexed_dataset in indexed_datasets:
            # Does the dataset exist in the file?
            if indexed_dataset in datasets_in_file:
                if indexed_dataset.is_archived:
                    yield ArchivedDatasetOnDisk(indexed_dataset, uri)
            else:
                yield LocationMissingOnDisk(indexed_dataset, uri)

        # For all file ids not in the index.
        file_ds_not_in_index = datasets_in_file.difference(indexed_datasets)
        log.debug("files_not_in_index", files_not_in_index=file_ds_not_in_index)

        for dataset in file_ds_not_in_index:
            # If it's already indexed, we just need to add the location.
            indexed_dataset = index.get(dataset.id)
            if indexed_dataset:
                yield LocationNotIndexed(indexed_dataset, uri)
            else:
                yield DatasetNotIndexed(dataset, uri)


def mismatches_for_collections(collections: Iterable[Collection], cache_folder: Path, index: Index):
    for collection in collections:
        log = _LOG.bind(collection=collection.name)
        collection_cache = cache_folder.joinpath(query_name(collection.query))
        fileutils.mkdir_p(str(collection_cache))

        with AgdcDatasetPathIndex(index, collection.query) as path_index:
            yield from find_index_disk_mismatches(log,
                                                  path_index,
                                                  collection.base_path,
                                                  collection.offset_pattern,
                                                  cache_path=collection_cache)


def query_name(query: Mapping[str, Any]) -> str:
    """
    Get a string name for the given query args.

    >>> query_name({'product': 'ls8_level1_scene'})
    'product_ls8_level1_scene'
    >>> query_name({'metadata_type': 'telemetry'})
    'metadata_type_telemetry'
    >>> query_name({'a': '1', 'b': 2, 'c': '"3"'})
    'a_1-b_2-c_3'
    """
    return "-".join(
        '{}_{}'.format(k, strutils.slugify(str(v)))
        for k, v in sorted(query.items())
    )