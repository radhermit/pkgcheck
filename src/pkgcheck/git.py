"""Git specific support and addon."""

import argparse
import itertools
import os
import re
import shlex
import subprocess
from collections import deque
from contextlib import AbstractContextManager
from functools import partial

from pathspec import PathSpec
from pkgcore.ebuild import cpv
from pkgcore.ebuild.atom import MalformedAtom
from pkgcore.ebuild.atom import atom as atom_cls
from pkgcore.repository import multiplex
from pkgcore.repository.util import SimpleTree
from pkgcore.restrictions import packages, values
from snakeoil.cli import arghparse
from snakeoil.cli.exceptions import UserException
from snakeoil.iterables import partition
from snakeoil.klass import jit_attr
from snakeoil.osutils import pjoin
from snakeoil.process import CommandNotFound, find_binary
from snakeoil.strings import pluralism

from . import base, caches
from .checks import GitCheck
from .eclass import matching_eclass
from .log import logger


class GitCommit:
    """Git commit objects."""

    def __init__(self, hash, commit_date, author, committer, message, pkgs=()):
        self.hash = hash
        self.commit_date = commit_date
        self.author = author
        self.committer = committer
        self.message = message
        self.pkgs = pkgs

    def __str__(self):
        return self.hash

    def __hash__(self):
        return hash(self.hash)

    def __eq__(self, other):
        return self.hash == other.hash


class GitPkgChange:
    """Git package change objects."""

    def __init__(self, atom, status, commit, commit_date, **kwargs):
        self.atom = atom
        self.status = status
        self.commit = commit
        self.commit_date = commit_date
        self.data = kwargs


class GitError(Exception):
    """Generic git-related error."""


class GitCache(caches.DictCache):
    """Dictionary-based cache that encapsulates git commit data."""

    def __init__(self, *args, commit):
        super().__init__(*args)
        self.commit = commit


class GitLog:
    """Iterator for decoded `git log` line output."""

    def __init__(self, cmd, path):
        self._running = False
        self.proc = subprocess.Popen(
            cmd, cwd=path,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def __iter__(self):
        return self

    def __next__(self):
        # use replacement character for non-UTF8 decoding issues (issue #166)
        line = self.proc.stdout.readline().decode('utf-8', 'replace')

        # verify git log is running as expected after pulling the first line
        if not self._running:
            if self.proc.poll():
                error = self.proc.stderr.read().decode().strip()
                raise GitError(f'failed running git log: {error}')
            self._running = True

        # EOF has been reached when readline() returns an empty string
        if not line:
            raise StopIteration

        return line.rstrip()


class _ParseGitRepo:
    """Generic iterator for custom git log output parsing support."""

    # git command to run on the targeted repo
    _git_cmd = 'git log --name-status --date=short --diff-filter=ARMD'

    # custom git log format lines, see the "PRETTY FORMATS" section of
    # the git log man page for details
    _format = ('# BEGIN COMMIT',)

    # hacky path regexes for git log parsing, proper validation is handled later
    _ebuild_regex = '([^/]+)/[^/]+/([^/]+)\\.ebuild'
    _git_log_regex = re.compile(
        fr'^([ADM])\t{_ebuild_regex}|(R)\d+\t{_ebuild_regex}\t{_ebuild_regex}$')

    def __init__(self, path, commit_range):
        self.path = os.path.realpath(path)
        cmd = shlex.split(self._git_cmd)
        cmd.append(f"--pretty=tformat:{'%n'.join(self._format)}")
        cmd.append(commit_range)

        self.git_log = GitLog(cmd, self.path)
        # discard the initial '# BEGIN COMMIT' line
        next(self.git_log)

    def __iter__(self):
        return self

    def __next__(self):
        raise NotImplementedError(self.__next__)


class GitRepoCommits(_ParseGitRepo):
    """Parse git log output into an iterator of commit objects."""

    _format = (
        '# BEGIN COMMIT',
        '%h',  # abbreviated commit hash
        '%cd',  # commit date
        '%an <%ae>',  # Author Name <author@email.com>
        '%cn <%ce>',  # Committer Name <committer@email.com>
        '%B',  # commit message
        '# END MESSAGE BODY',
    )

    def __next__(self):
        commit_hash = next(self.git_log)
        commit_date = next(self.git_log)
        author = next(self.git_log)
        committer = next(self.git_log)
        message = list(itertools.takewhile(
            lambda x: x != '# END MESSAGE BODY', self.git_log))
        pkgs = tuple(self._pkgs())
        return GitCommit(commit_hash, commit_date, author, committer, message, pkgs)

    def _pkgs(self):
        """Yield changed package atoms from git log file changes."""
        try:
            while (line := next(self.git_log)) != '# BEGIN COMMIT':
                if mo := self._git_log_regex.match(line):
                    data = mo.groups()
                    try:
                        if data[0] is not None:
                            # matched ADM status change
                            status, category, pn = data[0:3]
                            yield atom_cls(f'={category}/{pn}')
                        else:
                            # matched R status change
                            status, category, pn = data[3:6]
                            yield atom_cls(f'={category}/{pn}')
                            category, pn = data[6:]
                            yield atom_cls(f'={category}/{pn}')
                    except MalformedAtom:
                        pass
        except StopIteration:
            return


class GitRepoPkgs(_ParseGitRepo):
    """Parse git log output into an iterator of package change objects."""

    _format = (
        '# BEGIN COMMIT',
        '%h',  # abbreviated commit hash
        '%cd',  # commit date
    )

    def __init__(self, *args, local=False):
        super().__init__(*args)
        self.local = local
        self._pkgs = deque()

    def __next__(self):
        while True:
            try:
                return self._pkgs.popleft()
            except IndexError:
                commit_hash = next(self.git_log)
                commit_date = next(self.git_log)
                self._pkg_changes(commit_hash, commit_date)

    def _pkg_changes(self, commit_hash, commit_date):
        """Queue package change objects from git log file changes."""
        try:
            while (line := next(self.git_log)) != '# BEGIN COMMIT':
                if mo := self._git_log_regex.match(line):
                    data = mo.groups()
                    try:
                        if data[0] is not None:
                            # matched ADM status change
                            status, category, pn = data[0:3]
                            pkg = atom_cls(f'={category}/{pn}')
                            self._pkgs.append(
                                GitPkgChange(pkg, status, commit_hash, commit_date))
                        else:
                            # matched R status change
                            status, category, pn = data[3:6]
                            old_pkg = atom_cls(f'={category}/{pn}')
                            category, pn = data[6:]
                            new_pkg = atom_cls(f'={category}/{pn}')
                            if not self.local:  # treat rename as addition and removal
                                self._pkgs.append(
                                    GitPkgChange(new_pkg, 'A', commit_hash, commit_date))
                                self._pkgs.append(
                                    GitPkgChange(old_pkg, 'D', commit_hash, commit_date))
                            else:
                                # renames are split into add/remove ops at
                                # the check level for the local commits repo
                                self._pkgs.append(GitPkgChange(
                                    new_pkg, 'R', commit_hash, commit_date, old_pkg=old_pkg))
                    except MalformedAtom:
                        pass
        except StopIteration:
            return


class _GitCommitPkg(cpv.VersionedCPV):
    """Fake packages encapsulating commits parsed from git log."""

    def __init__(self, category, package, status, version, date, commit, data=None):
        super().__init__(category, package, version)

        # add additional attrs
        sf = object.__setattr__
        sf(self, 'date', date)
        sf(self, 'status', status)
        sf(self, 'commit', commit)
        if data is not None:
            for k, v in data.items():
                sf(self, k, v)

    def _old_pkg(self):
        """Create a new object from a rename commit's old atom."""
        old = self.old_pkg
        return self.__class__(
            old.category, old.package, self.status, old.version, self.date, self.commit)


class GitChangedRepo(SimpleTree):
    """Historical git repo consisting of the latest changed packages."""

    # selected pkg status filter
    _status_filter = {'A', 'R', 'M', 'D'}

    def __init__(self, *args, **kwargs):
        kwargs.setdefault('pkg_klass', _GitCommitPkg)
        super().__init__(*args, **kwargs)

    def _get_versions(self, cp):
        versions = []
        for status, data in self.cpv_dict[cp[0]][cp[1]].items():
            if status in self._status_filter:
                for commit in data:
                    versions.append((status, commit))
        return versions

    def _internal_gen_candidates(self, candidates, sorter, raw_pkg_cls, **kwargs):
        for cp in sorter(candidates):
            yield from sorter(
                raw_pkg_cls(cp[0], cp[1], status, *commit)
                for status, commit in self.versions.get(cp, ()))


class GitModifiedRepo(GitChangedRepo):
    """Historical git repo consisting of the latest modified packages."""

    _status_filter = {'A', 'M'}


class GitAddedRepo(GitChangedRepo):
    """Historical git repo consisting of added packages."""

    _status_filter = {'A'}


class GitRemovedRepo(GitChangedRepo):
    """Historical git repo consisting of removed packages."""

    _status_filter = {'D'}


class _ScanCommits(argparse.Action):
    """Argparse action that enables git commit checks."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @staticmethod
    def _pkg_atoms(paths):
        """Filter package atoms from commit paths."""
        for x in paths:
            try:
                yield atom_cls(os.sep.join(x.split(os.sep, 2)[:2]))
            except MalformedAtom:
                continue

    @property
    def git_checks(self):
        # avoid circular import issues
        from . import objects
        return [cls for cls in objects.CHECKS.values() if issubclass(cls, GitCheck)]

    def __call__(self, parser, namespace, value, option_string=None):
        if namespace.targets:
            targets = ' '.join(namespace.targets)
            s = pluralism(namespace.targets)
            parser.error(f'--commits is mutually exclusive with target{s}: {targets}')

        ref = value if value is not None else 'origin'
        setattr(namespace, self.dest, ref)

        # generate restrictions based on git commit changes
        repo = namespace.target_repo
        targets = sorted(repo.category_dirs)
        if os.path.isdir(pjoin(repo.location, 'eclass')):
            targets.append('eclass')
        git_diff_cmd = ['git', 'diff', '--cached', ref, '--name-only']
        try:
            p = subprocess.run(
                git_diff_cmd + targets,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=repo.location, check=True, encoding='utf8')
        except FileNotFoundError:
            parser.error('git not available to determine targets for --commits')
        except subprocess.CalledProcessError as e:
            error = e.stderr.splitlines()[0]
            parser.error(f'failed running git: {error}')

        if not p.stdout:
            # no changes exist, exit early
            parser.exit()

        pkgs, eclasses = partition(
            p.stdout.splitlines(), predicate=lambda x: x.startswith('eclass/'))
        pkgs = sorted(self._pkg_atoms(pkgs))

        eclass_regex = re.compile(r'^eclass/(?P<eclass>\S+)\.eclass$')
        eclasses = filter(None, (eclass_regex.match(x) for x in eclasses))
        eclasses = sorted(x.group('eclass') for x in eclasses)

        restrictions = []
        if pkgs:
            restrict = packages.OrRestriction(*pkgs)
            restrictions.append((base.package_scope, restrict))
        if eclasses:
            func = partial(matching_eclass, frozenset(eclasses))
            restrict = values.AnyMatch(values.FunctionRestriction(func))
            restrictions.append((base.eclass_scope, restrict))

        # no pkgs or eclasses to check, exit early
        if not restrictions:
            parser.exit()

        # make sure git checks are properly enabled
        namespace.enabled_checks.update(self.git_checks)

        namespace.contexts.append(GitStash(repo.location))
        namespace.restrictions = restrictions


class GitStash(AbstractContextManager):
    """Context manager for stashing untracked or modified/uncommitted files.

    This assumes that no git actions are performed on the repo while a scan is
    underway otherwise `git stash` usage may cause issues.
    """

    def __init__(self, path):
        self.path = path
        self._stashed = False

    def __enter__(self):
        """Stash all untracked or modified files in working tree."""
        # check for untracked or modified/uncommitted files
        try:
            p = subprocess.run(
                ['git', 'ls-files', '-mo', '--exclude-standard'],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                cwd=self.path, encoding='utf8', check=True)
        except subprocess.CalledProcessError:
            raise ValueError(f'not a git repo: {self.path}')

        if not p.stdout:
            return

        # stash all existing untracked or modified/uncommitted files
        try:
            subprocess.run(
                ['git', 'stash', 'push', '-u', '-m', 'pkgcheck scan --commits'],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                cwd=self.path, check=True, encoding='utf8')
        except subprocess.CalledProcessError as e:
            error = e.stderr.splitlines()[0]
            raise UserException(f'git failed stashing files: {error}')
        self._stashed = True

    def __exit__(self, _exc_type, _exc_value, _traceback):
        """Apply any previously stashed files back to the working tree."""
        if self._stashed:
            try:
                subprocess.run(
                    ['git', 'stash', 'pop'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    cwd=self.path, check=True, encoding='utf8')
            except subprocess.CalledProcessError as e:
                error = e.stderr.splitlines()[0]
                raise UserException(f'git failed applying stash: {error}')


class GitAddon(caches.CachedAddon):
    """Git repo support for various checks.

    Pkgcheck can create virtual package repos from a given git repo's history
    in order to provide more info for checks relating to stable requests,
    outdated blockers, or local commits. These virtual repos are cached and
    updated every run if new commits are detected.

    Git repos must have a supported config in order to work properly.
    Specifically, pkgcheck assumes that both origin and master branches exist
    and relate to the upstream and local development states, respectively.

    Additionally, the origin/HEAD ref must exist. If it doesn't, running ``git
    fetch origin`` should create it. Otherwise, using ``git remote set-head
    origin master`` or similar will also create the reference.
    """

    # cache registry
    cache = caches.CacheData(type='git', file='git.pickle', version=4)

    @classmethod
    def mangle_argparser(cls, parser):
        group = parser.add_argument_group('git', docs=cls.__doc__)
        group.add_argument(
            '--commits', nargs='?', metavar='COMMIT',
            action=arghparse.Delayed, target=_ScanCommits, priority=100,
            help="determine scan targets from local git repo commits",
            docs="""
                For a local git repo, pkgcheck will determine targets to scan
                from the committed changes compared to a given reference that
                defaults to the repo's origin.

                For example, to scan all the packages that have been changed in
                the current branch compared to the branch named 'old' use
                ``pkgcheck scan --commits old``. For two separate branches
                named 'old' and 'new' use ``pkgcheck scan --commits old..new``.

                Note that will also enable eclass-specific checks if it
                determines any commits have been made to eclasses.
            """)

    def __init__(self, *args):
        super().__init__(*args)
        # disable git support if git isn't installed
        if self.options.cache['git']:
            try:
                find_binary('git')
            except CommandNotFound:
                self.options.cache['git'] = False

        # mapping of repo locations to their corresponding git repo caches
        self._cached_repos = {}

    @jit_attr
    def _gitignore(self):
        """Load a repo's .gitignore and .git/info/exclude files for path matching."""
        patterns = []
        for path in ('.gitignore', '.git/info/exclude'):
            try:
                with open(pjoin(self.options.target_repo.location, path)) as f:
                    patterns.extend(f)
            except (FileNotFoundError, IOError):
                pass
        if patterns:
            return PathSpec.from_lines('gitwildmatch', patterns)
        return None

    def gitignored(self, path):
        """Determine if a given path in a repository is matched by .gitignore settings."""
        if self._gitignore is not None:
            if path.startswith(self.options.target_repo.location):
                repo_prefix_len = len(self.options.target_repo.location) + 1
                path = path[repo_prefix_len:]
            return self._gitignore.match_file(path)
        return False

    @staticmethod
    def _get_commit_hash(path, commit='origin/HEAD'):
        """Retrieve a git repo's commit hash for a specific commit object."""
        try:
            p = subprocess.run(
                ['git', 'rev-parse', commit],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                cwd=path, check=True, encoding='utf8')
        except subprocess.CalledProcessError:
            raise GitError(f'failed retrieving commit hash for git repo: {path!r}')
        return p.stdout.strip()

    @staticmethod
    def pkg_history(path, commit_range, data=None, local=False, verbosity=-1):
        """Create or update historical package data for a given commit range."""
        if data is None:
            data = {}
        seen = set()
        with base.ProgressManager(verbosity=verbosity) as progress:
            for pkg in GitRepoPkgs(path, commit_range, local=local):
                atom = pkg.atom
                key = (atom, pkg.status)
                if key not in seen:
                    seen.add(key)
                    if local:
                        commit = (atom.fullver, pkg.commit_date, pkg.commit, pkg.data)
                    else:
                        progress(f'updating git cache: commit date: {pkg.commit_date}')
                        commit = (atom.fullver, pkg.commit_date, pkg.commit)
                    data.setdefault(atom.category, {}).setdefault(
                        atom.package, {}).setdefault(pkg.status, []).append(commit)
        return data

    def update_cache(self, force=False):
        """Update related cache and push updates to disk."""
        if self.options.cache['git']:
            for repo in self.options.target_repo.trees:
                try:
                    commit = self._get_commit_hash(repo.location)
                except GitError:
                    continue

                # initialize cache file location
                cache_file = self.cache_file(repo)
                git_cache = None
                cache_repo = True

                if not force:
                    git_cache = self.load_cache(cache_file)

                if git_cache is None or commit != git_cache.commit:
                    logger.debug('updating %s git repo cache to %s', repo, commit[:13])
                    if git_cache is None:
                        data = {}
                        commit_range = 'origin/HEAD'
                    else:
                        data = git_cache.data
                        commit_range = f'{git_cache.commit}..origin/HEAD'
                    try:
                        self.pkg_history(
                            repo.location, commit_range, data=data,
                            verbosity=self.options.verbosity)
                    except GitError as e:
                        raise UserException(str(e))
                    git_cache = GitCache(data, self.cache, commit=commit)
                else:
                    cache_repo = False

                if git_cache:
                    self._cached_repos[repo.location] = git_cache
                    # push repo to disk if it was created or updated
                    if cache_repo:
                        self.save_cache(git_cache, cache_file)

    def cached_repo(self, repo_cls, target_repo=None):
        cached_repo = None
        if target_repo is None:
            target_repo = self.options.target_repo

        if self.options.cache['git']:
            git_repos = []
            for repo in target_repo.trees:
                # only enable repo queries if history was found, e.g. a
                # shallow clone with a depth of 1 won't have any history
                if git_cache := self._cached_repos.get(repo.location, None):
                    git_repos.append(repo_cls(git_cache, repo_id=f'{repo.repo_id}-history'))
                else:
                    # skip git checks
                    break
            else:
                if len(git_repos) > 1:
                    cached_repo = multiplex.tree(*git_repos)
                elif len(git_repos) == 1:
                    cached_repo = git_repos[0]

        return cached_repo

    def commits_repo(self, repo_cls, repo=None):
        repo = self.options.target_repo if repo is None else repo
        data = {}

        if self.options.cache['git']:
            try:
                origin = self._get_commit_hash(repo.location)
                master = self._get_commit_hash(repo.location, commit='master')
                if origin != master:
                    data = self.pkg_history(repo.location, 'origin/HEAD..master', local=True)
            except GitError as e:
                raise UserException(str(e))

        repo_id = f'{repo.repo_id}-commits'
        return repo_cls(data, repo_id=repo_id)

    def commits(self, repo=None):
        repo = self.options.target_repo if repo is None else repo
        commits = ()

        if self.options.cache['git']:
            try:
                origin = self._get_commit_hash(repo.location)
                master = self._get_commit_hash(repo.location, commit='master')
                if origin != master:
                    commits = GitRepoCommits(repo.location, 'origin/HEAD..master')
            except GitError as e:
                raise UserException(str(e))

        return iter(commits)
