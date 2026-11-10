#!/bin/sh
# Build Postroom as a native package for this Linux distribution and install it.
#
#   sh install.sh               build and install (asks for your sudo password)
#   sh install.sh --build-only  only build the package into dist/ (or the checkout, on Arch)
#   sh install.sh --generic     copy into /usr/local without a package (other distributions)
#   sh install.sh --uninstall   remove Postroom again
#
# Arch uses PKGBUILD and makepkg, Debian and Ubuntu get a .deb, Fedora gets an .rpm.
# Your settings in ~/.config/postroom are never touched.
set -eu

root=$(cd "$(dirname "$0")" && pwd)
cd "$root"
version=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' postroom/__init__.py)
mode=install
case "${1:-}" in
    "") ;;
    --build-only) mode=build ;;
    --generic) mode=generic ;;
    --uninstall) mode=uninstall ;;
    -h|--help) sed -n '2,10s/^# \{0,1\}//p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1. Try: sh install.sh --help" >&2; exit 2 ;;
esac

say() { printf '%s\n' "$*"; }

if [ "$(id -u)" -eq 0 ]; then
    say "Run this as your normal user, not root. It asks for sudo only when installing."
    exit 1
fi

family=other
if [ -r /etc/os-release ]; then
    . /etc/os-release
    case " ${ID:-} ${ID_LIKE:-} " in
        *" arch "*) family=arch ;;
        *" debian "*|*" ubuntu "*) family=debian ;;
        *" fedora "*|*" rhel "*) family=fedora ;;
    esac
fi
[ "$mode" = generic ] && family=generic

if [ "$mode" = uninstall ]; then
    [ -d /usr/local/share/postroom ] && family=generic
    case "$family" in
        arch) sudo pacman -R postroom ;;
        debian) sudo apt remove postroom ;;
        fedora) sudo dnf remove postroom ;;
        *) sudo rm -rf /usr/local/share/postroom /usr/local/bin/postroom \
               /usr/local/share/applications/io.github.postroom.Postroom.desktop \
               /usr/local/share/doc/postroom /usr/local/share/licenses/postroom
           say "Removed Postroom from /usr/local." ;;
    esac
    exit 0
fi

mkdir -p dist
case "$family" in
    arch)
        say "Arch Linux: building with makepkg."
        python3 packaging/make_source.py >/dev/null
        if [ "$mode" = build ]; then
            makepkg -f --nocheck
        else
            makepkg -fsi --nocheck
        fi
        ;;
    debian)
        say "Debian or Ubuntu: building a .deb package."
        deb=$(python3 packaging/build_deb.py)
        say "Built $deb"
        if [ "$mode" = install ]; then
            sudo apt install -y "$deb"
        fi
        ;;
    fedora)
        say "Fedora: building an .rpm package."
        if ! command -v rpmbuild >/dev/null 2>&1; then
            say "rpmbuild is missing; installing rpm-build."
            sudo dnf install -y rpm-build
        fi
        python3 packaging/make_source.py >/dev/null
        rpmbuild -bb --define "_sourcedir $root" --define "_rpmdir $root/dist" \
            --define "_builddir $root/dist/build" packaging/postroom.spec
        rpm="$root/dist/noarch/postroom-$version-1$(rpm --eval '%{?dist}').noarch.rpm"
        say "Built $rpm"
        if [ "$mode" = install ]; then
            sudo dnf install -y "$rpm"
        fi
        ;;
    *)
        say "Installing into /usr/local without a package."
        say "Postroom needs Python 3.10 or later, PyGObject, GTK 4 and libsecret."
        if [ "$mode" = install ] || [ "$mode" = generic ]; then
            sudo sh packaging/stage.sh / /usr/local
            say "Installed. Remove it later with: sh install.sh --uninstall"
        fi
        ;;
esac

if [ "$mode" = build ]; then
    say "Done. Nothing was installed."
else
    say "Done. Start Postroom from your applications menu, or run: postroom"
fi
