import os
import shlex
import shutil
import subprocess
import tempfile
from collections import defaultdict
from functools import partial
from io import StringIO
from operator import attrgetter
from unittest.mock import patch

import pytest
from pkgcheck import __title__ as project
from pkgcheck import base, checks, objects, reporters
from pkgcheck.scripts import run
from pkgcore import const as pkgcore_const
from pkgcore.ebuild import atom, restricts
from pkgcore.ebuild.repository import UnconfiguredTree
from pkgcore.restrictions import packages
from snakeoil.contexts import chdir
from snakeoil.fileutils import touch
from snakeoil.formatters import PlainTextFormatter
from snakeoil.osutils import pjoin


class TestPkgcheckScanParseArgs:

    @pytest.fixture(autouse=True)
    def _setup(self, tool):
        self.tool = tool
        self.args = ['scan']

    def test_skipped_checks(self):
        options, _func = self.tool.parse_args(self.args)
        assert options.enabled_checks
        # some checks should always be skipped by default
        assert set(options.enabled_checks) != set(objects.CHECKS.values())

    def test_enabled_check(self):
        options, _func = self.tool.parse_args(self.args + ['-c', 'PkgDirCheck'])
        assert options.enabled_checks == [checks.pkgdir.PkgDirCheck]

    def test_disabled_check(self):
        options, _func = self.tool.parse_args(self.args)
        assert checks.pkgdir.PkgDirCheck in options.enabled_checks
        options, _func = self.tool.parse_args(self.args + ['-c=-PkgDirCheck'])
        assert options.enabled_checks
        assert checks.pkgdir.PkgDirCheck not in options.enabled_checks

    def test_targets(self):
        options, _func = self.tool.parse_args(self.args + ['dev-util/foo'])
        assert list(options.restrictions) == [(base.package_scope, atom.atom('dev-util/foo'))]

    def test_stdin_targets(self):
        with patch('sys.stdin', StringIO('dev-util/foo')):
            options, _func = self.tool.parse_args(self.args + ['-'])
            assert list(options.restrictions) == [(base.package_scope, atom.atom('dev-util/foo'))]

    def test_invalid_targets(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            options, _func = self.tool.parse_args(self.args + ['dev-util/f$o'])
        assert excinfo.value.code == 2
        out, err = capsys.readouterr()
        err = err.strip()
        assert err == "pkgcheck scan: error: invalid package atom: 'dev-util/f$o'"

    def test_selected_targets(self, fakerepo):
        # selected repo
        options, _func = self.tool.parse_args(self.args + ['-r', 'stubrepo'])
        assert options.target_repo.repo_id == 'stubrepo'
        assert list(options.restrictions) == [(base.repo_scope, packages.AlwaysTrue)]

        # dir path
        options, _func = self.tool.parse_args(self.args + [fakerepo])
        assert options.target_repo.repo_id == 'fakerepo'
        assert list(options.restrictions) == [(base.repo_scope, packages.AlwaysTrue)]

        # file path
        os.makedirs(pjoin(fakerepo, 'dev-util', 'foo'))
        ebuild_path = pjoin(fakerepo, 'dev-util', 'foo', 'foo-0.ebuild')
        touch(ebuild_path)
        options, _func = self.tool.parse_args(self.args + [ebuild_path])
        restrictions = [
            restricts.CategoryDep('dev-util'),
            restricts.PackageDep('foo'),
            restricts.VersionMatch('=', '0'),
        ]
        assert list(options.restrictions) == [(base.version_scope, packages.AndRestriction(*restrictions))]
        assert options.target_repo.repo_id == 'fakerepo'

        # cwd path in unconfigured repo
        with chdir(pjoin(fakerepo, 'dev-util', 'foo')):
            options, _func = self.tool.parse_args(self.args)
            assert options.target_repo.repo_id == 'fakerepo'
            restrictions = [
                restricts.CategoryDep('dev-util'),
                restricts.PackageDep('foo'),
            ]
            assert list(options.restrictions) == [(base.package_scope, packages.AndRestriction(*restrictions))]

        # cwd path in configured repo
        stubrepo = pjoin(pkgcore_const.DATA_PATH, 'stubrepo')
        with chdir(stubrepo):
            options, _func = self.tool.parse_args(self.args)
            assert options.target_repo.repo_id == 'stubrepo'
            assert list(options.restrictions) == [(base.repo_scope, packages.AlwaysTrue)]

    def test_unknown_repo(self, capsys):
        for opt in ('-r', '--repo'):
            with pytest.raises(SystemExit) as excinfo:
                options, _func = self.tool.parse_args(self.args + [opt, 'foo'])
            assert excinfo.value.code == 2
            out, err = capsys.readouterr()
            err = err.strip().split('\n')
            assert err[-1].startswith(
                "pkgcheck scan: error: argument -r/--repo: couldn't find repo 'foo'")

    def test_unknown_reporter(self, capsys):
        for opt in ('-R', '--reporter'):
            with pytest.raises(SystemExit) as excinfo:
                options, _func = self.tool.parse_args(self.args + [opt, 'foo'])
            assert excinfo.value.code == 2
            out, err = capsys.readouterr()
            err = err.strip().split('\n')
            assert err[-1].startswith(
                "pkgcheck scan: error: no reporter matches 'foo'")

    def test_unknown_scope(self, capsys):
        for opt in ('-s', '--scopes'):
            with pytest.raises(SystemExit) as excinfo:
                options, _func = self.tool.parse_args(self.args + [opt, 'foo'])
            assert excinfo.value.code == 2
            out, err = capsys.readouterr()
            err = err.strip().split('\n')
            assert "unknown scope: 'foo'" in err[-1]

    def test_unknown_check(self, capsys):
        for opt in ('-c', '--checks'):
            with pytest.raises(SystemExit) as excinfo:
                options, _func = self.tool.parse_args(self.args + [opt, 'foo'])
            assert excinfo.value.code == 2
            out, err = capsys.readouterr()
            err = err.strip().split('\n')
            assert "unknown check: 'foo'" in err[-1]

    def test_unknown_keyword(self, capsys):
        for opt in ('-k', '--keywords'):
            with pytest.raises(SystemExit) as excinfo:
                options, _func = self.tool.parse_args(self.args + [opt, 'foo'])
            assert excinfo.value.code == 2
            out, err = capsys.readouterr()
            err = err.strip().split('\n')
            assert "unknown keyword: 'foo'" in err[-1]

    def test_selected_keywords(self):
        for opt in ('-k', '--keywords'):
            options, _func = self.tool.parse_args(self.args + [opt, 'InvalidPN'])
            result_cls = next(v for k, v in objects.KEYWORDS.items() if k == 'InvalidPN')
            assert options.filtered_keywords == {result_cls}
            check = next(x for x in objects.CHECKS.values() if result_cls in x.known_results)
            assert options.enabled_checks == [check]

    def test_missing_scope(self, capsys):
        for opt in ('-s', '--scopes'):
            with pytest.raises(SystemExit) as excinfo:
                options, _func = self.tool.parse_args(self.args + [opt])
            assert excinfo.value.code == 2
            out, err = capsys.readouterr()
            err = err.strip().split('\n')
            assert err[0] == (
                'pkgcheck scan: error: argument -s/--scopes: expected one argument')

    def test_no_active_checks(self, capsys):
        args = self.args + ['-c', 'UnusedInMastersCheck']
        with pytest.raises(SystemExit) as excinfo:
            options, _func = self.tool.parse_args(args)
        assert excinfo.value.code == 2
        out, err = capsys.readouterr()
        err = err.strip().split('\n')
        assert err[-1].startswith("pkgcheck scan: error: no matching checks available")


class TestPkgcheckScan:

    script = partial(run, project)
    _results = defaultdict(set)
    _checks_run = defaultdict(set)

    @classmethod
    def setup_class(cls):
        testdir = os.path.dirname(os.path.dirname(__file__))
        cls.repos_data = pjoin(testdir, 'data', 'repos')
        cls.repos_dir = pjoin(testdir, 'repos')

    @pytest.fixture(autouse=True)
    def _setup(self, testconfig):
        self.args = [project, '--config', testconfig, 'scan', '--config', 'no']

    @staticmethod
    def _patch(fix, repo_path):
        with open(fix) as f:
            p = subprocess.run(
                ['patch', '-p1'], cwd=repo_path, stdout=subprocess.DEVNULL, stdin=f)
            p.check_returncode()

    @staticmethod
    def _script(fix, repo_path):
        p = subprocess.run([fix], cwd=repo_path)
        p.check_returncode()

    def test_empty_repo(self, capsys, cache_dir):
        # no reports should be generated since the default repo is empty
        with patch('sys.argv', self.args), \
                patch('pkgcheck.const.USER_CACHE_DIR', cache_dir):
            with pytest.raises(SystemExit) as excinfo:
                self.script()
            assert excinfo.value.code == 0
            out, err = capsys.readouterr()
            assert out == err == ''

    @pytest.mark.parametrize(
        'action, module',
        (('init', 'Process'), ('queue', 'UnversionedSource'), ('run', 'CheckRunner.run')))
    def test_pipeline_exceptions(self, action, module, capsys, cache_dir):
        """Test checkrunner pipeline against unhandled exceptions."""
        with patch('sys.argv', self.args), \
                patch('pkgcheck.const.USER_CACHE_DIR', cache_dir), \
                patch(f'pkgcheck.pipeline.{module}') as faked:
            faked.side_effect = Exception('foobar')
            with pytest.raises(SystemExit) as excinfo:
                self.script()
            assert excinfo.value.code == 1
            out, err = capsys.readouterr()
            assert out == ''
            assert err.splitlines()[-1] == 'Exception: foobar'

    results = []
    for name, cls in sorted(objects.CHECKS.items()):
        for result in sorted(cls.known_results, key=attrgetter('__name__')):
            results.append((cls, result))

    def test_pkgcheck_test_repos(self):
        """Make sure the test repos are up to date check/result naming wise."""
        # grab custom targets
        custom_targets = set()
        for repo in os.listdir(self.repos_data):
            for root, _dirs, files in os.walk(pjoin(self.repos_data, repo)):
                for f in files:
                    if f == 'target':
                        with open(pjoin(root, f)) as target:
                            custom_targets.add(target.read().strip())

        # all pkgs that aren't custom targets or stubs must be check/keyword
        for repo_dir in os.listdir(self.repos_dir):
            repo = UnconfiguredTree(pjoin(self.repos_dir, repo_dir))

            # determine pkg stubs added to the repo
            stubs = set()
            try:
                with open(pjoin(repo.location, 'metadata', 'stubs')) as f:
                    stubs.update(x.rstrip() for x in f)
            except FileNotFoundError:
                pass

            allowed = custom_targets | stubs
            results = {(check.__name__, result.__name__) for check, result in self.results}
            for cat, pkgs in sorted(repo.packages.items()):
                if cat.startswith('stub'):
                    continue
                for pkg in sorted(pkgs):
                    if pkg.startswith('stub'):
                        continue
                    if f'{cat}/{pkg}' not in allowed:
                        if pkg in objects.KEYWORDS:
                            assert (cat, pkg) in results
                        else:
                            assert cat in objects.KEYWORDS

    def test_pkgcheck_test_data(self):
        """Make sure the test data is up to date check/result naming wise."""
        for repo in os.listdir(self.repos_data):
            for check in os.listdir(pjoin(self.repos_data, repo)):
                assert check in objects.CHECKS
                for keyword in os.listdir(pjoin(self.repos_data, repo, check)):
                    assert keyword in objects.KEYWORDS

    @pytest.mark.parametrize('check, result', results)
    def test_scan(self, check, result, capsys, cache_dir, tmp_path):
        """Run pkgcheck against test pkgs in bundled repo, verifying result output."""
        tested = False
        check_name = check.__name__
        keyword = result.__name__
        for repo in os.listdir(self.repos_data):
            for verbosity, file in ((0, 'expected'), (1, 'expected-verbose')):
                expected_path = pjoin(self.repos_data, f'{repo}/{check_name}/{keyword}/{file}')
                if not os.path.exists(expected_path):
                    continue

                repo_dir = pjoin(self.repos_dir, repo)

                # create issue related to keyword as required
                trigger = pjoin(self.repos_data, f'{repo}/{check_name}/{keyword}/trigger.sh')
                if os.path.exists(trigger):
                    triggered_repo = str(tmp_path / f'triggered-{repo}')
                    shutil.copytree(repo_dir, triggered_repo)
                    self._script(trigger, triggered_repo)
                    repo_dir = triggered_repo

                args = (['-v'] * verbosity) + ['-r', repo_dir, '-c', check_name, '-k', keyword]

                # add any defined extra repo args
                try:
                    with open(f'{repo_dir}/metadata/pkgcheck-args') as f:
                        args.extend(shlex.split(f.read()))
                except FileNotFoundError:
                    pass

                with open(expected_path) as f:
                    expected = f.read()
                    # JsonStream reporter, cache results to compare against repo run
                    with patch('sys.argv', self.args + ['-R', 'JsonStream'] + args), \
                            patch('pkgcheck.const.USER_CACHE_DIR', cache_dir):
                        with pytest.raises(SystemExit) as excinfo:
                            self.script()
                        out, err = capsys.readouterr()
                        if not verbosity:
                            assert not err
                        assert excinfo.value.code == 0
                        if not expected:
                            assert not out
                        else:
                            results = []
                            lines = out.rstrip('\n').split('\n')
                            for deserialized_result in reporters.JsonStream.from_iter(lines):
                                assert deserialized_result.__class__ == result
                                results.append(deserialized_result)
                                if not verbosity:
                                    self._results[repo].add(deserialized_result)
                            # compare rendered fancy out to expected
                            assert self._render_results(
                                results, verbosity=verbosity) == expected
                tested = True
                self._checks_run[repo].add(check_name)

        if not tested:
            pytest.skip('expected test data not available')

    def _render_results(self, results, **kwargs):
        """Render a given set of result objects into their related string form."""
        with tempfile.TemporaryFile() as f:
            with reporters.FancyReporter(out=PlainTextFormatter(f), **kwargs) as reporter:
                for result in sorted(results):
                    reporter.report(result)
            f.seek(0)
            output = f.read().decode()
            return output

    def test_scan_repos(self, capsys, cache_dir, tmp_path):
        """Verify full repo scans don't return any extra, unknown results."""
        # TODO: replace with matching against expected full scan dump once
        # sorting is implemented
        if not self._results:
            pytest.skip('test_pkgcheck_scan() must be run before this to populate results')
        else:
            for repo in os.listdir(self.repos_data):
                unknown_results = []
                repo_dir = pjoin(self.repos_dir, repo)

                # create issues related to keyword as required
                triggers = []
                for root, _dirs, files in os.walk(pjoin(self.repos_data, repo)):
                    for f in files:
                        if f == 'trigger.sh':
                            triggers.append(pjoin(root, f))
                if triggers:
                    triggered_repo = str(tmp_path / f'triggered-{repo}')
                    shutil.copytree(repo_dir, triggered_repo)
                    for trigger in triggers:
                        self._script(trigger, triggered_repo)
                    repo_dir = triggered_repo

                args = ['-r', repo_dir, '-c', ','.join(self._checks_run[repo])]

                # add any defined extra repo args
                try:
                    with open(f'{repo_dir}/metadata/pkgcheck-args') as f:
                        args.extend(shlex.split(f.read()))
                except FileNotFoundError:
                    pass

                with patch('sys.argv', self.args + ['-R', 'JsonStream'] + args), \
                        patch('pkgcheck.const.USER_CACHE_DIR', cache_dir):
                    with pytest.raises(SystemExit) as excinfo:
                        self.script()
                    out, err = capsys.readouterr()
                    assert out, f'{repo} repo failed, no results'
                    assert excinfo.value.code == 0
                    lines = out.rstrip('\n').split('\n')
                    for result in reporters.JsonStream.from_iter(lines):
                        # ignore results generated from stubs
                        stubs = (getattr(result, x, '') for x in ('category', 'package'))
                        if any(x.startswith('stub') for x in stubs):
                            continue
                        try:
                            self._results[repo].remove(result)
                        except KeyError:
                            unknown_results.append(result)

                if self._results[repo]:
                    output = self._render_results(self._results[repo])
                    pytest.fail(f'{repo} repo missing results:\n{output}')
                if unknown_results:
                    output = self._render_results(unknown_results)
                    pytest.fail(f'{repo} repo has unknown results:\n{output}')

    @pytest.mark.parametrize('check, result', results)
    def test_fix(self, check, result, capsys, cache_dir, tmp_path):
        """Apply fixes to pkgs, verifying the related results are fixed."""
        check_name = check.__name__
        keyword = result.__name__
        tested = False
        for repo in os.listdir(self.repos_data):
            keyword_dir = pjoin(self.repos_data, f'{repo}/{check_name}/{keyword}')
            if os.path.exists(pjoin(keyword_dir, 'fix.patch')):
                fix = pjoin(keyword_dir, 'fix.patch')
                func = self._patch
            elif os.path.exists(pjoin(keyword_dir, 'fix.sh')):
                fix = pjoin(keyword_dir, 'fix.sh')
                func = self._script
            else:
                continue

            # apply a fix if one exists and make sure the related result doesn't appear
            repo_dir = pjoin(self.repos_dir, repo)
            fixed_repo = str(tmp_path / f'fixed-{repo}')
            shutil.copytree(repo_dir, fixed_repo)
            func(fix, fixed_repo)

            args = ['-r', fixed_repo, '-c', check_name, '-k', keyword]

            # add any defined extra repo args
            try:
                with open(f'{repo_dir}/metadata/pkgcheck-args') as f:
                    args.extend(shlex.split(f.read()))
            except FileNotFoundError:
                pass

            cmd = self.args + args
            with patch('sys.argv', cmd), \
                    patch('pkgcheck.const.USER_CACHE_DIR', cache_dir):
                with pytest.raises(SystemExit) as excinfo:
                    self.script()
                out, err = capsys.readouterr()
                assert not err, f"failed fixing error, command: {' '.join(cmd)}"
                assert not out, f"failed fixing error, command: {' '.join(cmd)}"
                assert excinfo.value.code == 0
            shutil.rmtree(fixed_repo)
            tested = True

        if not tested:
            pytest.skip('fix not available')