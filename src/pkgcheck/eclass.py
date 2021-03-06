"""Eclass specific support and addon."""

import os
from functools import total_ordering

from pkgcore.ebuild.eclass import EclassDoc, EclassDocParsingError
from snakeoil.klass import jit_attr_none
from snakeoil.mappings import ImmutableDict
from snakeoil.osutils import pjoin

from . import base, caches


def matching_eclass(eclasses_set, eclass):
    """Stub method for matching eclasses against a given set.

    Used to create pickleable eclass scanning restrictions.
    """
    return eclass in eclasses_set


@total_ordering
class Eclass:
    """Generic eclass object."""

    def __init__(self, name, path):
        self.name = name
        self.path = os.path.realpath(path)

    def __str__(self):
        return self.name

    @property
    def lines(self):
        try:
            with open(self.path) as f:
                return tuple(f)
        except FileNotFoundError:
            return ()

    def __lt__(self, other):
        if isinstance(other, Eclass):
            return self.name < other.name
        return self.name < other

    def __hash__(self):
        return hash(self.path)

    def __eq__(self, other):
        if isinstance(other, Eclass):
            return self.path == other.path
        return self.path == other


class EclassAddon(caches.CachedAddon):
    """Eclass support for various checks."""

    # cache registry
    cache = caches.CacheData(type='eclass', file='eclass.pickle', version=1)

    def __init__(self, *args):
        super().__init__(*args)
        # mapping of repo locations to their corresponding eclass caches
        self._eclass_repos = {}

    @jit_attr_none
    def eclasses(self, repo=None):
        """Mapping of available eclasses to eclass doc info."""
        d = {}
        for r in self.options.target_repo.trees:
            d.update(self._eclass_repos.get(r.location, ()))
        return ImmutableDict(d)

    @jit_attr_none
    def deprecated(self):
        """Mapping of deprecated eclasses to their replacements (if any)."""
        d = {}
        for r in self.options.target_repo.trees:
            try:
                for k, v in self._eclass_repos[r.location].items():
                    if 'deprecated' in v:
                        d[k] = v['deprecated']
            except KeyError:
                continue
        return ImmutableDict(d)

    def update_cache(self, force=False):
        """Update related cache and push updates to disk."""
        if self.options.cache['eclass']:
            for repo in self.options.target_repo.trees:
                cache_file = self.cache_file(repo)
                cache_eclasses = False
                eclasses = {}

                if not force:
                    eclasses = self.load_cache(cache_file, fallback={})

                # check for eclass removals
                for name, eclass in list(eclasses.items()):
                    if not os.path.exists(eclass.path):
                        del eclasses[name]
                        cache_eclasses = True

                # verify the repo has eclasses
                eclass_dir = pjoin(repo.location, 'eclass')
                try:
                    repo_eclasses = sorted(
                        (x[:-7], pjoin(eclass_dir, x)) for x in os.listdir(eclass_dir)
                        if x.endswith('.eclass'))
                except FileNotFoundError:
                    repo_eclasses = []

                if repo_eclasses:
                    # padding for progress output
                    padding = max(len(x[0]) for x in repo_eclasses)

                    # check for eclass additions and updates
                    with base.ProgressManager(verbosity=self.options.verbosity) as progress:
                        for name, path in repo_eclasses:
                            try:
                                if os.path.getmtime(path) != eclasses[name].mtime:
                                    raise KeyError
                            except (KeyError, AttributeError):
                                try:
                                    progress(f'updating eclass cache: {name:<{padding}}')
                                    eclasses[name] = EclassDoc(path, sourced=True)
                                    cache_eclasses = True
                                except (IOError, EclassDocParsingError):
                                    continue

                if cache_eclasses:
                    # reset jit attrs
                    self._eclasses = None
                    self._deprecated = None
                    # push cache updates to disk
                    data = caches.DictCache(eclasses, self.cache)
                    self.save_cache(data, cache_file)

                self._eclass_repos[repo.location] = eclasses
