"""Unit tests for the pure logic in the build scripts: python3 -m unittest discover -s scripts"""

import datetime
import importlib.util
import pathlib
import unittest
from unittest import mock

import common
from common import LABEL_PREFIX, env_value, extra_tags, is_eol, platform_digests
from versions import parse_apk_upgrades, rebuild_reason, wanted_labels

def load_script(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), pathlib.Path(__file__).parent / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


update_readme = load_script("update-readme")
shared_tags = load_script("shared-tags")

TODAY = datetime.date(2026, 10, 30)


def wanted():
    return wanted_labels({"amd64": "sha256:a", "arm64": "sha256:b"}, {"imagick": "3.8.1", "redis": "6.3.0"}, "hash")


def no_updates():
    return []


class SupportTest(unittest.TestCase):
    def test_eol_date(self):
        self.assertTrue(is_eol({"eol": "2025-12-31"}, TODAY))
        self.assertTrue(is_eol({"eol": "2026-10-30"}, TODAY))
        self.assertFalse(is_eol({"eol": "2026-12-31"}, TODAY))

    def test_eol_boolean(self):
        self.assertTrue(is_eol({"eol": True}, TODAY))
        self.assertFalse(is_eol({"eol": False}, TODAY))

    def test_extra_tags(self):
        self.assertEqual(
            extra_tags(["8.5", "8.4", "8.3", "8.2"]),
            {"8.5": ["8", "latest"], "8.4": [], "8.3": [], "8.2": []},
        )

    def test_extra_tags_new_major(self):
        self.assertEqual(
            extra_tags(["8.10", "8.9", "9.0"]),
            {"8.10": ["8"], "8.9": [], "9.0": ["9", "latest"]},
        )


class SharedTagsTest(unittest.TestCase):
    def test_targets_newest_published(self):
        self.assertEqual(shared_tags.targets(["8.2", "8.5", "8.4"]), {"8": "8.5", "latest": "8.5"})

    def test_new_major(self):
        self.assertEqual(shared_tags.targets(["8.5", "9.0"]), {"8": "8.5", "9": "9.0", "latest": "9.0"})

    def test_nothing_published(self):
        self.assertEqual(shared_tags.targets([]), {})


class RegistryParsingTest(unittest.TestCase):
    def test_platform_digests_ignores_attestations_and_other_arches(self):
        index = {"manifests": [
            {"digest": "sha256:a", "platform": {"os": "linux", "architecture": "amd64"}},
            {"digest": "sha256:x", "platform": {"os": "unknown", "architecture": "unknown"}},
            {"digest": "sha256:v7", "platform": {"os": "linux", "architecture": "arm", "variant": "v7"}},
            {"digest": "sha256:b", "platform": {"os": "linux", "architecture": "arm64", "variant": "v8"}},
        ]}
        self.assertEqual(platform_digests(index), {"amd64": "sha256:a", "arm64": "sha256:b"})

    def test_env_value(self):
        config = {"config": {"Env": ["PATH=/usr/bin", "PHP_VERSION=8.5.12"]}}
        self.assertEqual(env_value(config, "PHP_VERSION"), "8.5.12")
        self.assertIsNone(env_value(config, "MISSING"))


class ApkUpgradesTest(unittest.TestCase):
    def test_parses_simulated_upgrades(self):
        output = (
            "WARNING: This simulation is not reliable as apk-tools upgrade is available.\n"
            "(1/3) Upgrading musl (1.2.4-r0 -> 1.2.4-r3)\n"
            "(2/3) Installing libfoo (1.0-r0)\n"
            "(3/3) Upgrading ca-certificates-bundle (20230506-r0 -> 20241121-r1)\n"
            "OK: 290 MiB in 100 packages\n"
        )
        self.assertEqual(parse_apk_upgrades(output), ["ca-certificates-bundle", "musl"])

    def test_nothing_to_upgrade(self):
        self.assertEqual(parse_apk_upgrades("OK: 290 MiB in 100 packages\n"), [])


class RegistryTokenTest(unittest.TestCase):
    def test_refreshes_expired_token_once(self):
        tokens = iter(["first", "second"])
        responses = iter([
            (401, {}, b""),
            (200, {"Docker-Content-Digest": "sha256:abc"}, b'{"manifests": []}'),
        ])
        seen = []

        def fake_http_get(url, headers=None, method="GET"):
            seen.append(headers["Authorization"])
            return next(responses)

        with mock.patch.object(common, "get_json", lambda url: {"token": next(tokens)}), \
                mock.patch.object(common, "http_get", fake_http_get):
            manifest, digest = common.Registry("ghcr.io/megathorx/nginx-php").manifest("8.5")
        self.assertEqual((manifest, digest), ({"manifests": []}, "sha256:abc"))
        self.assertEqual(seen, ["Bearer first", "Bearer second"])


class RebuildReasonTest(unittest.TestCase):
    def test_up_to_date(self):
        self.assertIsNone(rebuild_reason(wanted(), wanted(), no_updates))

    def test_new_version(self):
        self.assertEqual(rebuild_reason(None, wanted(), no_updates), "new PHP version")

    def test_image_without_labels(self):
        self.assertEqual(rebuild_reason({}, wanted(), no_updates), "image predates automated builds")

    def test_source_changed(self):
        current = {**wanted(), f"{LABEL_PREFIX}source-hash": "old"}
        self.assertEqual(rebuild_reason(current, wanted(), no_updates), "Dockerfile, conf or src changed")

    def test_base_changed(self):
        current = {**wanted(), f"{LABEL_PREFIX}base-digest.arm64": "sha256:old"}
        self.assertEqual(rebuild_reason(current, wanted(), no_updates), "base image updated (arm64)")

    def test_pecl_changed(self):
        current = {**wanted(), f"{LABEL_PREFIX}redis.version": "6.2.0"}
        self.assertEqual(rebuild_reason(current, wanted(), no_updates), "redis 6.2.0 → 6.3.0")

    def test_apk_updates(self):
        updates = ["a", "b", "c", "d", "e", "f", "g"]
        self.assertEqual(
            rebuild_reason(wanted(), wanted(), lambda: updates),
            "Alpine package updates: a, b, c, d, e and 2 more",
        )

    def test_apk_not_checked_when_other_reason(self):
        def fail():
            raise AssertionError("apk check should be skipped")
        self.assertEqual(rebuild_reason(None, wanted(), fail), "new PHP version")


class ReadmeTest(unittest.TestCase):
    rows = [
        {"cycle": "8.4", "php": "8.4.27", "alpine": "3.24.3", "nginx": "1.30.6", "eol": "2028-12-31",
         "is_eol": False, "tags": ["8.4", "8.4.27"]},
        {"cycle": "8.5", "php": "8.5.12", "alpine": "3.24.3", "nginx": "1.30.6", "eol": "2029-12-31",
         "is_eol": False, "tags": ["8.5", "8.5.12", "latest", "8"]},
        {"cycle": "7.4", "php": "7.4.33", "alpine": None, "nginx": None, "eol": "2022-11-28",
         "is_eol": True, "tags": ["7.4"]},
    ]

    def test_render(self):
        block = update_readme.render(self.rows)
        lines = block.splitlines()
        self.assertEqual(lines[0], update_readme.START)
        self.assertEqual(lines[-1], update_readme.END)
        supported = lines.index("| `8.5` `8.5.12` `latest` `8` | 8.5.12 | 3.24.3 | 1.30.6 | 2029-12-31 |")
        self.assertEqual(lines[supported + 1], "| `8.4` `8.4.27` | 8.4.27 | 3.24.3 | 1.30.6 | 2028-12-31 |")
        self.assertIn("| `7.4` | 7.4.33 | 2022-11-28 |", lines)
        self.assertLess(supported, lines.index("### End of life (no longer updated)"))

    def test_render_is_deterministic(self):
        self.assertEqual(update_readme.render(self.rows), update_readme.render(list(reversed(self.rows))))

    def test_replace_block_keeps_surrounding_text(self):
        text = f"intro\n{update_readme.START}\nold\n{update_readme.END}\noutro\n"
        self.assertEqual(update_readme.replace_block(text, "NEW"), "intro\nNEW\noutro\n")

    def test_replace_block_requires_markers(self):
        with self.assertRaises(RuntimeError):
            update_readme.replace_block("no markers", "NEW")


if __name__ == "__main__":
    unittest.main()
