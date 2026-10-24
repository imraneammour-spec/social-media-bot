"""Exercise the actual age CLI; never real production data or recovery keys."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.fixture
def age_keys(tmp_path):
    age = os.environ.get("AGE_BINARY") or shutil.which("age")
    if not age:
        pytest.skip("age CLI unavailable; encryption integration was not executed.")
    keygen = Path(age).with_name("age-keygen.exe" if os.name == "nt" else "age-keygen")
    identity = tmp_path / "test-identity.txt"
    subprocess.run([str(keygen), "-o", str(identity)], check=True, capture_output=True)
    recipient = subprocess.check_output([str(keygen), "-y", str(identity)], text=True).strip()
    return age, identity, recipient


def test_age_backup_round_trip_and_private_permissions(tmp_path, age_keys):
    from scripts.encrypted_backup import encrypt_file, decrypt_file
    age, identity, recipient = age_keys
    source = tmp_path / "source.dump"
    original = b"disposable database snapshot\x00" * 10000
    source.write_bytes(original)
    encrypted = tmp_path / "backup.age"
    encrypt_file(source, encrypted, recipient, age_binary=age)
    assert original[:100] not in encrypted.read_bytes()
    assert source.read_bytes() == original
    restored = tmp_path / "restored.dump"
    decrypt_file(encrypted, restored, identity, age_binary=age)
    assert restored.read_bytes() == original
    if os.name != "nt":
        assert restored.stat().st_mode & 0o077 == 0
        assert encrypted.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("action", ["encrypt", "decrypt"])
def test_existing_output_is_never_overwritten(tmp_path, age_keys, action):
    from scripts.encrypted_backup import encrypt_file, decrypt_file
    age, identity, recipient = age_keys
    source = tmp_path / "source"
    source.write_bytes(b"disposable snapshot")
    encrypted = tmp_path / "backup.age"
    encrypt_file(source, encrypted, recipient, age_binary=age)
    output = tmp_path / "existing"
    output.write_bytes(b"preserve this file")
    with pytest.raises(FileExistsError):
        if action == "encrypt":
            encrypt_file(source, output, recipient, age_binary=age)
        else:
            decrypt_file(encrypted, output, identity, age_binary=age)
    assert output.read_bytes() == b"preserve this file"


@pytest.mark.parametrize("corruption", ["modified", "truncated", "wrong_key"])
def test_failed_decryption_never_publishes_plaintext(tmp_path, age_keys, corruption):
    from scripts.encrypted_backup import encrypt_file, decrypt_file
    age, identity, recipient = age_keys
    source = tmp_path / "source"
    source.write_bytes(b"private disposable snapshot" * 1000)
    encrypted = tmp_path / "backup.age"
    encrypt_file(source, encrypted, recipient, age_binary=age)
    if corruption == "wrong_key":
        keygen = Path(age).with_name("age-keygen.exe" if os.name == "nt" else "age-keygen")
        identity = tmp_path / "wrong-key"
        subprocess.run([str(keygen), "-o", str(identity)], check=True, capture_output=True)
    else:
        data = bytearray(encrypted.read_bytes())
        if corruption == "modified":
            data[-8] ^= 1
        else:
            data = data[:-20]
        encrypted.write_bytes(data)
    restored = tmp_path / "restored"
    with pytest.raises(RuntimeError):
        decrypt_file(encrypted, restored, identity, age_binary=age)
    assert not restored.exists()
    assert not list(tmp_path.glob(".postenza-crypto-*"))
