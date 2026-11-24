#!/usr/bin/env python3
"""p8lib: fetches Prog8 libraries and prints the directories for -srcdirs.

The design is in docs/DESIGN.md.  Standard library only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

MANIFEST = "p8lib.toml"
LOCK = "p8lib.lock"
LOCK_VERSION = 1

NAME_RE = re.compile(r"[a-z][a-z0-9_]*\Z")
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
GITHUB_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+\Z")
COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
TAG_RE = re.compile(r"(?!-)[!-?A-~]+\Z")        # printable, no @, not an option

# ---------------------------------------------------------------------------
# Targets.  A target or family points at the family above it; the CPU is that
# of the target or of the nearest family that has one.  From the target files
# of p8p8 (targets/*/target.properties) and the official compiler's targets.

PARENT = {
    "coco2ram": "coco2", "coco2": "coco",
    "coco3ram": "coco3", "coco3": "coco",
    "dragon64": "dragon32", "dragon32": "dragon",
    "trs80m4": "trs80",
    "vic20plus3": "vic20", "vic20plus8": "vic20", "vic20plus24": "vic20",
    "amiga500": "amiga", "amiga1200": "amiga",
    "llvm16": "llvm", "llvm32": "llvm", "llvm64": "llvm",
}

CPU = {
    "atari": "6502", "c128": "6502", "c64": "6502", "c64os": "6502",
    "mc64": "6502", "pet32": "6502", "plus4": "6502", "sim6502": "6502",
    "vic20": "6502",
    "cx16": "65c02", "cx16os": "65c02", "f256": "65c02", "gametank": "65c02",
    "rp6502": "65c02", "w65c816sxb": "65c02",
    "mega65": "45gs02",
    "coco": "6809", "dragon": "6809", "m6809": "6809", "sim6809": "6809",
    "cpm": "z80", "trs80": "z80", "z80": "z80",
    "amiga": "68000", "qemu68k": "68000",
    "llvm": None, "virtual": None,
}

# A CPU runs the code of the ones it falls back to.
CPU_FALLBACK = {
    "65816": ["65c02", "6502"],
    "45gs02": ["65c02", "6502"],
    "65c02": ["6502"],
    "6502": [], "z80": [], "6809": [], "68000": [],
}

# Modules of the compilers' own libraries.  A module of the same name in
# -srcdirs replaces them without a message, so a library may not use them.
BUILTIN_MODULES = frozenset("""
adpcm ahi arena arexx asllib asyncio audio bcd blitter bmx buffers c64_textio
compression conv conv2 copper coroutines custom cx16logo datatypes diskfont
diskio diskio_mega65 dos easygfx emudbg exec farmem fileio floats gametank
gametank_audio gametank_logo gametank_music gfx_hires gfx_lores gfxtext
graphics highram hwinfo icon iffparse intuition lineclip lists lowlevel math
mobs monogfx os palette petgfx petsnd playfield prog8_lib prog8_math psg
ptplayer serial sorting sound sprites strings syslib test_stack textio timer
trackdisk trs80 utility vera vera_gfx vera_palette vera_psg vera_sprites
vera_text verafx wavfile
""".split())
BUILTIN_PREFIX = "shared_"      # shared_cbm_diskio, shared_sys_functions, ...


class P8Error(Exception):
    """Something the user has to fix; the text goes to standard error."""


def known_target(name: str) -> bool:
    return name in PARENT or name in CPU


def cpu_of(name: str) -> str | None:
    while name is not None:
        if name in CPU:
            return CPU[name]
        name = PARENT.get(name)
    return None


def target_keys(target: str, as_name: str | None = None, cpu: str | None = None) -> list[str]:
    """The keys of [targets] to try for a target, in order."""
    name = target
    if name.endswith(".properties") or "/" in name or "\\" in name:
        name = Path(name.replace("\\", "/")).stem      # a custom target file
    base = as_name or name
    if not known_target(base):
        what = f"'{base}' (given with -as)" if as_name else f"'{base}'"
        raise P8Error(
            f"unknown target {what}.  Say which known target to treat it as with "
            f"-as NAME.  Known: {' '.join(sorted(set(PARENT) | set(CPU)))}")
    keys = [name]
    walk = base
    while walk is not None:
        keys.append(walk)
        walk = PARENT.get(walk)
    if cpu:
        chosen = cpu.lower()
        if chosen not in CPU_FALLBACK:
            raise P8Error(f"unknown CPU '{cpu}'.  Known: {' '.join(sorted(CPU_FALLBACK))}")
    else:
        chosen = cpu_of(base)
    if chosen:
        keys.append(chosen)
        keys.extend(CPU_FALLBACK[chosen])
    keys.append("default")
    return list(dict.fromkeys(keys))


# ---------------------------------------------------------------------------
# Manifests

@dataclass
class Dep:
    name: str
    kind: str                       # "git" or "path"
    url: str = ""
    ref: dict = field(default_factory=dict)     # at most one of tag, branch, rev
    path: str = ""

    def describe(self) -> str:
        if self.kind == "path":
            return f"path {self.path}"
        for kind, value in self.ref.items():
            return f"{self.url} {kind} {value}"
        return f"{self.url} (default branch)"


def norm_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    if url.lower().startswith("https://github.com/"):
        url = url.lower()
    return url


def parse_dep(name: str, value) -> Dep:
    if not NAME_RE.match(name):
        raise P8Error(f"'{name}' is not a library name (lower case letters, digits and _)")
    if isinstance(value, str):
        repo, at, tag = value.partition("@")
        if not GITHUB_RE.match(repo) or (at and not TAG_RE.match(tag)):
            raise P8Error(f"{name} = \"{value}\": expected \"owner/repo\" or \"owner/repo@tag\"")
        return Dep(name, "git", url=f"https://github.com/{repo}", ref={"tag": tag} if at else {})
    if not isinstance(value, dict):
        raise P8Error(f"{name}: expected \"owner/repo\" or a table")
    unknown = set(value) - {"git", "path", "tag", "branch", "rev"}
    if unknown:
        raise P8Error(f"{name}: unknown key {sorted(unknown)[0]}")
    for key, item in value.items():
        if not isinstance(item, str) or not item or any(c.isspace() or ord(c) < 32 for c in item):
            raise P8Error(f"{name}: {key} must be a string without spaces")
        if item.startswith("-"):
            raise P8Error(f"{name}: {key} may not start with '-'")
    refs = {k: value[k] for k in ("tag", "branch", "rev") if k in value}
    if ("git" in value) == ("path" in value):
        raise P8Error(f"{name}: give one of git and path")
    if "path" in value:
        if refs:
            raise P8Error(f"{name}: a path has no tag, branch or rev")
        return Dep(name, "path", path=value["path"])
    if len(refs) > 1:
        raise P8Error(f"{name}: give one of tag, branch and rev")
    if "rev" in refs and not COMMIT_RE.match(refs["rev"]):
        raise P8Error(f"{name}: rev is the full commit, 40 hexadecimal digits")
    return Dep(name, "git", url=value["git"], ref=refs)


class Manifest:
    """A p8lib.toml as it is written.  Only [dependencies] is checked here;
    check_library() does the rest for a library."""

    def __init__(self, path: Path, text: str | None = None):
        self.path = path
        self.dir = path.parent
        try:
            if text is None:
                text = path.read_text(encoding="utf-8")
            data = tomllib.loads(text)
        except FileNotFoundError:
            raise P8Error(f"{path}: not found") from None
        except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
            raise P8Error(f"{path}: {error}") from None
        self.has_targets = "targets" in data
        self.package = self._table(data, "package")
        self.targets = self._table(data, "targets")
        self.merges = self._table(data, "merges")
        self.deps: list[Dep] = []
        for name, value in self._table(data, "dependencies").items():
            try:
                self.deps.append(parse_dep(name, value))
            except P8Error as error:
                raise P8Error(f"{path}: [dependencies] {error}") from None

    def _table(self, data: dict, key: str) -> dict:
        value = data.get(key, {})
        if not isinstance(value, dict):
            raise P8Error(f"{self.path}: [{key}] must be a table")
        return value

    @property
    def name(self):
        return self.package.get("name")

    def merge_names(self) -> list[str]:
        """block.name for everything [merges] lists (after check_library)."""
        return [f"{block}.{name}" for block, names in self.merges.items() for name in names]


def rel_problem(rel) -> str | None:
    """Why a directory of [targets] is not acceptable as written, or None."""
    if not isinstance(rel, str) or not rel:
        return "is not a directory name"
    if "\\" in rel:
        return "has a backslash; write directories with /"
    if rel.startswith("/") or re.match(r"[A-Za-z]:", rel):
        return "is not relative"
    if ".." in rel.split("/"):
        return "has .. in it"
    return None


def contained(root: Path, rel: str) -> Path:
    """The directory rel of a library, which must be inside the library."""
    problem = rel_problem(rel)
    if problem:
        raise P8Error(f"directory '{rel}' {problem}")
    real_root = root.resolve()
    path = (real_root / rel).resolve()
    if path != real_root and real_root not in path.parents:
        raise P8Error(f"directory '{rel}' leads outside the library")
    if not path.is_dir():
        raise P8Error(f"directory '{rel}' does not exist")
    return path


def own_name(name: str, package: str) -> bool:
    return name == package or name.startswith(package + "_")


def check_library(manifest: Manifest, published: bool = False) -> list[str]:
    """The problems of a library: the rules of "Names" and of "What a
    manifest may say" that need no parsing of Prog8.  published: also what
    is wrong only for a library that others fetch."""
    problems = []
    name = manifest.name
    if not isinstance(name, str) or not NAME_RE.match(name):
        return [f"[package] name is missing or not a name (lower case letters, digits and _)"]
    if name in BUILTIN_MODULES or name.startswith(BUILTIN_PREFIX):
        problems.append(f"the name '{name}' is that of a module in a compiler's library")

    dirs = []
    try:
        dirs.append(contained(manifest.dir, "src"))
    except P8Error as error:
        problems.append(f"{error}; a library keeps its sources in src/")
    for key, value in manifest.targets.items():
        if value is False:
            continue
        if not isinstance(value, list):
            problems.append(f"[targets] {key} must be a list of directories or false")
            continue
        for rel in value:
            try:
                dirs.append(contained(manifest.dir, rel))
            except P8Error as error:
                problems.append(f"[targets] {key}: {error}")

    for directory in dict.fromkeys(dirs):
        for source in sorted(directory.glob("*.p8")):
            shown = source.relative_to(manifest.dir.resolve()).as_posix()
            if not own_name(source.stem, name):
                problems.append(f"{shown}: a file of '{name}' is named {name}.p8 or {name}_....p8")
            elif source.stem in BUILTIN_MODULES:
                problems.append(f"{shown}: '{source.stem}' is a module in a compiler's library")

    for block, names in manifest.merges.items():
        if not IDENT_RE.match(block):
            problems.append(f"[merges] '{block}' is not a block name")
        elif own_name(block, name):
            problems.append(f"[merges] '{block}' is the library's own block; list only blocks of others")
        if not isinstance(names, list) or not all(isinstance(n, str) and IDENT_RE.match(n) for n in names):
            problems.append(f"[merges] {block} must be a list of names")
            manifest.merges[block] = []

    if published:
        for dep in manifest.deps:
            if dep.kind == "path":
                problems.append(f"[dependencies] {dep.name}: a path works only on this machine")
            elif not dep.url.startswith("https://"):
                problems.append(f"[dependencies] {dep.name}: only https:// URLs are taken from a library")
    return problems


# ---------------------------------------------------------------------------
# The store and git

def store_dir() -> Path:
    override = os.environ.get("P8LIB_HOME")
    if override:
        return Path(override)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        return (Path(base) if base else Path.home() / "AppData" / "Local") / "p8lib"
    base = os.environ.get("XDG_DATA_HOME")
    return (Path(base) if base else Path.home() / ".local" / "share") / "p8lib"


def clone_dir(url: str, commit: str) -> Path:
    normal = norm_url(url)
    slug = re.sub(r"[^A-Za-z0-9._-]", "_", normal.rsplit("/", 1)[-1])[:40] or "repo"
    digest = hashlib.sha256(normal.encode()).hexdigest()[:12]
    return store_dir() / "clones" / f"{slug}-{digest}" / commit


def git(*args, check: bool = True) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(["git", *map(str, args)], capture_output=True, text=True)
    except FileNotFoundError:
        raise P8Error("git is not installed or not on the PATH") from None
    if check and result.returncode:
        raise P8Error(f"git {args[0] if args[0] != '-C' else args[2]} failed: {result.stderr.strip()}")
    return result


def remote_commit(dep: Dep) -> str:
    """The commit a dependency's tag, branch or default branch is at now."""
    if "rev" in dep.ref:
        return dep.ref["rev"]
    if "tag" in dep.ref:
        tag = dep.ref["tag"]
        patterns, what = [f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"], f"tag '{tag}'"
    elif "branch" in dep.ref:
        patterns, what = [f"refs/heads/{dep.ref['branch']}"], f"branch '{dep.ref['branch']}'"
    else:
        patterns, what = ["HEAD"], "default branch"
    output = git("ls-remote", "--", dep.url, *patterns).stdout
    found = dict(reversed(line.split("\t", 1)) for line in output.splitlines() if "\t" in line)
    for pattern in reversed(patterns):          # the commit a tag points at, before the tag
        if COMMIT_RE.match(found.get(pattern, "")):
            return found[pattern]
    raise P8Error(f"no {what} in {dep.url}")


def fetch(url: str, commit: str, dest: Path) -> None:
    """Puts the sources of one commit at dest.  The clone is made beside it
    and renamed, so that dest is complete from the moment it exists."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".tmp-", dir=dest.parent))
    try:
        git("init", "-q", tmp)
        shallow = git("-C", tmp, "fetch", "-q", "--depth", "1", "--no-tags", "--", url, commit, check=False)
        if shallow.returncode:                  # a server that gives no single commit
            git("-C", tmp, "fetch", "-q", "--tags", "--", url, "+refs/heads/*:refs/remotes/origin/*")
        git("-C", tmp, "-c", "advice.detachedHead=false", "checkout", "-q", "--detach", commit)
        try:
            tmp.rename(dest)
        except OSError:
            if not dest.is_dir():               # else: another p8lib got there first
                raise
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Resolving a program's libraries

@dataclass
class Lib:
    name: str
    dir: Path
    manifest: Manifest
    dep: Dep
    commit: str | None              # None for a path
    wanted_by: list = field(default_factory=list)   # (who, Dep)


EVERYTHING = object()


def find_manifest(given: str | None) -> Path:
    if given:
        path = Path(given)
        return (path / MANIFEST if path.is_dir() else path).resolve()
    here = Path.cwd().resolve()
    for directory in (here, *here.parents):
        if (directory / MANIFEST).is_file():
            return directory / MANIFEST
    raise P8Error(f"no {MANIFEST} in this directory or above it")


def read_lock(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        libraries = data["libraries"]
        if not isinstance(libraries, dict):
            raise ValueError("libraries is not a table")
        return libraries
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise P8Error(f"{path}: not a lock file ({error}); remove it and run \"p8lib install\"") from None


def write_lock(path: Path, libs: dict[str, Lib]) -> None:
    libraries = {
        lib.name: {"git": lib.dep.url, "requested": lib.dep.ref, "commit": lib.commit}
        for lib in libs.values() if lib.commit}
    if not libraries and not path.exists():
        return
    text = json.dumps({"version": LOCK_VERSION, "libraries": libraries}, indent=2, sort_keys=True) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return
    path.write_text(text, encoding="utf-8")


def resolve(root: Manifest, lock: dict, online: bool, relock=None) -> dict[str, Lib]:
    """The libraries of a program, its own first and theirs after them.

    online: may ask the network; without it, everything must be in the lock
    and in the store.  relock: the names whose lock entry is not used, or
    EVERYTHING."""
    advice = 'run "p8lib install"'

    def commit_for(dep: Dep) -> str:
        entry = lock.get(dep.name)
        stale = relock is EVERYTHING or (relock and dep.name in relock)
        if (isinstance(entry, dict) and not stale
                and norm_url(str(entry.get("git"))) == norm_url(dep.url)
                and entry.get("requested") == dep.ref
                and COMMIT_RE.match(str(entry.get("commit")))):
            return entry["commit"]
        if "rev" in dep.ref:
            return dep.ref["rev"]
        if not online:
            raise P8Error(f"'{dep.name}' is not in {LOCK} as {root.path.name} wants it; {advice}")
        return remote_commit(dep)

    libs: dict[str, Lib] = {}
    queue = [(dep, "the program") for dep in root.deps]
    while queue:
        dep, who = queue.pop(0)
        from_program = who == "the program"
        have = libs.get(dep.name)
        if have:
            have.wanted_by.append((who, dep))
            if have.dep.kind == "path":
                continue                        # the program's path stands for every request
            same = dep.kind == "git" and norm_url(dep.url) == norm_url(have.dep.url)
            if same and online:
                same = commit_for(dep) == have.commit
            if not same:
                lines = [f"  {by} wants {wanted.describe()}" for by, wanted in have.wanted_by]
                raise P8Error(f"'{dep.name}' is wanted in two ways that do not agree:\n" + "\n".join(lines))
            continue

        if dep.kind == "path":
            if not from_program:
                raise P8Error(f"{who} wants '{dep.name}' from a path; only the program's own {MANIFEST} may")
            directory = (root.dir / dep.path).resolve()
            if not directory.is_dir():
                raise P8Error(f"'{dep.name}': {directory} is not a directory")
            commit = None
        else:
            if not from_program and not dep.url.startswith("https://"):
                raise P8Error(
                    f"{who} wants '{dep.name}' from {dep.url}.  Only https:// URLs are taken from a "
                    f"library; list '{dep.name}' with this URL in the program's {MANIFEST} to allow it")
            commit = commit_for(dep)
            directory = clone_dir(dep.url, commit)
            if not directory.is_dir():
                if not online:
                    raise P8Error(f"'{dep.name}' is not installed; {advice}")
                print(f"p8lib: fetching {dep.name} ({dep.url} at {commit[:10]})", file=sys.stderr)
                fetch(dep.url, commit, directory)

        if not (directory / MANIFEST).is_file():
            raise P8Error(f"'{dep.name}': there is no {MANIFEST} in {dep.describe()}")
        manifest = Manifest(directory / MANIFEST)
        if manifest.name != dep.name:
            raise P8Error(
                f"{who} lists '{dep.name}', and the library at {dep.describe()} calls itself "
                f"'{manifest.name}'.  The key is the library's name")
        problems = check_library(manifest)
        if problems:
            raise P8Error(f"'{dep.name}' ({dep.describe()}) cannot be used:\n" + "\n".join(f"  {p}" for p in problems))
        libs[dep.name] = Lib(dep.name, directory, manifest, dep, commit, [(who, dep)])
        queue.extend((sub, f"'{dep.name}'") for sub in manifest.deps)

    merged: dict[str, str] = {}
    for lib in libs.values():
        for item in lib.manifest.merge_names():
            if item in merged:
                raise P8Error(
                    f"'{merged[item]}' and '{lib.name}' both define {item}.  The compiler would take "
                    f"the one imported last; drop one of the two libraries")
            merged[item] = lib.name
    return libs


def dirs_for(lib: Lib, keys: list[str], target: str) -> list[Path]:
    rels = []
    if lib.manifest.has_targets:
        for key in keys:
            if key in lib.manifest.targets:
                rels = lib.manifest.targets[key]
                if rels is False:
                    raise P8Error(f"'{lib.name}' is not for {key} (target {target})")
                break
        else:
            raise P8Error(f"'{lib.name}' has nothing for target {target} (tried: {', '.join(keys)})")
    try:
        return [contained(lib.dir, rel) for rel in ("src", *rels)]
    except P8Error as error:
        raise P8Error(f"'{lib.name}': {error}") from None


# ---------------------------------------------------------------------------
# Commands

def load(args) -> tuple[Manifest, Path, dict]:
    manifest = Manifest(find_manifest(args.manifest))
    lock_path = manifest.dir / LOCK
    return manifest, lock_path, read_lock(lock_path)


def dependency_line(name: str, spec: str, ref: dict) -> tuple[str, Dep]:
    """The line of [dependencies] for a library given on the command line."""
    repo, at, tag = spec.partition("@")
    github = bool(GITHUB_RE.match(repo))
    if github and at:
        if ref:
            raise P8Error(f"'{spec}' names a tag already; leave out -{next(iter(ref))}")
        ref = {"tag": tag}
    if github and set(ref) <= {"tag"}:
        value = repo + (f"@{ref['tag']}" if ref else "")
        written = json.dumps(value)
    else:
        url = f"https://github.com/{repo}" if github else spec
        if "://" not in url:
            raise P8Error(f"'{spec}' is not owner/repo and not a URL")
        value = {"git": url, **ref}
        written = "{ " + ", ".join(f"{k} = {json.dumps(v)}" for k, v in value.items()) + " }"
    return f"{name} = {written}", parse_dep(name, value)


def with_dependency(text: str, line: str) -> str:
    """The text of a manifest with one more line in [dependencies]."""
    lines = text.splitlines()
    header = re.compile(r"\s*\[")
    start = next((i for i, l in enumerate(lines) if re.fullmatch(r"\s*\[dependencies\]\s*(#.*)?", l)), None)
    if start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines += ["[dependencies]", line]
    else:
        end = next((i for i in range(start + 1, len(lines)) if header.match(lines[i])), len(lines))
        while end > start + 1 and not lines[end - 1].strip():
            end -= 1                            # before the blank lines that close the table
        lines.insert(end, line)
    return "\n".join(lines) + "\n"


def add_dependency(args) -> tuple[Manifest, str]:
    """install with a library named: the manifest as it will be, and its
    text.  Nothing is written here."""
    ref = {k: v for k in ("tag", "branch", "rev") if (v := getattr(args, k))}
    try:
        path = find_manifest(args.manifest)
    except P8Error:
        path = Path.cwd().resolve() / MANIFEST
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    before = Manifest(path, text)

    # the name is the library's own, so the library is fetched to ask it
    _, probe = dependency_line("x", args.library, ref)
    commit = remote_commit(probe)
    directory = clone_dir(probe.url, commit)
    if not directory.is_dir():
        print(f"p8lib: fetching {probe.url} at {commit[:10]}", file=sys.stderr)
        fetch(probe.url, commit, directory)
    if not (directory / MANIFEST).is_file():
        raise P8Error(f"there is no {MANIFEST} in {probe.describe()}; it is not a p8lib library (yet)")
    name = Manifest(directory / MANIFEST).name
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise P8Error(f"the {MANIFEST} in {probe.describe()} has no usable [package] name")
    if any(dep.name == name for dep in before.deps):
        raise P8Error(f"'{name}' is in {path} already; edit it there, or run \"p8lib update {name}\"")

    line, dep = dependency_line(name, args.library, ref)
    new_text = with_dependency(text, line)
    try:
        after = Manifest(path, new_text)
    except P8Error:
        after = None
    if after is None or after.deps != before.deps + [dep]:      # a manifest this cannot add a line to
        raise P8Error(f"cannot add to [dependencies] of {path}; add this line by hand:\n  {line}")
    print(f"p8lib: {path.name}: {line}", file=sys.stderr)
    return after, new_text


def cmd_install(args) -> None:
    if not args.library:
        if args.tag or args.branch or args.rev:
            raise P8Error("-tag, -branch and -rev go with a library: p8lib install owner/repo -tag v1.0")
        manifest, lock_path, lock = load(args)
        new_text = None
    else:
        manifest, new_text = add_dependency(args)
        lock_path = manifest.dir / LOCK
        lock = read_lock(lock_path)
    libs = resolve(manifest, lock, online=True)
    if new_text is not None:                    # only now: the program still builds if this failed
        manifest.path.write_text(new_text, encoding="utf-8")
    write_lock(lock_path, libs)
    print(f"p8lib: {len(libs)} librar{'y' if len(libs) == 1 else 'ies'} ready", file=sys.stderr)


def prog8_of(manifest: Manifest) -> str:
    """The version of the language a library says it is written for."""
    value = manifest.package.get("prog8", "")
    return value if isinstance(value, str) else str(value)


def prog8_of_clone(entry: dict) -> str:
    """The same for the commit a lock entry names, when its clone is in the store."""
    try:
        return prog8_of(Manifest(clone_dir(str(entry["git"]), str(entry["commit"])) / MANIFEST))
    except (KeyError, P8Error):
        return ""


def cmd_update(args) -> None:
    manifest, lock_path, lock = load(args)
    for name in args.names:
        if name not in lock:
            raise P8Error(f"'{name}' is not in {LOCK}")
    libs = resolve(manifest, lock, online=True, relock=set(args.names) or EVERYTHING)
    write_lock(lock_path, libs)
    for lib in libs.values():
        entry = lock.get(lib.name, {})
        before = entry.get("commit")
        if lib.commit and before != lib.commit:
            print(f"p8lib: {lib.name} {(before or 'new')[:10]} -> {lib.commit[:10]}", file=sys.stderr)
            was, now = prog8_of_clone(entry), prog8_of(lib.manifest)
            if was != now and (was or now):
                print(f"p8lib: {lib.name} was written for Prog8 {was or '(not said)'}, "
                      f"and is now for Prog8 {now or '(not said)'}", file=sys.stderr)


def cmd_list(args) -> None:
    manifest, _, lock = load(args)
    libs = resolve(manifest, lock, online=False)
    width = max((len(name) for name in libs), default=0)
    versions = {name: f"prog8 {prog8_of(lib.manifest)}" if prog8_of(lib.manifest) else "" for name, lib in libs.items()}
    vwidth = max((len(v) for v in versions.values()), default=0)
    for lib in libs.values():
        where = lib.commit[:10] if lib.commit else "path"
        print(f"{lib.name.ljust(width)}  {where.ljust(10)}  {versions[lib.name].ljust(vwidth)}  "
              f"{lib.manifest.package.get('description', '')}".rstrip())
        if lib.manifest.merge_names():
            print(f"{''.ljust(width)}  merges: {', '.join(lib.manifest.merge_names())}")


def cmd_srcdirs(args) -> None:
    manifest, _, lock = load(args)
    keys = target_keys(args.target, args.as_name, args.cpu)
    libs = resolve(manifest, lock, online=False)
    dirs: list[Path] = []
    for lib in libs.values():
        dirs.extend(dirs_for(lib, keys, args.target))
    dirs = list(dict.fromkeys(dirs))
    modules: dict[str, Path] = {}
    for directory in dirs:
        if os.pathsep in str(directory):
            raise P8Error(f"{directory} has a '{os.pathsep}' in it and cannot be in -srcdirs")
        for source in sorted(directory.glob("*.p8")):
            if source.stem in modules:
                raise P8Error(
                    f"the module '{source.stem}' is there twice, and the compiler would take the "
                    f"first without a message:\n  {modules[source.stem]}\n  {source}")
            modules[source.stem] = source
    print(os.pathsep.join(map(str, dirs)))


def cmd_check(args) -> None:
    manifest = Manifest(find_manifest(args.manifest))
    if not manifest.package:
        raise P8Error(f"{manifest.path} has no [package]; \"p8lib check\" is for a library")
    problems = check_library(manifest, published=True)
    known_keys = set(PARENT) | set(CPU) | set(CPU_FALLBACK) | {"default"}
    for key in manifest.targets:
        if key not in known_keys:
            print(f"p8lib: note: [targets] {key} is not a target, family or CPU that p8lib knows", file=sys.stderr)
    if problems:
        raise P8Error(f"{manifest.path}:\n" + "\n".join(f"  {p}" for p in problems))
    print(f"p8lib: '{manifest.name}' follows the rules", file=sys.stderr)


def guess_name(directory: Path) -> str:
    """A library's name from what is there: the one module in src/, or the
    directory's name without a prog8 in front."""
    sources = [p.stem for p in (directory / "src").glob("*.p8")]
    if len(sources) == 1 and NAME_RE.match(sources[0]):
        return sources[0]
    name = re.sub(r"^(prog8|p8)[-_]?(?=.)", "", directory.name.lower())
    name = re.sub(r"[^a-z0-9_]", "_", name)
    if not NAME_RE.match(name):
        raise P8Error("cannot make a name from this directory; give one: p8lib init NAME")
    return name


def cmd_init(args) -> None:
    directory = Path.cwd().resolve()
    path = directory / MANIFEST
    if path.exists():
        raise P8Error(f"there is a {MANIFEST} here already")
    name = args.name or guess_name(directory)
    if not NAME_RE.match(name):
        raise P8Error(f"'{name}' is not a library name (lower case letters, digits and _)")
    lines = [
        "[package]",
        f'name = "{name}"',
        'description = ""',
        'license = ""',
        '# prog8 = "12"            # the oldest version of the language this is written for',
    ]
    src = directory / "src"
    targets = sorted(d.name for d in src.iterdir() if d.is_dir() and any(d.glob("*.p8"))) if src.is_dir() else []
    if targets:
        width = max(len(t) for t in targets)
        lines += ["", "[targets]"]
        for target in targets:
            key = target if re.fullmatch(r"[A-Za-z0-9_-]+", target) else json.dumps(target)
            lines.append(f'{key.ljust(width)} = ["src/{target}"]')
    lines += ["", "[dependencies]"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"p8lib: wrote {path}", file=sys.stderr)

    todo = check_library(Manifest(path), published=True)
    known_keys = set(PARENT) | set(CPU) | set(CPU_FALLBACK) | {"default"}
    todo += [f"[targets] {t} is not a target, family or CPU that p8lib knows" for t in targets if t not in known_keys]
    todo += ["fill in description and license"]
    if targets:
        todo.append('other targets are refused; add default = [] if src/ alone is enough for them')
    print("p8lib: to do, then run \"p8lib check\":\n" + "\n".join(f"  {t}" for t in todo), file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="p8lib", description="Prog8 library manager", allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")

    def command(name, function, text):
        sub = commands.add_parser(name, help=text, description=text, allow_abbrev=False)
        sub.add_argument("-manifest", "--manifest", metavar="PATH",
                         help=f"the {MANIFEST} to use, or its directory (default: found from the current directory up)")
        sub.set_defaults(function=function)
        return sub

    install = command("install", cmd_install,
                      f"fetch the libraries of {MANIFEST} and bring {LOCK} up to date; "
                      f"with a library, add it to {MANIFEST} first")
    install.add_argument("library", nargs="?", metavar="owner/repo",
                         help=f"a library to add to {MANIFEST}: a GitHub repository or a git URL")
    for ref in ("tag", "branch", "rev"):
        install.add_argument(f"-{ref}", f"--{ref}", metavar=ref.upper(), help=f"the {ref} of the library to add")
    update = command("update", cmd_update, "move the lock to the newest commits the manifest allows")
    update.add_argument("names", nargs="*", metavar="name", help="only these libraries")
    command("list", cmd_list, "list the libraries of the project")
    srcdirs = command("srcdirs", cmd_srcdirs, "print the directories for the compiler's -srcdirs")
    srcdirs.add_argument("-target", "--target", required=True, metavar="NAME", help="the compiler's target")
    srcdirs.add_argument("-as", "--as", dest="as_name", metavar="NAME",
                         help="the known target to treat an unknown one as")
    srcdirs.add_argument("-cpu", "--cpu", metavar="NAME", help="the CPU, when it is not the target's usual one")
    command("check", cmd_check, "check the library in the current directory against the rules")
    text = f"write a first {MANIFEST} for the library in the current directory"
    init = commands.add_parser("init", help=text, description=text, allow_abbrev=False)
    init.add_argument("name", nargs="?", help="the library's name (default: from src/ or the directory)")
    init.set_defaults(function=cmd_init)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.function(args)
    except P8Error as error:
        print(f"p8lib: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
