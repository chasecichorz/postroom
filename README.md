# Postroom

A keyboard-first Linux email client laid out like Kelly Ford's
[QuickMail](https://github.com/kellylford/QuickMail): accounts and folders on the left,
messages and the reading pane on the right, and everything reachable from the keyboard.
Built with GTK 4 and Python, and designed for use with the Orca screen reader.

## Running it

```sh
./bin/postroom
```

It needs Python 3, GTK 4, PyGObject and libsecret, which are all standard on Arch
(`gtk4 python-gobject libsecret`). Nothing else has to be installed.

The first time it starts, the Add Account window opens. For Gmail:

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
  Postroom keeps a refresh token in place of a password. Works for Gmail, Outlook and
  Microsoft 365, Yahoo, AOL and Fastmail.

Browser sign-in needs Postroom's own registration with each provider. Mozilla's
registrations belong to Thunderbird, and Postroom must not borrow them. Put the client IDs in
`~/.config/postroom/oauth-clients.json`:

```json
{"accounts.google.com": {"client_id": "….apps.googleusercontent.com", "client_secret": "…"},
 "login.microsoftonline.com": {"client_id": "…"}}
```

Until a provider has an entry, its accounts use app passwords.

## New mail

Every couple of minutes Postroom checks the folder you're in and every account's Inbox.
New mail plays a short chime (Postroom's own sound, made by the app) and says who it is
from, like "New mail from Ann Lee: Lunch on Friday?". The View menu turns the sound
(Play a Sound for New Mail) and the announcement (Say Who New Mail Is From) on or off.

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
Ctrl+Enter or Alt+S send, and Escape to close (it asks before throwing away your text).

## Tests

```sh
python3 -m unittest tests.test_core                                # mail, cache, threading
python3 -m unittest tests.test_accounts                            # finding servers, browser sign-in
python3 -m unittest tests.test_contacts                            # address book, Google contacts
xvfb-run -a dbus-run-session -- python3 -m tests.completion_probe  # what To suggestions say
xvfb-run -a dbus-run-session -- python3 -m tests.ui_smoke          # the window, on a hidden screen
xvfb-run -a dbus-run-session -- python3 -m tests.a11y_probe        # what a screen reader is told
python3 -m tests.fake_app                                          # try it with a pretend account
```

## Files

- `~/.config/postroom/config.json`: accounts and settings
- `~/.cache/postroom/cache.db`: local copy of the newest 1000 messages per folder, for speed
- `~/.cache/postroom/postroom.log`: log, useful when something goes wrong

## Licenses

`postroom/autoconfig.py` and `postroom/oauth2.py` are ported from Thunderbird's account
setup and OAuth code and stay under the [Mozilla Public License 2.0](https://mozilla.org/MPL/2.0/),
as that license requires. Thunderbird's source is at
<https://github.com/thunderbird/thunderbird-desktop>.
