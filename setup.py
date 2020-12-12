#!/usr/bin/env python3

import os
import sys
from collections import defaultdict
from distutils import log
from distutils.command import install_data as dst_install_data
from distutils.util import byte_compile
from itertools import chain
from textwrap import dedent

from setuptools import setup
from snakeoil.dist import distutils_extensions as pkgdist

pkgdist_setup, pkgdist_cmds = pkgdist.setup()

DATA_INSTALL_OFFSET = 'share/pkgcheck'


class build_py(pkgdist.build_py):
    """Build wrapper to generate pkgcheck-related files."""

    def run(self):
        super().run()
        # create tree-sitter bash-parsing library
        from tree_sitter import Language
        lib_path = os.path.join(self.build_lib, pkgdist.MODULE_NAME, '_bash-lang.so')
        Language.build_library(lib_path, ['tree-sitter-bash'])


class install(pkgdist.install):
    """Install wrapper to generate and install pkgcheck-related files."""

    def run(self):
        super().run()
        target = self.install_data
        root = self.root or '/'
        if target.startswith(root):
            target = os.path.join('/', os.path.relpath(target, root))
        target = os.path.abspath(target)

        if not self.dry_run:
            # Install configuration data so the program can find its content,
            # rather than assuming it is running from a tarball/git repo.
            write_obj_lists(self.install_purelib, target)


def write_obj_lists(python_base, install_prefix):
    """Generate config file of keyword, check, and other object lists."""
    objects_path = os.path.join(python_base, pkgdist.MODULE_NAME, "_objects.py")
    os.makedirs(os.path.dirname(objects_path), exist_ok=True)
    log.info(f'writing config to {objects_path!r}')

    # hack to drop quotes on modules in generated files
    class _kls:

        def __init__(self, module):
            self.module = module

        def __repr__(self):
            return self.module

    with pkgdist.syspath(pkgdist.PACKAGEDIR):
        from pkgcheck import objects

    modules = defaultdict(set)
    objs = defaultdict(list)
    for obj in ('KEYWORDS', 'CHECKS', 'REPORTERS'):
        for name, cls in getattr(objects, obj).items():
            parent, module = cls.__module__.rsplit('.', 1)
            modules[parent].add(module)
            objs[obj].append((name, _kls(f'{module}.{name}')))

    keywords = tuple(objs['KEYWORDS'])
    checks = tuple(objs['CHECKS'])
    reporters = tuple(objs['REPORTERS'])

    with open(objects_path, 'w') as f:
        os.chmod(objects_path, 0o644)
        for k, v in sorted(modules.items()):
            f.write(f"from {k} import {', '.join(sorted(v))}\n")
        f.write(dedent(f"""\
            KEYWORDS = {keywords}
            CHECKS = {checks}
            REPORTERS = {reporters}
        """))

    const_path = os.path.join(python_base, pkgdist.MODULE_NAME, "_const.py")
    with open(const_path, 'w') as f:
        os.chmod(const_path, 0o644)
        # write install path constants to config
        if install_prefix != os.path.abspath(sys.prefix):
            # write more dynamic _const file for wheel installs
            f.write(dedent("""\
                import os.path as osp
                import sys
                INSTALL_PREFIX = osp.abspath(sys.prefix)
                DATA_PATH = osp.join(INSTALL_PREFIX, {!r})
            """.format(DATA_INSTALL_OFFSET)))
        else:
            f.write("INSTALL_PREFIX=%r\n" % install_prefix)
            f.write("DATA_PATH=%r\n" %
                    os.path.join(install_prefix, DATA_INSTALL_OFFSET))

    # only optimize during install, skip during wheel builds
    if install_prefix == os.path.abspath(sys.prefix):
        for path in (const_path, objects_path):
            byte_compile([path], prefix=python_base)
            byte_compile([path], optimize=1, prefix=python_base)
            byte_compile([path], optimize=2, prefix=python_base)


class install_data(dst_install_data.install_data):
    """Generate data files for install.

    Currently this includes keyword, check, and reporter name lists.
    """

    def run(self):
        self._generate_files()
        super().run()

    def _generate_files(self):
        with pkgdist.syspath(pkgdist.PACKAGEDIR):
            from pkgcheck import base, caches, objects

        os.makedirs(os.path.join(pkgdist.REPODIR, '.generated'), exist_ok=True)
        files = []

        # generate available scopes
        path = os.path.join(pkgdist.REPODIR, '.generated', 'scopes')
        with open(path, 'w') as f:
            f.write('\n'.join(base.scopes) + '\n')
        files.append(os.path.join('.generated', 'scopes'))

        # generate available cache types
        path = os.path.join(pkgdist.REPODIR, '.generated', 'caches')
        cache_objs = caches.CachedAddon.caches.values()
        with open(path, 'w') as f:
            f.write('\n'.join(x.type for x in cache_objs))
        files.append(os.path.join('.generated', 'caches'))

        # generate available object lists
        for obj in ('KEYWORDS', 'CHECKS', 'REPORTERS'):
            log.info(f'Generating {obj.lower()} list')
            path = os.path.join(pkgdist.REPODIR, '.generated', obj.lower())
            with open(path, 'w') as f:
                f.write('\n'.join(getattr(objects, obj)) + '\n')
            files.append(os.path.join('.generated', obj.lower()))
        self.data_files.append(('share/pkgcheck', files))


class test(pkgdist.pytest):
    """Wrapper to enforce testing against built version."""

    def run(self):
        # This is fairly hacky, but is done to ensure that the tests
        # are ran purely from what's in build, reflecting back to the source config data.
        key = 'PKGCHECK_OVERRIDE_REPO_PATH'
        original = os.environ.get(key)
        try:
            os.environ[key] = os.path.dirname(os.path.realpath(__file__))
            super().run()
        finally:
            if original is not None:
                os.environ[key] = original
            else:
                os.environ.pop(key, None)


setup(**dict(
    pkgdist_setup,
    license='BSD',
    author='Tim Harder',
    author_email='radhermit@gmail.com',
    description='pkgcore-based QA utility for ebuild repos',
    url='https://github.com/pkgcore/pkgcheck',
    data_files=list(chain(
        pkgdist.data_mapping('share/zsh/site-functions', 'completion/zsh'),
        pkgdist.data_mapping('share/pkgcheck', 'data'),
    )),
    cmdclass=dict(
        pkgdist_cmds,
        test=test,
        build_py=build_py,
        install_data=install_data,
        install=install,
    ),
    classifiers=[
        'License :: OSI Approved :: BSD License',
        'Programming Language :: Python :: 3.8',
        'Programming Language :: Python :: 3.9',
    ],
    extras_require={
        'network': ['requests'],
    },
))
