#!/usr/bin/env python3
"""Create a local recipient key, request maintenance, or decrypt its artifact."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from scripts.fx_loadtest_credentials import decrypt, generate_keys
from cryptography.exceptions import InvalidTag

REPO = "Renko6626/TouhouCCB"


def write_private(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["keygen", "request", "download"])
    parser.add_argument("--directory", required=True, help="private directory outside the repository")
    parser.add_argument("--run-id", help="test run ID, not GitHub run number")
    parser.add_argument("--github-run", help="GitHub Actions run number for download")
    parser.add_argument("--operation", choices=["prepare", "mint", "inspect", "cleanup"], default="inspect")
    parser.add_argument("--source-pair", type=int)
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    root = Path(args.directory).expanduser().resolve()
    if root.is_relative_to(Path(__file__).resolve().parents[1]):
        parser.error("credential directory must be outside the repository")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    try:
        if args.action == "keygen":
            if (root / "private.pem").exists() or (root / "public.pem").exists():
                raise ValueError("key already exists; keep it to decrypt earlier artifacts")
            private, public = generate_keys()
            write_private(root / "private.pem", private)
            write_private(root / "public.pem", public)
            print(f"Recipient keys saved in {root}; private.pem stays on this machine.")
            return
        if not args.run_id or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}", args.run_id):
            raise ValueError("provide a valid --run-id")
        if args.action == "request":
            if args.operation == "prepare" and (not args.source_pair or args.source_pair < 1 or args.confirm != "PREPARE"):
                raise ValueError("prepare requires --source-pair and --confirm PREPARE")
            if args.operation == "cleanup" and args.confirm != "CLEANUP":
                raise ValueError("cleanup requires --confirm CLEANUP")
            inputs = {"action": args.operation, "run_id": args.run_id,
                      "confirmation": args.confirm, "source_pair": str(args.source_pair or "")}
            if args.operation in {"prepare", "mint"}:
                inputs["recipient_public_key"] = (root / "public.pem").read_text()
            subprocess.run(["gh", "workflow", "run", "fx-loadtest.yml", "-R", REPO, "--ref", "main", "--json"],
                            input=json.dumps(inputs), text=True, check=True)
            print(f"Requested {args.operation}. Use gh run list -R {REPO} --workflow fx-loadtest.yml to find its run number.")
            return
        if not args.github_run or not re.fullmatch(r"[0-9]{1,20}", args.github_run):
            raise ValueError("download requires --github-run NUMBER")
        with tempfile.TemporaryDirectory(dir=root) as tmp:
            subprocess.run(["gh", "run", "download", args.github_run, "-R", REPO, "-n",
                            f"fx-loadtest-{args.run_id}-credentials", "-D", tmp], check=True)
            envelope = json.loads((Path(tmp) / "credentials.enc.json").read_text())
            data = json.loads(decrypt(envelope, (root / "private.pem").read_bytes()))
            if data["run_id"] != args.run_id:
                raise ValueError("artifact belongs to another test run")
            output = root / f"{args.run_id}-tokens-{args.github_run}.json"
            write_private(output, (json.dumps(data)+"\n").encode())
            print(f"Decrypted credentials: {output}; expires at {data['expires_at']}.")
    except InvalidTag:
        parser.exit(1, "fx-credentials: decryption failed; wrong recipient key or modified artifact\n")
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"fx-credentials: {type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    main()
