# Postroom

A keyboard-first Linux email client laid out like Kelly Ford's
[QuickMail](https://github.com/kellylford/QuickMail): accounts and folders on the left,
messages and the reading pane on the right, and everything reachable from the keyboard.
Built with GTK 4 and Python, and designed for Cthulhu and other AT-SPI screen readers.

## Running it

```sh
./bin/postroom
```

It needs Python 3, GTK 4, PyGObject and libsecret, which are all standard on Arch
(`gtk4 python-gobject libsecret`). To install it as a package, see Installing below.

The first time it starts, the Add Account window opens. The Account type combo box
offers Find from email address, Gmail, Outlook / Microsoft 365, Yahoo, AOL, Fastmail,
iCloud, and Other. Other lets you enter custom IMAP and SMTP servers. Provider
selection supplies server discovery defaults; it does not change the account’s
authentication method or create an OAuth registration. For Gmail:

1. Turn on two-step verification for your Google account if it isn't on already.
2. Create an app password at <https://myaccount.google.com/apppasswords>.
3. In Add Account, enter your Gmail address and that app password. Leave the server settings alone.

### Where your password is kept

If a keyring is running (GNOME Keyring, KWallet, KeePassXC), the app password is stored
there. If not, which is usual on i3, it goes in `~/.config/postroom/passwords.json`, which
only your user can read (permissions 600). To use a keyring instead, install
`gnome-keyring`, start it with your session, and add the account again.

## Adding an account

Type your email address. Postroom finds the server settings for you, the way Thunderbird
does: it asks your provider first, then Thunderbird's public provider database, then the
provider behind your domain's mail (MX) record, and last it tries the usual server names.
It uses HTTPS only and sends just your domain, never your address. You only need Server
settings if all of that fails.

Then either:

- **App password**: type or paste it. Postroom says how many characters the field holds,
  and Alt+W shows it if you want to check it.
- **Sign in with browser** (Alt+B): Postroom opens your provider's sign-in page in your web
  browser. You sign in there, the browser hands Postroom a one-time code on 127.0.0.1, and
  Postroom keeps a refresh token in place of a password. Available for providers whose registration supports Postroom’s local callback.
  Yahoo and AOL currently need app passwords.

Postroom uses Thunderbird's public registrations for Google, Microsoft and Fastmail, so the
sign-in page names Thunderbird. To use your own registrations instead, put the client IDs in
`~/.config/postroom/oauth-clients.json`:

```json
{"accounts.google.com": {"client_id": "….apps.googleusercontent.com", "client_secret": "…"},
 "login.microsoftonline.com": {"client_id": "…"}}
```

An entry there takes priority over the built-in IDs.

## New mail

Every couple of minutes Postroom checks the folder you're in and every account's Inbox.
New mail plays a short chime (Postroom's own sound, made by the app) and says who it is
from, like "New mail from Ann Lee: Lunch on Friday?". The View menu turns the sound
(Play a Sound for New Mail) and the announcement (Say Who New Mail Is From) on or off.

### When Postroom is closed

`postroom --background` keeps checking every account's Inbox when the window is closed. It
opens the Inbox read-only, plays the chime, and speaks who new mail is from through
speech-dispatcher (`spd-say`), as I38's own scripts do. It also sends a desktop
notification when a notification service such as dunst is running. It stays quiet while
the Postroom window is open, and never announces mail you've already been told about. Its
log is `~/.cache/postroom/background.log`. To start it with i3, add this to
`~/.config/i3/customizations`:

```
exec --no-startup-id ~/.local/bin/postroom --background
```

## Address book

Postroom learns addresses from your mail: people who write to you, and especially people
you write to (it reads your Sent folder once to get started). Automated senders such as
no-reply are left out. For Gmail accounts that use Sign in with browser, your Google
contacts are brought in too, every few hours or right away with File, Update Contacts from
Google. Postroom only reads your contacts and never changes them.

In To and Cc, type two or more letters of a name or address. The best match is spoken,
like "Ann Lee, ann@example.com, 1 of 3". Down and Up step through the matches, Enter or Tab
adds one, and Escape dismisses the suggestions without closing the message.

## Layout

| Pane | Get there with |
|---|---|
| Accounts | Ctrl+1 |
| Folders | Ctrl+2 |
| Messages | Ctrl+3 |
| Reading pane | Enter on a message |
| Status bar | Ctrl+9 |

Tab and Shift+Tab move through the panes the way QuickMail does: Accounts, Folders,
Messages, then the reading pane (attachments, then the message), and wrap around. Arrow keys
move within a pane. The menu bar (F10 or Alt) and the status bar (Ctrl+9) are not Tab stops;
Escape on the menu bar returns to where you were. F6 and Shift+F6 move through all the panes,
including the status bar. F1 lists every shortcut.

The message list groups mail into conversations. Right expands a conversation, Left
collapses it or goes back to the conversation. Turn grouping off in the View menu. Type
letters to jump to a sender. Messages are marked read when you open them, not when you
arrow past them.

## Keyboard shortcuts

These match QuickMail's wherever QuickMail has the same command.

| Keys | Action |
|---|---|
| Ctrl+N | New message |
| Ctrl+R / Ctrl+Shift+R | Reply / Reply all |
| Ctrl+Shift+F | Forward |
| Delete | Delete (moves to Trash) |
| Ctrl+Shift+M | Archive |
| Ctrl+Q / Ctrl+U | Mark read / unread |
| K | Star or unstar |
| Alt+Down / Alt+Up | Next / previous unread |
| Alt+I | Go to the Inbox |
| Alt+A | Attachments of the open message |
| Ctrl+Shift+O | Links in the open message |
| Ctrl+Shift+H | Switch between the plain text and formatted versions |
| F5 | Check for mail |
| Shift+F10 or Menu | Message actions |

In the compose window: Alt+T To, Alt+C Cc, Alt+U Subject, Alt+Y body, Alt+A attach,
Ctrl+S save draft, Ctrl+Enter or Alt+S send, and Escape save the draft and close.
The Discard button asks before deleting your saved draft.

## Drafts and sending

Postroom automatically saves unfinished messages on this computer after a short
pause in editing. To resume one, use File, Saved Drafts. The saved copy includes
attachments, From account, reply headers and the body cursor position. Ctrl+S
saves immediately; Escape saves and closes. Closing the main window saves open
composers; quitting waits for any send in progress. Discard explicitly removes a
draft after confirmation. Drafts currently stay local rather than synchronizing
to your provider's Drafts folder.

If the server accepts only some recipients, Postroom tells you which ones and
leaves only the rejected recipients in the draft for retry. If the connection
fails during sending, delivery may be uncertain: check Sent and the recipients
before choosing Send Again. Postroom never retries SMTP delivery automatically.

File, Local Sent Copies lets you read or save an .eml copy of messages accepted by
the sending server, including attachments. The copy identifies the accepted
recipients; acceptance does not guarantee final delivery to their inboxes. A
failure to file the server's Sent copy is reported separately from delivery.

## Search and older mail

Ctrl+F or View, Find Messages opens a separate search window for the current
folder. Search sender, To/Cc recipients, subject, body, or headers and body together.
You can restrict it to unread or starred mail and supply dates as YYYY-MM-DD.
Since includes that day; Before excludes it. Server search covers the folder's
mail, including messages outside the cache. Downloaded-mail search uses cached
headers and bodies already opened on this computer; it cannot search bodies
that have not been downloaded.

Results appear in the existing message list. Escape in that list or View, Clear
Search restores the folder and its earlier selection. View, Load Older Messages
loads the next 500 earlier messages, or another page of search results. Deliberate
history loading increases the folder's cache allowance so routine refreshes keep
that larger history window. No extra controls are added to the main Tab order.

## Installing

`install.sh` builds a native package for your distribution and installs it. Run it as
your normal user; it asks for your sudo password when it installs.

```sh
sh install.sh               # Arch (makepkg), Debian/Ubuntu (.deb) or Fedora (.rpm)
sh install.sh --build-only  # only build the package
sh install.sh --generic     # other distributions: copy into /usr/local
sh install.sh --uninstall   # remove it again
```

You can also build each package by hand:

- Arch: `python3 packaging/make_source.py && makepkg -si`
- Debian or Ubuntu: `python3 packaging/build_deb.py`, then
  `sudo apt install ./dist/postroom_0.2.0-1_all.deb`
- Fedora: `sudo dnf install rpm-build`, `python3 packaging/make_source.py`, then
  `rpmbuild -bb --define "_sourcedir $PWD" packaging/postroom.spec`

Dependencies are pulled in by the package manager: Python 3.10 or later, PyGObject,
GTK 4 and libsecret. Speech Dispatcher, libnotify and PipeWire are recommended for
background mail alerts. The Debian and Fedora packages keep Postroom's modules in
/usr/share/postroom; the Arch package uses Python's site-packages.

The packages install /usr/bin/postroom and a desktop entry. If you keep a checkout
launcher at ~/.local/bin/postroom, your PATH may still choose it first; `type -a
postroom` shows both. Your settings in ~/.config/postroom are never touched.

Browser sign-in uses Thunderbird's public OAuth client IDs unless you add your own
in `~/.config/postroom/oauth-clients.json`.

## Tests

```sh
python3 -m unittest tests.test_core                                # mail, cache, threading
python3 -m unittest tests.test_accounts                            # finding servers, browser sign-in
python3 -m unittest tests.test_contacts                            # address book, Google contacts
python3 -m unittest tests.test_background                          # checking while closed
python3 -m unittest tests.test_readiness tests.test_ui_guards       # drafts, sending, search, history and guards
xvfb-run -a dbus-run-session -- python3 -m tests.completion_probe  # what To suggestions say
xvfb-run -a dbus-run-session -- python3 -m tests.ui_smoke          # the window, on a hidden screen
xvfb-run -a dbus-run-session -- python3 -m tests.menu_probe        # asserted real-key menu checks
xvfb-run -a dbus-run-session -- python3 -m tests.a11y_probe        # what a screen reader is told
python3 -m tests.fake_app                                          # try it with a pretend account
```

## Files

- `~/.config/postroom/config.json`: accounts and settings
- `~/.config/postroom/drafts.db`: private local drafts, attachments and accepted-mail copies
- `~/.cache/postroom/cache.db`: local headers (initially 500, normally up to 1000 per folder; expanded when you load older mail) and recently opened bodies
- `~/.cache/postroom/postroom.log`: log, useful when something goes wrong

## Licenses

Postroom is free software: you can redistribute it and/or modify it under the terms of
the GNU General Public License as published by the Free Software Foundation, either
version 3 of the License, or (at your option) any later version. See
[LICENSES/GPL-3.0-or-later.txt](LICENSES/GPL-3.0-or-later.txt).

`postroom/autoconfig.py` and `postroom/oauth2.py` are ported from Thunderbird's account
setup and OAuth code and stay under the [Mozilla Public License 2.0](https://mozilla.org/MPL/2.0/),
as that license requires. Thunderbird's source is at
<https://github.com/thunderbird/thunderbird-desktop>.
