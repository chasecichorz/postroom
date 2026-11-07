"""The compose window, for new messages, replies and forwards."""
from __future__ import annotations

import mimetypes
import os

from gi.repository import Gdk, GLib, Gtk

from .. import mime, sending
from ..config import Account
from ..mime import Attachment
from .common import announce, focus_within, mods, set_label
from .completion import AddressCompleter


class ComposeWindow(Gtk.Window):
    def __init__(self, main, account: Account, draft: sending.Draft):
        super().__init__(default_width=760, default_height=600)
        self.main = main
        self.accounts = list(main.cfg.accounts)
        self.draft = draft
        self.sent = False
        self.attachments: list[Attachment] = list(draft.attachments)

        grid = Gtk.Grid(column_spacing=12, row_spacing=6, margin_top=12, margin_bottom=12,
                        margin_start=12, margin_end=12)
        self.set_child(grid)
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
        discard.connect("clicked", lambda *_: self.close())
        self.send_btn = Gtk.Button(label="_Send", use_underline=True)
        self.send_btn.add_css_class("suggested-action")
        self.send_btn.connect("clicked", lambda *_: self.send())
        buttons.append(discard)
        buttons.append(self.send_btn)
        grid.attach(buttons, 0, 8, 2, 1)

        # Address suggestions for To and Cc, from the address book.
        self.completers = [
            AddressCompleter(e, lambda t: main.store.search_contacts(t, 8), self._show_hint)
            for e in (self.to, self.cc)]

        sc = Gtk.ShortcutController()
        sc.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        for trig, fn in (("<Control>Return", self.send), ("<Control>KP_Enter", self.send),
                         ("Escape", self._escape), ("F6", lambda: self._cycle(1)),
                         ("<Shift>F6", lambda: self._cycle(-1))):
            sc.add_shortcut(Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string(trig),
                                             Gtk.CallbackAction.new(lambda *_a, fn=fn: (fn(), True)[1])))
        self.add_controller(sc)
        self.connect("close-request", self._on_close_request)
        self.connect("destroy", lambda *_: main.present())
        self._refresh_attachments()
        self._retitle()
        self._initial = self._snapshot()

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
                buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False), len(self.attachments))

    def _say(self, text: str, important: bool = False):
        self.status.set_text(text)
        self.status.set_visible(True)
        announce(self, text, important)

    def send(self):
        if not self.send_btn.get_sensitive():
            return
        buf = self.body.get_buffer()
        draft = sending.Draft(
            to=self.to.get_text(), cc=self.cc.get_text(), subject=self.subject.get_text(),
            body=buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False),
            in_reply_to=self.draft.in_reply_to, references=self.draft.references,
            attachments=self.attachments, reply_source=self.draft.reply_source)
        problem = sending.validate(draft)
        if problem:
            self._say(problem, True)
            self.to.grab_focus()
            return
        acct = self.accounts[self.from_dd.get_selected()]
        sess = self.main.sessions.get(acct.id)
        if sess is None:
            return self._say("That account isn't ready yet.", True)
        self.send_btn.set_sensitive(False)
        self._say("Sending…")
        sess.submit(sess.send, draft, on_done=lambda _r: self._sent(draft), on_error=self._failed)

    def _sent(self, draft):
        self.sent = True
        self.main.message_sent(draft)
        self.destroy()

    def _failed(self, e):
        self.send_btn.set_sensitive(True)
        self._say(str(e), True)

    def _on_close_request(self, *_):
        if self.sent or self._snapshot() == self._initial:
            return False
        d = Gtk.AlertDialog(message="Discard this message?", detail="What you wrote will be lost.",
                            buttons=["_Keep Writing", "_Discard"], cancel_button=0, default_button=0,
                            modal=True)

        def done(dlg, res):
            try:
                if dlg.choose_finish(res) == 1:
                    self.sent = True
                    self.destroy()
            except GLib.Error:
                pass
        d.choose(self, None, done)
        return True
