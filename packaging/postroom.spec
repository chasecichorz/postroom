# Fedora package. Build from a checkout with:  sh install.sh --build-only
# (or: python3 packaging/make_source.py && rpmbuild -bb --define "_sourcedir $PWD" packaging/postroom.spec)
Name:           postroom
Version:        0.2.0
Release:        1%{?dist}
Summary:        Accessible keyboard-first GTK email client
License:        GPL-3.0-or-later AND MPL-2.0
URL:            https://github.com/chasecichorz/postroom
Source0:        postroom-%{version}.tar.gz
BuildArch:      noarch

Requires:       python3 >= 3.10
Requires:       python3-gobject
Requires:       gtk4
Requires:       libsecret
Recommends:     speech-dispatcher-utils
Recommends:     libnotify
Recommends:     pipewire-utils

%description
Postroom is an IMAP and SMTP email client built for screen reader users,
with separate Accounts and Folders panes and QuickMail-style shortcuts.

%prep
%autosetup -n postroom-%{version}

%build
# Pure Python; nothing to build.

%install
sh packaging/stage.sh %{buildroot} %{_prefix}
rm -rf %{buildroot}%{_datadir}/licenses %{buildroot}%{_docdir}

%files
%license LICENSES/GPL-3.0-or-later.txt LICENSES/MPL-2.0.txt
%doc README.md
%{_bindir}/postroom
%{_datadir}/postroom/
%{_datadir}/applications/io.github.postroom.Postroom.desktop

%changelog
* Tue Nov 10 2026 Postroom developers - 0.2.0-1
- First Fedora package.
