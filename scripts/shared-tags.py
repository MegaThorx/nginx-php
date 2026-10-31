#!/usr/bin/env python3
"""Points the shared tags (`latest` and one per PHP major) at the newest supported PHP version that has an image.

Runs after the builds. A new PHP version only takes over `latest` once its image
is published, so while it cannot be built yet (e.g. a PECL extension does not
compile on it), the previous version keeps `latest` and keeps it up to date.

Usage: shared-tags.py [--dry-run]
"""

import datetime
import subprocess
import sys

from common import ENDOFLIFE_URL, IMAGE, Registry, extra_tags, get_json, is_eol, is_released


def targets(cycles):
    """Maps each shared tag to the cycle it should point at."""
    return {tag: cycle for cycle, tags in extra_tags(cycles).items() for tag in tags}


def main():
    dry_run = "--dry-run" in sys.argv[1:]
    today = datetime.datetime.now(datetime.timezone.utc).date()
    registry = Registry(IMAGE)

    published = {}
    for entry in get_json(ENDOFLIFE_URL):
        if is_released(entry, today) and not is_eol(entry, today):
            digest = registry.digest(entry["cycle"])
            if digest:
                published[entry["cycle"]] = digest

    for tag, cycle in sorted(targets(list(published)).items()):
        if registry.digest(tag) == published[cycle]:
            print(f"{tag} → {cycle} (unchanged)")
            continue
        print(f"{tag} → {cycle}")
        if not dry_run:
            subprocess.run(
                ["docker", "buildx", "imagetools", "create", "--tag", f"{IMAGE}:{tag}", f"{IMAGE}@{published[cycle]}"],
                check=True,
            )


if __name__ == "__main__":
    main()
