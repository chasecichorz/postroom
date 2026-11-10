# Postroom readiness

Updated 2026-11-10. Keep separate Accounts and Folders panes and the existing
message/reading layout. New commands use menus, shortcuts or separate ordinary
windows, preserving the main window's short wrapping Tab order.

## Implemented in the current uncommitted work

- Menu keys: the message-list controller no longer handles arrows or type-ahead
  while a menu is active. The context popover is a child of that list, which let
  the list previously intercept its keys. Tab handling also defers to active
  menus and the pane return-focus location is preserved. View mnemonics are unique.
- Confirmation windows and About are nonmodal and have no transient parent.
- Local drafts: private durable storage under config, separate from disposable
  mail cache. Autosave, explicit Ctrl+S, close/quit saving, restore selected From,
  reply headers, attachments and body cursor. File, Saved Drafts resumes work.
  Discard asks explicitly. A failed save blocks closing; sending blocks quitting.
- SMTP: accepted and rejected recipients are distinguished; partial retries use
  only rejected recipients. A sending marker survives a crash. Unknown delivery
  requires an explicit Send Again decision. Message IDs survive retries. Local
  copies of accepted messages can be read/exported via File, Local Sent Copies.
  Sent filing warnings are separate from SMTP acceptance.
- IMAP: moves, permanent deletes and Sent appends are not automatically replayed
  after dropped connections. No unrestricted EXPUNGE on servers without UIDPLUS.
- Search: server or downloaded-mail search for current folder, text/sender/To+Cc/
  subject/body, unread/starred and dates. Results reuse Messages; Escape restores
  the original folder/selection. Results page independently of the folder cache.
- History: View, Load Older Messages downloads batches of 500, including more
  search results. Explicit history loading expands the normal cache allowance.
- Account type combo: automatic discovery, Gmail, Microsoft, Yahoo, AOL, Fastmail,
  iCloud or Other. Other permits custom servers. Account edits now replace the
  saved record; existing authentication is preserved when no new password is given.
  Only Gmail's formatted app passwords have spaces stripped.
- Local Arch packaging: PKGBUILD, .SRCINFO, allowlisted/checksummed source snapshot,
  installed launcher, desktop entry, license notices and isolated check function.

## Current verification

The original hidden-display tests passed before this larger implementation. That
transcript exposed duplicate first-item focus events and a brief previous-menu
heading event when switching menus. These may still affect Cthulhu speech; the
specific context-menu interception fix needs the new real-key regression probe.

71 tests not requiring local server sockets pass in the coding sandbox. The
OAuth and CardDAV test classes cannot bind their fake servers there. UI and
AT-SPI runs also cannot start D-Bus sockets there. Run the updated
`tests/check_hidden_ui.sh` outside the sandbox; it now runs full unit discovery,
UI smoke, asserted menu regression checks, accessibility and completion probes.
The new UI smoke covers draft close/reopen/discard, attachments/cursor recovery,
partial delivery, provider selection, local sent export, search and older mail.
Compilation, whitespace, desktop-entry validation and local package build have
been checked. Full UI validation of the new work remains pending.

## Remaining product work

- Synchronize drafts to server Drafts folders; current recovery is local only.
- Add Bcc, signatures, sender identities, folder management, junk/not-junk, and
  multi-message selection with spoken counts.
- Add editable contacts and message import/mailto integration.
- Develop an explicit offline outbox and cache-management interface. Current
  downloaded search cannot examine bodies that have not been downloaded.
- Validate Cthulhu speech with the user after keyboard and AT-SPI checks. Do not
  open tests on the real display or read unfiltered speech logs.
- Test disconnects against representative IMAP/SMTP providers. Automatic retry
  does not establish exactly-once delivery; SMTP acceptance is not final delivery.

## Distribution decisions

Original code is GPL-3.0-or-later (chosen by the user 2026-11-10); autoconfig.py
and oauth2.py keep their MPL 2.0 notices. At the user's request, oauth2.py uses
Thunderbird's public client IDs unless oauth-clients.json supplies others. Never ship
user configuration. Existing local credentials were not altered.
The user decides separately whether to enable background startup.

## Coordination

The user cancelled the assistant bridge. Maintain CLAUDE.md and AGENTS.md for
handoffs. Do not resume bridge calls or connection testing without a new request.
