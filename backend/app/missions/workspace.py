"""What a mission may touch: folders the user approved, and inside them no secrets. The same rules
apply to the dashboard, the voice and the model, because all of them come through here."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", "dist", "build", ".idea", ".vscode"}
WATCH_SUFFIXES = {".py", ".toml", ".cfg", ".ini"}  # what starts a test run: never files a test might write
READ_SUFFIXES = WATCH_SUFFIXES | {".json", ".yaml", ".yml", ".txt", ".md", ".rst", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".sh", ".sql"}
SECRET_NAME = re.compile(r"(^|/)(\.env(\..*)?|.*\.(pem|key|p12|pfx)|id_(rsa|ed25519|ecdsa).*|.*(secret|credential|password|token).*|\.netrc|\.npmrc|\.pypirc)$", re.I)
MAX_READ_BYTES = 200_000
MAX_WATCHED_FILES = 4000
_SECRETS = [
    re.compile(r"\b(sk|pk|rk)-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{12,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"(?i)\b([A-Z0-9_]*(?:API_?KEY|SECRET|TOKEN|PASSWORD|PASSWD)[A-Z0-9_]*)\s*[:=]\s*[\"']?[^\s\"',]{6,}"),
]


class PolicyError(ValueError):
    """A request outside what the user approved. The message is safe to show and to say."""


def redact(text: str) -> str:
    out = text or ""
    for pattern in _SECRETS:
        out = pattern.sub(lambda m: f"{m.group(1)}=[redacted]" if m.lastindex else "[redacted]", out)
    home = str(Path.home())
    return out.replace(home, "~")


def is_test_file(rel: str) -> bool:
    parts = rel.split("/")
    name = parts[-1]
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py" or "tests" in parts[:-1] or "test" in parts[:-1])


@dataclass(frozen=True)
class FileMention:
    """A file a person's words name. Only says which file they meant: Workspace.resolve decides if it may be read."""

    path: str
    said: str
    corrected: bool = False  # no such file, but a project file differs from it the way a misheard word does


# What a recogniser writes for a spoken file name, turned back into the name. Order matters.
_SPOKEN_PATH = (
    (re.compile(r"\bdot (env|netrc|npmrc|pypirc|ssh|git)\b", re.I), r".\1"),
    (re.compile(r"\bdot dot slash\s*", re.I), "../"),
    (re.compile(r"\b(read|open|show me|show|view|cat|at|in|from|file)\s+slash\s*", re.I), r"\1 /"),
    (re.compile(r"\s*\bslash\b\s*", re.I), "/"),
    (re.compile(r"\s*\bunderscore\b\s*", re.I), "_"),
    (re.compile(r"(?<=\w)\s+dot\s+(?=\w)", re.I), "."),
    (re.compile(r"\.(?:pie|pi|p y)\b", re.I), ".py"),
)
_EXTENSION = re.compile(r"\.[A-Za-z][A-Za-z0-9]{0,7}$")
_DOTFILE = re.compile(r"\.[A-Za-z][\w.-]*$")
_NOT_A_FILE = {"e.g", "i.e", "a.m", "p.m", "u.s", "u.k"}
_SEE_VERB = r"(?:read|open|show(?: me)?|look at|view|cat|print|pull up|bring up|summari[sz]e|explain|walk me through|go through|check|inspect)"
_ARTICLE = r"(?:the |my |this |that |our )?"
_ASKS_TO_SEE = re.compile(rf"\b{_SEE_VERB}\b|\bwhat(?:'s| is| does)\b|\bhow does\b|\btell me about\b", re.I)
_DO_NOT = re.compile(r"\b(?:don'?t|do not|never|stop)\b", re.I)
_TEST_FILE = re.compile(rf"\b{_SEE_VERB} {_ARTICLE}(?:test file|test code|tests?\b(?! (?:output|results?|failures?|runs?|names?)))|\bthe test file\b")
_SECRET_FILE = re.compile(rf"\b{_SEE_VERB} {_ARTICLE}(\w*(?:secret|credential|password|token)\w*)\b|\b(\w*(?:secret|credential|password|token)\w*) file\b")
_ENV_FILE = re.compile(rf"\benv file\b|\b{_SEE_VERB} {_ARTICLE}env\b")


def asks_to_see_file(text: str) -> bool:
    """Whether the words ask to see what is in a file, rather than to do something else to it."""
    plain = text.replace("’", "'")
    return bool(_ASKS_TO_SEE.search(plain)) and not _DO_NOT.search(plain)


class Workspace:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def mentioned_file(self, text: str, *, test_file: str | None = None) -> FileMention | None:
        """The file these words name, if any. A name that looks secret or leaves the folder is kept exactly as
        said, so resolve() refuses it; it is never swapped for a file that could be read."""
        spoken = text.replace("’", "'")
        for pattern, replacement in _SPOKEN_PATH:
            spoken = pattern.sub(replacement, spoken)
        files = self.files()
        before = ""
        for word in spoken.split():
            token = word.strip("\"'`,;:!?()[]{}<>").rstrip(".")
            name = token.rsplit("/", 1)[-1]
            is_path = token.lower() not in _NOT_A_FILE and (
                bool(_EXTENSION.search(name)) or bool(_DOTFILE.fullmatch(name)) or (name != token and token.startswith(("/", "~/", "../", "./")))
            )
            if is_path:
                if before.lower() in ("test", "tests") and "/" not in token:  # "test cart dot py"
                    path, corrected = self._project_file(f"test_{token}", files)
                    if path in files and not corrected:
                        return FileMention(path, token)
                path, corrected = self._project_file(token, files)
                return FileMention(path, token, corrected)
            before = token
        return self._named_without_extension(spoken.lower(), files, test_file)

    @staticmethod
    def _project_file(name: str, files: list[str]) -> tuple[str, bool]:
        if SECRET_NAME.search(name) or name.startswith(("/", "~", "../")):
            return name, False
        name = name.removeprefix("./")
        exact = {f.lower(): f for f in files}
        if name.lower() in exact:
            return exact[name.lower()], False
        by_name: dict[str, list[str]] = {}
        for f in files:
            by_name.setdefault(f.rsplit("/", 1)[-1].lower(), []).append(f)
        base = name.rsplit("/", 1)[-1].lower()
        if len(by_name.get(base, ())) == 1:
            return by_name[base][0], False
        near = difflib.get_close_matches(base, by_name, n=2, cutoff=0.85)
        if len(near) == 1 and len(by_name[near[0]]) == 1:
            return by_name[near[0]][0], True  # "card.py"
        return name, False

    @staticmethod
    def _named_without_extension(low: str, files: list[str], test_file: str | None) -> FileMention | None:
        secret = _SECRET_FILE.search(low)
        if secret:
            word = secret.group(1) or secret.group(2)
            return FileMention(word, word)
        if _ENV_FILE.search(low):
            return FileMention(".env", ".env")
        stems: dict[str, str] = {}
        for f in sorted(files, key=lambda f: f.count("/")):
            stem = f.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
            stems.setdefault(stem, f)
            stems.setdefault(stem.replace("_", " "), f)
        if "readme" in stems:
            stems.setdefault("read me", stems["readme"])
        if stems:
            names = "|".join(re.escape(s) for s in sorted(stems, key=len, reverse=True))
            m = re.search(rf"\b{_SEE_VERB} {_ARTICLE}({names})\b(?! (?:is|was|function|method|class|tests?)\b)|\b({names}) file\b", low)
            if m:
                stem = m.group(1) or m.group(2)
                return FileMention(stems[stem], stem)
        if _TEST_FILE.search(low):
            tests = [f for f in files if is_test_file(f) and not f.endswith("conftest.py")]
            chosen = test_file if test_file in files else (tests[0] if tests else None)
            if chosen:
                return FileMention(chosen, "the test file")
        return None

    def resolve(self, rel: str) -> Path:
        rel = (rel or "").strip().strip("\"'")
        if not rel or "\0" in rel:
            raise PolicyError("no file named")
        candidate = (self.root / rel).resolve()
        try:
            inside = candidate.relative_to(self.root).as_posix()
        except ValueError:
            raise PolicyError(f"{rel[:60]} is outside the approved project") from None
        if SECRET_NAME.search(inside):
            raise PolicyError(f"{inside[:60]} looks like a secret, so it stays closed")
        if any(p in SKIP_DIRS for p in inside.split("/")):
            raise PolicyError(f"{inside[:60]} is in a folder that stays closed")
        return candidate

    def read_text(self, rel: str) -> str:
        path = self.resolve(rel)
        if not path.is_file():
            raise PolicyError(f"no file called {rel[:60]} in this project")
        if path.suffix.lower() not in READ_SUFFIXES:
            raise PolicyError(f"{path.name[:60]} is not a text file I can read")
        if path.stat().st_size > MAX_READ_BYTES:
            raise PolicyError(f"{path.name[:60]} is too large to read")
        return redact(path.read_text(encoding="utf-8", errors="replace"))

    def _walk(self, suffixes=WATCH_SUFFIXES):
        count = 0
        for folder, dirs, files in os.walk(self.root):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
            for name in sorted(files):
                path = Path(folder) / name
                if path.suffix.lower() in suffixes:
                    count += 1
                    if count > MAX_WATCHED_FILES:
                        return
                    yield path

    def files(self) -> list[str]:
        return [p.relative_to(self.root).as_posix() for p in self._walk(READ_SUFFIXES) if not SECRET_NAME.search(p.relative_to(self.root).as_posix())]

    def signature(self) -> str:
        """Changes when a watched file changes. Sizes and times only: quick enough for every second."""
        h = hashlib.sha256()
        for path in self._walk():
            try:
                st = path.stat()
            except OSError:
                continue
            h.update(f"{path}\0{st.st_mtime_ns}\0{st.st_size}\n".encode())
        return h.hexdigest()

    def test_hashes(self) -> dict[str, str]:
        out = {}
        for path in self._walk():
            rel = path.relative_to(self.root).as_posix()
            if is_test_file(rel):
                try:
                    out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
                except OSError:
                    continue
        return out


def changed_tests(before: dict[str, str], now: dict[str, str]) -> list[str]:
    """Test files edited or deleted since ``before``. A brand new test file is not a change: it only adds work."""
    return sorted(rel for rel, digest in before.items() if now.get(rel) != digest)


class ProjectRegistry:
    """The folders whose code the user agreed to run. Kept in a file, changed only by the user."""

    def __init__(self, path: Path):
        self.path = path
        self._projects: list[str] = []
        if path.exists():
            try:
                self._projects = [str(p) for p in json.loads(path.read_text()) if isinstance(p, str)]
            except (OSError, json.JSONDecodeError, TypeError):
                self._projects = []

    def all(self) -> list[str]:
        return list(self._projects)

    def approved(self, folder: str | Path) -> bool:
        return str(Path(folder).resolve()) in self._projects

    def approve(self, folder: str | Path) -> str:
        path = Path(folder).expanduser().resolve()
        home = Path.home().resolve()
        if not path.is_dir():
            raise PolicyError(f"no such folder: {redact(str(path))[:120]}")
        if path == home or home.is_relative_to(path):
            raise PolicyError("that folder is too broad: choose one project folder")
        if not path.is_relative_to(home):
            raise PolicyError("only folders inside your home folder can be approved")
        if str(path) not in self._projects:
            self._projects.append(str(path))
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._projects, indent=1))
        return str(path)
