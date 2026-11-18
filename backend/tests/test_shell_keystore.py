"""ShellKeyStore: the macOS key kept by the app (`mnemosyne --keychain …`), here with a stand-in
binary that keeps its items in a JSON file. Never the real keyring: the old place is a
MemoryKeyStore."""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest

from mnemosyne.storage import crypto
from mnemosyne.storage.crypto import MemoryKeyStore, ShellKeyStore, SystemKeyStore

KEY = bytes(range(32))
OTHER = bytes(range(1, 33))

FAKE = """#!{python}
import json, os, sys, time
store, log = os.environ["FAKE_KEYCHAIN"], os.environ["FAKE_KEYCHAIN_LOG"]
deny = os.environ.get("FAKE_KEYCHAIN_DENY", "").split(",")
with open(log, "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
if os.environ.get("FAKE_KEYCHAIN_SLEEP"):
    time.sleep(float(os.environ["FAKE_KEYCHAIN_SLEEP"]))
if sys.argv[1:2] != ["--keychain"] or len(sys.argv) != 4:
    sys.exit(2)
op, account = sys.argv[2], sys.argv[3]
if op in deny:
    print("keychain " + op + ": access denied", file=sys.stderr)
    sys.exit(4)
items = json.load(open(store)) if os.path.exists(store) else {{}}
if op == "get":
    if account not in items:
        sys.exit(3)
    value = items[account]
    if os.environ.get("FAKE_KEYCHAIN_GARBLE"):
        value = "QUJD"
    print(value)
elif op == "set":
    items[account] = sys.stdin.read().strip()
elif op == "delete":
    items.pop(account, None)
json.dump(items, open(store, "w"))
"""


@pytest.fixture
def shell(tmp_path, monkeypatch):
    binary = tmp_path / "mnemosyne"
    binary.write_text(FAKE.format(python=sys.executable))
    binary.chmod(0o755)
    store, log = tmp_path / "keychain.json", tmp_path / "argv.log"
    monkeypatch.setenv("MNEMOSYNE_SHELL_BIN", str(binary))
    monkeypatch.setenv("FAKE_KEYCHAIN", str(store))
    monkeypatch.setenv("FAKE_KEYCHAIN_LOG", str(log))

    class Shell:
        path = binary

        @staticmethod
        def items() -> dict[str, str]:
            return json.loads(store.read_text()) if store.exists() else {}

        @staticmethod
        def calls() -> list[list[str]]:
            return (
                [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
            )

    return Shell


def store(tmp_path: Path, legacy: MemoryKeyStore | None = None) -> ShellKeyStore:
    return ShellKeyStore(tmp_path / "data", legacy if legacy is not None else MemoryKeyStore())


def test_the_secret_goes_on_stdin_never_argv(shell, tmp_path):
    keys = store(tmp_path)
    keys.set(KEY)
    assert keys.get() == KEY
    encoded = base64.b64encode(KEY).decode()
    assert shell.items() == {keys.account: encoded}
    calls = shell.calls()
    assert [c[:2] for c in calls] == [["--keychain", "set"], ["--keychain", "get"]]
    assert all(encoded not in arg for call in calls for arg in call)


def test_its_own_account_beside_keyrings(tmp_path):
    # keyring's macOS backend uses the same SecItem namespace: the same account would be the
    # item Python created, whose ACL trusts Python.
    keys = ShellKeyStore(tmp_path, MemoryKeyStore())
    assert keys.account == SystemKeyStore(tmp_path).account + " (app)"


def test_no_key_anywhere(shell, tmp_path):
    assert store(tmp_path).get() is None
    assert shell.items() == {}


def test_the_old_key_moves_into_the_app_item(shell, tmp_path):
    legacy = MemoryKeyStore(KEY)
    keys = store(tmp_path, legacy)
    assert keys.get() == KEY
    assert shell.items() == {keys.account: base64.b64encode(KEY).decode()}
    assert legacy.key is None  # deleted only after it read back the same
    assert keys.get() == KEY  # from the app's item from now on
    assert legacy.key is None


def test_the_app_item_wins_over_an_old_copy(shell, tmp_path):
    legacy = MemoryKeyStore(OTHER)
    keys = store(tmp_path, legacy)
    keys.set(KEY)
    assert keys.get() == KEY
    assert legacy.key == OTHER  # not migrated: the app already has its item


def test_a_denied_prompt_keeps_the_old_item_and_still_starts(shell, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEYCHAIN_DENY", "set")
    legacy = MemoryKeyStore(KEY)
    keys = store(tmp_path, legacy)
    assert keys.get() == KEY  # this run uses the old item
    assert legacy.key == KEY
    assert shell.items() == {}
    monkeypatch.delenv("FAKE_KEYCHAIN_DENY")
    assert keys.get() == KEY  # the next start moves it
    assert legacy.key is None


def test_a_wrong_read_back_keeps_the_old_item(shell, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEYCHAIN_GARBLE", "1")
    legacy = MemoryKeyStore(KEY)
    assert store(tmp_path, legacy).get() == KEY
    assert legacy.key == KEY


def test_denied_reading_the_app_item_falls_back(shell, tmp_path, monkeypatch):
    legacy = MemoryKeyStore(OTHER)
    keys = store(tmp_path, legacy)
    keys.set(KEY)
    monkeypatch.setenv("FAKE_KEYCHAIN_DENY", "get")
    assert keys.get() == OTHER  # whatever the old place has (None after a migration: locked)
    assert shell.items() == {keys.account: base64.b64encode(KEY).decode()}


def test_without_the_app_binary(shell, tmp_path, monkeypatch):
    legacy = MemoryKeyStore(KEY)
    keys = store(tmp_path, legacy)
    shell.path.unlink()  # e.g. the app moved away while a backend outlives it
    assert keys.get() == KEY
    keys.set(OTHER)  # turning encryption on or unlocking: kept in the old place
    assert legacy.key == OTHER
    monkeypatch.delenv("MNEMOSYNE_SHELL_BIN")
    with pytest.raises(crypto.ShellUnavailable, match="not set"):
        keys._run("get")


def test_a_hung_binary_times_out(shell, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEYCHAIN_SLEEP", "5")
    monkeypatch.setattr(ShellKeyStore, "TIMEOUT", 0.5)
    legacy = MemoryKeyStore(KEY)
    assert store(tmp_path, legacy).get() == KEY
    assert legacy.key == KEY


def test_delete_clears_both_places(shell, tmp_path):
    legacy = MemoryKeyStore(OTHER)
    keys = store(tmp_path, legacy)
    keys.set(KEY)
    keys.delete()
    assert shell.items() == {}
    assert legacy.key is None
    keys.delete()  # nothing left: still fine


def test_chosen_only_on_a_mac_started_by_the_app(shell, tmp_path, monkeypatch):
    monkeypatch.setattr(crypto.sys, "platform", "darwin")
    assert isinstance(crypto.system_keystore(tmp_path), ShellKeyStore)
    monkeypatch.delenv("MNEMOSYNE_SHELL_BIN")  # `mnemosyne-backend` from a terminal
    assert isinstance(crypto.system_keystore(tmp_path), SystemKeyStore)
    monkeypatch.setenv("MNEMOSYNE_SHELL_BIN", str(tmp_path / "missing"))
    assert isinstance(crypto.system_keystore(tmp_path), SystemKeyStore)
    monkeypatch.setattr(crypto.sys, "platform", "linux")
    monkeypatch.setenv("MNEMOSYNE_SHELL_BIN", str(shell.path))
    assert isinstance(crypto.system_keystore(tmp_path), SystemKeyStore)
