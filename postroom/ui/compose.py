"""The compose window, for new messages, replies and forwards."""
from __future__ import annotations

import mimetypes
import os
from dataclasses import replace

from gi.repository import Gdk, GLib, Gtk

from .. import mime, sending
from ..config import Account
from ..mime import Attachment
from .common import announce, confirm, focus_within, mods, set_label
from .completion import AddressCompleter


class ComposeWindow(Gtk.Window):
    def __init__(self, main, account: Account, draft: sending.Draft):
        super().__init__(default_width=760, default_height=600)
        self.main = main
        self.accounts = list(main.cfg.accounts)
        self.draft = draft
        self.sent = False
        self._discard_prompt = None
        self._retry_prompt = None
        self._autosave_source = 0
        self.draft_store = main.app.drafts
        self.attachments: list[Attachment] = list(draft.attachments)

        grid = Gtk.Grid(column_spacing=12, row_spacing=6, margin_top=12, margin_bottom=12,
                        margin_start=12, margin_end=12)
        self.set_child(grid)
        self.editor_grid = grid
        self.from_dd = Gtk.DropDown.new_from_strings([f"{a.name} <{a.email}>" for a in self.accounts])
        idx = next((i for i, a in enumerate(self.accounts) if account and a.id == account.id), 0)
        self.from_dd.set_selected(idx)
        self._row(grid, 0, "Fro_m", self.from_dd)
        self.to = self._entry(grid, 1, "_To", draft.to)
        self.cc = self._entry(grid, 2, "_Cc", draft.cc)
        self.subject = self._entry(grid, 3, "S_ubject", draft.subject)
        self.subject.connect("changed", lambda *_: self._retitle())

        att_row = Gtk.Box(spacing=8)
        attach = Gtk.Button(label="_Attach File…", use_underline=True)
        attach.connect("clicked", self._pick_file)
        self.att_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE, hexpand=True)
        set_label(self.att_list, "Attachments. Press Delete to remove one.")
        kc = Gtk.EventControllerKey()
        kc.connect("key-pressed", self._att_key)
        self.att_list.add_controller(kc)
        att_row.append(attach)
        att_row.append(self.att_list)
        grid.attach(att_row, 0, 4, 2, 1)

        body_label = Gtk.Label(label="Message bod_y", use_underline=True, xalign=0)
        self.body = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, vexpand=True, hexpand=True,
                                 left_margin=8, right_margin=8, top_margin=6, bottom_margin=6,
                                 accepts_tab=False)
        body_label.set_mnemonic_widget(self.body)
        set_label(self.body, "Message body")
        self.body.get_buffer().set_text(draft.body)
        grid.attach(body_label, 0, 5, 2, 1)
        grid.attach(Gtk.ScrolledWindow(child=self.body, vexpand=True), 0, 6, 2, 1)

        self.status = Gtk.Label(xalign=0, wrap=True, selectable=True, visible=False)
        grid.attach(self.status, 0, 7, 2, 1)
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        discard = Gtk.Button(label="_Discard", use_underline=True)
        discard.connect("clicked", self.discard)
        self.send_btn = Gtk.Button(label="_Send", use_underline=True)
        self.send_btn.add_css_class("suggested-action")
        self.send_btn.connect("clicked", lambda *_: self.send())
        save = Gtk.Button(label="Save _Draft", use_underline=True)
        save.connect("clicked", lambda *_: self.save_draft())
        buttons.append(save)
        buttons.append(discard)
        buttons.append(self.send_btn)
        grid.attach(buttons, 0, 8, 2, 1)

        # Address suggestions for To and Cc, from the address book.
        self.completers = [
            AddressCompleter(e, lambda t: main.store.search_contacts(t, 8), self._show_hint)
            for e in (self.to, self.cc)]

        sc = Gtk.ShortcutController()
        sc.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        for trig, fn in (("<Control>s", self.save_draft), ("<Control>Return", self.send), ("<Control>KP_Enter", self.send),
                         ("Escape", self._escape), ("F6", lambda: self._cycle(1)),
                         ("<Shift>F6", lambda: self._cycle(-1))):
            sc.add_shortcut(Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string(trig),
                                             Gtk.CallbackAction.new(lambda *_a, fn=fn: (fn(), True)[1])))
        self.add_controller(sc)
        self.connect("close-request", self._on_close_request)
        self.connect("destroy", self._destroyed)
        self._refresh_attachments()
        self._retitle()
        self._initial = self._snapshot()
        for entry in (self.to, self.cc, self.subject):
            entry.connect("changed", self._queue_save)
        self.body.get_buffer().connect("changed", self._queue_save)
        self.body.get_buffer().connect("notify::cursor-position", self._queue_save)
        self.from_dd.connect("notify::selected", self._queue_save)
        if self.draft.delivery_state in ("sending", "uncertain"):
            self.draft.delivery_state = "uncertain"
            self._say("This draft may already have been sent. Check Sent and the recipients before retrying.", True)
        self._queue_save()


        if draft.in_reply_to:
            buf = self.body.get_buffer()
            buf.place_cursor(buf.get_start_iter())
            self.body.grab_focus()
        elif draft.to:
            self.body.grab_focus()
        else:
            self.to.grab_focus()

    def _escape(self):
        """Escape dismisses address suggestions first; otherwise it closes the window."""
        for c in self.completers:
            if c.active:
                c.dismiss()
                return
        self.close()

    def _show_hint(self, text: str | None):
        if text:
            self.status.set_text(text)
            self.status.set_visible(True)
        elif self.status.get_text().startswith("Suggestion:"):
            self.status.set_visible(False)

    # -- layout helpers ---------------------------------------------------------------------
    def _row(self, grid, row, label, widget):
        lbl = Gtk.Label(label=label, use_underline=True, mnemonic_widget=widget, xalign=0)
        set_label(widget, label.replace("_", ""))
        widget.set_hexpand(True)
        grid.attach(lbl, 0, row, 1, 1)
        grid.attach(widget, 1, row, 1, 1)

    def _entry(self, grid, row, label, text) -> Gtk.Entry:
        e = Gtk.Entry(text=text or "")
        self._row(grid, row, label, e)
        return e

    def _retitle(self):
        s = self.subject.get_text().strip()
        self.set_title(s if s else "New Message")

    def _cycle(self, step: int):
        stops = [self.from_dd, self.to, self.cc, self.subject, self.body]
        cur = next((i for i, w in enumerate(stops) if focus_within(self, w)), -1)
        stops[(cur + step) % len(stops)].grab_focus()

    # -- attachments ------------------------------------------------------------------------
    def _refresh_attachments(self):
        self.att_list.remove_all()
        for att in self.attachments:
            text = f"{att.filename}, {mime.human_size(att.size)}"
            row = Gtk.ListBoxRow()
            row.set_child(Gtk.Label(label=text, xalign=0))
            set_label(row, text)
            self.att_list.append(row)
        self.att_list.set_visible(bool(self.attachments))
        if hasattr(self, "_initial"):
            self._queue_save()

    def _pick_file(self, *_):
        def done(d, res):
            try:
                f = d.open_finish(res)
            except GLib.Error:
                return
            path = f.get_path()
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
            except OSError as e:
                return self._say(f"Couldn't read the file: {e}", True)
            name = os.path.basename(path)
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
            self.attachments.append(Attachment(name, ctype, data))
            self._refresh_attachments()
            self._say(f"Attached {name}")
        Gtk.FileDialog(title="Attach a File").open(self, None, done)

    def _att_key(self, _c, keyval, _code, state):
        if keyval in (Gdk.KEY_Delete, Gdk.KEY_BackSpace) and not mods(state):
            row = self.att_list.get_selected_row()
            if row:
                i = row.get_index()
                removed = self.attachments.pop(i)
                self._refresh_attachments()
                self._say(f"Removed {removed.filename}")
                nxt = self.att_list.get_row_at_index(min(i, len(self.attachments) - 1))
                (nxt or self.body).grab_focus()
            return True
        return False

    # -- sending ----------------------------------------------------------------------------
    def _snapshot(self):
        buf = self.body.get_buffer()
        return (self.to.get_text(), self.cc.get_text(), self.subject.get_text(),
                buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False), self.from_dd.get_selected(),
                tuple((a.filename, a.content_type, a.data) for a in self.attachments))

    def _say(self, text: str, important: bool = False):
        self.status.set_text(text)
        self.status.set_visible(True)
        announce(self, text, important)

    def _capture(self):
        buf = self.body.get_buffer()
        recipients_changed = (self.to.get_text(), self.cc.get_text()) != (self.draft.to, self.draft.cc)
        return replace(self.draft, to=self.to.get_text(), cc=self.cc.get_text(),
                       subject=self.subject.get_text(),
                       body=buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False),
                       attachments=list(self.attachments),
                       pending_recipients=None if recipients_changed else self.draft.pending_recipients)

    def _queue_save(self, *_):
        if self._autosave_source:
            GLib.source_remove(self._autosave_source)
        self._autosave_source = GLib.timeout_add(750, self._autosave)

    def _autosave(self):
        self._autosave_source = 0
        if self.send_btn.get_sensitive():
            self.save_draft(quiet=True)
        return False

    def save_draft(self, quiet=False):
        if self.sent or not self.send_btn.get_sensitive():
            return self.sent
        draft = self._capture()
        acct = self.accounts[self.from_dd.get_selected()]
        buf = self.body.get_buffer()
        try:
            if any((draft.to.strip(), draft.cc.strip(), draft.subject.strip(),
                    draft.body.strip(), draft.attachments)):
                self.draft_store.save(acct.id, draft, buf.props.cursor_position)
            else:
                self.draft_store.delete(draft.id)
            self.draft = draft
        except Exception:
            self._say("Could not save this draft. Keep this window open and check available disk space.", True)
            return False
        if not quiet:
            self._say("Draft saved on this computer")
        return True

    def _destroyed(self, *_):
        if self._autosave_source:
            GLib.source_remove(self._autosave_source)
            self._autosave_source = 0
        self.main.composers.pop(self.draft.id, None)
        self.main.present()

    def send(self, confirmed=False):
        if not self.send_btn.get_sensitive():
            return
        if self.draft.delivery_state == "uncertain" and not confirmed:
            if self._retry_prompt:
                self._retry_prompt.present()
                return
            self._retry_prompt = confirm(
                self, "Send this message again?",
                "Its earlier delivery could not be confirmed. Check Sent and the recipients first. "
                "Retrying could send a duplicate.", "_Cancel", "_Send Again", lambda: self.send(True))
            self._retry_prompt.connect("close-request", lambda *_: setattr(self, "_retry_prompt", None))
            return
        draft = self._capture()
        problem = sending.validate(draft)
        if problem:
            self._say(problem, True)
            self.to.grab_focus()
            return
        acct = self.accounts[self.from_dd.get_selected()]
        sess = self.main.sessions.get(acct.id)
        if sess is None:
            return self._say("That account isn't ready yet.", True)
        # Allocate the message ID before persisting the sending marker.
        try:
            sending.build(acct, draft)
        except (ValueError, TypeError):
            return self._say("Check the recipient and subject fields for invalid characters.", True)
        draft.delivery_state = "sending"
        try:
            self.draft_store.save(acct.id, draft, self.body.get_buffer().props.cursor_position)
        except Exception:
            return self._say("Could not save a recovery copy. The message has not been sent.", True)
        self.draft = draft
        self.send_btn.set_sensitive(False)
        self.editor_grid.set_sensitive(False)
        self._say("Sending…")
        sess.submit(sess.send, draft, on_done=lambda result: self._sent(draft, result), on_error=self._failed)

    def _sent(self, draft, result):
        warning = result.warning if result else ""
        if result and result.refused:
            self.editor_grid.set_sensitive(True)
            self.send_btn.set_sensitive(True)
            self.to.set_text(draft.to)
            self.cc.set_text(draft.cc)
            self._say("Mail accepted for " + ", ".join(result.accepted) +
                      ". Not sent to " + ", ".join(result.refused) +
                      ". Only those remaining recipients are left in this draft. " + warning, True)
            self.to.grab_focus()
            return
        self.sent = True
        self.main.message_sent(draft)
        if warning:
            self.main.say(warning, True)
        self.destroy()

    def _failed(self, e):
        self.editor_grid.set_sensitive(True)
        self.send_btn.set_sensitive(True)
        if isinstance(e, sending.DeliveryUncertain) or self.draft.delivery_state == "uncertain":
            self.draft.delivery_state = "uncertain"
        elif isinstance(e, sending.SendError):
            self.draft.delivery_state = "draft"
        else:
            self.draft.delivery_state = "uncertain"
            e = sending.DeliveryUncertain("An unexpected sending error occurred. Check Sent and the "
                                          "recipients before retrying; delivery could not be confirmed.")
        self.save_draft(quiet=True)
        self._say(str(e), True)

    def discard(self, *_):
        if not self.send_btn.get_sensitive():
            return
        if self._discard_prompt is not None:
            self._discard_prompt.present()
            return

        def discard():
            try:
                self.draft_store.delete(self.draft.id)
            except Exception:
                self._say("Could not remove the saved draft.", True)
                return
            self.sent = True
            self.destroy()

        self._discard_prompt = confirm(
            self, "Discard this message?", "The saved draft and its attachments will be removed.",
            "_Keep Writing", "_Discard", discard)
        self._discard_prompt.connect("close-request", lambda *_: setattr(self, "_discard_prompt", None))

    def _on_close_request(self, *_):
        if not self.send_btn.get_sensitive() and not self.sent:
            self._say("Sending is still in progress. Wait for the result before closing.", True)
            return True
        return not self.save_draft(quiet=True)
