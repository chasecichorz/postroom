"""Add or edit an account. For Gmail you only need your address and an app password."""
from __future__ import annotations

import threading
from dataclasses import replace

from gi.repository import Gdk, GLib, Gtk

from .. import autoconfig, credentials, oauth2, passwords
from ..config import Account
from ..imap import ImapError
from ..session import check_login
from .common import announce, set_description, set_label, standalone_window

GMAIL_HELP = ("For Gmail, use an app password, not your normal password. Create one at "
              "myaccount.google.com/apppasswords. Two-step verification has to be turned on. "
              "Or use Sign in with browser.")
GMAIL = autoconfig.ServerConfig(
    autoconfig.Server("imap.gmail.com", 993, autoconfig.SSL, auth=["oauth2", "password"]),
    autoconfig.Server("smtp.gmail.com", 465, autoconfig.SSL), "Gmail", "built-in")


class AccountDialog(Gtk.Window):
    def __init__(self, parent, account: Account | None, on_saved):
        super().__init__(default_width=520, title="Edit Account" if account else "Add Account")
        standalone_window(self, parent)
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
        # A hidden field the screen reader keeps private, and a plain one for checking what
        # was pasted. "Show password" swaps between them; they always hold the same text.
        self.password = Gtk.PasswordEntry(show_peek_icon=True, hexpand=True)
        self.password_plain = Gtk.Entry(hexpand=True)
        self.password_stack = Gtk.Stack(hexpand=True)
        self.password_stack.add_child(self.password)
        self.password_stack.add_child(self.password_plain)
        self.password_label = self._attach(grid, 3, "App _password", self.password_stack,
                                           focus=self.password)
        set_label(self.password_plain, "App password, shown")
        pw_help = GMAIL_HELP if not account else "Leave this empty to keep the saved password."
        set_description(self.password, pw_help)
        set_description(self.password_plain, pw_help)
        self._count_source = 0
        self.password.connect("changed", self._password_changed)

        self.show_pw = Gtk.CheckButton(label="Sho_w password", use_underline=True)
        set_description(self.show_pw, "Shows the password as ordinary text so you can check "
                        "it. Your screen reader will read it aloud.")
        self.show_pw.connect("toggled", self._toggle_password)
        row = Gtk.Box(spacing=12)
        row.append(self.show_pw)
        self.browser_btn = Gtk.Button(label="Sign in with _browser", use_underline=True)
        set_description(self.browser_btn, "Opens your email provider's sign-in page in your "
                        "web browser, instead of using an app password.")
        self.browser_btn.connect("clicked", self._browser_sign_in)
        row.append(self.browser_btn)
        grid.attach(row, 1, 4, 1, 1)
        self._signin = None

        help_label = Gtk.Label(label=GMAIL_HELP, wrap=True, xalign=0, max_width_chars=60)
        help_label.add_css_class("dim")
        grid.attach(help_label, 0, 5, 2, 1)

        exp = Gtk.Expander(label="_Server settings (found for you; change only if needed)",
                           use_underline=True)
        sgrid = Gtk.Grid(column_spacing=12, row_spacing=8, margin_top=8)
        exp.set_child(sgrid)
        self.imap_host = self._field(sgrid, 0, "_IMAP server")
        self.imap_port = self._field(sgrid, 1, "IMAP p_ort")
        self.smtp_host = self._field(sgrid, 2, "S_MTP server")
        self.smtp_port = self._field(sgrid, 3, "SMTP po_rt")
        grid.attach(exp, 0, 6, 2, 1)

        self.error = Gtk.Label(wrap=True, xalign=0, selectable=True, visible=False)
        grid.attach(self.error, 0, 7, 2, 1)

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="_Cancel", use_underline=True)
        cancel.connect("clicked", lambda *_: self.close())
        self.save_btn = Gtk.Button(label="_Save", use_underline=True)
        self.save_btn.add_css_class("suggested-action")
        self.save_btn.connect("clicked", self._save)
        buttons.append(cancel)
        buttons.append(self.save_btn)
        grid.attach(buttons, 0, 8, 2, 1)
        self.set_default_widget(self.save_btn)
        for e in (self.name, self.full_name, self.email, self.imap_host, self.imap_port,
                  self.smtp_host, self.smtp_port):
            e.set_activates_default(True)
        self.password.connect("activate", self._save)
        self.password_plain.connect("activate", self._save)

        a = account or Account(name="", email="")
        self.name.set_text(a.name)
        self.full_name.set_text(a.full_name)
        self.email.set_text(a.email)
        self.imap_host.set_text(a.imap_host)
        self.imap_port.set_text(str(a.imap_port))
        self.smtp_host.set_text(a.smtp_host)
        self.smtp_port.set_text(str(a.smtp_port))
        # Server settings are looked up from the address unless you change them yourself.
        self._filling = False
        self._servers_edited = account is not None
        for e in (self.imap_host, self.imap_port, self.smtp_host, self.smtp_port):
            e.connect("changed", self._server_changed)
        self.connect("close-request", lambda *_: (self._signin and self._signin.cancel(), False)[1])

        kc = Gtk.EventControllerKey()
        kc.connect("key-pressed", lambda _c, kv, _k, _s: (self.close(), True)[1]
                   if kv == Gdk.KEY_Escape else False)
        self.add_controller(kc)
        (self.email if not account else self.password).grab_focus()

    def _field(self, grid, row, label) -> Gtk.Entry:
        entry = Gtk.Entry(hexpand=True)
        self._attach(grid, row, label, entry)
        return entry

    def _attach(self, grid, row, label, widget, focus=None):
        focus = focus or widget
        lbl = Gtk.Label(label=label, use_underline=True, mnemonic_widget=focus, xalign=0)
        set_label(focus, label.replace("_", ""))
        grid.attach(lbl, 0, row, 1, 1)
        grid.attach(widget, 1, row, 1, 1)
        return lbl

    def _password_widget(self) -> Gtk.Widget:
        return self.password_plain if self.show_pw.get_active() else self.password

    def _password_text(self) -> str:
        return self._password_widget().get_text()

    def _toggle_password(self, button):
        shown = button.get_active()
        src, dst = (self.password, self.password_plain) if shown else (self.password_plain, self.password)
        dst.set_text(src.get_text())
        src.set_text("")
        self.password_stack.set_visible_child(dst)
        self.password_label.set_mnemonic_widget(dst)
        announce(self, "Password shown" if shown else "Password hidden")

    def _password_changed(self, entry):
        """Say how long the hidden password is, so a paste can be confirmed without hearing it."""
        if self._count_source:
            GLib.source_remove(self._count_source)

        def say():
            self._count_source = 0
            if not self.show_pw.get_active():
                n = len(entry.get_text().replace(" ", ""))  # spaces are ignored on save
                announce(self, "Password field empty" if n == 0 else
                         f"Password field has {n} character{'s' if n != 1 else ''}")
            return False
        self._count_source = GLib.timeout_add(500, say)

    def _show_error(self, text: str, focus: Gtk.Widget | None = None):
        self.error.set_text(text)
        self.error.set_visible(True)
        announce(self, text, important=True)
        if focus:
            focus.grab_focus()

    def _server_changed(self, *_):
        if not self._filling:
            self._servers_edited = True

    def _fill_servers(self, cfg: autoconfig.ServerConfig):
        self._filling = True
        self.imap_host.set_text(cfg.incoming.hostname)
        self.imap_port.set_text(str(cfg.incoming.port))
        self.smtp_host.set_text(cfg.outgoing.hostname)
        self.smtp_port.set_text(str(cfg.outgoing.port))
        self._filling = False

    def _account_from_fields(self, email: str, auth: str) -> Account | str:
        """The account as typed, or an error message."""
        try:
            imap_port, smtp_port = int(self.imap_port.get_text()), int(self.smtp_port.get_text())
        except ValueError:
            return "The port numbers must be numbers."
        base = self.account or Account(name="", email="")
        return replace(base, name=self.name.get_text().strip() or email,
                       full_name=self.full_name.get_text().strip(), email=email,
                       imap_host=self.imap_host.get_text().strip() or "imap.gmail.com",
                       imap_port=imap_port,
                       imap_security="starttls" if imap_port == 143 else "ssl",
                       smtp_host=self.smtp_host.get_text().strip() or "smtp.gmail.com",
                       smtp_port=smtp_port, smtp_security="", auth=auth)

    def _find_servers(self, email: str) -> autoconfig.ServerConfig | None:
        """Look the servers up (worker thread). None means keep what is in the fields."""
        if self._servers_edited:
            return None
        if email.lower().endswith(("@gmail.com", "@googlemail.com")):
            return GMAIL
        return autoconfig.find_config(email)

    def _busy(self, busy: bool):
        self.save_btn.set_sensitive(not busy)
        self.browser_btn.set_sensitive(not busy)

    def _start(self, email: str, auth: str, work):
        """Find servers, then run work(acct) on a worker thread; it returns an error or None."""
        self._busy(True)
        self.error.set_visible(False)

        def run():
            try:
                cfg = self._find_servers(email)
            except (LookupError, ValueError) as e:
                msg = (f"Postroom couldn't find the mail servers for this address ({e}). "
                       "Open Server settings and enter them.")
                GLib.idle_add(lambda: self._failed(msg, self.email) and False)
                return
            if cfg is not None:
                done = threading.Event()

                def fill():
                    self._fill_servers(cfg)
                    done.set()
                    return False
                GLib.idle_add(fill)
                done.wait()
            holder = {}

            def make():
                holder["acct"] = self._account_from_fields(email, auth)
                done2.set()
                return False
            done2 = threading.Event()
            GLib.idle_add(make)
            done2.wait()
            acct = holder["acct"]
            if isinstance(acct, str):
                GLib.idle_add(lambda: self._failed(acct, self.imap_port) and False)
                return
            try:
                work(acct)
            except (ImapError, credentials.CredentialError, oauth2.OAuthError) as e:
                err = str(e)
                GLib.idle_add(lambda: self._failed(err, self._password_widget()) and False)
            except Exception as e:  # noqa: BLE001
                err = f"Something went wrong: {e}"
                GLib.idle_add(lambda: self._failed(err, self._password_widget()) and False)
        threading.Thread(target=run, daemon=True).start()

    def _failed(self, msg: str, focus=None):
        self._busy(False)
        self._signin = None
        self._show_error(msg, focus)

    def _done(self, acct: Account):
        note = getattr(self, "_note", None)
        self.on_saved(acct, self.account is None)
        if note:
            GLib.timeout_add(1500, lambda: (announce(self.parent_win, note, important=True), False)[1])
        self.destroy()

    def _email(self) -> str | None:
        email = self.email.get_text().strip()
        if "@" not in email:
            self._show_error("Enter your full email address.", self.email)
            return None
        return email

    def _save(self, *_):
        email = self._email()
        if email is None:
            return
        pw = self._password_text().replace(" ", "").strip()
        keep_oauth = (not pw and self.account is not None and self.account.auth == "oauth2")
        if not pw and not self.account:
            return self._show_error("Enter the app password, or use Sign in with browser.",
                                    self._password_widget())
        announce(self, "Checking the account, please wait")

        def work(acct: Account):
            secret = pw
            if keep_oauth:
                secret = credentials.secret(acct)
            elif not secret:
                secret = passwords.lookup(acct.id) or ""
            try:
                check_login(acct, secret)
            except ImapError as e:
                hint = (" Make sure you used an app password." if acct.is_gmail and not keep_oauth
                        and "fail" in str(e).lower() else "")
                raise ImapError(str(e) + hint) from e
            if not keep_oauth:
                passwords.store(acct.id, f"Postroom: {acct.email}", secret)
            GLib.idle_add(lambda: self._done(acct) and False)
        self._start(email, "oauth2" if keep_oauth else "password", work)

    def _browser_sign_in(self, *_):
        email = self._email()
        if email is None:
            return
        announce(self, "Finding your provider's sign-in page, please wait")

        def work(acct: Account):
            issuer, client = credentials.issuer_and_client(acct)
            self._signin = signin = oauth2.SignIn(issuer, client, login_hint=acct.email)
            url = signin.start()
            GLib.idle_add(lambda: self._open_browser(url) and False)
            tokens = signin.wait()
            GLib.idle_add(lambda: announce(self, "Signed in. Checking the account, please wait")
                          and False)
            check_login(acct, tokens.access_token)
            credentials.save_oauth(acct, tokens)
            if acct.is_gmail and not tokens.grants(oauth2.GOOGLE_CONTACTS_SCOPE):
                self._note = ("Signed in for mail. Google didn't allow contacts, so Postroom "
                              "can't bring in your Google contacts. To add them, sign in again "
                              "and tick the box for contacts on Google's page.")
            GLib.idle_add(lambda: self._done(acct) and False)
        self._start(email, "oauth2", work)

    def _open_browser(self, url: str):
        def finished(launcher, res):
            try:
                launcher.launch_finish(res)
            except GLib.Error as e:
                if self._signin:
                    self._signin.cancel()
                self._failed(f"Couldn't open your web browser: {e.message}")
        Gtk.UriLauncher(uri=url).launch(self, None, finished)
        announce(self, "Your web browser is opening your provider's sign-in page. Sign in "
                 "there, then come back to Postroom. Press Escape here to cancel.", important=True)
