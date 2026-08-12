#!/usr/bin/env python3
"""One-shot release for zellij-auto-name.

Preflight → (optional local build) → annotated tag → push tag →
wait for GitHub Actions Release workflow → verify release assets.

Usage:
  ./scripts/release.py              # tag = v{Cargo.toml version}
  ./scripts/release.py 0.1.1        # tag = v0.1.1 (also updates Cargo.toml)
  ./scripts/release.py --dry-run
  ./scripts/release.py --skip-build
  ./scripts/release.py --allow-dirty
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CARGO_TOML = ROOT / "Cargo.toml"
WORKFLOW_FILE = "release.yml"
ASSET_NAME = "zellij-auto-name.wasm"
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")


class StepError(SystemExit):
    pass


def log(msg: str = "") -> None:
    print(msg, flush=True)


def ok(msg: str) -> None:
    log(f"  ✓ {msg}")


def fail(msg: str) -> None:
    log(f"  ✗ {msg}")
    raise StepError(1)


def warn(msg: str) -> None:
    log(f"  ! {msg}")


def run(
    args: list[str],
    *,
    check: bool = True,
    capture: bool = True,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    log(f"  $ {' '.join(args)}")
    try:
        return subprocess.run(
            args,
            cwd=cwd or ROOT,
            check=check,
            text=True,
            capture_output=capture,
        )
    except FileNotFoundError:
        fail(f"command not found: {args[0]}")
    except subprocess.CalledProcessError as e:
        if e.stdout:
            log(e.stdout.rstrip())
        if e.stderr:
            log(e.stderr.rstrip())
        fail(f"command failed ({e.returncode}): {' '.join(args)}")


def out(args: list[str], **kwargs) -> str:
    return run(args, **kwargs).stdout.strip()


# ── version helpers ──────────────────────────────────────────────────────────


def read_cargo_version() -> str:
    text = CARGO_TOML.read_text(encoding="utf-8")
    m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    if not m:
        fail("could not parse version from Cargo.toml")
    ver = m.group(1)
    if not VERSION_RE.match(ver):
        fail(f"Cargo.toml version not semver X.Y.Z: {ver!r}")
    return ver


def write_cargo_version(version: str) -> None:
    text = CARGO_TOML.read_text(encoding="utf-8")
    new, n = re.subn(
        r'(?m)^version\s*=\s*"[^"]+"',
        f'version = "{version}"',
        text,
        count=1,
    )
    if n != 1:
        fail("failed to rewrite Cargo.toml version")
    CARGO_TOML.write_text(new, encoding="utf-8")


def normalize_version(raw: str | None) -> tuple[str, str]:
    """Return (version without v, tag with v)."""
    if raw is None:
        ver = read_cargo_version()
    else:
        ver = raw[1:] if raw.startswith("v") else raw
        if not VERSION_RE.match(ver):
            fail(f"version must be X.Y.Z (got {raw!r})")
    return ver, f"v{ver}"


# ── preflight ────────────────────────────────────────────────────────────────


def preflight(args: argparse.Namespace, version: str, tag: str) -> None:
    log("==> preflight")

    if not (ROOT / ".git").exists():
        fail("not a git repository")
    ok("git repo")

    for cmd in ("git", "gh", "cargo", "rustup"):
        run(["which", cmd], capture=True)
        ok(cmd)

    # gh auth
    r = run(["gh", "auth", "status"], check=False)
    if r.returncode != 0:
        fail("gh not authenticated — run: gh auth login")
    ok("gh authenticated")

    # remote
    remotes = out(["git", "remote"])
    if "origin" not in remotes.splitlines():
        fail("no git remote named origin")
    origin = out(["git", "remote", "get-url", "origin"])
    ok(f"origin = {origin}")

    # branch
    branch = out(["git", "branch", "--show-current"])
    if branch != args.branch and not args.allow_branch:
        fail(f"on branch {branch!r}, expected {args.branch!r} (or pass --allow-branch)")
    ok(f"branch = {branch}")

    # clean tree
    dirty = out(["git", "status", "--porcelain"])
    if dirty:
        if args.allow_dirty:
            warn("working tree dirty (--allow-dirty)")
        else:
            log(dirty)
            fail("working tree not clean — commit/stash or pass --allow-dirty")
    else:
        ok("working tree clean")

    # sync with origin
    run(["git", "fetch", "origin", "--tags", "--quiet"])
    if branch:
        local = out(["git", "rev-parse", "HEAD"])
        remote_ref = f"refs/remotes/origin/{branch}"
        has_remote = (
            run(["git", "show-ref", "--verify", "--quiet", remote_ref], check=False).returncode
            == 0
        )
        if has_remote:
            remote = out(["git", "rev-parse", remote_ref])
            if local != remote:
                ahead = out(["git", "rev-list", "--count", f"{remote_ref}..HEAD"])
                behind = out(["git", "rev-list", "--count", f"HEAD..{remote_ref}"])
                if behind != "0":
                    fail(f"branch is behind origin/{branch} by {behind} — pull first")
                if ahead != "0":
                    if args.push_commits:
                        warn(f"branch is ahead of origin/{branch} by {ahead} — will push")
                    else:
                        fail(
                            f"branch is ahead of origin/{branch} by {ahead} — "
                            "push first or pass --push-commits"
                        )
                ok(f"synced with origin/{branch}" if ahead == "0" else f"ahead by {ahead}")
            else:
                ok(f"synced with origin/{branch}")
        else:
            warn(f"origin/{branch} does not exist yet")

    # tag free?
    if out(["git", "tag", "-l", tag]):
        fail(f"tag {tag} already exists locally")
    r = run(["git", "ls-remote", "--tags", "origin", tag], check=False)
    remote_tags = (r.stdout or "").strip()
    if remote_tags:
        fail(f"tag {tag} already exists on origin")
    ok(f"tag {tag} is free")

    # cargo version alignment
    cargo_ver = read_cargo_version()
    if cargo_ver != version:
        if args.version:
            warn(f"Cargo.toml is {cargo_ver}, release will set it to {version}")
        else:
            fail(f"internal: cargo version {cargo_ver} != {version}")
    else:
        ok(f"Cargo.toml version = {cargo_ver}")

    # wasm target
    targets = out(["rustup", "target", "list", "--installed"])
    if "wasm32-wasip1" not in targets.splitlines():
        if args.skip_build:
            warn("wasm32-wasip1 not installed (ok with --skip-build)")
        else:
            log("  → rustup target add wasm32-wasip1")
            if not args.dry_run:
                run(["rustup", "target", "add", "wasm32-wasip1"], capture=False)
            ok("installed wasm32-wasip1")
    else:
        ok("wasm32-wasip1 installed")

    log(f"  → will release {tag}")


# ── build ────────────────────────────────────────────────────────────────────


def local_build(dry_run: bool) -> None:
    log("==> local build (pre-check)")
    cmd = ["cargo", "build", "--release", "--target", "wasm32-wasip1"]
    if dry_run:
        log(f"  $ {' '.join(cmd)}  (dry-run)")
        return
    run(cmd, capture=False)
    wasm = ROOT / "target" / "wasm32-wasip1" / "release" / ASSET_NAME
    if not wasm.is_file():
        fail(f"missing build output: {wasm}")
    ok(f"built {wasm} ({wasm.stat().st_size} bytes)")


# ── tag & push ───────────────────────────────────────────────────────────────


def maybe_bump_cargo(version: str, dry_run: bool) -> bool:
    """Return True if Cargo.toml changed."""
    current = read_cargo_version()
    if current == version:
        return False
    log(f"==> bump Cargo.toml {current} → {version}")
    if dry_run:
        log("  (dry-run, skip write)")
        return True
    write_cargo_version(version)
    run(["git", "add", "Cargo.toml"])
    run(
        [
            "git",
            "commit",
            "-m",
            f"chore: bump version to {version}",
        ],
        capture=False,
    )
    ok("committed version bump")
    return True


def push_commits(branch: str, dry_run: bool) -> None:
    log(f"==> push commits ({branch})")
    if dry_run:
        log(f"  $ git push origin {branch}  (dry-run)")
        return
    run(["git", "push", "origin", branch], capture=False)
    ok(f"pushed {branch}")


def create_and_push_tag(tag: str, version: str, dry_run: bool) -> None:
    log(f"==> tag {tag}")
    msg = f"Release {tag}"
    if dry_run:
        log(f"  $ git tag -a {tag} -m {msg!r}  (dry-run)")
        log(f"  $ git push origin {tag}  (dry-run)")
        return
    run(["git", "tag", "-a", tag, "-m", msg])
    ok(f"created annotated tag {tag}")
    run(["git", "push", "origin", tag], capture=False)
    ok(f"pushed {tag}")


# ── wait for CI / release ────────────────────────────────────────────────────


def wait_for_workflow(tag: str, timeout: int, poll: int, dry_run: bool) -> str:
    """Return run URL (or placeholder on dry-run)."""
    log("==> wait for GitHub Actions")
    if dry_run:
        log("  (dry-run, skip wait)")
        return "(dry-run)"

    deadline = time.time() + timeout
    run_id: str | None = None
    run_url = ""

    # Tag push runs have headBranch == tag name
    while time.time() < deadline:
        listing = out(
            [
                "gh",
                "run",
                "list",
                "--workflow",
                WORKFLOW_FILE,
                "--branch",
                tag,
                "--limit",
                "5",
                "--json",
                "databaseId,headBranch,status,conclusion,url,event,displayTitle",
            ]
        )
        runs = json.loads(listing) if listing else []
        match = next((r for r in runs if r.get("headBranch") == tag), None)
        if match is None and runs:
            match = runs[0]

        if match:
            run_id = str(match["databaseId"])
            run_url = match.get("url") or ""
            log(f"  found run {run_id}: {match.get('status')} {run_url}")
            break

        log(f"  … waiting for workflow run for {tag} (poll {poll}s)")
        time.sleep(poll)
    else:
        fail(f"timed out after {timeout}s waiting for workflow run")

    assert run_id is not None

    # gh run watch blocks until completion; --exit-status fails on non-success
    r = run(
        [
            "gh",
            "run",
            "watch",
            run_id,
            "--exit-status",
            "--interval",
            str(poll),
        ],
        check=False,
        capture=False,
    )
    if r.returncode != 0:
        # Fall back to JSON status if watch fails/times out oddly
        info = out(["gh", "run", "view", run_id, "--json", "status,conclusion,url"])
        data = json.loads(info)
        run_url = data.get("url") or run_url
        if data.get("status") == "completed" and data.get("conclusion") == "success":
            ok(f"workflow succeeded: {run_url}")
            return run_url
        fail(
            f"workflow failed (conclusion={data.get('conclusion')}): {run_url}\n"
            f"  logs: gh run view {run_id} --log-failed"
        )

    if not run_url:
        info = out(["gh", "run", "view", run_id, "--json", "url"])
        run_url = json.loads(info).get("url") or ""
    ok(f"workflow succeeded: {run_url}")
    return run_url


def wait_for_release_assets(tag: str, timeout: int, poll: int, dry_run: bool) -> str:
    log("==> wait for release assets")
    if dry_run:
        log("  (dry-run, skip)")
        return "(dry-run)"

    deadline = time.time() + timeout
    while time.time() < deadline:
        r = run(
            ["gh", "release", "view", tag, "--json", "url,assets,isDraft"],
            check=False,
        )
        if r.returncode == 0 and r.stdout.strip():
            data = json.loads(r.stdout)
            assets = [a.get("name") for a in data.get("assets") or []]
            if ASSET_NAME in assets and not data.get("isDraft"):
                ok(f"release ready: {data.get('url')}")
                for name in assets:
                    log(f"    - {name}")
                return data.get("url") or ""
            log(f"  … release exists, assets={assets} (poll {poll}s)")
        else:
            log(f"  … release {tag} not visible yet (poll {poll}s)")
        time.sleep(poll)

    fail(f"timed out after {timeout}s waiting for release {tag} assets")


# ── main ─────────────────────────────────────────────────────────────────────


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Preflight → tag → push → wait for GitHub Release",
    )
    p.add_argument(
        "version",
        nargs="?",
        help="X.Y.Z or vX.Y.Z (default: version from Cargo.toml)",
    )
    p.add_argument(
        "--branch",
        default="main",
        help="expected branch (default: main)",
    )
    p.add_argument(
        "--allow-branch",
        action="store_true",
        help="allow releasing from a non-default branch",
    )
    p.add_argument(
        "--allow-dirty",
        action="store_true",
        help="allow dirty working tree",
    )
    p.add_argument(
        "--push-commits",
        action="store_true",
        help="push local commits on the branch before tagging",
    )
    p.add_argument(
        "--skip-build",
        action="store_true",
        help="skip local cargo build pre-check",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="run preflight only; do not tag/push/wait",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=900,
        help="seconds to wait for CI + release (default: 900)",
    )
    p.add_argument(
        "--poll",
        type=int,
        default=10,
        help="poll interval seconds (default: 10)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    version, tag = normalize_version(args.version)

    log(f"release target: {tag}")
    log(f"repo root:      {ROOT}")
    if args.dry_run:
        log("mode:           dry-run")
    log()

    preflight(args, version, tag)

    if not args.skip_build:
        local_build(args.dry_run)
    else:
        log("==> local build skipped")

    bumped = maybe_bump_cargo(version, args.dry_run)

    # If we committed a bump, or branch was ahead, push commits
    branch = out(["git", "branch", "--show-current"]) or args.branch
    if bumped or args.push_commits:
        push_commits(branch, args.dry_run)

    create_and_push_tag(tag, version, args.dry_run)

    run_url = wait_for_workflow(tag, args.timeout, args.poll, args.dry_run)
    # Remaining time budget shared roughly; assets usually appear with the run
    rel_url = wait_for_release_assets(tag, args.timeout, args.poll, args.dry_run)

    log()
    log("==> done")
    log(f"  tag:      {tag}")
    log(f"  workflow: {run_url}")
    log(f"  release:  {rel_url}")
    if not args.dry_run:
        repo = out(
            ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
            check=False,
        )
        if repo:
            log(
                f"  download: https://github.com/{repo}/releases/download/{tag}/{ASSET_NAME}"
            )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("\naborted")
        sys.exit(130)
