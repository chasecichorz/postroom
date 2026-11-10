# Local source snapshot: run python3 packaging/make_source.py before makepkg.
pkgname=postroom
pkgver=0.2.0
pkgrel=1
pkgdesc='Accessible keyboard-first GTK email client'
arch=('any')
# Private build: oauth2.py embeds Thunderbird's client IDs. Do not publish this package.
license=('GPL-3.0-or-later' 'MPL-2.0')
depends=('python' 'python-gobject' 'gtk4>=4.24' 'libsecret')
checkdepends=('xorg-server-xvfb' 'xorg-xauth' 'xdotool' 'dbus' 'at-spi2-core')
optdepends=('speech-dispatcher: spoken background mail notifications'
            'libnotify: desktop background mail notifications'
            'pipewire-audio: new-mail sound through pw-play'
            'libpulse: alternative new-mail sound through paplay'
            'alsa-utils: alternative new-mail sound through aplay')
source=("postroom-$pkgver.tar.gz")
# Updated by packaging/make_source.py for the generated local source snapshot.
sha256sums=('8bffb6d83875cca5bfc51d7f310cb2bcc06f4bd4aa069394c370d3abe1d36e9e')

check() {
    cd "$srcdir/postroom-$pkgver"
    local test_profile
    test_profile=$(mktemp -d "$srcdir/test-profile.XXXXXXXX")
    export POSTROOM_PROFILE="$test_profile" POSTROOM_NO_KEYRING=1
    python -m unittest discover -s tests || return
    xvfb-run -a dbus-run-session -- python -m tests.ui_smoke || return
    xvfb-run -a dbus-run-session -- python -m tests.menu_probe
}

package() {
    cd "$srcdir/postroom-$pkgver"
    local site_packages
    site_packages=$(python -c 'import sysconfig; print(sysconfig.get_path("purelib"))')
    install -d "$pkgdir$site_packages/postroom/ui"
    install -m644 postroom/*.py "$pkgdir$site_packages/postroom/"
    install -m644 postroom/ui/*.py "$pkgdir$site_packages/postroom/ui/"
    install -Dm755 packaging/postroom "$pkgdir/usr/bin/postroom"
    install -Dm644 data/io.github.postroom.Postroom.desktop "$pkgdir/usr/share/applications/io.github.postroom.Postroom.desktop"
    install -Dm644 README.md "$pkgdir/usr/share/doc/postroom/README.md"
    install -Dm644 LICENSES/MPL-2.0.txt "$pkgdir/usr/share/licenses/postroom/MPL-2.0.txt"
    install -Dm644 LICENSES/GPL-3.0-or-later.txt \
        "$pkgdir/usr/share/licenses/postroom/GPL-3.0-or-later.txt"
}
