#!/usr/bin/env python3
"""Create a local source snapshot using only the explicit application allowlist."""
from pathlib import Path
import tarfile
import gzip
import hashlib
import os
import re

ROOT = Path(__file__).resolve().parents[1]
VERSION = '0.2.0'


def source_files():
    for name in ('README.md', 'install.sh'):
        yield ROOT / name
    for directory, suffix in (('postroom', '.py'), ('tests', '.py'), ('tests', '.sh'), ('data', '.desktop'),
                              ('LICENSES', '.txt')):
        yield from sorted(path for path in (ROOT / directory).rglob('*' + suffix)
                          if not path.is_symlink())
    for name in ('postroom', 'stage.sh', 'build_deb.py', 'postroom.spec', 'make_source.py'):
        yield ROOT / 'packaging' / name


def main():
    archive = ROOT / f'postroom-{VERSION}.tar.gz'
    epoch = int(os.environ.get('SOURCE_DATE_EPOCH', '0'))
    with archive.open('wb') as output, gzip.GzipFile(filename='', mode='wb', fileobj=output, mtime=epoch) as zipped:
        with tarfile.open(fileobj=zipped, mode='w') as tar:
            for path in source_files():
                if path.is_symlink():
                    raise ValueError(f'Symlink is not a source file: {path}')
                info = tar.gettarinfo(str(path), arcname=f'postroom-{VERSION}/{path.relative_to(ROOT)}')
                info.uid = info.gid = 0
                info.uname = info.gname = ''
                info.mtime = epoch
                info.mode = 0o644
                with path.open('rb') as stream:
                    tar.addfile(info, stream)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    pkgbuild = ROOT / 'PKGBUILD'
    text, count = re.subn(r"^sha256sums=\([^\n]*\)$", f"sha256sums=('{digest}')", pkgbuild.read_text(), flags=re.MULTILINE)
    if count != 1:
        raise ValueError('Could not update the PKGBUILD checksum.')
    pkgbuild.write_text(text)
    print(archive)


if __name__ == '__main__':
    main()
