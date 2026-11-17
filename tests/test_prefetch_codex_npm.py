"""Offline hostile artifact cases; no network, npm, credentials or model."""
import base64
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("prefetch_codex_npm",
    Path(__file__).resolve().parent.parent / "tools/prefetch_codex_npm.py")
npm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(npm)


def package():
    return {"name": "@openai/codex", "version": npm.VERSION,
        "bin": {"codex": "bin/codex.js"},
        "optionalDependencies": {f"@openai/codex-{p}": f"npm:@openai/codex@{npm.VERSION}-{p}"
                                 for p in npm.PLATFORMS},
        "dist": {"integrity": npm.INTEGRITY[npm.VERSION],
            "tarball": npm.expected_tarball(npm.VERSION), "unpackedSize": 1024, "fileCount": 3}}


class PrefetchSecurityTests(unittest.TestCase):
    def test_url_and_redirect_boundaries_keep_official_https(self):
        npm.validate_url(npm.metadata_url(npm.VERSION))
        npm.validate_url(npm.expected_tarball(npm.VERSION))
        for bad in ("http://registry.npmjs.org/a", "https://registry.npmjs.org.evil/a",
                    "https://user@registry.npmjs.org/a", "https://registry.npmjs.org:8443/a",
                    "https://127.0.0.1/a", "https://registry.npmjs.org/a?token=secret",
                    "https://registry.npmjs.org/a#x", "https://registry.npmjs.org/a\n"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                npm.validate_url(bad)
            with self.assertRaises(ValueError):
                npm.OfficialRedirect().redirect_request(None, None, 302, "", {}, bad)

    def test_metadata_identity_integrity_and_dependency_graph_are_pinned(self):
        npm.validate_metadata(package(), npm.VERSION)
        changes = [("name", "@evil/codex"), ("version", "latest"),
            ("dependencies", {"secret": "https://evil.example/a"}),
            ("scripts", {"postinstall": "curl evil"}), ("optionalDependencies", {})]
        for key, value in changes:
            bad = package(); bad[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                npm.validate_metadata(bad, npm.VERSION)
        for key, value in (("integrity", "sha512-AAAA"),
                           ("tarball", "https://registry.npmjs.org/other.tgz"),
                           ("unpackedSize", npm.MAX_UNPACKED + 1), ("fileCount", True)):
            bad = package(); bad["dist"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                npm.validate_metadata(bad, npm.VERSION)

    def test_sha512_detects_tampering_without_accepting_weak_hashes(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "data.tgz"
            path.write_bytes(b"official synthetic bytes")
            integrity = "sha512-" + base64.b64encode(hashlib.sha512(path.read_bytes()).digest()).decode()
            self.assertEqual(npm.verify_digest(path, integrity)["bytes"], 24)
            path.write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "mismatch"):
                npm.verify_digest(path, integrity)
            for value in ("sha1-AAAA", "sha512-AAAA", integrity + " sha1-AAAA", "sha512-%%%%"):
                with self.assertRaises(ValueError): npm.integrity_digest(value)

    def test_archive_validation_never_extracts_links_traversal_or_scripts(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "archive.tgz"
            def make(extra=None, mutate=None):
                obj = package(); obj.pop("dist")
                if mutate: mutate(obj)
                data = json.dumps(obj).encode()
                with tarfile.open(path, "w:gz") as archive:
                    info = tarfile.TarInfo("package/package.json"); info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))
                    if extra: archive.addfile(extra, io.BytesIO(b""))
                return {"unpackedSize": len(data), "fileCount": 1}
            dist = make()
            npm.inspect_archive(path, npm.VERSION, dist)
            for name in ("../../token", "/root/.codex/token", "package/../other", "package\\evil", "package/file\n"):
                info = tarfile.TarInfo(name)
                dist = make(info)
                with self.subTest(name=name), self.assertRaises(ValueError):
                    npm.inspect_archive(path, npm.VERSION, dist)
            link = tarfile.TarInfo("package/link"); link.type = tarfile.SYMTYPE; link.linkname = "/root"
            with self.assertRaises(ValueError): npm.inspect_archive(path, npm.VERSION, make(link))
            dist = make(mutate=lambda obj: obj.update(scripts={"postinstall":"do harm"}))
            with self.assertRaises(ValueError): npm.inspect_archive(path, npm.VERSION, dist)
            dist = make(); dist["unpackedSize"] += 1
            with self.assertRaises(ValueError): npm.inspect_archive(path, npm.VERSION, dist)
            self.assertEqual([p.name for p in Path(root).iterdir()], ["archive.tgz"])

    def test_linux_binary_identity_is_explicit_and_has_no_transitive_dependencies(self):
        binary = {"name":"@openai/codex", "version":npm.VERSION+'-linux-x64',
                  "os":["linux"], "cpu":["x64"]}
        npm.validate_graph(binary, binary["version"])
        for key, value in (("cpu", ["arm64"]), ("os", ["darwin"]),
                           ("dependencies", {"foreign":"latest"}), ("bin", {"other":"run"})):
            bad = copy.deepcopy(binary); bad[key] = value
            with self.assertRaises(ValueError): npm.validate_graph(bad, binary["version"])

    def test_arm64_bundle_cannot_silently_install_x64_binary(self):
        binary = {'name':'@openai/codex','version':npm.VERSION+'-linux-arm64',
            'os':['linux'],'cpu':['arm64']}
        npm.validate_graph(binary,binary['version'])
        binary['cpu']=['x64']
        with self.assertRaises(ValueError): npm.validate_graph(binary,binary['version'])
        project = npm.install_project('linux-arm64')['dependencies']
        self.assertEqual(set(project),{'@openai/codex','@openai/codex-linux-arm64'})
        self.assertEqual(project['@openai/codex-linux-arm64'],f'file:codex-{npm.VERSION}-linux-arm64.tgz')
        with self.assertRaises(ValueError): npm.install_project('linux-arm')

    def test_failed_download_does_not_keep_partial_or_publish_unverified_artifact(self):
        class Response(io.BytesIO):
            def geturl(self): return npm.expected_tarball(npm.VERSION)
        class Opener:
            def open(self, url, timeout): return Response(b"tampered artifact")
        with tempfile.TemporaryDirectory() as root:
            dest = Path(root) / "artifact.tgz"
            with self.assertRaisesRegex(ValueError, "mismatch"):
                npm.fetch_artifact(Opener(), package()["dist"], dest)
            self.assertFalse(dest.exists())
            self.assertFalse(dest.with_suffix(".partial").exists())

    def test_installer_project_config_and_metadata_cannot_redirect_offline_install(self):
        with tempfile.TemporaryDirectory() as root:
            dest = Path(root)
            (dest / "package.json").write_text(json.dumps(npm.install_project()))
            (dest / "empty-global.npmrc").write_bytes(b"")
            for version in npm.INTEGRITY:
                (dest / f"metadata-{version}.json").write_text('{}')
            with patch.object(npm, 'validate_metadata', side_effect=RuntimeError('reached artifact')):
                with self.assertRaisesRegex(RuntimeError, 'reached artifact'):
                    npm.verify_bundle(dest)
            bad = npm.install_project();bad['dependencies']['evil']='https://evil.example'
            (dest / "package.json").write_text(json.dumps(bad))
            with self.assertRaisesRegex(ValueError, 'installer project'):
                npm.verify_bundle(dest)
            (dest / "package.json").write_text(json.dumps(npm.install_project()))
            (dest / "empty-global.npmrc").write_text('registry=https://evil.example')
            with self.assertRaisesRegex(ValueError, 'empty file'):
                npm.verify_bundle(dest)
            (dest / "empty-global.npmrc").unlink()
            (dest / "empty-global.npmrc").symlink_to('/dev/null')
            with self.assertRaisesRegex(ValueError, 'regular files'):
                npm.verify_bundle(dest)


if __name__ == "__main__":
    unittest.main()
