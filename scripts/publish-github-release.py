#!/usr/bin/env python3
"""Prepare an immutable AeroAtlas release locally; publish only with explicit --publish.

Uses the normal GitHub CLI authentication. Never creates a repository, pushes source,
reads authentication tokens or keystores, replaces an asset, or changes visibility.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import quote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
DEDICATED_REPOSITORY = "Lotuzzzz/aeroatlas-updates"
APPLICATION_ID = "com.aeroatlas.personal"
MAX_APK_BYTES = 150 * 1024 * 1024
MAX_MANIFEST_BYTES = 65536
DEFAULT_NOTES = [
    "Corrigido o bloqueio de foco no seletor de aeroporto de entrega: busca, filtros e seleção por toque permanecem interativos durante as atualizações da interface.",
    "Correção também preserva o foco dos campos nos demais modais, sem alterar sua companhia, preços ou progresso."
]

def capture(pattern: str, text: str, label: str) -> str:
    match = re.search(pattern, text, re.MULTILINE)
    if not match:
        raise ValueError(f"Não encontramos {label} nos arquivos do projeto.")
    return match.group(1)


def channel_metadata(repo_argument: str | None = None) -> dict:
    path = ROOT / "release-channel.json"
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 4096:
        raise ValueError("O repositório de atualizações precisa de um release-channel.json regular de até 4 KB.")

    def unique_fields(pairs: list[tuple[str, object]]) -> dict:
        value = dict(pairs)
        if len(value) != len(pairs):
            raise ValueError("release-channel.json contém campos duplicados.")
        return value

    metadata = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_fields)
    expected_keys = {"repo", "appId", "versionName", "versionCode", "manifestUrl"}
    if not isinstance(metadata, dict) or set(metadata) != expected_keys:
        raise ValueError("release-channel.json deve conter somente repo, appId, versionName, versionCode e manifestUrl.")
    for key in expected_keys - {"versionCode"}:
        if not isinstance(metadata[key], str):
            raise ValueError("Os nomes e endereços de release-channel.json precisam ser textos válidos.")
    if metadata["repo"] != DEDICATED_REPOSITORY or metadata["appId"] != APPLICATION_ID:
        raise ValueError("release-channel.json não pertence ao canal dedicado de atualizações do AeroAtlas.")
    if (repo_argument and repo_argument.lower() != DEDICATED_REPOSITORY.lower()) or metadata["manifestUrl"] != f"https://github.com/{DEDICATED_REPOSITORY}/releases/latest/download/latest.json":
        raise ValueError("O repositório e o endereço do canal público de atualizações precisam corresponder exatamente.")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", metadata["versionName"]) or len(metadata["versionName"]) > 80 or type(metadata["versionCode"]) is not int or not 1 <= metadata["versionCode"] <= 2147483647:
        raise ValueError("O canal estável exige versão x.y.z e versionCode inteiro positivo.")
    return metadata


def project_metadata(repo_argument: str | None = None) -> dict:
    source_paths = [ROOT / name for name in ("src/release.ts", "android/app/build.gradle", "capacitor.config.ts", "package.json")]
    present = [path.exists() or path.is_symlink() for path in source_paths]
    if not any(present):
        return channel_metadata(repo_argument)
    if not all(present) or any(path.is_symlink() or not path.is_file() for path in source_paths):
        raise ValueError("Os metadados do código privado estão incompletos; não usaremos o canal público como fallback.")
    release, gradle, capacitor, package_text = [path.read_text(encoding="utf-8") for path in source_paths]
    package = json.loads(package_text)
    name = capture(r"export\s+const\s+APP_VERSION\s*=\s*['\"]([^'\"]+)['\"]\s*;", release, "APP_VERSION")
    code = int(capture(r"export\s+const\s+APP_VERSION_CODE\s*=\s*(\d+)\s*;", release, "APP_VERSION_CODE"))
    feed = capture(r"export\s+const\s+UPDATE_MANIFEST_URL\s*=\s*['\"]([^'\"]+)['\"]\s*;", release, "UPDATE_MANIFEST_URL")
    gradle_name = capture(r"^\s*versionName\s+['\"]([^'\"]+)['\"]\s*$", gradle, "versionName")
    gradle_code = int(capture(r"^\s*versionCode\s+(\d+)\s*$", gradle, "versionCode"))
    app_id = capture(r"^\s*applicationId\s+['\"]([^'\"]+)['\"]\s*$", gradle, "applicationId")
    capacitor_id = capture(r"\bappId\s*:\s*['\"]([^'\"]+)['\"]", capacitor, "appId do Capacitor")
    if name != gradle_name or code != gradle_code or name != package.get("version") or app_id != capacitor_id:
        raise ValueError("Versão ou pacote divergem entre release.ts, package.json, build.gradle e capacitor.config.ts.")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", name) or len(name) > 80 or not 1 <= code <= 2147483647:
        raise ValueError("O canal estável exige versão x.y.z e versionCode inteiro positivo.")
    if app_id != APPLICATION_ID:
        raise ValueError("O identificador Android precisa pertencer ao AeroAtlas.")
    parsed = urlsplit(feed)
    match = re.fullmatch(r"/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/releases/latest/download/latest\.json", parsed.path)
    if parsed.scheme != "https" or parsed.netloc != "github.com" or parsed.query or parsed.fragment or not match:
        raise ValueError("UPDATE_MANIFEST_URL precisa apontar para releases/latest/download/latest.json no github.com.")
    repo = f"{match.group(1)}/{match.group(2)}"
    if repo.lower() != DEDICATED_REPOSITORY.lower() or (repo_argument and repo_argument.lower() != repo.lower()):
        raise ValueError(f"Este publicador só aceita o repositório dedicado {DEDICATED_REPOSITORY}, igual ao canal do app.")
    return {"repo": repo, "appId": app_id, "versionCode": code, "versionName": name, "manifestUrl": feed}


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def find_aapt(explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_file() or not os.access(path, os.X_OK):
            raise ValueError("O executável indicado em --aapt não está disponível.")
        return path
    installed = shutil.which("aapt")
    if installed:
        return Path(installed)
    for value in [os.environ.get("ANDROID_HOME"), os.environ.get("ANDROID_SDK_ROOT"), str(ROOT.parent / ".android-toolchain/sdk")]:
        if not value:
            continue
        candidates = list((Path(value) / "build-tools").glob("*/aapt"))
        candidates.sort(key=lambda path: tuple(int(part) for part in re.findall(r"\d+", path.parent.name)), reverse=True)
        for path in candidates:
            if path.is_file() and os.access(path, os.X_OK):
                return path
    raise ValueError("Instale Android SDK Build Tools ou informe --aapt.")


def inspect_apk(apk: Path, aapt: Path, metadata: dict) -> None:
    result = subprocess.run([str(aapt), "dump", "badging", str(apk)], capture_output=True, text=True, timeout=45, check=False)
    package = re.search(r"^package: name='([^']+)' versionCode='(\d+)' versionName='([^']*)'", result.stdout, re.MULTILINE)
    if result.returncode or not package:
        raise ValueError("O aapt não conseguiu validar o APK final.")
    actual = (package.group(1), int(package.group(2)), package.group(3))
    expected = (metadata["appId"], metadata["versionCode"], metadata["versionName"])
    if actual != expected:
        raise ValueError(f"O APK contém pacote/versão {actual}, mas o projeto exige {expected}. Compile o APK final desta versão.")


def atomic_text(path: Path, text: str) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=".aeroatlas-", delete=False) as file:
            temporary = Path(file.name)
            file.write(text)
        os.replace(temporary, path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def prepare(apk: Path, output: Path, metadata: dict, aapt: Path, notes: list[str]) -> dict:
    apk = apk.expanduser().resolve()
    if not apk.is_file() or not 0 < apk.stat().st_size <= MAX_APK_BYTES:
        raise ValueError("O APK deve ser um arquivo final existente de até 150 MB.")
    if not 1 <= len(notes) <= 40 or any(not isinstance(note, str) or not note.strip() or len(note) > 2000 for note in notes):
        raise ValueError("Informe de 1 a 40 notas não vazias, com até 2.000 caracteres cada.")
    inspect_apk(apk, aapt, metadata)
    size, sha256 = apk.stat().st_size, checksum(apk)
    tag = f"v{metadata['versionName']}"
    output = output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    target = output / f"AeroAtlas-{tag}.apk"
    if target.exists() and (target.stat().st_size != size or checksum(target) != sha256):
        raise ValueError("A saída já contém outro APK desta versão. Use outra pasta de revisão (--out); uma tag já publicada exige nova versão.")
    if not target.exists():
        with tempfile.NamedTemporaryFile(dir=output, prefix=".aeroatlas-", delete=False) as file:
            temporary = Path(file.name)
        try:
            shutil.copyfile(apk, temporary)
            if temporary.stat().st_size != size or checksum(temporary) != sha256:
                raise ValueError("O APK mudou durante a preparação. Aguarde o término da compilação e tente novamente.")
            inspect_apk(temporary, aapt, metadata)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    inspect_apk(target, aapt, metadata)
    manifest = {"schema": 1, "appId": metadata["appId"], "versionCode": metadata["versionCode"], "versionName": metadata["versionName"],
                "apkUrl": f"https://github.com/{metadata['repo']}/releases/download/{tag}/{target.name}",
                "sha256": sha256, "size": size, "notes": notes}
    manifest_path = output / "latest.json"
    old = None
    if manifest_path.is_file():
        try:
            old = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    comparable = {key: value for key, value in old.items() if key != "publishedAt"} if isinstance(old, dict) else None
    if comparable == manifest and isinstance(old.get("publishedAt"), str) and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", old["publishedAt"]):
        manifest["publishedAt"] = old["publishedAt"]
    else:
        manifest["publishedAt"] = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    manifest_text = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    if len(manifest_text.encode("utf-8")) > MAX_MANIFEST_BYTES:
        raise ValueError("O manifesto excede 64 KB, limite aceito pelo aplicativo Android. Reduza as notas.")
    atomic_text(manifest_path, manifest_text)
    body = f"# AeroAtlas {metadata['versionName']}\n\n" + "\n".join(f"- {note}" for note in notes)
    body += f"\n\nAndroid · `{metadata['appId']}` · código `{metadata['versionCode']}`\n\n"
    body += "Atualize sobre a instalação existente, sem desinstalar nem limpar dados. A instalação requer confirmação do Android.\n\n"
    body += f"APK: `{target.name}` · {size} bytes\n\nSHA-256: `{sha256}`\n"
    notes_path = output / "release-notes.md"
    atomic_text(notes_path, body)
    assets = {target.name: target, "latest.json": manifest_path}
    plan = {"repository": metadata["repo"], "tag": tag, "title": f"AeroAtlas {metadata['versionName']}", "manifestUrl": metadata["manifestUrl"],
            "assets": [{"name": name, "size": path.stat().st_size, "sha256": checksum(path)} for name, path in assets.items()],
            "notesFile": str(notes_path), "publication": "Somente --publish envia estes dois assets a um rascunho e publica após verificação."}
    atomic_text(output / "publication-plan.json", json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
    return {"metadata": metadata, "tag": tag, "title": plan["title"], "body": body, "assets": assets, "manifest": manifest, "output": output, "assetMetadata": {name: {"size": path.stat().st_size, "sha256": checksum(path)} for name, path in assets.items()}}


class GitHubCLI:
    def __init__(self) -> None:
        if not shutil.which("gh"):
            raise ValueError("Instale o GitHub CLI e configure sua autenticação normal antes de usar --publish.")

    def api(self, method: str, endpoint: str, payload: dict | None = None, missing_ok: bool = False) -> dict | list | None:
        command = ["gh", "api", "--hostname", "github.com", "--method", method, endpoint]
        if payload is not None:
            command += ["--input", "-"]
        result = subprocess.run(command, input=json.dumps(payload) if payload is not None else None,
                                capture_output=True, text=True, timeout=120, check=False)
        if result.returncode:
            if missing_ok and re.search(r"\bHTTP 404\b", result.stderr):
                return None
            raise ValueError(f"GitHub recusou {method} {endpoint}: {result.stderr.strip()[:1000]}")
        try:
            return json.loads(result.stdout)
        except ValueError as error:
            raise ValueError("O GitHub retornou uma resposta JSON inválida.") from error

    def upload(self, repo: str, tag: str, paths: list[Path]) -> None:
        if not paths:
            return
        result = subprocess.run(["gh", "release", "upload", tag, *map(str, paths), "--repo", f"github.com/{repo}"],
                                capture_output=True, text=True, timeout=600, check=False)
        if result.returncode:
            raise ValueError(f"Upload não concluído; o release permanece em rascunho: {result.stderr.strip()[:1000]}")

    def download(self, repo: str, asset_id: int, path: Path) -> None:
        with path.open("wb") as destination:
            result = subprocess.run(["gh", "api", "--hostname", "github.com", "-H", "Accept: application/octet-stream",
                                     f"repos/{repo}/releases/assets/{asset_id}"], stdout=destination,
                                    stderr=subprocess.PIPE, text=True, timeout=600, check=False)
        if result.returncode:
            raise ValueError(f"Não foi possível verificar o asset remoto: {result.stderr.strip()[:1000]}")


def remote_assets(release: dict, expected: dict[str, Path], *, allow_missing: bool) -> dict[str, dict]:
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise ValueError("O GitHub não retornou a lista de assets do release.")
    result = {}
    for asset in assets:
        name = asset.get("name") if isinstance(asset, dict) else None
        if name not in expected or name in result:
            raise ValueError("O release existente contém assets extras ou duplicados. Nenhum arquivo será alterado.")
        if asset.get("state") != "uploaded" or not isinstance(asset.get("id"), int) or asset["id"] < 1:
            raise ValueError("Um asset remoto ainda não terminou o upload ou possui identificador inválido.")
        path = expected[name]
        if asset.get("size") != path.stat().st_size:
            raise ValueError(f"O asset remoto {name} tem tamanho diferente. Nunca substituímos assets de uma versão existente.")
        digest = asset.get("digest")
        if digest is not None and digest != f"sha256:{checksum(path)}":
            raise ValueError(f"O asset remoto {name} tem SHA-256 diferente. A versão existente é imutável.")
        result[name] = asset
    if not allow_missing and set(result) != set(expected):
        raise ValueError("O release não contém exatamente o APK e latest.json esperados.")
    return result


def verify_assets(gh: GitHubCLI, repo: str, release: dict, expected: dict[str, Path], *, allow_missing: bool = False) -> dict[str, dict]:
    assets = remote_assets(release, expected, allow_missing=allow_missing)
    with tempfile.TemporaryDirectory(prefix="aeroatlas-remote-check-") as directory:
        for name, asset in assets.items():
            downloaded = Path(directory) / name
            gh.download(repo, asset["id"], downloaded)
            if downloaded.stat().st_size != expected[name].stat().st_size or checksum(downloaded) != checksum(expected[name]):
                raise ValueError(f"Os bytes remotos de {name} são diferentes. O release não será publicado nem sobrescrito.")
    return assets


def ensure_newer_than_latest(gh: GitHubCLI, prepared: dict) -> None:
    repo = prepared["metadata"]["repo"]
    latest = gh.api("GET", f"repos/{repo}/releases/latest", missing_ok=True)
    if latest is None:
        return
    if not isinstance(latest, dict) or latest.get("draft") or latest.get("prerelease"):
        raise ValueError("O release latest atual não é um release estável válido.")
    assets = [asset for asset in latest.get("assets", []) if asset.get("name") == "latest.json"]
    if len(assets) != 1 or not isinstance(assets[0].get("id"), int) or assets[0].get("size", 0) > MAX_MANIFEST_BYTES:
        raise ValueError("O release latest atual não contém um manifesto único e verificável.")
    with tempfile.TemporaryDirectory(prefix="aeroatlas-latest-check-") as directory:
        path = Path(directory) / "latest.json"
        gh.download(repo, assets[0]["id"], path)
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            raise ValueError("O manifesto latest é grande demais.")
        manifest = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("appId") != prepared["metadata"]["appId"] or type(manifest.get("versionCode")) is not int:
        raise ValueError("O manifesto latest atual não pertence a este aplicativo ou não tem versionCode válido.")
    if manifest["versionCode"] >= prepared["metadata"]["versionCode"]:
        raise ValueError("A versão latest publicada já tem código igual ou maior. Não publicamos regressões ou códigos duplicados.")


def _publish_verified(prepared: dict, gh: GitHubCLI) -> str:
    repo, tag = prepared["metadata"]["repo"], prepared["tag"]
    if os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("GITHUB_REPOSITORY", "").lower() != DEDICATED_REPOSITORY.lower():
        raise ValueError("A publicação em Actions deve executar no próprio repositório público de atualizações.")
    base = f"repos/{repo}"
    repository = gh.api("GET", base)
    if not isinstance(repository, dict) or repository.get("full_name", "").lower() != repo.lower() or repository.get("private") is not False or repository.get("archived"):
        raise ValueError("O repositório dedicado precisa existir, ser público e não estar arquivado. O script não cria nem altera repositórios.")
    release = gh.api("GET", f"{base}/releases/tags/{quote(tag, safe='')}", missing_ok=True)
    if release is not None and (not isinstance(release, dict) or release.get("tag_name") != tag or not isinstance(release.get("id"), int)):
        raise ValueError("O release existente não corresponde à tag solicitada.")
    if release is not None and not release.get("draft"):
        if release.get("prerelease"):
            raise ValueError("A tag já está publicada como pré-release; o script não modifica releases existentes.")
        verify_assets(gh, repo, release, prepared["assets"])
        return "A versão já está publicada com os mesmos bytes. Nenhuma alteração remota foi feita."
    ensure_newer_than_latest(gh, prepared)
    if release is not None:
        if release.get("name") != prepared["title"] or release.get("body") != prepared["body"] or release.get("prerelease"):
            raise ValueError("Já existe um rascunho diferente nesta tag. Revise-o manualmente; o script não sobrescreve seu conteúdo.")
        existing = verify_assets(gh, repo, release, prepared["assets"], allow_missing=True)
    else:
        branch = repository.get("default_branch")
        if not isinstance(branch, str) or not branch:
            raise ValueError("O repositório precisa de um primeiro commit antes de receber releases.")
        gh.api("GET", f"{base}/git/ref/heads/{quote(branch, safe='')}")
        release = gh.api("POST", f"{base}/releases", {"tag_name": tag, "target_commitish": branch, "name": prepared["title"],
                                                    "body": prepared["body"], "draft": True, "prerelease": False, "generate_release_notes": False})
        if not isinstance(release, dict) or not isinstance(release.get("id"), int) or release.get("tag_name") != tag or release.get("draft") is not True:
            raise ValueError("O GitHub não confirmou a criação do rascunho esperado.")
        existing = remote_assets(release, prepared["assets"], allow_missing=True)
    release_id = release["id"]
    gh.upload(repo, tag, [path for name, path in prepared["assets"].items() if name not in existing])
    uploaded = gh.api("GET", f"{base}/releases/{release_id}")
    if not isinstance(uploaded, dict) or uploaded.get("id") != release_id or uploaded.get("tag_name") != tag or uploaded.get("draft") is not True or uploaded.get("name") != prepared["title"] or uploaded.get("body") != prepared["body"] or uploaded.get("prerelease") is not False:
        raise ValueError("O rascunho mudou durante o upload; não vamos publicar um estado não verificado.")
    verified = verify_assets(gh, repo, uploaded, prepared["assets"])
    ensure_newer_than_latest(gh, prepared)
    reviewed = gh.api("GET", f"{base}/releases/{release_id}")
    if not isinstance(reviewed, dict) or reviewed.get("id") != release_id or reviewed.get("tag_name") != tag or reviewed.get("draft") is not True or reviewed.get("name") != prepared["title"] or reviewed.get("body") != prepared["body"] or reviewed.get("prerelease") is not False:
        raise ValueError("O rascunho foi alterado durante a verificação. Nenhuma publicação será feita.")
    current_assets = remote_assets(reviewed, prepared["assets"], allow_missing=False)
    fingerprint = lambda assets: {name: (asset["id"], asset["size"], asset.get("digest")) for name, asset in assets.items()}
    if fingerprint(current_assets) != fingerprint(verified):
        raise ValueError("Os assets mudaram durante a verificação. Nenhuma publicação será feita.")
    # Run only one publisher for this channel: GitHub offers no compare-and-swap for latest.
    # Make the manifest reachable via /releases/latest only after BOTH assets have been verified.
    published = gh.api("PATCH", f"{base}/releases/{release_id}", {"draft": False, "prerelease": False, "make_latest": "true"})
    if not isinstance(published, dict) or published.get("id") != release_id or published.get("draft") is not False or published.get("tag_name") != tag:
        raise ValueError("O GitHub não confirmou a publicação. Confira o release antes de tentar novamente.")
    latest = gh.api("GET", f"{base}/releases/latest")
    if not isinstance(latest, dict) or latest.get("id") != release_id:
        raise ValueError("O release foi publicado, mas o GitHub ainda não o confirmou como latest. Confira o canal antes de distribuir o link.")
    return f"Publicado: https://github.com/{repo}/releases/tag/{tag}\nCanal: {prepared['metadata']['manifestUrl']}"


def publish(prepared: dict, gh: GitHubCLI) -> str:
    # Upload a private temporary snapshot of the reviewed files. A concurrent build must not
    # change either the bytes checked remotely or the hash already recorded in latest.json.
    with tempfile.TemporaryDirectory(prefix="aeroatlas-publish-") as directory:
        assets = {}
        for name, original in prepared["assets"].items():
            path = Path(directory) / name
            shutil.copyfile(original, path)
            expected = prepared["assetMetadata"][name]
            if path.stat().st_size != expected["size"] or checksum(path) != expected["sha256"]:
                raise ValueError("Os arquivos preparados mudaram após a revisão. Prepare novamente antes de publicar.")
            assets[name] = path
        manifest = prepared["manifest"]
        apk = assets[f"AeroAtlas-{prepared['tag']}.apk"]
        if apk.stat().st_size != manifest["size"] or checksum(apk) != manifest["sha256"] or json.loads(assets["latest.json"].read_text(encoding="utf-8")) != manifest:
            raise ValueError("O manifesto preparado não corresponde ao APK. Nada foi publicado.")
        return _publish_verified({**prepared, "assets": assets}, gh)


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepara o APK e manifesto locais; só --publish envia assets ao GitHub e publica após verificá-los.")
    parser.add_argument("--apk", type=Path, required=True, help="APK final já compilado e assinado. Pacote e versão precisam corresponder ao projeto.")
    parser.add_argument("--out", type=Path, help="Pasta de revisão (padrão: update-channel/github/vVERSÃO). Preserve-a para reexecuções idempotentes.")
    parser.add_argument("--repo", help=f"Confirma o repositório do canal; único destino aceito: {DEDICATED_REPOSITORY}.")
    parser.add_argument("--note", action="append", help="Nota da versão. Repita para várias notas; sem flag usa as notas desta atualização.")
    parser.add_argument("--aapt", help="Executável aapt do Android SDK, se não estiver no PATH/SDK conhecido.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Prepara somente arquivos locais, como o comportamento padrão. Não chama gh.")
    mode.add_argument("--publish", action="store_true", help="Envia somente APK + latest.json e publica um release estável após verificação.")
    args = parser.parse_args()
    try:
        metadata = project_metadata(args.repo)
        output = args.out or ROOT / "update-channel/github" / f"v{metadata['versionName']}"
        prepared = prepare(args.apk, output, metadata, find_aapt(args.aapt), args.note or DEFAULT_NOTES)
        print(f"Arquivos de revisão: {prepared['output']}")
        print(f"APK: {prepared['manifest']['size']} bytes · SHA-256 {prepared['manifest']['sha256']}")
        if args.publish:
            print(publish(prepared, GitHubCLI()))
        else:
            print("Somente preparação local. Nenhum acesso ou publicação no GitHub foi feito.")
            print("Revise latest.json, release-notes.md e publication-plan.json; use --publish somente após revisar o APK final.")
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Erro: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
