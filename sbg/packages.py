from __future__ import annotations

import json
import re
import shutil
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from .errors import PackageError
from .globals import PACKAGE_MANIFEST, SBG_MODULES_DIR

# =============================================================================
# Package manager
# =============================================================================

def is_url(ref: str) -> bool:
    return ref.startswith("http://") or ref.startswith("https://")

def safe_package_name(name: str) -> str:
    name = name.strip().replace(" ", "-")
    if not re.match(r"^[A-Za-z0-9_.-]+$", name):
        raise PackageError(f"invalid package name {name!r}; use letters, digits, _, . or -")
    return name

def read_json_ref(ref: Union[str, Path]) -> Dict[str, Any]:
    ref_s = str(ref)
    try:
        if is_url(ref_s):
            with urllib.request.urlopen(ref_s, timeout=20) as r:  # nosec - user provided package URL
                return json.loads(r.read().decode("utf-8"))
        return json.loads(Path(ref_s).read_text(encoding="utf-8"))
    except Exception as e:
        raise PackageError(f"cannot read JSON from {ref_s!r}: {e}") from e

def write_json(path: Path, data: Dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

def package_manifest_path(root: Union[str, Path]) -> Path:
    return Path(root) / PACKAGE_MANIFEST

def load_project_manifest(root: Union[str, Path]) -> Dict[str, Any]:
    path = package_manifest_path(root)
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            raise PackageError(f"cannot parse {path}: {e}") from e
    else:
        data = {}
    data.setdefault("name", Path(root).resolve().name)
    data.setdefault("dependencies", {})
    return data

def save_project_manifest(root: Union[str, Path], data: Dict[str, Any]) -> None:
    write_json(package_manifest_path(root), data)

def package_init(root: Union[str, Path], name: Optional[str] = None) -> Path:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    data = load_project_manifest(root)
    if name:
        data["name"] = safe_package_name(name)
    data.setdefault("version", "0.1.0")
    data.setdefault("dependencies", {})
    save_project_manifest(root, data)
    (root / SBG_MODULES_DIR).mkdir(exist_ok=True)
    return package_manifest_path(root)

def load_registry_entry(package: str, registry: Optional[str]) -> Tuple[str, Dict[str, Any]]:
    if not registry:
        raise PackageError(f"{package!r} is not a local path/URL. Pass --registry registry.json or install from a file/folder/URL")
    registry_data = read_json_ref(registry)
    packages = registry_data.get("packages", registry_data)
    if package not in packages:
        raise PackageError(f"package {package!r} not found in registry {registry!r}")
    entry = packages[package]
    def absolutize_local_source(source: str) -> str:
        if is_url(source) or is_url(str(registry)) or Path(source).is_absolute():
            return source
        return str((Path(str(registry)).parent / source).resolve())

    if isinstance(entry, str):
        source = absolutize_local_source(entry)
        return source, {"name": package, "source": source}
    if isinstance(entry, dict):
        source = entry.get("source") or entry.get("url") or entry.get("path")
        if not source:
            raise PackageError(f"registry entry for {package!r} has no source/url/path")
        source = absolutize_local_source(str(source))
        meta = dict(entry)
        meta.setdefault("name", package)
        return source, meta
    raise PackageError(f"invalid registry entry for {package!r}")

def find_package_main(directory: Path, meta: Optional[Dict[str, Any]] = None) -> str:
    meta = meta or {}
    if meta.get("main"):
        return str(meta["main"])
    manifest = directory / PACKAGE_MANIFEST
    if manifest.is_file():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            if data.get("main"):
                return str(data["main"])
        except Exception:
            pass
    for candidate in ("main.sbg", "index.sbg"):
        if (directory / candidate).is_file():
            return candidate
    files = sorted(directory.glob("*.sbg"))
    if files:
        return files[0].name
    raise PackageError(f"package directory {directory} contains no .sbg entry file")

def infer_package_name(source: str, explicit_name: Optional[str], meta: Optional[Dict[str, Any]] = None) -> str:
    if explicit_name:
        return safe_package_name(explicit_name)
    if meta and meta.get("name"):
        return safe_package_name(str(meta["name"]))
    if is_url(source):
        tail = source.rstrip("/").split("/")[-1]
        stem = re.sub(r"\.(zip|sbg)$", "", tail, flags=re.I) or "package"
        return safe_package_name(stem)
    return safe_package_name(Path(source).stem if Path(source).is_file() else Path(source).name)

def copy_package_dir(src: Path, dst: Path) -> None:
    def ignore(dirpath: str, names: List[str]) -> set[str]:
        banned = {".git", "__pycache__", SBG_MODULES_DIR}
        return {n for n in names if n in banned or n.endswith(".pyc")}
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=ignore)

def install_from_directory(src: Path, root: Path, package_name: str, meta: Optional[Dict[str, Any]], source_desc: str) -> Dict[str, Any]:
    main = find_package_main(src, meta)
    dst = root / SBG_MODULES_DIR / package_name
    (root / SBG_MODULES_DIR).mkdir(exist_ok=True)
    copy_package_dir(src, dst)
    pkg_manifest = dst / PACKAGE_MANIFEST
    if pkg_manifest.is_file():
        try:
            pkg_meta = json.loads(pkg_manifest.read_text(encoding="utf-8"))
        except Exception:
            pkg_meta = {}
    else:
        pkg_meta = {}
    pkg_meta.setdefault("name", package_name)
    pkg_meta.setdefault("version", (meta or {}).get("version", "0.1.0"))
    pkg_meta["main"] = main
    write_json(pkg_manifest, pkg_meta)
    return {"name": package_name, "main": main, "source": source_desc, "path": str(dst)}

def install_from_file(src: Path, root: Path, package_name: str, meta: Optional[Dict[str, Any]], source_desc: str) -> Dict[str, Any]:
    if src.suffix != ".sbg":
        raise PackageError(f"single-file packages must be .sbg files, got {src}")
    dst = root / SBG_MODULES_DIR / package_name
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst / "main.sbg")
    pkg_meta = {"name": package_name, "version": (meta or {}).get("version", "0.1.0"), "main": "main.sbg"}
    write_json(dst / PACKAGE_MANIFEST, pkg_meta)
    return {"name": package_name, "main": "main.sbg", "source": source_desc, "path": str(dst)}

def download_to_temp(source: str) -> Tuple[tempfile.TemporaryDirectory[str], Path]:
    tmp = tempfile.TemporaryDirectory()
    tail = source.rstrip("/").split("/")[-1] or "package"
    dst = Path(tmp.name) / tail
    try:
        urllib.request.urlretrieve(source, dst)  # nosec - user provided package URL
    except Exception as e:
        tmp.cleanup()
        raise PackageError(f"download failed for {source!r}: {e}") from e
    return tmp, dst

def install_from_source(source: str, *, root: Union[str, Path] = Path.cwd(), name: Optional[str] = None, registry: Optional[str] = None) -> Dict[str, Any]:
    root = Path(root)
    package_init(root)
    meta: Dict[str, Any] = {}
    source_desc = source
    actual_source = source
    path = Path(source)

    if not is_url(source) and not path.exists():
        actual_source, meta = load_registry_entry(source, registry)
        source_desc = source
        path = Path(actual_source)

    package_name = infer_package_name(source if source_desc == source else source_desc, name, meta)

    tmp: Optional[tempfile.TemporaryDirectory[str]] = None
    try:
        if is_url(actual_source):
            tmp, downloaded = download_to_temp(actual_source)
            if zipfile.is_zipfile(downloaded):
                extract_dir = Path(tmp.name) / "extract"
                with zipfile.ZipFile(downloaded) as z:
                    z.extractall(extract_dir)
                children = [p for p in extract_dir.iterdir() if p.name not in ("__MACOSX",)]
                src_dir = children[0] if len(children) == 1 and children[0].is_dir() else extract_dir
                result = install_from_directory(src_dir, root, package_name, meta, actual_source)
            else:
                result = install_from_file(downloaded, root, package_name, meta, actual_source)
        else:
            local = path.resolve()
            if local.is_dir():
                if not meta:
                    manifest = local / PACKAGE_MANIFEST
                    if manifest.is_file():
                        try:
                            meta = json.loads(manifest.read_text(encoding="utf-8"))
                            package_name = infer_package_name(source, name, meta)
                        except Exception:
                            pass
                result = install_from_directory(local, root, package_name, meta, str(local))
            elif local.is_file():
                result = install_from_file(local, root, package_name, meta, str(local))
            else:
                raise PackageError(f"source does not exist: {source}")
    finally:
        if tmp is not None:
            tmp.cleanup()

    manifest = load_project_manifest(root)
    deps = manifest.setdefault("dependencies", {})
    deps[result["name"]] = {
        "source": result["source"],
        "main": result["main"],
        "installedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    save_project_manifest(root, manifest)
    return result

def list_packages(root: Union[str, Path]) -> List[Dict[str, Any]]:
    root = Path(root)
    manifest = load_project_manifest(root)
    modules = root / SBG_MODULES_DIR
    rows: List[Dict[str, Any]] = []
    for name, dep in sorted(manifest.get("dependencies", {}).items()):
        pkg_manifest = modules / name / PACKAGE_MANIFEST
        version = "?"
        main = dep.get("main", "main.sbg") if isinstance(dep, dict) else "main.sbg"
        if pkg_manifest.is_file():
            try:
                data = json.loads(pkg_manifest.read_text(encoding="utf-8"))
                version = str(data.get("version", version))
                main = str(data.get("main", main))
            except Exception:
                pass
        rows.append({"name": name, "version": version, "main": main, "installed": (modules / name).exists()})
    return rows

def remove_package(root: Union[str, Path], name: str) -> None:
    root = Path(root)
    name = safe_package_name(name)
    dst = root / SBG_MODULES_DIR / name
    if dst.exists():
        shutil.rmtree(dst)
    manifest = load_project_manifest(root)
    manifest.setdefault("dependencies", {}).pop(name, None)
    save_project_manifest(root, manifest)
