"""Deterministic source archive, independent of Git and excluding runtime data."""
import argparse
import gzip
import hashlib
from pathlib import Path
import tarfile

FILES = ("Dockerfile", ".dockerignore", "pyproject.toml", "requirements.lock", "alembic.ini",
         "frontend/package.json", "frontend/package-lock.json", "frontend/vite.config.ts",
         "frontend/tsconfig.json", "frontend/index.html", "frontend/admin/index.html")
TREES = ("app", "migrations", "frontend/src", "frontend/public", "deploy/gcp")
SKIP_DIRS = {"__pycache__", ".git", ".venv", "node_modules", "dist", "var", "work",
             "test-results", "playwright-report", ".pytest_cache", ".ruff_cache"}


def selected_files(root):
    root = Path(root).resolve()
    paths = []
    for name in FILES:
        path = root / name
        if not path.is_file() or path.is_symlink():
            raise ValueError("Required source file missing or unsafe: " + name)
        paths.append(path)
    for name in TREES:
        tree = root / name
        if not tree.is_dir() or tree.is_symlink():
            raise ValueError("Required source directory missing or unsafe: " + name)
        for path in tree.rglob("*"):
            relative = path.relative_to(root)
            if any(part in SKIP_DIRS for part in relative.parts):
                continue
            if path.is_symlink():
                raise ValueError("Source archive refuses symlinks: " + relative.as_posix())
            if not path.is_file():
                continue
            # The sole env-shaped file allowed is the public deployment template.
            if (path.name.startswith(".env") or path.name.endswith((".env", ".pyc", ".log", ".eml"))
                    or path.suffix.lower() in {".pem", ".key", ".db", ".sqlite", ".sqlite3"}):
                continue
            paths.append(path)
    return sorted(set(paths), key=lambda path: path.relative_to(root).as_posix())


def create_package(root, output):
    root, output = Path(root).resolve(), Path(output).resolve()
    paths = selected_files(root)
    if output in paths:
        raise ValueError("Archive output must be outside the packaged source trees")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as zipped:
        with tarfile.open(fileobj=zipped, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for path in paths:
                info = archive.gettarinfo(str(path), arcname=path.relative_to(root).as_posix())
                info.uid = info.gid = info.mtime = 0
                info.uname = info.gname = ""
                info.mode = 0o755 if path.suffix == ".sh" else 0o644
                with path.open("rb") as source:
                    archive.addfile(info, source)
    return hashlib.sha256(output.read_bytes()).hexdigest(), len(paths)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    digest, count = create_package(args.root, args.output)
    print(f"{digest}  {args.output.name}\n{count} source files; no Git metadata or runtime settings included.")


if __name__ == "__main__":
    main()
