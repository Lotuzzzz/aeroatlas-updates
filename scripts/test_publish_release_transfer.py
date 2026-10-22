"""Transfer checks use a temporary local Git origin and a fake aapt only."""
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location("aeroatlas_transfer", Path(__file__).with_name("publish-release-transfer.py"))
transfer = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(transfer)
publisher = transfer.publisher


class TransferTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name)
        self.root = base / "checkout"
        self.origin = base / "origin.git"
        self.root.mkdir()
        self.command(self.root, "init", "--quiet")
        self.command(base, "init", "--bare", "--quiet", str(self.origin))
        self.command(self.origin, "config", "user.name", "Fixture")
        self.command(self.origin, "config", "user.email", "fixture@example.invalid")
        self.command(self.root, "remote", "add", "origin", str(self.origin))
        (self.root / "src").mkdir()
        (self.root / "android/app").mkdir(parents=True)
        (self.root / "src/release.ts").write_text("export const APP_VERSION = '0.5.0';\nexport const APP_VERSION_CODE = 5;\nexport const UPDATE_MANIFEST_URL = 'https://github.com/Lotuzzzz/aeroatlas-updates/releases/latest/download/latest.json';\n")
        (self.root / "android/app/build.gradle").write_text('applicationId "com.aeroatlas.personal"\nversionName "0.5.0"\nversionCode 5\n')
        (self.root / "capacitor.config.ts").write_text("export default {appId:'com.aeroatlas.personal'};")
        (self.root / "package.json").write_text('{"version":"0.5.0"}')
        patch = mock.patch.object(publisher, "ROOT", self.root)
        patch.start()
        self.addCleanup(patch.stop)
        self.aapt = base / "aapt"
        self.aapt.write_text("#!/usr/bin/env python3\nimport pathlib,sys\np,c,v=pathlib.Path(sys.argv[-1]).read_bytes().split(b'\\n')[0].decode().split('|')\nprint(f\"package: name='{p}' versionCode='{c}' versionName='{v}'\")\n")
        self.aapt.chmod(0o700)
        patch = mock.patch.object(publisher, "find_aapt", return_value=self.aapt)
        patch.start()
        self.addCleanup(patch.stop)
        apk = base / "source.apk"
        apk.write_bytes(b"com.aeroatlas.personal|5|0.5.0\nfixture APK bytes")
        self.seed = publisher.prepare(apk, base / "seed", publisher.project_metadata(), self.aapt, ["Reviewed release."])
        self.apk_name = "AeroAtlas-v0.5.0.apk"
        self.apk = apk.read_bytes()
        self.manifest = self.seed["assets"]["latest.json"].read_bytes()
        self.apk_hash = self.hash(self.apk)
        self.manifest_hash = self.hash(self.manifest)
        self.output = base / "review"

    @staticmethod
    def hash(data):
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def command(directory, *args, data=None):
        return subprocess.check_output(["git", "-C", str(directory), *args], input=data, stderr=subprocess.PIPE)

    def commit(self, *, apk=None, manifest=None, extras=None, mode="100644", kind="blob"):
        values = {self.apk_name: self.apk if apk is None else apk, "latest.json": self.manifest if manifest is None else manifest}
        values.update(extras or {})
        entries = []
        for name, content in sorted(values.items()):
            oid = self.command(self.origin, "hash-object", "-w", "--stdin", data=content).strip()
            entries.append((mode if name == self.apk_name else "100644").encode() + b" " + kind.encode() + b" " + oid + b"\t" + name.encode() + b"\0")
        tree = self.command(self.origin, "mktree", "-z", data=b"".join(entries)).strip().decode()
        oid = self.command(self.origin, "commit-tree", tree, data=b"Reviewed transfer\n").strip().decode()
        self.command(self.origin, "update-ref", "refs/heads/transfer", oid)
        return oid

    def prepare(self, commit, **hashes):
        return transfer.prepare_transfer(commit, hashes.get("apk", self.apk_hash), hashes.get("manifest", self.manifest_hash), self.output)

    def test_valid_transfer_preserves_both_reviewed_files_and_plan_paths(self):
        commit = self.commit()
        with mock.patch.object(publisher, "GitHubCLI", side_effect=AssertionError("GitHub API forbidden")):
            prepared = self.prepare(commit)
        self.assertEqual(prepared["assets"][self.apk_name].read_bytes(), self.apk)
        self.assertEqual(prepared["assets"]["latest.json"].read_bytes(), self.manifest)
        plan = json.loads((self.output / "publication-plan.json").read_text())
        self.assertEqual(plan["notesFile"], str(self.output / "release-notes.md"))
        self.assertEqual(prepared["assetMetadata"]["latest.json"]["sha256"], self.manifest_hash)

    def test_transfer_runs_in_public_infrastructure_without_private_project_files(self):
        metadata = publisher.project_metadata()
        for name in ("src/release.ts", "android/app/build.gradle", "capacitor.config.ts", "package.json"):
            (self.root / name).unlink()
        (self.root / "release-channel.json").write_text(json.dumps(metadata))
        prepared = self.prepare(self.commit())
        self.assertEqual(prepared["metadata"]["repo"], "Lotuzzzz/aeroatlas-updates")
        self.assertEqual(prepared["assets"]["latest.json"].read_bytes(), self.manifest)
        self.assertEqual(prepared["assets"][self.apk_name].read_bytes(), self.apk)

    def test_invalid_identifiers_stop_before_any_git_call(self):
        for commit, apk, manifest in [("main", self.apk_hash, self.manifest_hash), ("--upload-pack=bad", self.apk_hash, self.manifest_hash),
                                      ("a" * 39, self.apk_hash, self.manifest_hash), ("a" * 40, "g" * 64, self.manifest_hash),
                                      ("a" * 40, self.apk_hash, self.manifest_hash + "0")]:
            with self.subTest(commit=commit), mock.patch.object(transfer, "git", side_effect=AssertionError("must validate first")):
                with self.assertRaises(ValueError):
                    transfer.prepare_transfer(commit, apk, manifest, self.output)

    def test_exact_root_tree_rejects_extra_missing_and_symlink_entries(self):
        for kwargs in ({"extras": {"private.txt": b"not permitted"}}, {"mode": "120000"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.prepare(self.commit(**kwargs))
        empty_tree = self.command(self.origin, "mktree", data=b"").strip().decode()
        empty = self.command(self.origin, "commit-tree", empty_tree, data=b"Empty\n").strip().decode()
        self.command(self.origin, "update-ref", "refs/heads/transfer", empty)
        with self.assertRaises(ValueError):
            self.prepare(empty)

    def test_blob_and_gitlink_cannot_act_as_transfer_files(self):
        commit = self.commit()
        blob = self.command(self.origin, "hash-object", "-w", "--stdin", data=b"not a commit").strip().decode()
        self.command(self.origin, "update-ref", "refs/tags/blob", blob)
        with self.assertRaisesRegex(ValueError, "commit Git"):
            self.prepare(blob)
        manifest_blob = self.command(self.origin, "hash-object", "-w", "--stdin", data=self.manifest).strip().decode()
        entries = f"160000 commit {commit}\t{self.apk_name}\n100644 blob {manifest_blob}\tlatest.json\n".encode()
        tree = self.command(self.origin, "mktree", data=entries).strip().decode()
        oid = self.command(self.origin, "commit-tree", tree, data=b"Gitlink\n").strip().decode()
        self.command(self.origin, "update-ref", "refs/heads/transfer", oid)
        with self.assertRaisesRegex(ValueError, "gitlinks"):
            self.prepare(oid)

    def test_oversized_and_empty_blobs_stop_before_extraction(self):
        for kwargs, limit in [({}, 8), ({"apk": b""}, publisher.MAX_APK_BYTES),
                              ({"manifest": b"x" * (publisher.MAX_MANIFEST_BYTES + 1)}, publisher.MAX_APK_BYTES)]:
            with self.subTest(kwargs=tuple(kwargs)), mock.patch.object(publisher, "MAX_APK_BYTES", limit), \
                 mock.patch.object(transfer, "extract_blob", side_effect=AssertionError("size must be checked first")):
                with self.assertRaisesRegex(ValueError, "tamanho permitido"):
                    self.prepare(self.commit(**kwargs))

    def test_hash_mismatch_leaves_no_output(self):
        for hashes in ({"apk": "0" * 64}, {"manifest": "0" * 64}):
            with self.subTest(hashes=tuple(hashes)), self.assertRaisesRegex(ValueError, "SHA-256"):
                self.prepare(self.commit(), **hashes)
            self.assertFalse(self.output.exists())

    def test_manifest_fields_and_noncanonical_reviewed_bytes_are_refused(self):
        original = json.loads(self.manifest)
        changes = {"schema": 2, "appId": "com.other.app", "versionCode": 4, "versionName": "0.6.0",
                   "apkUrl": "https://example.invalid/other.apk", "size": len(self.apk) + 1,
                   "sha256": "0" * 64, "publishedAt": "2026-99-99T00:00:00Z", "notes": None}
        for key, value in changes.items():
            changed = {**original, key: value}
            data = (json.dumps(changed, ensure_ascii=False, indent=2) + "\n").encode()
            with self.subTest(field=key), self.assertRaises(ValueError):
                self.prepare(self.commit(manifest=data), manifest=self.hash(data))
            self.assertFalse(self.output.exists())
        data = json.dumps(original).encode()
        with self.assertRaisesRegex(ValueError, "bytes do manifesto"):
            self.prepare(self.commit(manifest=data), manifest=self.hash(data))

    def test_aapt_rejects_package_code_and_version_even_with_matching_file_hash(self):
        for package, code, version in [("com.other.app", 5, "0.5.0"), ("com.aeroatlas.personal", 4, "0.5.0"),
                                       ("com.aeroatlas.personal", 5, "0.6.0")]:
            apk = f"{package}|{code}|{version}\nfixture".encode()
            manifest = json.loads(self.manifest)
            manifest.update(sha256=self.hash(apk), size=len(apk))
            data = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
            with self.subTest(package=package, code=code, version=version), self.assertRaisesRegex(ValueError, "APK contém"):
                self.prepare(self.commit(apk=apk, manifest=data), apk=self.hash(apk), manifest=self.hash(data))

    def test_existing_output_is_preserved_before_fetch(self):
        self.output.mkdir()
        original = self.output / "keep.txt"
        original.write_text("reviewed")
        with mock.patch.object(transfer, "git", side_effect=AssertionError("no fetch")), self.assertRaises(ValueError):
            self.prepare("a" * 40)
        self.assertEqual(original.read_text(), "reviewed")

    def test_cli_default_does_not_construct_or_call_github_publisher(self):
        commit = self.commit()
        arguments = ["transfer", "--commit", commit, "--apk-sha256", self.apk_hash,
                     "--manifest-sha256", self.manifest_hash, "--out", str(self.output)]
        with mock.patch("sys.argv", arguments), mock.patch.object(publisher, "GitHubCLI", side_effect=AssertionError("no API")), \
             mock.patch.object(publisher, "publish", side_effect=AssertionError("no publication")), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(transfer.main(), 0)


if __name__ == "__main__":
    unittest.main()
