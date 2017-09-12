import os
import tempfile
import uuid

from pkgcore.test.misc import FakeRepo
from snakeoil import fileutils
from snakeoil.fileutils import touch
from snakeoil.osutils import pjoin
from snakeoil.test.mixins import TempDirMixin

from pkgcheck import pkgdir_checks
from pkgcheck.test import misc


class PkgDirReportTest(TempDirMixin, misc.ReportTestCase):
    """Various FILESDIR related test support."""

    check_kls = pkgdir_checks.PkgDirReport

    def setUp(self):
        TempDirMixin.setUp(self)
        self.check = pkgdir_checks.PkgDirReport(None, None)
        self.repo = FakeRepo(repo_id='repo', location=self.dir)

    def tearDown(self):
        TempDirMixin.tearDown(self)

    def mk_pkg(self, files={}):
        # generate random cat/PN
        category = uuid.uuid4().hex
        PN = uuid.uuid4().hex
        self.pkg = "%s/%s-0.7.1" % (category, PN)
        self.filesdir = pjoin(self.repo.location, category, PN, 'files')
        os.makedirs(self.filesdir)

        # create specified files in FILESDIR
        for fn, contents in files.iteritems():
            fileutils.write_file(pjoin(self.filesdir, fn), 'w', contents)

        return misc.FakeFilesDirPkg(self.pkg, repo=self.repo)


class TestDuplicateFilesReport(PkgDirReportTest):
    """Check DuplicateFiles results."""

    def test_it(self):
        # empty filesdir
        self.assertNoReport(self.check, [self.mk_pkg()])

        # filesdir with two unique files
        self.assertNoReport(self.check, [self.mk_pkg({'test': 'abc', 'test2': 'bcd'})])

        # filesdir with a duplicate
        r = self.assertReport(self.check, [self.mk_pkg({'test': 'abc', 'test2': 'abc'})])
        self.assertIsInstance(r, pkgdir_checks.DuplicateFiles)
        self.assertEqual(r.files, ('files/test', 'files/test2'))
        self.assertEqual(r.files, ('files/test', 'files/test2'))

        # two sets of duplicates and one unique
        r = self.assertReports(self.check, [self.mk_pkg(
            {'test': 'abc', 'test2': 'abc', 'test3': 'bcd', 'test4': 'bcd', 'test5': 'zzz'})])
        self.assertLen(r, 2)
        self.assertIsInstance(r[0], pkgdir_checks.DuplicateFiles)
        self.assertIsInstance(r[1], pkgdir_checks.DuplicateFiles)
        self.assertEqual(
            tuple(sorted(x.files for x in r)),
            (('files/test', 'files/test2'), ('files/test3', 'files/test4'))
        )


class TestEmptyFileReport(PkgDirReportTest):
    """Check EmptyFile results."""

    def test_it(self):
        # empty filesdir
        self.assertNoReport(self.check, [self.mk_pkg()])

        # filesdir with an empty file
        self.assertIsInstance(
            self.assertReport(self.check, [self.mk_pkg({'test': ''})]),
            pkgdir_checks.EmptyFile)

        # filesdir with a non-empty file
        self.assertNoReport(self.check, [self.mk_pkg({'test': 'asdfgh'})])

        # a mix of both
        r = self.assertIsInstance(
            self.assertReport(self.check, [self.mk_pkg({'test': 'asdfgh', 'test2': ''})]),
            pkgdir_checks.EmptyFile)
        self.assertEqual(r.filename, 'files/test2')
        r = self.assertIsInstance(
            self.assertReport(self.check, [self.mk_pkg({'test': '', 'test2': 'asdfgh'})]),
            pkgdir_checks.EmptyFile)
        self.assertEqual(r.filename, 'files/test')

        # two empty files
        r = self.assertReports(self.check, [self.mk_pkg({'test': '', 'test2': ''})])
        self.assertLen(r, 2)
        self.assertIsInstance(r[0], pkgdir_checks.EmptyFile)
        self.assertIsInstance(r[1], pkgdir_checks.EmptyFile)
        self.assertEqual(sorted(x.filename for x in r), ['files/test', 'files/test2'])


class TestMismatchedPN(PkgDirReportTest):
    """Check MismatchedPN results."""

    def test_it(self):
        # no files
        self.assertNoReport(self.check, [self.mk_pkg()])

        # multiple regular ebuilds
        pkg = self.mk_pkg()
        touch(pjoin(os.path.dirname(pkg.path), '%s-0.ebuild' % pkg.package))
        touch(pjoin(os.path.dirname(pkg.path), '%s-1.ebuild' % pkg.package))
        touch(pjoin(os.path.dirname(pkg.path), '%s-2.ebuild' % pkg.package))
        self.assertNoReport(self.check, [pkg])

        # single, mismatched ebuild
        pkg = self.mk_pkg()
        touch(pjoin(os.path.dirname(pkg.path), 'mismatched-0.ebuild'))
        r = self.assertReport(self.check, [pkg])
        self.assertIsInstance(r, pkgdir_checks.MismatchedPN)
        self.assertEqual(r.ebuilds, ('mismatched-0',))

        # multiple ebuilds, multiple mismatched
        pkg = self.mk_pkg()
        touch(pjoin(os.path.dirname(pkg.path), '%s-0.ebuild' % pkg.package))
        touch(pjoin(os.path.dirname(pkg.path), '%s-1.ebuild' % pkg.package))
        touch(pjoin(os.path.dirname(pkg.path), 'mismatched-0.ebuild'))
        touch(pjoin(os.path.dirname(pkg.path), 'abc-1.ebuild'))
        r = self.assertReport(self.check, [pkg])
        self.assertIsInstance(r, pkgdir_checks.MismatchedPN)
        self.assertEqual(r.ebuilds, ('abc-1', 'mismatched-0'))


class TestExecutableFile(PkgDirReportTest):
    """Check ExecutableFile results."""

    def test_it(self):
        # no files
        self.assertNoReport(self.check, [self.mk_pkg()])

        # non-empty filesdir
        self.assertNoReport(self.check, [self.mk_pkg({'test': 'asdfgh'})])

        # executable ebuild
        pkg = self.mk_pkg()
        touch(pkg.path, mode=0o777)
        r = self.assertReport(self.check, [pkg])
        self.assertIsInstance(r, pkgdir_checks.ExecutableFile)
        self.assertEqual(r.filename, os.path.basename(pkg.path))

        # executable Manifest and metadata
        pkg = self.mk_pkg()
        touch(pjoin(os.path.dirname(pkg.path), 'Manifest'), mode=0o755)
        touch(pjoin(os.path.dirname(pkg.path), 'metadata.xml'), mode=0o744)
        r = self.assertReports(self.check, [pkg])
        self.assertLen(r, 2)
        self.assertIsInstance(r[0], pkgdir_checks.ExecutableFile)
        self.assertIsInstance(r[1], pkgdir_checks.ExecutableFile)
        self.assertEqual(
            tuple(sorted(x.filename for x in r)),
            ('Manifest', 'metadata.xml')
        )

        # mix of regular files and executable FILESDIR file
        pkg = self.mk_pkg({'foo.init': 'blah'})
        touch(pkg.path)
        touch(pjoin(os.path.dirname(pkg.path), 'Manifest'))
        touch(pjoin(os.path.dirname(pkg.path), 'metadata.xml'))
        os.chmod(pjoin(os.path.dirname(pkg.path), 'files', 'foo.init'), 0o645)
        r = self.assertReport(self.check, [pkg])
        self.assertIsInstance(r, pkgdir_checks.ExecutableFile)
        self.assertEqual(r.filename, 'files/foo.init')
