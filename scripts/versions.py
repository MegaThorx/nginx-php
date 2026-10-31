#!/usr/bin/env python3
"""Decides which PHP versions to build and writes the build matrices for the workflow.

A PHP version is supported while it is released, not past its security EOL on
endoflife.date and published upstream as php:<version>-fpm-alpine. A supported
version is rebuilt when it has no image yet or when its Dockerfile/config, base
image, PECL extensions or Alpine packages changed since the published image.

Environment:
  FORCE               "true" builds every supported version
  GITHUB_OUTPUT, GITHUB_STEP_SUMMARY   written when set
"""

import datetime
import hashlib
import json
import os
import pathlib
import subprocess
import sys

from common import (
    ARCHES, BASE_IMAGE, ENDOFLIFE_URL, IMAGE, LABEL_PREFIX, PECL_STABLE_URL, Registry, env_value, get_json,
    get_text, is_eol, is_released, labels, platform_digests, version_key,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
RUNNERS = {"amd64": "ubuntu-24.04", "arm64": "ubuntu-24.04-arm"}
PECL_EXTENSIONS = ("imagick", "redis")


def source_hash():
    """Hash of the git blobs that make up the image, so it only changes when they change."""
    listing = subprocess.run(
        ["git", "ls-files", "-s", "--", "Dockerfile", "conf", "src"],
        cwd=REPO_ROOT, check=True, capture_output=True, text=True,
    ).stdout
    if not any(line.endswith("\tDockerfile") for line in listing.splitlines()):
        raise RuntimeError("Dockerfile is not tracked by git")
    return hashlib.sha256(listing.encode()).hexdigest()


def wanted_labels(base_digests, pecl_versions, source):
    wanted = {f"{LABEL_PREFIX}base-digest.{arch}": base_digests[arch] for arch in ARCHES}
    wanted[f"{LABEL_PREFIX}source-hash"] = source
    for ext, version in pecl_versions.items():
        wanted[f"{LABEL_PREFIX}{ext}.version"] = version
    return wanted


def rebuild_reason(current, wanted, apk_updates):
    """Why the published image needs a rebuild, or None if it is up to date.

    current: labels of the published image, or None if there is no image yet.
    wanted: the labels a build right now would get (see wanted_labels).
    apk_updates: callable returning the names of upgradable Alpine packages in the published image.
    """
    if current is None:
        return "new PHP version"
    key = f"{LABEL_PREFIX}source-hash"
    if current.get(key) != wanted[key]:
        return "Dockerfile, conf or src changed" if key in current else "image predates automated builds"
    for arch in ARCHES:
        key = f"{LABEL_PREFIX}base-digest.{arch}"
        if current.get(key) != wanted[key]:
            return f"base image updated ({arch})"
    for ext in PECL_EXTENSIONS:
        key = f"{LABEL_PREFIX}{ext}.version"
        if current.get(key) != wanted[key]:
            return f"{ext} {current.get(key)} → {wanted[key]}"
    updates = apk_updates()
    if updates:
        more = f" and {len(updates) - 5} more" if len(updates) > 5 else ""
        return f"Alpine package updates: {', '.join(updates[:5])}{more}"
    return None


def parse_apk_upgrades(output):
    # e.g. "(1/74) Upgrading musl (1.2.4-r0 -> 1.2.4-r3)"
    return sorted({line.split()[2] for line in output.splitlines() if line.split()[1:2] == ["Upgrading"]})


def apk_updates(image_ref):
    """Packages an `apk upgrade` would really upgrade. Unlike `apk list -u` this respects the
    dependency constraints, so a package held back by them does not cause endless rebuilds."""
    output = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "sh", image_ref, "-c",
         "apk update -q >/dev/null && apk upgrade --simulate"],
        check=True, capture_output=True, text=True,
    ).stdout
    return parse_apk_upgrades(output)


def published_labels(registry, cycle):
    """Labels of the published amd64 image and the reference to pull it by, or (None, None)."""
    manifest, digest = registry.manifest(cycle)
    if manifest is None:
        return None, None
    platforms = platform_digests(manifest)
    config_digest = platforms.get("amd64", digest) if "manifests" in manifest else digest
    return labels(registry.config(config_digest)), f"{IMAGE}@{digest}"


def main():
    today = datetime.datetime.now(datetime.timezone.utc).date()
    force = os.environ.get("FORCE") == "true"

    candidates = [e for e in get_json(ENDOFLIFE_URL) if is_released(e, today) and not is_eol(e, today)]
    upstream = Registry(BASE_IMAGE)
    ours = Registry(IMAGE)

    bases = {}
    for entry in candidates:
        index, index_digest = upstream.manifest(f"{entry['cycle']}-fpm-alpine")
        if index is None:
            print(f"{entry['cycle']}: php:{entry['cycle']}-fpm-alpine not published yet, skipping")
            continue
        bases[entry["cycle"]] = (entry, index, index_digest)
    if not bases:
        raise RuntimeError("no supported PHP version found, refusing to continue")

    cycles = sorted(bases, key=version_key, reverse=True)
    pecl_versions = {ext: get_text(PECL_STABLE_URL.format(ext)) for ext in PECL_EXTENSIONS}
    source = source_hash()

    build, merge, summary = [], [], []
    for cycle in cycles:
        entry, index, index_digest = bases[cycle]
        base_digests = platform_digests(index)
        missing = [arch for arch in ARCHES if arch not in base_digests]
        if missing:
            reason, decision = f"upstream image has no linux/{', linux/'.join(missing)} yet", "wait"
        else:
            php_versions = {arch: env_value(upstream.config(base_digests[arch]), "PHP_VERSION") for arch in ARCHES}
            wanted = wanted_labels(base_digests, pecl_versions, source)
            if len(set(php_versions.values())) > 1:
                reason, decision = f"upstream is still publishing ({php_versions})", "wait"
            elif force:
                reason, decision = "forced", "build"
            else:
                current, image_ref = published_labels(ours, cycle)
                reason = rebuild_reason(current, wanted, lambda: apk_updates(image_ref))
                decision = "build" if reason else "skip"
                reason = reason or "up to date"

        summary.append((cycle, entry["eol"], decision, reason))
        print(f"{cycle}: {decision} ({reason})")
        if decision != "build":
            continue

        label_lines = [f"{key}={value}" for key, value in wanted.items()]
        label_lines += [
            f"org.opencontainers.image.base.name={BASE_IMAGE}:{cycle}-fpm-alpine",
            f"org.opencontainers.image.base.digest={index_digest}",
        ]
        for arch in ARCHES:
            build.append({
                "cycle": cycle,
                "arch": arch,
                "platform": f"linux/{arch}",
                "runner": RUNNERS[arch],
                "base": f"{BASE_IMAGE}:{cycle}-fpm-alpine@{index_digest}",
                "labels": "\n".join(label_lines),
            })
        merge.append({"cycle": cycle})

    outputs = {
        "build_matrix": json.dumps({"include": build}),
        "merge_matrix": json.dumps({"include": merge}),
        "has_builds": json.dumps(bool(build)),
        "imagick_version": pecl_versions["imagick"],
        "redis_version": pecl_versions["redis"],
    }
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as file:
            for key, value in outputs.items():
                file.write(f"{key}={value}\n")
    else:
        json.dump(outputs, sys.stdout, indent=2)
        print()

    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as file:
            file.write("| PHP | Security support until | Decision | Reason |\n|---|---|---|---|\n")
            for row in summary:
                file.write("| " + " | ".join(str(cell) for cell in row) + " |\n")


if __name__ == "__main__":
    main()
