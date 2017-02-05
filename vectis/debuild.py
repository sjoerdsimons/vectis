# Copyright © 2016 Simon McVittie
# SPDX-License-Identifier: GPL-2.0+
# (see vectis/__init__.py)

import glob
import logging
import os
import subprocess
import textwrap
import time
from collections import (
        OrderedDict,
        )
from tempfile import TemporaryDirectory

from debian.changelog import (
        Changelog,
        )
from debian.deb822 import (
        Changes,
        Deb822,
        Dsc,
        )
from debian.debian_support import (
        Version,
        )

from vectis.error import (
        ArgumentError,
        CannotHappen,
        )
from vectis.util import (
        AtomicWriter,
        )

logger = logging.getLogger(__name__)

class Buildable:
    def __init__(self, buildable, *, vendor):
        self.buildable = buildable

        self._product_prefix = None
        self.arch_wildcards = set()
        self.archs = []
        self.binary_packages = []
        self.changes_produced = {}
        self.dirname = None
        self.dsc = None
        self.dsc_name = None
        self.indep = False
        self.logs = {}
        self.merged_changes = OrderedDict()
        self.nominal_suite = None
        self.source_from_archive = False
        self.source_package = None
        self.sourceful_changes_name = None
        self.suite = None
        self.together_with = None
        self.vendor = vendor
        self.version = None

        if os.path.exists(self.buildable):
            if os.path.isdir(self.buildable):
                changelog = os.path.join(self.buildable, 'debian', 'changelog')
                changelog = Changelog(open(changelog))
                self.source_package = changelog.get_package()
                self.nominal_suite = changelog.distributions
                self.version = Version(changelog.version)
                control = os.path.join(self.buildable, 'debian', 'control')

                if len(changelog.distributions.split()) != 1:
                    raise ArgumentError('Cannot build for multiple '
                            'distributions at once')

                for paragraph in Deb822.iter_paragraphs(open(control)):
                    self.arch_wildcards |= set(
                            paragraph.get('architecture', '').split())
                    binary = paragraph.get('package')

                if binary is not None:
                    self.binary_packages.append(binary)

            elif self.buildable.endswith('.changes'):
                self.dirname = os.path.dirname(self.buildable)
                self.sourceful_changes_name = self.buildable
                sourceful_changes = Changes(open(self.buildable))
                if 'source' not in sourceful_changes['architecture']:
                    raise ArgumentError('Changes file {!r} must be '
                            'sourceful'.format(self.buildable))

                self.nominal_suite = sourceful_changes['distribution']

                for f in sourceful_changes['files']:
                    if f['name'].endswith('.dsc'):
                        if self.dsc_name is not None:
                            raise ArgumentError('Changes file {!r} contained '
                                    'more than one .dsc '
                                    'file'.format(self.buildable))

                        self.dsc_name = os.path.join(self.dirname, f['name'])

                if self.dsc_name is None:
                    raise ArgumentError('Changes file {!r} did not contain a '
                            '.dsc file'.format(self.buildable))

                self.dsc = Dsc(open(self.dsc_name))

            elif self.buildable.endswith('.dsc'):
                self.dirname = os.path.dirname(self.buildable)
                self.dsc_name = self.buildable
                self.dsc = Dsc(open(self.dsc_name))

            else:
                raise ArgumentError('buildable must be .changes, .dsc or '
                        'directory, not {!r}'.format(self.buildable))
        else:
            self.source_from_archive = True

            if '_' in self.buildable:
                source, version = self.buildable.split('_', 1)
            else:
                source = self.buildable
                version = None

            self.source_package = source
            self.version = Version(version)

        if self.dsc is not None:
            self.source_package = self.dsc['source']
            self.version = Version(self.dsc['version'])
            self.arch_wildcards = set(self.dsc['architecture'].split())
            self.binary_packages = [p.strip()
                    for p in self.dsc['binary'].split(',')]

    @property
    def product_prefix(self):
        if self._product_prefix is None:
            version_no_epoch = Version(self.version)
            version_no_epoch.epoch = None
            self._product_prefix = '{}_{}'.format(self.source_package,
                    version_no_epoch)

        return self._product_prefix

    def copy_source_to(self, machine):
        machine.check_call(['mkdir', '-p', '-m755',
            '{}/in'.format(machine.scratch)])

        if self.dsc_name is not None:
            assert self.dsc is not None

            machine.copy_to_guest(self.dsc_name,
                    '{}/in/{}'.format(machine.scratch,
                        os.path.basename(self.dsc_name)))

            for f in self.dsc['files']:
                machine.copy_to_guest(os.path.join(self.dirname, f['name']),
                        '{}/in/{}'.format(machine.scratch, f['name']))
        elif not self.source_from_archive:
            machine.copy_to_guest(os.path.join(self.buildable, ''),
                    '{}/in/{}_source/'.format(machine.scratch,
                        self.product_prefix))
            machine.check_call(['chown', '-R', 'sbuild:sbuild',
                    '{}/in/'.format(machine.scratch)])
            if self.version.debian_revision is not None:
                machine.check_call(['install', '-d', '-m755',
                    '-osbuild', '-gsbuild',
                    '{}/out'.format(machine.scratch)])

                orig_pattern = glob.escape(os.path.join(self.buildable, '..',
                        '{}_{}.orig.tar.'.format(self.source_package,
                            self.version.upstream_version))) + '*'
                logger.info('Looking for original tarballs: {}'.format(
                        orig_pattern))

                for orig in glob.glob(orig_pattern):
                    logger.info('Copying original tarball: {}'.format(orig))
                    machine.copy_to_guest(orig,
                            '{}/in/{}'.format(machine.scratch,
                                os.path.basename(orig)))
                    machine.check_call(['ln', '-s',
                            '{}/in/{}'.format(machine.scratch,
                                os.path.basename(orig)),
                            '{}/out/{}'.format(machine.scratch,
                                os.path.basename(orig))])

    def select_archs(self, machine_arch, archs, indep, together):
        builds_i386 = False
        builds_natively = False

        for wildcard in self.arch_wildcards:
            if subprocess.call(['dpkg-architecture',
                    '-a' + machine_arch, '--is', wildcard]) == 0:
                logger.info('Package builds natively on %s', machine_arch)
                builds_natively = True

            if subprocess.call(['dpkg-architecture',
                    '-ai386', '--is', wildcard]) == 0:
                logger.info('Package builds on i386')
                builds_i386 = True

        if archs or indep:
            # the user is always right
            logger.info('Using architectures from command-line')
            self.archs = archs[:]
        else:
            logger.info('Choosing architectures to build')
            indep = ('all' in self.arch_wildcards)
            self.archs = []

            if builds_natively:
                self.archs.append(machine_arch)

            for line in subprocess.check_output([
                    'sh', '-c', '"$@" || :',
                    'sh', # argv[0]
                    'dpkg-query', '-W', r'--showformat=${binary:Package}\n',
                    ] + [p.strip() for p in self.binary_packages],
                    universal_newlines=True).splitlines():
                if ':' in line:
                    arch = line.split(':')[-1]
                    if arch not in self.archs:
                        logger.info('Building on %s because %s is installed',
                                arch, line)
                        self.archs.append(arch)

            if (machine_arch == 'amd64' and builds_i386 and
                    not builds_natively and 'i386' not in self.archs):
                self.archs.append('i386')

        if 'all' not in self.arch_wildcards:
            indep = False

        if indep:
            if together and self.archs:
                if machine_arch in self.archs:
                    self.together_with = machine_arch
                else:
                    self.together_with = self.archs[0]
            else:
                self.archs.insert(0, 'all')

        logger.info('Selected architectures: %r', self.archs)

        if indep and self.together_with is not None:
            logger.info('Architecture-independent packages will be built '
                        'alongside %s', self.together_with)

    def select_suite(self, suite):
        self.suite = self.nominal_suite

        if suite is not None:
            self.suite = suite

            if self.nominal_suite is None:
                self.nominal_suite = suite

        if self.suite is None:
            raise ArgumentError('Must specify --suite when building from '
                    '{!r}'.format(self.buildable))

    def __str__(self):
        return self.buildable

class Build:
    def __init__(self, buildable, arch, machine, *,
            dpkg_buildpackage_options,
            dpkg_source_options,
            output_builds,
            storage,
            suite,
            components=(),
            extra_repositories=()):
        self.arch = arch
        self.buildable = buildable
        self.components = components
        self.dpkg_buildpackage_options = dpkg_buildpackage_options
        self.dpkg_source_options = dpkg_source_options
        self.extra_repositories = extra_repositories
        self.machine = machine
        self.output_builds = output_builds
        self.storage = storage
        self.suite = suite

    def sbuild(self):
        self.machine.check_call(['install', '-d', '-m755',
            '-osbuild', '-gsbuild',
            '{}/out'.format(self.machine.scratch)])

        logger.info('Building architecture: %s', self.arch)

        if self.arch in ('all', 'source'):
            logger.info('(on %s)', self.machine.dpkg_architecture)
            use_arch = self.machine.dpkg_architecture
        else:
            use_arch = self.arch

        hierarchy = self.suite.hierarchy

        sbuild_tarball = (
                'sbuild-{vendor}-{base}-{arch}.tar.gz'.format(
                    arch=use_arch,
                    vendor=self.buildable.vendor,
                    base=hierarchy[-1],
                    ))

        self.machine.copy_to_guest(os.path.join(self.storage,
                    sbuild_tarball),
                '{}/in/{}'.format(self.machine.scratch, sbuild_tarball),
                cache=True)

        chroot = '{base}-{arch}-sbuild'.format(base=hierarchy[-1],
                arch=use_arch)


        with TemporaryDirectory() as tmp:
            with AtomicWriter(os.path.join(tmp, 'sbuild.conf')) as writer:
                writer.write(textwrap.dedent('''
                [{chroot}]
                type=file
                description=An autobuilder
                file={scratch}/in/{sbuild_tarball}
                groups=root,sbuild
                root-groups=root,sbuild
                profile=sbuild
                ''').format(
                    chroot=chroot,
                    sbuild_tarball=sbuild_tarball,
                    scratch=self.machine.scratch))
            self.machine.copy_to_guest(os.path.join(tmp, 'sbuild.conf'),
                    '/etc/schroot/chroot.d/{}'.format(chroot))

        argv = [
                self.machine.command_wrapper,
                '--chdir',
                '{}/out'.format(self.machine.scratch),
                '--',
                'runuser',
                '-u', 'sbuild',
                '--',
                'sbuild',
                '-c', chroot,
                '-d', self.buildable.nominal_suite,
                '--no-run-lintian',
        ]

        for x in self.dpkg_buildpackage_options:
            argv.append('--debbuildopt=' + x)

        for x in self.dpkg_source_options:
            argv.append('--dpkg-source-opt=' + x)

        for child in hierarchy[:-1]:
            argv.append('--extra-repository')
            argv.append('deb {} {} {}'.format(
                child.mirror,
                child.apt_suite,
                ' '.join(set(self.components or child.components) &
                    child.all_components)))

            if child.sbuild_resolver:
                argv.extend(child.sbuild_resolver)

        for x in self.extra_repositories:
            argv.append('--extra-repository')
            argv.append(x)

        if self.arch == 'all':
            logger.info('Architecture: all')
            argv.append('-A')
            argv.append('--no-arch-any')
        elif self.arch == self.buildable.together_with:
            logger.info('Architecture: %s + all', self.arch)
            argv.append('-A')
            argv.append('--arch')
            argv.append(self.arch)
        elif self.arch == 'source':
            logger.info('Source-only')
            argv.append('--no-arch-any')
            argv.append('--source')
        else:
            logger.info('Architecture: %s only', self.arch)
            argv.append('--arch')
            argv.append(self.arch)

        if self.buildable.dsc_name is not None:
            if 'source' in self.buildable.changes_produced:
                argv.append('{}/out/{}'.format(self.machine.scratch,
                    os.path.basename(self.buildable.dsc_name)))
            else:
                argv.append('{}/in/{}'.format(self.machine.scratch,
                    os.path.basename(self.buildable.dsc_name)))
        elif self.buildable.source_from_archive:
            argv.append(self.buildable.buildable)
        else:
            # build a source package as a side-effect of the first build
            # (in practice this will be the 'source' build)
            argv.append('--no-clean-source')
            argv.append('--source')
            argv.append('{}/in/{}_source'.format(self.machine.scratch,
                self.buildable.product_prefix))

        logger.info('Running %r', argv)
        try:
            self.machine.check_call(argv)
        finally:
            # Note that we mix use_arch and arch here: an Architecture: all
            # build produces foo_1.2_amd64.build, which we rename.
            # We also check for foo_amd64.build because
            # that's what comes out if we do "vectis sbuild --suite=sid hello".
            for prefix in (self.buildable.source_package,
                    self.buildable.product_prefix):
                product = '{}/out/{}_{}.build'.format(self.machine.scratch,
                        prefix, use_arch)
                product = self.machine.check_output(['readlink', '-f', product],
                        universal_newlines=True).rstrip('\n')

                if (self.machine.call(['test', '-e', product]) == 0 and
                        self.output_builds is not None):
                    logger.info('Copying %s back to host as %s_%s.build...',
                            product, self.buildable.product_prefix, self.arch)
                    copied_back = os.path.join(self.output_builds,
                            '{}_{}_{}.build'.format(self.buildable.product_prefix,
                                self.arch,
                                time.strftime('%Y%m%dt%H%M%S', time.gmtime())))
                    self.machine.copy_to_host(product, copied_back)
                    self.buildable.logs[self.arch] = copied_back

                    symlink = os.path.join(self.output_builds,
                            '{}_{}.build'.format(self.buildable.product_prefix,
                                self.arch))
                    try:
                        os.remove(symlink)
                    except FileNotFoundError:
                        pass

                    os.symlink(os.path.abspath(copied_back), symlink)
                    break
            else:
                logger.warning('Did not find build log at %s', product)
                logger.warning('Possible build logs:\n%s',
                        self.machine.check_call(['sh', '-c',
                            'cd "$1"; ls -l *.build || :',
                            'sh', # argv[0]
                            self.machine.scratch]))

        if self.arch == 'source' and self.buildable.source_from_archive:
            dscs = self.machine.check_output(['sh', '-c',
                'exec ls "$1"/*.dsc',
                'sh', # argv[0]
                self.machine.scratch], universal_newlines=True)

            dscs = dscs.splitlines()
            if len(dscs) != 1:
                raise CannotHappen('sbuild --source produced more than one '
                        '.dsc file from {!r}'.format(self.buildable))

            product = dscs[0]

            with TemporaryDirectory() as tmp:
                copied_back = os.path.join(tmp,
                        '{}.dsc'.format(self.buildable.buildable))
                self.machine.copy_to_host(product, copied_back)

                self.buildable.dsc = Dsc(open(copied_back))
                self.buildable.source_package = self.buildable.dsc['source']
                self.buildable.version = Version(self.buildable.dsc['version'])
                self.buildable.arch_wildcards = set(
                        self.buildable.dsc['architecture'].split())
                self.buildable.binary_packages = [p.strip()
                        for p in self.buildable.dsc['binary'].split(',')]

        if self.output_builds is None:
            return

        product = '{}/out/{}_{}.changes'.format(self.machine.scratch,
            self.buildable.product_prefix,
            self.arch)

        logger.info('Copying %s back to host...', product)
        copied_back = os.path.join(self.output_builds,
                '{}_{}.changes'.format(self.buildable.product_prefix,
                    self.arch))
        self.machine.copy_to_host(product, copied_back)
        self.buildable.changes_produced[self.arch] = copied_back

        changes_out = Changes(open(copied_back))

        if self.arch == 'source':
            self.buildable.dsc_name = None
            self.buildable.sourceful_changes_name = copied_back

            for f in changes_out['files']:
                if f['name'].endswith('.dsc'):
                    # expect to find exactly one .dsc file
                    assert self.buildable.dsc_name is None
                    self.buildable.dsc_name = os.path.join(self.output_builds,
                            f['name'])

            assert self.buildable.dsc_name is not None
            # Save some space
            self.machine.check_call(['rm', '-fr',
                    '{}/in/{}_source/'.format(self.machine.scratch,
                        self.buildable.product_prefix)])

        for f in changes_out['files']:
            assert '/' not in f['name']
            assert not f['name'].startswith('.')

            logger.info('Additionally copying %s back to host...',
                    f['name'])
            product = '{}/out/{}'.format(self.machine.scratch, f['name'])
            copied_back = os.path.join(self.output_builds, f['name'])
            self.machine.copy_to_host(product, copied_back)