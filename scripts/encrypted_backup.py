"""Encrypt backup files with age; keep decryption identities off the server.

Only a successful age operation publishes the output. Existing files are never
replaced, including when another process creates the destination concurrently.
On Windows, use a directory with a restricted NTFS ACL for plaintext recovery.
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def _transform(source, output, arguments, *, age_binary=None):
    source, output = Path(source), Path(output)
    if os.path.lexists(output):
        raise FileExistsError("Refusing to overwrite an existing recovery file.")
    if not source.is_file():
        raise ValueError("A regular input file is required.")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".postenza-crypto-", dir=output.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as destination, source.open("rb") as original:
            result = subprocess.run(
                [age_binary or os.getenv("AGE_BINARY", "age"), *arguments],
                stdin=original, stdout=destination, stderr=subprocess.DEVNULL,
                check=False,
            )
            if result.returncode:
                raise RuntimeError("age failed; no successful recovery output was published.")
            destination.flush()
            os.fsync(destination.fileno())
        # Hard-link creation is atomic and refuses existing names; replace() would overwrite.
        os.link(temporary, output)
        if os.name != "nt":
            descriptor = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def encrypt_file(source, output, recipient, *, age_binary=None):
    if not isinstance(recipient, str) or not recipient.startswith("age1") or any(c.isspace() for c in recipient):
        raise ValueError("An explicit age public recipient is required.")
    _transform(source, output, ["--encrypt", "--recipient", recipient], age_binary=age_binary)


def decrypt_file(source, output, identity, *, age_binary=None):
    if not Path(identity).is_file():
        raise ValueError("A local decryption identity file is required.")
    _transform(source, output, ["--decrypt", "--identity", str(identity)], age_binary=age_binary)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["encrypt", "decrypt"])
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--recipient")
    parser.add_argument("--identity", type=Path)
    args = parser.parse_args()
    try:
        if args.action == "encrypt":
            encrypt_file(args.source, args.output, args.recipient)
        else:
            decrypt_file(args.source, args.output, args.identity or "")
    except Exception as error:
        print("Backup encryption operation failed:", type(error).__name__, file=sys.stderr)
        sys.exit(1)
    print("Backup encryption operation completed; database restore verification remains separate.")
