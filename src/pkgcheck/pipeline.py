"""Pipeline building support for connecting sources and checks."""

from collections import defaultdict
import concurrent.futures
from itertools import chain
from multiprocessing import Pool, SimpleQueue, Process, cpu_count

from pkgcore.ebuild.cpv import CPV
from pkgcore.package.errors import MetadataException

from . import base
from .results import MetadataError
from .sources import VersionedSource, UnversionedSource


class GitPipeline:

    def __init__(self, source, checks):
        self.checkrunner = CheckRunner(source, checks)

    def __iter__(self):
        yield from self.checkrunner.start()
        yield from self.checkrunner.run()
        yield from self.checkrunner.finish()


class Pipeline:

    def __init__(self, options, scan_scope, pipes, restrict):
        self.options = options
        self.scan_scope = scan_scope
        self.pipes = pipes
        self.restrict = restrict
        self.jobs = options.jobs if options.jobs is not None else cpu_count()

    def _run_version_checks(self, pipes, restrict):
        results = []
        for pipe in pipes:
            results.extend(pipe.run(restrict))
        return results

    def _run_pkg_checks(self, restrict, pipe):
        return list(pipe.run(restrict))

    def _insert_pkgs(self, queue):
        source = UnversionedSource(self.options)
        for restrict in source.itermatch(self.restrict, raw_pkg_cls=CPV):
            queue.put(restrict)
        for i in range(self.jobs):
            queue.put(None)

    def _run_checks(self, pipes, restricts_q, results_q):
        while True:
            restrict = restricts_q.get()
            if restrict is None:
                return
            results = []
            for pipe in pipes:
                results.extend(pipe.run(restrict))
            results_q.put(results)

    def run(self, results_q):
        results = []
        for pipe in chain.from_iterable(self.pipes.values()):
            results.extend(pipe.start())
        if results:
            results_q.put(results)

        if self.scan_scope == base.version_scope:
            results = []
            for pipe in chain.from_iterable(self.pipes.values()):
                results.extend(pipe.run(self.restrict))
            if results:
                results_q.put(results)
        elif self.scan_scope == base.package_scope:
            # Optionally run package scope scans in parallel. This only makes
            # sense for packages hitting visibility checks or other CPU heavy
            # tests hard, e.g. packages with a lot of transitive USE flags, so
            # the default is to run them serially.
            if self.options.jobs is None:
                results = []
                for pipe in chain.from_iterable(self.pipes.values()):
                    results.extend(pipe.run(self.restrict))
                if results:
                    results_q.put(results)
            else:
                pkg_checks = []
                version_checks = []
                for scope, pipes in self.pipes.items():
                    if scope == base.package_scope:
                        pkg_checks.extend(pipes)
                    else:
                        version_checks.extend(pipes)
                source = VersionedSource(self.options)
                futures = []
                with concurrent.futures.ProcessPoolExecutor(self.jobs) as executor:
                    for r in source.itermatch(self.restrict, raw_pkg_cls=CPV):
                        futures.append(
                            executor.submit(self._run_version_checks, version_checks, r))
                    for p in pkg_checks:
                        futures.append(executor.submit(self._run_pkg_checks, self.restrict, p))
                results = []
                for future in concurrent.futures.as_completed(futures):
                    results.extend(future.result())
                results_q.put(results)
        else:
            # Performing scan runs at category scope and higher makes package
            # checks run in parallel.
            pkg_checks = []
            non_pkg_checks = []
            for scope, pipes in self.pipes.items():
                if scope <= base.package_scope:
                    pkg_checks.extend(pipes)
                else:
                    non_pkg_checks.extend(pipes)
            if pkg_checks:
                restricts_q = SimpleQueue()
                p = Process(target=self._insert_pkgs, args=(restricts_q,))
                p.start()
                pool = Pool(self.jobs, self._run_checks, (pkg_checks, restricts_q, results_q))
                pool.close()
                p.join()
                pool.join()
            if non_pkg_checks:
                results = []
                for pipe in non_pkg_checks:
                    results.extend(pipe.run(self.restrict))
                if results:
                    results_q.put(results)

        results = []
        for pipe in chain.from_iterable(self.pipes.values()):
            results.extend(pipe.finish())
        if results:
            results_q.put(results)

        results_q.put(None)


class CheckRunner:

    def __init__(self, source, checks):
        self.source = source
        self.checks = checks
        self._metadata_errors = set()

    def start(self):
        for check in self.checks:
            try:
                reports = check.start()
                if reports is not None:
                    yield from reports
            except MetadataException as e:
                exc_info = (e.pkg, e.error)
                # only report distinct metadata errors
                if exc_info not in self._metadata_errors:
                    self._metadata_errors.add(exc_info)
                    error_str = ': '.join(str(e.error).split('\n'))
                    yield MetadataError(e.attr, error_str, pkg=e.pkg)

    def run(self, restrict=None):
        source = self.source if restrict is None else self.source.itermatch(restrict)
        for item in source:
            for check in self.checks:
                try:
                    reports = check.feed(item)
                    if reports is not None:
                        yield from reports
                except MetadataException as e:
                    exc_info = (e.pkg, e.error)
                    # only report distinct metadata errors
                    if exc_info not in self._metadata_errors:
                        self._metadata_errors.add(exc_info)
                        error_str = ': '.join(str(e.error).split('\n'))
                        yield MetadataError(e.attr, error_str, pkg=e.pkg)

        # yield metadata errors from itermatch() error callbacks
        if getattr(self.source, 'metadata_errors', None):
            for e in self.source.metadata_errors:
                error_str = ': '.join(str(e.error).split('\n'))
                yield MetadataError(e.attr, error_str, pkg=e.pkg)
            self.source.metadata_errors.clear()

    def finish(self):
        for check in self.checks:
            reports = check.finish()
            if reports is not None:
                yield from reports

    # The plugger tests use these.
    def __eq__(self, other):
        return (
            self.__class__ is other.__class__ and
            frozenset(self.checks) == frozenset(other.checks))

    def __ne__(self, other):
        return not self == other

    def __hash__(self):
        return hash(frozenset(self.checks))

    def __repr__(self):
        checks = ', '.join(sorted(str(check) for check in self.checks))
        return f'{self.__class__.__name__}({checks})'


def plug(pipes):
    """Plug together a pipeline.

    :param pipes: Iterable of source -> check pipe mappings.
    :return: A generator of (source, consumer) tuples.
    """
    d = defaultdict(list)
    for pipe_mapping in pipes:
        for source, checks in pipe_mapping.items():
            d[source.feed_type].append(CheckRunner(source, checks))
    return d