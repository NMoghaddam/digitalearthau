import dawg
import logging
import uuid
from itertools import chain
from pathlib import Path
from typing import Iterable, List

import structlog
from boltons import fileutils
from datacubenci import paths

from datacube.index import index_connect
from datacube.index._api import Index
from datacube.utils import uri_to_local_path

_LOG = structlog.get_logger()


class DatasetPathIndex:
    def __init__(self):
        super().__init__()

    def iter_all_uris(self) -> Iterable[str]:
        raise NotImplementedError

    def get_dataset_ids_for_uri(self, uri: str) -> List[uuid.UUID]:
        raise NotImplementedError

    def has_dataset(self, dataset_id: uuid.UUID) -> bool:
        raise NotImplementedError

    def add_location(self, dataset_id: uuid.UUID, uri: str) -> bool:
        raise NotImplementedError

    def remove_location(self, dataset_id: uuid.UUID, uri: str) -> bool:
        raise NotImplementedError


class DatasetLite:
    def __init__(self, id_):
        self.id = id_


class AgdcDatasetPathIndex(DatasetPathIndex):
    def __init__(self, index: Index, query: dict):
        super().__init__()
        self._index = index
        self._query = query

    def iter_all_uris(self) -> Iterable[str]:
        for uri, in self._index.datasets.search_returning(['uri'], **self._query):
            yield str(uri)

    @classmethod
    def connect(cls, **query) -> 'AgdcDatasetPathIndex':
        return cls(index_connect(application_name='datacubenci-pathsync'), query=query)

    def get_dataset_ids_for_uri(self, uri: str) -> List[uuid.UUID]:
        for dataset_id, in self._index.datasets.search_returning(['id'], uri=uri):
            yield str(dataset_id)

    def remove_location(self, dataset_id: uuid.UUID, uri: str) -> bool:
        was_removed = self._index.datasets.remove_location(DatasetLite(dataset_id), uri)
        return was_removed

    def has_dataset(self, dataset_id: uuid.UUID) -> bool:
        return self._index.datasets.has(dataset_id)

    def add_location(self, dataset_id: uuid.UUID, uri: str) -> bool:
        was_removed = self._index.datasets.add_location(DatasetLite(dataset_id), uri)
        return was_removed

    def __enter__(self):
        return self

    def __exit__(self, type_, value, traceback):
        self._index.close()


def build_pathset(
        log: logging.Logger,
        path_search_root: Path,
        path_index: DatasetPathIndex,
        cache_path: Path = None) -> dawg.CompletionDAWG:
    locations_cache = cache_path.joinpath('locations.dawg') if cache_path else None
    if locations_cache and locations_cache.exists():
        path_set = dawg.CompletionDAWG()
        log.debug("paths.trie.cache.load", file=locations_cache)
        path_set.load(str(locations_cache))
    else:
        log.info("paths.trie.build")
        path_set = dawg.CompletionDAWG(
            chain(
                path_index.iter_all_uris(),
                (path.absolute().as_uri() for path in path_search_root.rglob("ga-metadata.yaml"))
            )
        )
        log.info("paths.trie.done")
        if locations_cache is not None:
            log.debug("paths.trie.cache.create", file=locations_cache)
            with fileutils.atomic_save(str(locations_cache)) as f:
                path_set.write(f)
    return path_set


class Mismatch:
    def __init__(self, dataset_id, uri):
        super().__init__()
        self.dataset_id = dataset_id
        self.uri = uri

    def __repr__(self, *args, **kwargs):
        """
        >>> Mismatch(dataset_id='96519c56-e133-11e6-a29f-185e0f80a5c0', uri='/tmp/test')
        Mismatch(dataset_id='96519c56-e133-11e6-a29f-185e0f80a5c0', uri='/tmp/test')
        """
        return "%s(%s)" % (
            self.__class__.__name__,
            ", ".join("%s=%r" % (k, v) for k, v in sorted(self.__dict__.items()))
        )

    def __eq__(self, other):
        """
        >>> m = Mismatch(dataset_id='96519c56-e133-11e6-a29f-185e0f80a5c0', uri='/tmp/test')
        >>> m == m
        True
        >>> import copy
        >>> m == copy.copy(m)
        True
        >>> n = Mismatch(dataset_id='96519c56-e133-11e6-a29f-185e0f80a5c0', uri='/tmp/test2')
        >>> m == n
        False
        """
        if not isinstance(other, self.__class__):
            return False

        return self.__dict__ == other.__dict__

    def __hash__(self):
        return hash(tuple(v for k, v in sorted(self.__dict__.items())))


class MissingIndexedFile(Mismatch):
    pass


class LocationNotIndexed(Mismatch):
    pass


class DatasetNotIndexed(Mismatch):
    pass


def compare_index_and_files(all_file_uris: Iterable[str], index: DatasetPathIndex):
    for uri in all_file_uris:
        path = uri_to_local_path(uri)
        log = _LOG.bind(path=path)
        log.debug("index.get_dataset_ids_for_uri")
        indexed_dataset_ids = set(index.get_dataset_ids_for_uri(uri))
        file_ids = set(paths.get_path_dataset_ids(path)) if path.exists() else set()
        log.info("dataset_ids", indexed_dataset_ids=indexed_dataset_ids, file_ids=file_ids)

        # For all indexed ids not in the file
        indexed_not_in_file = indexed_dataset_ids.difference(file_ids)
        log.info("indexed_not_in_file", indexed_not_in_file=indexed_not_in_file)
        for dataset_id in indexed_not_in_file:
            yield MissingIndexedFile(dataset_id, uri)

        # For all file ids not in the index.
        files_not_in_index = file_ids.difference(indexed_dataset_ids)
        log.info("files_not_in_index", files_not_in_index=files_not_in_index)

        for dataset_id in files_not_in_index:
            if index.has_dataset(dataset_id):
                yield LocationNotIndexed(dataset_id, uri)
            else:
                yield DatasetNotIndexed(dataset_id, uri)


def compare_product_locations(log, path_index, filesystem_root, cache_path=None):
    pathset = build_pathset(log, filesystem_root, path_index, cache_path=cache_path)
    yield from compare_index_and_files(pathset.iterkeys('file://'), path_index)


def main():
    root = Path('/tmp/test')
    cache = root.joinpath('cache')
    product_locations = {
        'ls8_level1_scene': root.joinpath('ls8_scenes'),
        'ls7_level1_scene': root.joinpath('ls7_scenes'),
    }

    for product, filesystem_root in product_locations.items():
        log = _LOG.bind(product=product)
        cache_path = cache.joinpath(product)
        fileutils.mkdir_p(str(cache_path))

        with AgdcDatasetPathIndex.connect(product=product) as path_index:
            for mismatch in compare_product_locations(log, path_index, product_locations, cache_path=cache_path):
                print(repr(mismatch))


if __name__ == '__main__':
    main()