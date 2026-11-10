#!/bin/sh
# Copy Postroom into a staging root, using the same layout on every distribution.
#
#   packaging/stage.sh DESTDIR [PREFIX]
#
# Modules go in PREFIX/share/postroom, so no distribution-specific Python path is
# needed. PREFIX defaults to /usr. Used by build_deb.py, postroom.spec and install.sh.
set -eu

dest=${1:?usage: stage.sh DESTDIR [PREFIX]}
prefix=${2:-/usr}
src=$(cd "$(dirname "$0")/.." && pwd)
share="$prefix/share/postroom"

install -d "$dest$share/postroom/ui"
install -m644 "$src"/postroom/*.py "$dest$share/postroom/"
install -m644 "$src"/postroom/ui/*.py "$dest$share/postroom/ui/"

install -d "$dest$prefix/bin"
cat > "$dest$prefix/bin/postroom" <<EOF
#!/bin/sh
exec /usr/bin/python3 -I -c 'import runpy, sys; sys.path.insert(0, "$share"); sys.argv[0] = "postroom"; runpy.run_module("postroom", run_name="__main__")' "\$@"
EOF
chmod 755 "$dest$prefix/bin/postroom"

desktop="$dest$prefix/share/applications/io.github.postroom.Postroom.desktop"
install -d "$(dirname "$desktop")"
sed "s|^Exec=postroom|Exec=$prefix/bin/postroom|" "$src/data/io.github.postroom.Postroom.desktop" > "$desktop"
chmod 644 "$desktop"

install -Dm644 "$src/README.md" "$dest$prefix/share/doc/postroom/README.md"
install -Dm644 "$src/LICENSES/GPL-3.0-or-later.txt" "$dest$prefix/share/licenses/postroom/GPL-3.0-or-later.txt"
install -Dm644 "$src/LICENSES/MPL-2.0.txt" "$dest$prefix/share/licenses/postroom/MPL-2.0.txt"
