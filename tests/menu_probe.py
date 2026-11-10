"""Real-key menu regression checks, including keys over the context popover.

Run only on an isolated display: xvfb-run -a dbus-run-session -- python3 -m tests.menu_probe
"""
import os
import subprocess
import sys
import tempfile

import gi
gi.require_version('Atspi', '2.0')
from gi.repository import Atspi, GLib

STEPS = [
    ('start', None, 'collapsed'),
    ('F10', 'F10', 'File'),
    ('Down', 'Down', 'New Message'),
    ('Down', 'Down', 'Saved Drafts'),
    ('Down', 'Down', 'Local Sent Copies'),
    ('Down across section', 'Down', 'Add Account'),
    ('Up', 'Up', 'Local Sent Copies'),
    ('Up', 'Up', 'Saved Drafts'),
    ('Right to Message', 'Right', 'Reply'),
    ('Down', 'Down', 'Reply All'),
    ('Escape submenu', 'Escape', 'Message'),
    ('Escape menubar', 'Escape', 'collapsed'),
    ('Context menu', 'shift+F10', 'Reply'),
    ('Right must not expand underlying thread', 'Right', 'Reply'),
    ('Down', 'Down', 'Reply All'),
    ('Up', 'Up', 'Reply'),
    ('Escape context', 'Escape', 'collapsed'),
]


def main():
    Atspi.init()
    failures = []
    latest = ['']
    def focused(event):
        if event.detail1:
            try:
                latest[0] = event.source.get_name() or ''
            except GLib.Error:
                pass
    listener = Atspi.EventListener.new(focused)
    listener.register('object:state-changed:focused')
    with tempfile.TemporaryDirectory(prefix='postroom-menu-') as profile:
        environment = dict(os.environ, POSTROOM_PROFILE=profile, POSTROOM_NO_KEYRING='1', GTK_A11Y='atspi')
        environment.pop('NO_AT_BRIDGE', None)
        app = subprocess.Popen([sys.executable, '-m', 'tests.fake_app'], env=environment,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        loop = GLib.MainLoop()
        index = [0]
        def advance():
            if index[0] >= len(STEPS):
                loop.quit()
                return False
            label, key, expected = STEPS[index[0]]
            if key:
                result = subprocess.run(['xdotool', 'key', '--clearmodifiers', key], check=False)
                if result.returncode:
                    failures.append(label + ': xdotool failed')
            def verify():
                ok = expected in latest[0]
                print(('PASS ' if ok else 'FAIL ') + label + ': ' + latest[0], flush=True)
                if not ok:
                    failures.append(label)
                index[0] += 1
                advance()
                return False
            GLib.timeout_add(700, verify)
            return False
        def deadline():
            failures.append('menu probe timed out')
            loop.quit()
            return False
        try:
            GLib.timeout_add(3500, advance)
            GLib.timeout_add(45000, deadline)
            loop.run()
        finally:
            app.terminate()
            try:
                app.wait(timeout=5)
            except subprocess.TimeoutExpired:
                app.kill()
                app.wait()
            listener.deregister('object:state-changed:focused')
    print('MENU CHECKS PASSED' if not failures else f'{len(failures)} MENU CHECKS FAILED')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
