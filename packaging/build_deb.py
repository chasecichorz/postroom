#!/usr/bin/env python3
"""Build postroom_VERSION_all.deb for Debian and Ubuntu.

Needs only Python and a POSIX shell, so it also runs on Arch or Fedora:

    python3 packaging/build_deb.py          # writes dist/postroom_<version>_all.deb
    sudo apt install ./dist/postroom_<version>_all.deb
"""
from __future__ import annotations

import gzip
import hashlib
import io
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = re.search(r'"(.+)"', (ROOT / "postroom" / "__init__.py").read_text()).group(1)

CONTROL = f"""\
Package: postroom
Version: {VERSION}-1
Architecture: all
Maintainer: Postroom developers <postroom@users.noreply.github.com>
Section: mail
Priority: optional
Depends: python3 (>= 3.10), python3-gi, gir1.2-gtk-4.0, gir1.2-secret-1, libsecret-1-0
Recommends: speech-dispatcher, libnotify-bin, pipewire-bin | pulseaudio-utils | alsa-utils
Homepage: https://github.com/chasecichorz/postroom
Description: Accessible keyboard-first GTK email client
 Postroom is an IMAP and SMTP email client built for screen reader users,
 with separate Accounts and Folders panes and QuickMail-style shortcuts.
"""


def _tar_gz(root: Path, mtime: int, extra: dict[str, bytes] | None = None) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=mtime) as gz, \
            tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as tar:
        def norm(info: tarfile.TarInfo) -> tarfile.TarInfo:
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mtime = mtime
            return info
        if root is not None:
            for path in sorted(root.rglob("*")):
                tar.add(path, arcname="./" + str(path.relative_to(root)), recursive=False, filter=norm)
        for name, data in (extra or {}).items():
            info = norm(tarfile.TarInfo("./" + name))
            info.size, info.mode = len(data), 0o644
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _ar(members: list[tuple[str, bytes]], mtime: int) -> bytes:
    out = bytearray(b"!<arch>\n")
    for name, data in members:
        out += f"{name:<16}{mtime:<12}{0:<6}{0:<6}{'100644':<8}{len(data):<10}`\n".encode()
        out += data
        if len(data) % 2:
            out += b"\n"
    return bytes(out)


def main() -> int:
    mtime = int(os.environ.get("SOURCE_DATE_EPOCH", time.time()))
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / "root"
        subprocess.run(["sh", str(ROOT / "packaging" / "stage.sh"), str(stage), "/usr"], check=True)
        # Debian keeps license texts under /usr/share/doc/<package>.
        lic = stage / "usr/share/licenses"
        doc = stage / "usr/share/doc/postroom"
        for f in (lic / "postroom").iterdir():
            f.rename(doc / f.name)
        (lic / "postroom").rmdir()
        lic.rmdir()
        (doc / "copyright").write_text(
            "Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/\n"
            "Upstream-Name: postroom\n\n"
            "Files: *\nLicense: GPL-3.0-or-later\n\n"
            "Files: postroom/autoconfig.py postroom/oauth2.py\nLicense: MPL-2.0\n")
        sums = "".join(
            f"{hashlib.md5(p.read_bytes()).hexdigest()}  {p.relative_to(stage)}\n"
            for p in sorted(stage.rglob("*")) if p.is_file())
        size_kib = sum(p.stat().st_size for p in stage.rglob("*") if p.is_file()) // 1024 + 1
        control = CONTROL.replace("Section:", f"Installed-Size: {size_kib}\nSection:", 1)
        control_tar = _tar_gz(None, mtime, {"control": control.encode(), "md5sums": sums.encode()})
        data_tar = _tar_gz(stage, mtime)
    deb = dist / f"postroom_{VERSION}-1_all.deb"
    deb.write_bytes(_ar([("debian-binary", b"2.0\n"), ("control.tar.gz", control_tar),
                         ("data.tar.gz", data_tar)], mtime))
    print(deb)
    return 0


if __name__ == "__main__":
    sys.exit(main())
