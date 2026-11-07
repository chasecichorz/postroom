# Mailpane

A keyboard-first Linux email client laid out like Kelly Ford's
[QuickMail](https://github.com/kellylford/QuickMail): accounts and folders on the left,
messages and the reading pane on the right, and everything reachable from the keyboard.
Built with GTK 4 and Python, and designed for use with the Orca screen reader.

## Running it

```sh
./bin/mailpane
```

It needs Python 3, GTK 4, PyGObject and libsecret, which are all standard on Arch
(`gtk4 python-gobject libsecret`). Nothing else has to be installed.

The first time it starts, the Add Account window opens. For Gmail:

1. Turn on two-step verification for your Google account if it isn't on already.
2. Create an app password at <https://myaccount.google.com/apppasswords>.
3. In Add Account, enter your Gmail address and that app password. Leave the server settings alone.

### Where your password is kept

If a keyring is running (GNOME Keyring, KWallet, KeePassXC), the app password is stored
there. If not, which is usual on i3, it goes in `~/.config/mailpane/passwords.json`, which
only your user can read (permissions 600). To use a keyring instead, install
`gnome-keyring`, start it with your session, and add the account again.

## Layout

| Pane | Get there with |
|---|---|
| Accounts | Ctrl+1 |
| Folders | Ctrl+2 |
| Messages | Ctrl+3 |
| Reading pane | Enter on a message |
| Status bar | Ctrl+9 |

F6 and Shift+F6 move through the panes in order. F1 lists every shortcut.

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
xvfb-run -a dbus-run-session -- python3 -m tests.ui_smoke          # the window, on a hidden screen
xvfb-run -a dbus-run-session -- python3 -m tests.a11y_probe        # what a screen reader is told
python3 -m tests.fake_app                                          # try it with a pretend account
```

## Files

- `~/.config/mailpane/config.json`: accounts and settings
- `~/.cache/mailpane/cache.db`: local copy of the newest 1000 messages per folder, for speed
- `~/.cache/mailpane/mailpane.log`: log, useful when something goes wrong
