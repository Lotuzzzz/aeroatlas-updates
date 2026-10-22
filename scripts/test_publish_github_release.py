"""Local publisher tests. Every GitHub interaction is an in-memory fake."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location("aeroatlas_publisher", Path(__file__).with_name("publish-github-release.py"))
publisher = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(publisher)


class FakeGitHub:
    def __init__(self, prepared):
        self.prepared = prepared
        self.release = None
        self.latest = None
        self.files = {}
        self.events = []
        self.private = False
        self.corrupt_upload = False

    def seed(self, *, draft, names=None):
        self.release = {"id": 77, "tag_name": self.prepared["tag"], "name": self.prepared["title"],
                        "body": self.prepared["body"], "draft": draft, "prerelease": False, "assets": []}
        for name, path in self.prepared["assets"].items():
            if names is None or name in names:
                self.add_asset(name, path.read_bytes())
        if not draft:
            self.latest = copy.deepcopy(self.release)

    def add_asset(self, name, data):
        asset_id = len(self.files) + 100
        self.files[asset_id] = data
        self.release["assets"].append({"id": asset_id, "name": name, "size": len(data), "state": "uploaded",
                                       "digest": "sha256:" + hashlib.sha256(data).hexdigest()})

    def api(self, method, endpoint, payload=None, missing_ok=False):
        self.events.append((method, endpoint, copy.deepcopy(payload)))
        base = f"repos/{self.prepared['metadata']['repo']}"
        if method == "GET" and endpoint == base:
            return {"full_name": self.prepared["metadata"]["repo"], "private": self.private, "archived": False, "default_branch": "main"}
        if method == "GET" and endpoint.endswith("/releases/latest"):
            return copy.deepcopy(self.latest)
        if method == "GET" and "/releases/tags/" in endpoint:
            return copy.deepcopy(self.release)
        if method == "GET" and "/git/ref/heads/" in endpoint:
            return {"ref": "refs/heads/main"}
        if method == "POST" and endpoint.endswith("/releases"):
            self.release = {"id": 77, "assets": [], **copy.deepcopy(payload)}
            return copy.deepcopy(self.release)
        if method == "GET" and endpoint.endswith("/releases/77"):
            return copy.deepcopy(self.release)
        if method == "PATCH" and endpoint.endswith("/releases/77"):
            self.release.update(payload)
            self.latest = copy.deepcopy(self.release)
            return copy.deepcopy(self.release)
        raise AssertionError((method, endpoint, payload))

    def upload(self, repo, tag, paths):
        self.events.append(("UPLOAD", [path.name for path in paths]))
        assert self.release["draft"] is True
        for path in paths:
            self.add_asset(path.name, path.read_bytes())
        if self.corrupt_upload:
            self.release["assets"][0]["digest"] = "sha256:" + "0" * 64

    def download(self, repo, asset_id, path):
        self.events.append(("DOWNLOAD", asset_id))
        path.write_bytes(self.files[asset_id])

    def mutations(self):
        return [event for event in self.events if event[0] in ("POST", "PATCH", "UPLOAD")]


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "src").mkdir()
        (self.root / "android/app").mkdir(parents=True)
        (self.root / "src/release.ts").write_text("export const APP_VERSION = '0.5.0';\nexport const APP_VERSION_CODE = 5;\nexport const UPDATE_MANIFEST_URL = 'https://github.com/Lotuzzzz/aeroatlas-updates/releases/latest/download/latest.json';\n")
        (self.root / "android/app/build.gradle").write_text('applicationId "com.aeroatlas.personal"\nversionName "0.5.0"\nversionCode 5\n')
        (self.root / "capacitor.config.ts").write_text("export default { appId: 'com.aeroatlas.personal' };")
        (self.root / "package.json").write_text('{"version":"0.5.0"}')
        self.root_patch = mock.patch.object(publisher, "ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.metadata = publisher.project_metadata()
        self.apk = self.root / "final.apk"
        self.apk.write_bytes(b"final signed APK fixture bytes")
        self.output = self.root / "review"
        self.inspect = mock.patch.object(publisher, "inspect_apk")
        self.inspect_mock = self.inspect.start()
        self.addCleanup(self.inspect.stop)

    def prepare(self):
        return publisher.prepare(self.apk, self.output, self.metadata, Path("aapt"), ["Atualizador online real, versão 0.5.0."])

    def test_prepare_is_offline_and_exact(self):
        prepared = self.prepare()
        manifest = prepared["manifest"]
        self.assertEqual(manifest["versionCode"], 5)
        self.assertEqual(manifest["sha256"], hashlib.sha256(self.apk.read_bytes()).hexdigest())
        self.assertEqual(manifest["size"], self.apk.stat().st_size)
        self.assertEqual(manifest["apkUrl"], "https://github.com/Lotuzzzz/aeroatlas-updates/releases/download/v0.5.0/AeroAtlas-v0.5.0.apk")
        self.assertEqual(set(prepared["assets"]), {"AeroAtlas-v0.5.0.apk", "latest.json"})
        self.assertTrue((self.output / "release-notes.md").is_file())
        self.assertTrue((self.output / "publication-plan.json").is_file())
        self.assertGreaterEqual(self.inspect_mock.call_count, 2)

    def test_main_dry_run_never_constructs_github_client(self):
        with mock.patch.object(publisher, "GitHubCLI", side_effect=AssertionError("network forbidden")), \
             mock.patch.object(publisher, "find_aapt", return_value=Path("aapt")), \
             mock.patch("sys.argv", ["publisher", "--apk", str(self.apk), "--dry-run", "--out", str(self.output)]):
            self.assertEqual(publisher.main(), 0)

    def test_project_mismatches_and_other_repositories_are_refused(self):
        with self.assertRaises(ValueError):
            publisher.project_metadata("Lotuzzzz/private-source")
        (self.root / "package.json").write_text('{"version":"0.6.0"}')
        with self.assertRaises(ValueError):
            publisher.project_metadata()

    def public_channel(self):
        for name in ("src/release.ts", "android/app/build.gradle", "capacitor.config.ts", "package.json"):
            (self.root / name).unlink()
        path = self.root / "release-channel.json"
        path.write_text(json.dumps(self.metadata))
        return path

    def test_standalone_public_channel_needs_no_private_source_files(self):
        self.public_channel()
        self.assertEqual(publisher.project_metadata(), self.metadata)
        prepared = self.prepare()
        self.assertIn("Lotuzzzz/aeroatlas-updates/releases/download/", prepared["manifest"]["apkUrl"])
        self.assertEqual(set(self.root.glob("src/*")), set())

    def test_public_channel_fields_and_types_are_strict(self):
        path = self.public_channel()
        changes = [("repo", "Lotuzzzz/aeroatlas"), ("appId", "com.other.app"),
                   ("versionCode", True), ("versionCode", 5.0), ("versionCode", 0), ("versionCode", 2147483648),
                   ("versionName", "v0.5.0"), ("versionName", "٠.٥.٠"), ("versionName", None),
                   ("manifestUrl", "https://github.com/Lotuzzzz/aeroatlas/releases/latest/download/latest.json"),
                   ("manifestUrl", self.metadata["manifestUrl"] + "?token=forbidden"),
                   ("manifestUrl", self.metadata["manifestUrl"].replace("https:", "http:")),
                   ("privateField", "not permitted")]
        for key, value in changes:
            path.write_text(json.dumps({**self.metadata, key: value}))
            with self.subTest(field=key, value=value), self.assertRaises(ValueError):
                publisher.project_metadata()
        path.write_text(json.dumps({key: value for key, value in self.metadata.items() if key != "appId"}))
        with self.assertRaises(ValueError):
            publisher.project_metadata()
        path.write_text(json.dumps(self.metadata).replace('"versionCode": 5', '"versionCode": 5, "versionCode": 5'))
        with self.assertRaisesRegex(ValueError, "duplicados"):
            publisher.project_metadata()
        path.write_bytes(b" " * 4097)
        with self.assertRaisesRegex(ValueError, "4 KB"):
            publisher.project_metadata()

    def test_public_channel_cannot_hide_partial_or_symlinked_private_metadata(self):
        path = self.public_channel()
        (self.root / "package.json").write_text('{"version":"0.5.0"}')
        with self.assertRaisesRegex(ValueError, "incompletos"):
            publisher.project_metadata()
        (self.root / "package.json").unlink()
        (self.root / "src/release.ts").symlink_to(self.root / "missing-release")
        with self.assertRaisesRegex(ValueError, "incompletos"):
            publisher.project_metadata()
        (self.root / "src/release.ts").unlink()
        path.rename(self.root / "regular-channel.json")
        path.symlink_to(self.root / "regular-channel.json")
        with self.assertRaisesRegex(ValueError, "regular"):
            publisher.project_metadata()

    def test_complete_private_build_metadata_remains_authoritative(self):
        (self.root / "release-channel.json").write_text('{"repo":"unrelated"}')
        self.assertEqual(publisher.project_metadata(), self.metadata)

    def test_actions_never_publishes_with_a_different_repository_token(self):
        prepared = self.prepare()
        gh = FakeGitHub(prepared)
        with mock.patch.dict(publisher.os.environ, {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "Lotuzzzz/aeroatlas"}):
            with self.assertRaisesRegex(ValueError, "próprio repositório"):
                publisher.publish(prepared, gh)
        self.assertEqual(gh.events, [])

    def test_aapt_checks_real_package_code_and_name(self):
        self.inspect.stop()
        self.addCleanup(lambda: None)
        good = "package: name='com.aeroatlas.personal' versionCode='5' versionName='0.5.0' platformBuildVersionName='15'"
        with mock.patch.object(publisher.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, good, "")):
            publisher.inspect_apk(self.apk, Path("aapt"), self.metadata)
        for bad in [good.replace("versionCode='5'", "versionCode='4'"), good.replace("personal", "other"), good.replace("0.5.0", "0.6.0")]:
            with mock.patch.object(publisher.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, bad, "")):
                with self.assertRaises(ValueError):
                    publisher.inspect_apk(self.apk, Path("aapt"), self.metadata)

    def test_prepared_payload_is_repeatable_and_same_version_apk_cannot_change(self):
        self.prepare()
        path = self.output / "latest.json"
        manifest = json.loads(path.read_text())
        manifest["publishedAt"] = "2020-01-01T00:00:00Z"
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        before = path.read_bytes()
        again = self.prepare()
        self.assertEqual(again["manifest"]["publishedAt"], "2020-01-01T00:00:00Z")
        self.assertEqual(path.read_bytes(), before)
        self.apk.write_bytes(b"different APK, same tag")
        with self.assertRaises(ValueError):
            self.prepare()

    def test_new_release_is_a_draft_until_both_assets_are_verified(self):
        prepared = self.prepare()
        gh = FakeGitHub(prepared)
        result = publisher.publish(prepared, gh)
        self.assertIn("Publicado:", result)
        mutations = gh.mutations()
        self.assertEqual([event[0] for event in mutations], ["POST", "UPLOAD", "PATCH"])
        self.assertTrue(mutations[0][2]["draft"])
        self.assertEqual(set(mutations[1][1]), {"AeroAtlas-v0.5.0.apk", "latest.json"})
        self.assertEqual(mutations[2][2], {"draft": False, "prerelease": False, "make_latest": "true"})
        patch_index = next(index for index, event in enumerate(gh.events) if event[0] == "PATCH")
        self.assertEqual(sum(event[0] == "DOWNLOAD" for event in gh.events[:patch_index]), 2)
        self.assertIs(gh.release["draft"], False)

    def test_published_identical_bytes_are_a_read_only_noop(self):
        prepared = self.prepare()
        gh = FakeGitHub(prepared)
        gh.seed(draft=False)
        self.assertIn("mesmos bytes", publisher.publish(prepared, gh))
        self.assertEqual(gh.mutations(), [])

    def test_published_different_payload_is_never_overwritten(self):
        prepared = self.prepare()
        gh = FakeGitHub(prepared)
        gh.seed(draft=False)
        gh.release["assets"][0]["digest"] = "sha256:" + "1" * 64
        with self.assertRaises(ValueError):
            publisher.publish(prepared, gh)
        self.assertEqual(gh.mutations(), [])

    def test_missing_digest_still_requires_exact_downloaded_bytes(self):
        prepared = self.prepare()
        gh = FakeGitHub(prepared)
        gh.seed(draft=False)
        asset = gh.release["assets"][0]
        del asset["digest"]
        gh.files[asset["id"]] = b"x" * asset["size"]
        with self.assertRaises(ValueError):
            publisher.publish(prepared, gh)
        self.assertEqual(gh.mutations(), [])

    def test_failed_upload_verification_leaves_draft_unpublished(self):
        prepared = self.prepare()
        gh = FakeGitHub(prepared)
        gh.corrupt_upload = True
        with self.assertRaises(ValueError):
            publisher.publish(prepared, gh)
        self.assertIs(gh.release["draft"], True)
        self.assertFalse(any(event[0] == "PATCH" for event in gh.events))

    def test_matching_partial_draft_resumes_only_the_missing_asset(self):
        prepared = self.prepare()
        gh = FakeGitHub(prepared)
        gh.seed(draft=True, names={"AeroAtlas-v0.5.0.apk"})
        publisher.publish(prepared, gh)
        self.assertEqual([event[1] for event in gh.events if event[0] == "UPLOAD"], [["latest.json"]])
        self.assertFalse(any(event[0] == "POST" for event in gh.events))

    def test_unreviewed_draft_private_repo_and_changed_local_files_stop_before_mutation(self):
        prepared = self.prepare()
        gh = FakeGitHub(prepared)
        gh.seed(draft=True)
        gh.release["body"] = "unreviewed changes"
        with self.assertRaises(ValueError):
            publisher.publish(prepared, gh)
        self.assertEqual(gh.mutations(), [])
        gh = FakeGitHub(prepared)
        gh.private = True
        with self.assertRaises(ValueError):
            publisher.publish(prepared, gh)
        self.assertEqual(gh.mutations(), [])
        prepared["assets"]["latest.json"].write_text("{}")
        gh = FakeGitHub(prepared)
        with self.assertRaises(ValueError):
            publisher.publish(prepared, gh)
        self.assertEqual(gh.events, [])

    def test_changed_draft_notes_after_upload_are_not_published(self):
        prepared = self.prepare()
        gh = FakeGitHub(prepared)
        original_upload = gh.upload
        def changed_upload(repo, tag, paths):
            original_upload(repo, tag, paths)
            gh.release["body"] = "Concurrent unreviewed release notes"
        gh.upload = changed_upload
        with self.assertRaises(ValueError):
            publisher.publish(prepared, gh)
        self.assertTrue(gh.release["draft"])
        self.assertFalse(any(event[0] == "PATCH" for event in gh.events))

    def test_oversized_manifest_cannot_be_prepared_for_the_android_client(self):
        with self.assertRaises(ValueError):
            publisher.prepare(self.apk, self.output, self.metadata, Path("aapt"), ["á" * 2000] * 40)
        self.assertFalse((self.output / "latest.json").exists())

    def test_a_new_publication_never_rolls_latest_back(self):
        prepared = self.prepare()
        gh = FakeGitHub(prepared)
        newer = {**prepared["manifest"], "versionCode": 6}
        data = json.dumps(newer).encode()
        gh.files[999] = data
        gh.latest = {"draft": False, "prerelease": False, "assets": [{"name": "latest.json", "id": 999, "size": len(data)}]}
        with self.assertRaises(ValueError):
            publisher.publish(prepared, gh)
        self.assertEqual(gh.mutations(), [])


if __name__ == "__main__":
    unittest.main()
