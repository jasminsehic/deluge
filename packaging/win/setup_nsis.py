#!/usr/bin/env python
#
# Copyright (C) 2012-2015 Calum Lind <calumlind@gmail.com>
# Copyright (C) 2010 Damien Churchill <damoxc@gmail.com>
# Copyright (C) 2009-2010 Andrew Resch <andrewresch@gmail.com>
# Copyright (C) 2009 Jesper Lund <mail@jesperlund.com>
#
# This file is part of Deluge and is licensed under GNU General Public License 3.0, or later, with
# the additional special exception to link portions of this program with the OpenSSL library.
# See LICENSE for more details.
#

import os
import shutil
from pathlib import Path

import deluge.common

# Get build_version from installed deluge.
build_version = deluge.common.get_version()
build_dir = os.path.join('freeze', 'Deluge')

# Copy version info to file for nsis script.
with open('VERSION.tmp', 'w') as ver_file:
    ver_file.write('build_version = "%s"' % build_version)

# PyInstaller may create symlinks in its frozen distribution. NSIS File
# cannot package a symlink reliably on Windows, so materialize its target
# before generating the manifest. Missing symlink targets cannot be packaged.
for root, dirnames, filenames in os.walk(build_dir):
    for filename in dirnames + filenames:
        path = Path(root) / filename
        if not path.is_symlink():
            continue
        target = path.resolve()
        if not target.exists():
            print(f'Skipping dangling frozen symlink: {path}')
            path.unlink()
        elif target.is_dir():
            path.unlink()
            shutil.copytree(target, path)
        else:
            # Read before unlinking, as the symlink may refer to another item
            # in the same frozen distribution.
            data = path.read_bytes()
            path.unlink()
            path.write_bytes(data)

# Create the install and uninstall file list for NSIS. Only real files are
# allowed in File directives; directory links are handled by the walk above.
filedir_list = []
for root, dirnames, filenames in os.walk(build_dir):
    dirnames.sort()
    files = []
    for filename in sorted(filenames):
        path = Path(root) / filename
        if path.is_dir():
            # Some Windows reparse points appear in os.walk's file list.
            print(f'Skipping directory entry in NSIS file list: {path}')
            continue
        if not path.is_file():
            raise FileNotFoundError(f'Frozen manifest entry is not a file: {path}')
        files.append(filename)
    filedir_list.append((root[len(build_dir) :], files))

with open('install_files.nsh', 'w') as f:
    f.write('; Files to install\n')
    for dirname, files in filedir_list:
        if not dirname:
            dirname = os.sep
        f.write('\nSetOutPath "$INSTDIR%s"\n' % dirname)
        for filename in files:
            f.write('File "' + build_dir + os.path.join(dirname, filename) + '"\n')

with open('uninstall_files.nsh', 'w') as f:
    f.write('; Files to uninstall\n')
    for dirname, files in reversed(filedir_list):
        f.write('\n')
        if not dirname:
            dirname = os.sep
        for filename in files:
            f.write('Delete "$INSTDIR%s"\n' % os.path.join(dirname, filename))
        f.write('RMDir "$INSTDIR%s"\n' % dirname)
