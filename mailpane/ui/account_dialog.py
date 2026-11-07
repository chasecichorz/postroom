"""Add or edit an account. For Gmail you only need your address and an app password."""
from __future__ import annotations

import threading
from dataclasses import replace

from gi.repository import Gdk, GLib, Gtk

from .. import passwords
from ..config import Account
from ..imap import ImapError
from ..session import check_login
from .common import announce, set_description, set_label

GMAIL_HELP = ("For Gmail, use an app password, not your normal password. Create one at "
              "myaccount.google.com/apppasswords. Two-step verification has to be turned on.")


class AccountDialog(Gtk.Window):
    def __init__(self, parent, account: Account | None, on_saved):
        super().__init__(transient_for=parent, modal=True, default_width=520,
                         title="Edit Account" if account else "Add Account")
        self.parent_win = parent
        self.account = account
        self.on_saved = on_saved

        grid = Gtk.Grid(column_spacing=12, row_spacing=8, margin_top=16, margin_bottom=16,
                        margin_start=16, margin_end=16)
        self.set_child(grid)
        self.name = self._field(grid, 0, "Account _name (for example Personal)")
        self.full_name = self._field(grid, 1, "_Your name, as recipients see it")
        self.email = self._field(grid, 2, "_Email address")
        self.email.set_input_purpose(Gtk.InputPurpose.EMAIL)
        self.password = Gtk.PasswordEntry(show_peek_icon=True, hexpand=True)
        self._attach(grid, 3, "App _password", self.password)
        set_description(self.password, GMAIL_HELP if not account else
                        "Leave this empty to keep the saved password.")

        help_label = Gtk.Label(label=GMAIL_HELP, wrap=True, xalign=0, max_width_chars=60)
        help_label.add_css_class("dim")
        grid.attach(help_label, 0, 4, 2, 1)

        exp = Gtk.Expander(label="_Server settings (only for providers other than Gmail)",
                           use_underline=True)
        sgrid = Gtk.Grid(column_spacing=12, row_spacing=8, margin_top=8)
        exp.set_child(sgrid)
        self.imap_host = self._field(sgrid, 0, "_IMAP server")
        self.imap_port = self._field(sgrid, 1, "IMAP p_ort")
        self.smtp_host = self._field(sgrid, 2, "S_MTP server")
        self.smtp_port = self._field(sgrid, 3, "SMTP po_rt")
        grid.attach(exp, 0, 5, 2, 1)

        self.error = Gtk.Label(wrap=True, xalign=0, selectable=True, visible=False)
        grid.attach(self.error, 0, 6, 2, 1)

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="_Cancel", use_underline=True)
        cancel.connect("clicked", lambda *_: self.close())
        self.save_btn = Gtk.Button(label="_Save", use_underline=True)
        self.save_btn.add_css_class("suggested-action")
        self.save_btn.connect("clicked", self._save)
        buttons.append(cancel)
        buttons.append(self.save_btn)
        grid.attach(buttons, 0, 7, 2, 1)
        self.set_default_widget(self.save_btn)
        for e in (self.name, self.full_name, self.email, self.imap_host, self.imap_port,
                  self.smtp_host, self.smtp_port):
            e.set_activates_default(True)
        self.password.connect("activate", self._save)

        a = account or Account(name="", email="")
        self.name.set_text(a.name)
        self.full_name.set_text(a.full_name)
        self.email.set_text(a.email)
        self.imap_host.set_text(a.imap_host)
        self.imap_port.set_text(str(a.imap_port))
        self.smtp_host.set_text(a.smtp_host)
        self.smtp_port.set_text(str(a.smtp_port))

        kc = Gtk.EventControllerKey()
        kc.connect("key-pressed", lambda _c, kv, _k, _s: (self.close(), True)[1]
                   if kv == Gdk.KEY_Escape else False)
        self.add_controller(kc)
        (self.email if not account else self.password).grab_focus()

    def _field(self, grid, row, label) -> Gtk.Entry:
        entry = Gtk.Entry(hexpand=True)
        self._attach(grid, row, label, entry)
        return entry

    def _attach(self, grid, row, label, widget):
        lbl = Gtk.Label(label=label, use_underline=True, mnemonic_widget=widget, xalign=0)
        set_label(widget, label.replace("_", ""))
        grid.attach(lbl, 0, row, 1, 1)
        grid.attach(widget, 1, row, 1, 1)

    def _show_error(self, text: str, focus: Gtk.Widget | None = None):
        self.error.set_text(text)
        self.error.set_visible(True)
        announce(self, text, important=True)
        if focus:
            focus.grab_focus()

    def _save(self, *_):
        email = self.email.get_text().strip()
        pw = self.password.get_text().replace(" ", "").strip()
        if "@" not in email:
            return self._show_error("Enter your full email address.", self.email)
        if not pw and not self.account:
            return self._show_error("Enter the app password.", self.password)
        try:
            imap_port, smtp_port = int(self.imap_port.get_text()), int(self.smtp_port.get_text())
        except ValueError:
            return self._show_error("The port numbers must be numbers.", self.imap_port)
        base = self.account or Account(name="", email="")
        acct = replace(base, name=self.name.get_text().strip() or email,
                       full_name=self.full_name.get_text().strip(), email=email,
                       imap_host=self.imap_host.get_text().strip() or "imap.gmail.com",
                       imap_port=imap_port,
                       smtp_host=self.smtp_host.get_text().strip() or "smtp.gmail.com",
                       smtp_port=smtp_port)
        if not pw:
            pw = passwords.lookup(acct.id) or ""
        self.save_btn.set_sensitive(False)
        self.error.set_visible(False)
        announce(self, "Checking the account, please wait")

        def work():
            try:
                check_login(acct, pw)
                err = None
            except ImapError as e:
                err = str(e)
            except Exception as e:  # noqa: BLE001
                err = f"Something went wrong: {e}"
            GLib.idle_add(lambda: self._checked(acct, pw, err) and False)
        threading.Thread(target=work, daemon=True).start()

    def _checked(self, acct: Account, pw: str, err: str | None):
        self.save_btn.set_sensitive(True)
        if err:
            hint = " Make sure you used an app password." if acct.is_gmail and "fail" in err.lower() else ""
            self._show_error(err + hint, self.password)
            return
        passwords.store(acct.id, f"Mailpane: {acct.email}", pw)
        self.on_saved(acct, self.account is None)
        self.destroy()
