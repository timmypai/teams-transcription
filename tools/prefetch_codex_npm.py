#!/usr/bin/env python3
"""Fetch the launcher and a pinned platform binary from public npm artifacts using unchanged host proxy/TLS.

Only registry.npmjs.org is contacted. No npmrc, credentials, trust files, network
configuration, npm subprocess, model or account is touched. The generated bundle
supports local-file npm install --offline --omit=optional --ignore-scripts.
The fixed SHA512 values were obtained from official version metadata over TLS.
"""
import argparse
import base64
import hashlib
import hmac
import json
from pathlib import Path, PurePosixPath
import tarfile
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, build_opener

VERSION = "0.159.0-alpha.3"
REGISTRY = "https://registry.npmjs.org"
INTEGRITY = {
    VERSION: "sha512-K54D+7OWbERb4Xg4uSXvoGxQ53SCxsdX4AB96rFgQTxnynCLweMAssOl27Is7ZMFKMBWf83R5yTNZpZZYYYy0Q==",
    VERSION + "-linux-arm64": "sha512-msMA83A8ojY3KIBTp7seUHcKoZ8rZgCUEW7st2uwbzsrvR0zBjyIQ/ZE1JfJ5l/J0GXKY0PkDjYtHoNvvr86eQ==",
    VERSION + "-linux-x64": "sha512-7VdPMTC7OIKF8Mlr8LJrjzM7AKDK3VvslvseFsVsBjvo7Ih8Y0Ju4vh5hzrw9KZMd1Ocorjh6Rhukyp7yxq2QA==",
}
PLATFORMS = ("linux-x64", "win32-x64", "darwin-x64", "linux-arm64", "win32-arm64", "darwin-arm64")
MAX_ARCHIVE = 256 * 1024 * 1024
MAX_UNPACKED = 512 * 1024 * 1024


def validate_url(url):
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc != "registry.npmjs.org"
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or any(ord(char) < 33 or ord(char) == 127 for char in url)):
        raise ValueError("Only the public official npm registry with normal HTTPS is accepted")
    return url


class OfficialRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(request, fp, code, message, headers, newurl)


def metadata_url(version):
    if version not in INTEGRITY:
        raise ValueError("Unpinned version")
    return f"{REGISTRY}/@openai%2Fcodex/{version}"


def expected_tarball(version):
    return f"{REGISTRY}/@openai/codex/-/codex-{version}.tgz"


def validate_graph(package, version):
    if package.get("name") != "@openai/codex" or package.get("version") != version:
        raise ValueError("Package identity does not match the pinned official version")
    if package.get("dependencies") or package.get("peerDependencies") or package.get("scripts"):
        raise ValueError("Unexpected runtime dependency or lifecycle script")
    if version == VERSION:
        expected = {f"@openai/codex-{platform}": f"npm:@openai/codex@{VERSION}-{platform}"
                    for platform in PLATFORMS}
        if package.get("optionalDependencies") != expected or package.get("bin") != {"codex": "bin/codex.js"}:
            raise ValueError("Unexpected official launcher dependency graph")
    else:
        if (package.get("optionalDependencies") or package.get("bin")
                or version not in INTEGRITY or package.get("os") != ["linux"]
                or package.get("cpu") != [version.rsplit("-", 1)[1]]):
            raise ValueError("Unexpected Linux binary dependency graph")


def validate_metadata(package, version):
    validate_graph(package, version)
    dist = package.get("dist", {})
    if dist.get("integrity") != INTEGRITY[version]:
        raise ValueError("Official metadata integrity differs from the pinned SHA512")
    if dist.get("tarball") != expected_tarball(version):
        raise ValueError("Unexpected artifact URL")
    validate_url(dist["tarball"])
    if (type(dist.get("unpackedSize")) is not int or not 0 < dist["unpackedSize"] <= MAX_UNPACKED
            or type(dist.get("fileCount")) is not int or not 0 < dist["fileCount"] <= 100):
        raise ValueError("Artifact bounds are missing or invalid")
    return dist


def integrity_digest(value):
    if not value.startswith("sha512-") or " " in value:
        raise ValueError("Only one SHA512 integrity value is accepted")
    try:
        digest = base64.b64decode(value[7:], validate=True)
    except ValueError:
        raise ValueError("Invalid integrity encoding") from None
    if len(digest) != 64:
        raise ValueError("Invalid SHA512 digest size")
    return digest


def verify_digest(path, integrity):
    digest = hashlib.sha512()
    sha256 = hashlib.sha256()
    length = 0
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            length += len(block)
            if length > MAX_ARCHIVE:
                raise ValueError("Artifact exceeds compressed size bound")
            digest.update(block)
            sha256.update(block)
    if not hmac.compare_digest(digest.digest(), integrity_digest(integrity)):
        raise ValueError("Artifact SHA512 mismatch")
    return {"bytes": length, "sha256": sha256.hexdigest(), "integrity": integrity}


def inspect_archive(path, version, dist):
    """Inspect without extracting or executing any tar member."""
    names = set()
    files = 0
    unpacked = 0
    package = None
    with tarfile.open(path, "r|gz") as archive:
        for member in archive:
            name = PurePosixPath(member.name)
            if (name.is_absolute() or ".." in name.parts or "\\" in member.name
                    or any(ord(char) < 32 or ord(char) == 127 for char in member.name)
                    or not name.parts or name.parts[0] != "package" or member.name in names):
                raise ValueError("Unsafe or duplicated archive path")
            names.add(member.name)
            if not (member.isfile() or member.isdir()):
                raise ValueError("Archive links and special files are not accepted")
            if member.isfile():
                files += 1
                unpacked += member.size
                if unpacked > MAX_UNPACKED or files > 100:
                    raise ValueError("Archive exceeds unpacked size/file bounds")
            if member.name == "package/package.json":
                if not member.isfile() or member.size > 65536:
                    raise ValueError("Invalid package.json member")
                stream = archive.extractfile(member)
                package = json.loads(stream.read())
    if package is None:
        raise ValueError("Missing package.json")
    validate_graph(package, version)
    if unpacked != dist["unpackedSize"] or files != dist["fileCount"]:
        raise ValueError("Archive size/count does not match official metadata")
    return {"file_count": files, "unpacked_bytes": unpacked}


def fetch_metadata(opener, version):
    with opener.open(metadata_url(version), timeout=30) as response:
        validate_url(response.geturl())
        data = response.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise ValueError("Metadata size bound exceeded")
    package = json.loads(data)
    validate_metadata(package, version)
    return package


def fetch_artifact(opener, dist, destination):
    partial = destination.with_suffix(".partial")
    length = 0
    try:
        with opener.open(validate_url(dist["tarball"]), timeout=30) as response, partial.open("xb") as output:
            validate_url(response.geturl())
            while block := response.read(1024 * 1024):
                length += len(block)
                if length > MAX_ARCHIVE:
                    raise ValueError("Artifact exceeds compressed size bound")
                output.write(block)
        result = verify_digest(partial, dist["integrity"])
        partial.replace(destination)
        return result
    finally:
        partial.unlink(missing_ok=True)


def versions(platform):
    if platform not in ("linux-x64", "linux-arm64"):
        raise ValueError("Unsupported bundle platform")
    return (VERSION, VERSION + "-" + platform)


def verify_bundle(destination, platform="linux-x64"):
    for filename in ("package.json", "empty-global.npmrc",
                     *(f"metadata-{version}.json" for version in versions(platform))):
        path = destination / filename
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
            raise ValueError("Bundle metadata/config must be bounded regular files")
    if json.loads((destination / "package.json").read_text()) != install_project(platform):
        raise ValueError("Offline installer project differs from the pinned local-file graph")
    config = destination / "empty-global.npmrc"
    if config.is_symlink() or config.read_bytes() != b"":
        raise ValueError("Global npm config must be an explicit empty file")
    outputs = {}
    for version in versions(platform):
        metadata = json.loads((destination / f"metadata-{version}.json").read_text())
        dist = validate_metadata(metadata, version)
        artifact = destination / f"codex-{version}.tgz"
        if artifact.is_symlink():
            raise ValueError("Artifact symlinks are not accepted")
        outputs[version] = {**verify_digest(artifact, INTEGRITY[version]),
                            **inspect_archive(artifact, version, dist)}
    return outputs


def install_project(platform="linux-x64"):
    versions(platform)
    return {"name": "meeting-codex-offline-install", "version": "1.0.0", "private": True,
        "dependencies": {"@openai/codex": f"file:codex-{VERSION}.tgz",
            f"@openai/codex-{platform}": f"file:codex-{VERSION}-{platform}.tgz"}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--verify", action="store_true", help="Offline reverify the existing bundle")
    parser.add_argument("--platform", choices=("linux-x64", "linux-arm64"), default="linux-x64")
    args = parser.parse_args()
    platform = args.platform
    if args.destination.is_symlink():
        raise SystemExit("Choose a regular bundle directory")
    destination = args.destination.resolve()
    if not any(destination.is_relative_to(Path(root)) for root in ("/tmp", "/workspace")):
        raise SystemExit("Bundle must stay under /tmp or /workspace")
    if args.verify:
        print(json.dumps({"schema_version": 1, "verified": verify_bundle(destination, platform)}, indent=2))
        return
    if destination.exists():
        raise SystemExit("Choose a new destination; existing files are never overwritten")
    destination.mkdir(parents=True, mode=0o755)
    # Uses Python's default environment proxy handler and default TLS validation.
    # Never supplies a replacement CA bundle or logs environment/proxy values.
    opener = build_opener(OfficialRedirect())
    records = {}
    for version in versions(platform):
        package = fetch_metadata(opener, version)
        dist = validate_metadata(package, version)
        (destination / f"metadata-{version}.json").write_text(json.dumps(package, indent=2) + "\n")
        artifact = destination / f"codex-{version}.tgz"
        record = fetch_artifact(opener, dist, artifact)
        record.update(inspect_archive(artifact, version, dist))
        record.update(url=dist["tarball"], metadata_url=metadata_url(version))
        records[version] = record
        print(json.dumps({"verified_version": version, **record}), flush=True)
    (destination / "package.json").write_text(json.dumps(install_project(platform), indent=2) + "\n")
    (destination / "empty-global.npmrc").write_bytes(b"")
    manifest = {"schema_version": 1, "platform": platform, "version": VERSION,
        "registry": REGISTRY, "artifacts": records,
        "trust": "official registry normal TLS + fixed official SHA512; registry signatures/provenance not separately verified",
        "install": "npm install --offline --omit=optional --ignore-scripts --no-audit --no-fund --userconfig=/dev/null --globalconfig=./empty-global.npmrc --cache=/tmp/codex-npm-cache",
        "binary": "node_modules/.bin/codex", "global_bin": "link this binary from /usr/local/bin/codex",
        "scope": "public package prefetch only; no OAuth, model, meeting or network/trust setting changes"}
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    for path in destination.iterdir():
        path.chmod(0o644)
    destination.chmod(0o755)
    print(json.dumps({"bundle": str(destination), "version": VERSION, "platform": platform, "artifacts": 2}))


if __name__ == "__main__":
    main()
