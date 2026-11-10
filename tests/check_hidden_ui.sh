#!/usr/bin/env bash
# Run from any directory, outside the coding sandbox. Never use real mail/settings.
set -u
cd -- "$(dirname -- "$0")/.." || exit 1
profile=$(mktemp -d /tmp/postroom-ui-check.XXXXXXXX) || exit 1
trap 'rm -rf -- "$profile"' EXIT
export POSTROOM_PROFILE="$profile/profile"
export POSTROOM_NO_KEYRING=1
export XDG_RUNTIME_DIR="$profile/runtime"
mkdir -m 700 -- "$XDG_RUNTIME_DIR"
export GTK_A11Y=atspi
unset NO_AT_BRIDGE
result=0
echo "Running unit tests"
python3 -m unittest discover -s tests
unit_status=$?
echo "Unit tests exit status: $unit_status"
if [ "$unit_status" -ne 0 ]; then
    result=1
fi
for test_module in tests.ui_smoke tests.menu_probe tests.a11y_probe tests.completion_probe; do
    echo "Running $test_module"
    xvfb-run -a dbus-run-session -- python3 -m "$test_module"
    test_status=$?
    echo "$test_module exit status: $test_status"
    if [ "$test_status" -ne 0 ]; then
        result=1
    fi
done
exit "$result"
