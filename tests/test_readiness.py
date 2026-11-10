"""Durability, partial delivery, safe IMAP mutations, history and search."""
import os
from pathlib import Path
import smtplib
import tempfile
import unittest
from unittest.mock import Mock, patch

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

os.environ['POSTROOM_PROFILE'] = tempfile.mkdtemp(prefix='postroom-readiness-')
os.environ['POSTROOM_NO_KEYRING'] = '1'

from postroom.config import Account
from postroom.drafts import DraftStore
from postroom import sending
from postroom.imap import ImapClient, ImapError
from postroom.mime import Attachment
from postroom.models import Message
from postroom.search import Query
from postroom.session import AccountSession
from postroom.store import Store
from tests.fake_imap import FakeClient, Mailbox, _raw


class DraftTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'drafts.db'
        self.repo = DraftStore(self.path)
        self.addCleanup(self.repo.close)

    def test_restart_keeps_body_attachments_reply_identity_and_cursor(self):
        draft = sending.Draft(to='Ann <ann@example.com>', body='Unfinished text',
                              reply_source=('a', 'INBOX', 42), in_reply_to='<original@example.com>',
                              attachments=[Attachment('report.txt', 'text/plain', b'work')])
        self.repo.save('account', draft, 9)
        other = DraftStore(self.path)
        self.addCleanup(other.close)
        account, restored, cursor = other.load(draft.id)
        self.assertEqual((account, cursor), ('account', 9))
        self.assertEqual(restored, draft)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_replace_and_discard_are_atomic(self):
        draft = sending.Draft(body='first', attachments=[Attachment('one', 'text/plain', b'1')])
        self.repo.save('a', draft)
        draft.body = 'second'
        draft.attachments = [Attachment('two', 'text/plain', b'2')]
        self.repo.save('a', draft)
        self.assertEqual(self.repo.load(draft.id)[1].attachments[0].filename, 'two')
        self.repo.delete(draft.id)
        self.assertEqual(self.repo.list(), [])
        self.assertEqual(self.repo.db.execute('SELECT count(*) FROM attachments').fetchone()[0], 0)

    def test_delivery_record_removes_sent_draft_but_retains_copy(self):
        draft = sending.Draft(body='delivered', delivery_state='sent')
        self.repo.save('a', draft)
        self.repo.accepted('a', draft, b'raw accepted mail', ['ann@example.com'])
        self.assertEqual(self.repo.list(), [])
        self.assertEqual(self.repo.db.execute('SELECT raw FROM sent_copies').fetchone()[0], b'raw accepted mail')


class SendingTests(unittest.TestCase):
    def setUp(self):
        self.account = Account(name='Test', email='me@example.com')
        self.draft = sending.Draft(to='Ann <ann@example.com>, Bob <bob@example.com>', subject='Test')
        self.server = Mock()
        self.server.send_message.return_value = {}

    def deliver(self):
        with patch.object(sending.smtplib, 'SMTP_SSL', return_value=self.server):
            return sending.send(self.account, 'not-a-real-password', sending.build(self.account, self.draft))

    def test_partial_delivery_reports_successes_and_failures(self):
        self.server.send_message.return_value = {'bob@example.com': (550, b'no such user')}
        result = self.deliver()
        self.assertEqual(result.accepted, ['ann@example.com'])
        self.assertEqual(result.refused, ['bob@example.com'])

    def test_lost_response_is_uncertain_not_automatically_retried(self):
        self.server.send_message.side_effect = smtplib.SMTPServerDisconnected('lost after DATA')
        with self.assertRaises(sending.DeliveryUncertain):
            self.deliver()
        self.assertEqual(self.server.send_message.call_count, 1)

    def test_all_recipients_rejected_is_a_definite_failure(self):
        self.server.send_message.side_effect = smtplib.SMTPRecipientsRefused({'ann@example.com': (550, b'no')})
        with self.assertRaises(sending.SendError) as caught:
            self.deliver()
        self.assertNotIsInstance(caught.exception, sending.DeliveryUncertain)

    def test_failed_cleanup_does_not_invalidate_accepted_mail(self):
        self.server.close.side_effect = OSError('closed')
        self.assertEqual(len(self.deliver().accepted), 2)

    def test_retry_envelope_only_contains_failed_recipients(self):
        with patch.object(sending.smtplib, 'SMTP_SSL', return_value=self.server):
            sending.send(self.account, 'fake', sending.build(self.account, self.draft), ['bob@example.com'])
        self.assertEqual(self.server.send_message.call_args.kwargs['to_addrs'], ['bob@example.com'])

    def test_message_id_is_stable_across_recovery_and_retry(self):
        first = sending.build(self.account, self.draft)['Message-ID']
        self.assertEqual(first, sending.build(self.account, self.draft)['Message-ID'])


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'cache.db')
        self.addCleanup(self.store.db.close)
        self.repo = DraftStore(Path(self.temp.name) / 'drafts.db')
        self.addCleanup(self.repo.close)
        self.account = Account(name='Test', email='me@gmail.com')
        self.session = AccountSession(self.account, self.store, self.repo)
        self.addCleanup(self.session.pool.shutdown, wait=True)
        self.session.client = Mock()

    def test_partial_send_persists_only_pending_recipients(self):
        draft = sending.Draft(to='ann@example.com', cc='bob@example.com', body='hi')
        self.repo.save(self.account.id, draft)
        with patch('postroom.session.credentials.secret', return_value='fake'), \
             patch('postroom.sending.send', return_value=sending.SendResult(['ann@example.com'], ['bob@example.com'])):
            self.session.send(draft)
        recovered = self.repo.load(draft.id)[1]
        self.assertEqual(recovered.delivery_state, 'partial')
        self.assertEqual((recovered.to, recovered.cc), ('', 'bob@example.com'))
        self.assertEqual(recovered.pending_recipients, ['bob@example.com'])

    def test_mutation_is_not_replayed_on_connection_loss(self):
        self.session.client.move.side_effect = OSError('lost after move')
        with self.assertRaises(ImapError):
            self.session.move('INBOX', [1], 'Trash')
        self.assertEqual(self.session.client.move.call_count, 1)

    def test_sent_copy_failure_is_warning_not_failed_delivery(self):
        self.account.imap_host = 'mail.example.com'
        self.session.folders = []
        with patch('postroom.session.credentials.secret', return_value='fake'), \
             patch('postroom.sending.send', return_value=sending.SendResult(['ann@example.com'])):
            result = self.session.send(sending.Draft(to='ann@example.com'))
        self.assertIn('no Sent folder', result.warning)
        self.assertEqual(self.repo.list(), [])

    def test_search_does_not_pollute_history_cache(self):
        recent = Message(self.account.id, 'INBOX', 100, subject='Recent')
        older = Message(self.account.id, 'INBOX', 2, subject='Found in search')
        self.store.reset_folder(self.account.id, 'INBOX', 1)
        self.store.upsert([recent])
        self.session.client.select.return_value = (100, 1)
        self.session.client.search_uids.return_value = [2]
        self.session.client.fetch_headers.return_value = [older]
        found, more = self.session.search('INBOX', Query('Found', 'subject'))
        self.assertEqual(found, [older])
        self.assertEqual(self.store.uids(self.account.id, 'INBOX'), [100])

    def test_server_search_invalidates_bodies_after_uid_validity_change(self):
        self.store.reset_folder(self.account.id, 'INBOX', 1)
        self.store.save_body(self.account.id, 'INBOX', 7, b'old message with reused UID')
        self.session.client.select.return_value = (1, 2)
        self.session.client.search_uids.return_value = []
        self.session.search('INBOX', Query())
        self.assertIsNone(self.store.body(self.account.id, 'INBOX', 7))
        self.assertEqual(self.store.uidvalidity(self.account.id, 'INBOX'), 2)

    def test_history_pages_uid_gaps_and_survives_normal_sync(self):
        FakeClient.mailbox = Mailbox()
        fake = FakeClient(self.account, 'secret')
        self.session.client = fake
        self.addCleanup(setattr, FakeClient, 'mailbox', None)
        for i in range(1010):
            fake.mb.add('INBOX', _raw(i + 100, 'old@example.com', f'Old {i}', 'body', i + 1, str(i)), {'\\seen'}, str(i))
        self.session.sync_folder('INBOX')
        self.assertEqual(len(self.store.uids(self.account.id, 'INBOX')), 500)
        count, more = self.session.load_older('INBOX')
        self.assertEqual((count, more), (500, True))
        count, more = self.session.load_older('INBOX')
        self.assertEqual(count, 15)
        self.assertFalse(more)
        self.session.sync_folder('INBOX')
        self.assertEqual(len(self.store.uids(self.account.id, 'INBOX')), 1015)


class SearchTests(unittest.TestCase):
    def test_unicode_quotes_and_control_character_rejection(self):
        self.assertEqual(Query('Jörg "plan"', 'subject').criteria(), b'SUBJECT "J\xc3\xb6rg \\"plan\\""')
        with self.assertRaises(ValueError):
            Query('x\r\nALL').criteria()

    def test_dates_flags_and_invalid_range(self):
        self.assertEqual(Query(unread=True, starred=True, since='2026-11-01').criteria(),
                         b'UNSEEN FLAGGED SINCE 01-Nov-2026')
        with self.assertRaises(ValueError):
            Query(since='2026-11-02', before='2026-11-01').criteria()
        with self.assertRaises(ValueError):
            Query(before='tomorrow').criteria()

    def test_offline_search_in_downloaded_body_and_headers(self):
        msg = Message('a', 'INBOX', 7, subject='Lunch', from_name='Jörg', from_addr='j@example.com')
        self.assertTrue(Query('JÖRG', 'from').matches(msg))
        self.assertFalse(Query('buried', 'body').matches(msg))
        self.assertTrue(Query('buried', 'body').matches(msg, b'Subject: X\n\nA buried phrase'))


class ImapSafetyTests(unittest.TestCase):
    def test_no_unrestricted_expunge_or_copy_when_server_lacks_safe_capability(self):
        client = ImapClient(Account(name='Test', email='me@example.com'), 'fake')
        client.conn = Mock()
        client.selected = 'INBOX'
        with self.assertRaises(ImapError):
            client.move('INBOX', [1], 'Trash')
        with self.assertRaises(ImapError):
            client.delete_forever('INBOX', [1])
        client.conn.uid.assert_not_called()
        client.conn.expunge.assert_not_called()


class ProviderTests(unittest.TestCase):
    def test_explicit_provider_lookup_uses_provider_domain(self):
        from types import SimpleNamespace
        from postroom.ui.account_dialog import AccountDialog
        dialog = SimpleNamespace(_servers_edited=False, _selected_type='fastmail')
        with patch('postroom.ui.account_dialog.autoconfig.find_config') as lookup:
            AccountDialog._find_servers(dialog, 'me@custom.example')
        lookup.assert_called_once_with('setup@fastmail.com')

    def test_custom_servers_skip_lookup(self):
        from types import SimpleNamespace
        from postroom.ui.account_dialog import AccountDialog
        dialog = SimpleNamespace(_servers_edited=True, _selected_type='other')
        with patch('postroom.ui.account_dialog.autoconfig.find_config') as lookup:
            self.assertIsNone(AccountDialog._find_servers(dialog, 'me@example.com'))
        lookup.assert_not_called()

    def test_editing_type_does_not_change_existing_oauth_registration_or_identity(self):
        from types import SimpleNamespace
        from postroom.ui.account_dialog import AccountDialog
        def field(value):
            return SimpleNamespace(get_text=lambda: value)
        original = Account(name='Existing', email='me@gmail.com', auth='oauth2', id='preserved')
        dialog = SimpleNamespace(account=original, _selected_type='gmail',
                                 name=field('Existing'), full_name=field('Name'),
                                 imap_host=field('imap.gmail.com'), imap_port=field('993'),
                                 smtp_host=field('smtp.gmail.com'), smtp_port=field('465'))
        saved = AccountDialog._account_from_fields(dialog, original.email, original.auth)
        self.assertEqual((saved.id, saved.auth, saved.account_type), ('preserved', 'oauth2', 'gmail'))


class PasswordInputTests(unittest.TestCase):
    def test_other_provider_password_preserves_spaces(self):
        from types import SimpleNamespace
        from postroom.ui.account_dialog import AccountDialog
        dialog = SimpleNamespace(_password_text=lambda: ' pass word ', _selected_type='other')
        self.assertEqual(AccountDialog._password_for_save(dialog), ' pass word ')

    def test_gmail_formatted_app_password_ignores_spaces(self):
        from types import SimpleNamespace
        from postroom.ui.account_dialog import AccountDialog
        dialog = SimpleNamespace(_password_text=lambda: ' app pass ', _selected_type='gmail')
        self.assertEqual(AccountDialog._password_for_save(dialog), 'apppass')


class AccountSaveTests(unittest.TestCase):
    def test_edit_replaces_old_config_record(self):
        from dataclasses import replace
        from types import SimpleNamespace
        from postroom.ui.main_window import MainWindow
        original = Account(name='Before', email='me@gmail.com', auth='oauth2')
        changed = replace(original, name='After', account_type='gmail')
        cfg = SimpleNamespace(accounts=[original], save=Mock())
        window = SimpleNamespace(cfg=cfg, sessions={}, _sending_for=lambda _: False,
                                 _reload_accounts=Mock(), say=Mock())
        MainWindow._account_saved(window, changed, False)
        self.assertIs(cfg.accounts[0], changed)
        self.assertEqual(cfg.accounts[0].auth, 'oauth2')
        cfg.save.assert_called_once()


class QuitGuardTests(unittest.TestCase):
    def test_quit_saves_all_composers_before_destroying_them(self):
        from types import SimpleNamespace
        from postroom.ui.main_window import MainWindow
        calls = []
        def composer(name):
            return SimpleNamespace(send_btn=SimpleNamespace(get_sensitive=lambda: True),
                                   save_draft=lambda **_: calls.append('save ' + name) or True,
                                   destroy=lambda: calls.append('destroy ' + name))
        window = SimpleNamespace(composers={'a': composer('a'), 'b': composer('b')})
        self.assertTrue(MainWindow.prepare_close(window))
        self.assertEqual(calls, ['save a', 'save b', 'destroy a', 'destroy b'])

    def test_failed_draft_save_blocks_quit(self):
        from types import SimpleNamespace
        from postroom.ui.main_window import MainWindow
        composer = SimpleNamespace(send_btn=SimpleNamespace(get_sensitive=lambda: True),
                                   save_draft=lambda **_: False, present=Mock(), destroy=Mock())
        self.assertFalse(MainWindow.prepare_close(SimpleNamespace(composers={'a': composer})))
        composer.destroy.assert_not_called()


if __name__ == '__main__':
    unittest.main()
