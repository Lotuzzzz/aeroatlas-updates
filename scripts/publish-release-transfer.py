#!/usr/bin/env python3
"""Read a reviewed two-blob Git transfer and reuse the normal release publisher.

The default only fetches immutable Git objects and prepares local review files.
Only --publish constructs a GitHub client; the existing publisher still requires
the dedicated repository to be public before uploading or publishing anything.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

_spec = importlib.util.spec_from_file_location("aeroatlas_transfer_publisher", Path(__file__).with_name("publish-github-release.py"))
publisher = importlib.util.module_from_spec(_spec)
assert _spec.loader
_spec.loader.exec_module(publisher)


def checked_identifiers(commit: str, apk_sha256: str, manifest_sha256: str) -> tuple[str, str, str]:
    for value, length, label in ((commit, 40, "commit"), (apk_sha256, 64, "SHA-256 do APK"),
                                  (manifest_sha256, 64, "SHA-256 do manifesto")):
        if not isinstance(value, str) or not re.fullmatch(rf"[0-9a-fA-F]{{{length}}}", value):
            raise ValueError(f"Informe {label} completo, com exatamente {length} caracteres hexadecimais.")
    return commit.lower(), apk_sha256.lower(), manifest_sha256.lower()


def git(*arguments: str) -> bytes:
    result = subprocess.run(["git", "--no-replace-objects", "-C", str(publisher.ROOT), *arguments],
                            capture_output=True, timeout=120, check=False)
    if result.returncode:
        # Remote configuration can contain credentials; never echo Git stderr.
        raise ValueError(f"Não foi possível concluir git {arguments[0]} da transferência.")
    return result.stdout


def transfer_blobs(commit: str, apk_name: str) -> dict[str, tuple[str, int]]:
    git("fetch", "--no-tags", "origin", commit)
    if git("cat-file", "-t", commit).strip() != b"commit":
        raise ValueError("O identificador da transferência precisa apontar para um commit Git.")
    entries = {}
    for entry in git("ls-tree", "-z", "--full-tree", commit).split(b"\0"):
        if not entry:
            continue
        try:
            header, raw_name = entry.split(b"\t", 1)
            mode, kind, oid = header.decode("ascii").split()
            name = raw_name.decode("utf-8")
        except (ValueError, UnicodeDecodeError) as error:
            raise ValueError("A árvore da transferência é inválida.") from error
        if mode not in ("100644", "100755") or kind != "blob" or not re.fullmatch(r"[0-9a-f]{40}", oid):
            raise ValueError("A transferência aceita somente arquivos regulares, sem diretórios, symlinks ou gitlinks.")
        if name in entries:
            raise ValueError("A transferência contém nomes duplicados.")
        entries[name] = oid
    if set(entries) != {apk_name, "latest.json"}:
        raise ValueError("A árvore precisa conter exatamente o APK esperado e latest.json na raiz, sem arquivos extras.")
    result = {}
    for name, oid in entries.items():
        size = int(git("cat-file", "-s", oid).strip())
        maximum = publisher.MAX_MANIFEST_BYTES if name == "latest.json" else publisher.MAX_APK_BYTES
        if not 0 < size <= maximum:
            raise ValueError(f"O blob {name} está vazio ou excede o tamanho permitido.")
        result[name] = (oid, size)
    return result


def extract_blob(oid: str, size: int, expected_hash: str, destination: Path) -> None:
    digest = hashlib.sha256()
    count = 0
    with destination.open("xb") as output, tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(["git", "--no-replace-objects", "-C", str(publisher.ROOT), "cat-file", "blob", oid],
                                   stdout=subprocess.PIPE, stderr=errors)
        try:
            assert process.stdout
            while chunk := process.stdout.read(65536):
                count += len(chunk)
                if count > size:
                    raise ValueError("O blob mudou de tamanho durante a extração.")
                digest.update(chunk)
                output.write(chunk)
            if process.wait(timeout=30) or count != size:
                raise ValueError("Não foi possível extrair o blob completo da transferência.")
        finally:
            if process.stdout:
                process.stdout.close()
            if process.poll() is None:
                process.kill()
                process.wait()
    if digest.hexdigest() != expected_hash:
        raise ValueError(f"O SHA-256 de {destination.name} difere do arquivo revisado.")


def prepare_transfer(commit: str, apk_sha256: str, manifest_sha256: str, output: Path) -> dict:
    commit, apk_sha256, manifest_sha256 = checked_identifiers(commit, apk_sha256, manifest_sha256)
    metadata = publisher.project_metadata()
    output = output.expanduser().resolve()
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError("A pasta de saída precisa estar ausente ou vazia; arquivos anteriores não serão substituídos.")
    apk_name = f"AeroAtlas-v{metadata['versionName']}.apk"
    blobs = transfer_blobs(commit, apk_name)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".aeroatlas-transfer-", dir=output.parent) as directory:
        staging = Path(directory)
        for name, expected_hash in ((apk_name, apk_sha256), ("latest.json", manifest_sha256)):
            oid, size = blobs[name]
            extract_blob(oid, size, expected_hash, staging / name)
        original = (staging / "latest.json").read_bytes()
        manifest = json.loads(original)
        expected = {"schema": 1, "appId": metadata["appId"], "versionCode": metadata["versionCode"],
                    "versionName": metadata["versionName"], "sha256": apk_sha256, "size": blobs[apk_name][1],
                    "apkUrl": f"https://github.com/{metadata['repo']}/releases/download/v{metadata['versionName']}/{apk_name}"}
        if not isinstance(manifest, dict) or any(type(manifest.get(key)) is not type(value) or manifest.get(key) != value
                                                 for key, value in expected.items()):
            raise ValueError("O manifesto transferido diverge do pacote, versão, endereço, tamanho ou SHA-256 revisados.")
        if not isinstance(manifest.get("notes"), list):
            raise ValueError("As notas do manifesto revisado precisam ser uma lista.")
        try:
            datetime.strptime(manifest["publishedAt"], "%Y-%m-%dT%H:%M:%SZ")
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("A data do manifesto revisado é inválida.") from error
        review = staging / "review"
        review.mkdir()
        # prepare() preserves this timestamp only if all reviewed metadata agrees.
        (review / "latest.json").write_bytes(original)
        prepared = publisher.prepare(staging / apk_name, review, metadata, publisher.find_aapt(None), manifest.get("notes"))
        if publisher.checksum(review / "latest.json") != manifest_sha256 or (review / "latest.json").read_bytes() != original:
            raise ValueError("A preparação alterou os bytes do manifesto revisado; nada será publicado.")
        if publisher.checksum(review / apk_name) != apk_sha256:
            raise ValueError("A preparação alterou os bytes do APK revisado; nada será publicado.")
        # Paths in the review plan must remain usable after the private staging is removed.
        plan_path = review / "publication-plan.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["notesFile"] = str(output / "release-notes.md")
        publisher.atomic_text(plan_path, json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
        os.replace(review, output)
    return {**prepared, "output": output, "assets": {name: output / name for name in prepared["assets"]}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", required=True, help="SHA completo do commit imutável com os dois blobs.")
    parser.add_argument("--apk-sha256", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--publish", action="store_true", help="Usa o publicador existente, que exige repositório público.")
    args = parser.parse_args()
    try:
        prepared = prepare_transfer(args.commit, args.apk_sha256, args.manifest_sha256, args.out)
        print(f"Transferência verificada. Arquivos de revisão: {prepared['output']}")
        if args.publish:
            print(publisher.publish(prepared, publisher.GitHubCLI()))
        else:
            print("Somente preparação local. Nenhum asset ou release foi enviado ao GitHub.")
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Erro: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
