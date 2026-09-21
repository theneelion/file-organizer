"""Portable, configuration-driven organizer for the folder passed on the command line."""

from __future__ import annotations

import json
import mimetypes
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox
from typing import Any, Callable, Iterable, cast


CONFIG_NAME = "config.json"
STATE_NAME = ".organizer_state.json"
LOCK_NAME = ".organizer.lock"
ESTABLISHED_MARKER = b"1"
STATE_TEMP_PREFIX = ".organizer_state_"
PROTECTED_NAMES = frozenset(
    {
        "organizer.py",
        "organize files.bat",
        CONFIG_NAME.casefold(),
        STATE_NAME.casefold(),
        LOCK_NAME.casefold(),
    }
)
FALLBACK_CATEGORIES = ("Unknown", "No Extension")
FALLBACK_CATEGORY_NAMES = frozenset(category.casefold() for category in FALLBACK_CATEGORIES)
WINDOWS_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}
EXTENSION_PATTERN = re.compile(r"\.[^.\\/:*?\"<>|\s]+\Z")
MIME_PATTERN = re.compile(r"(?:[a-z0-9!#$&^_.+-]+|\*)/(?:[a-z0-9!#$&^_.+-]+|\*)\Z", re.IGNORECASE)


class OrganizerError(Exception):
    """Expected error that should be shown to a user without a traceback."""


class ConfigError(OrganizerError):
    pass


class StateError(OrganizerError):
    pass


class PathError(OrganizerError):
    pass


class LockError(OrganizerError):
    pass


@dataclass(frozen=True)
class ClassificationRules:
    extensions: dict[str, str]
    mime_types: tuple[tuple[str, str], ...]
    categories: tuple[str, ...]
    rename_by_category: dict[str, bool]


@dataclass
class SequenceState:
    sequences: dict[str, int]

    def key_for(self, category: str) -> str:
        folded = category.casefold()
        for key in self.sequences:
            if key.casefold() == folded:
                return key
        return category

    def next_number(self, category: str) -> int:
        return self.sequences.get(self.key_for(category), 1)

    def reserve_through(self, category: str, number: int) -> None:
        self.sequences[self.key_for(category)] = number + 1

    def as_json(self) -> dict[str, object]:
        return {"version": 1, "sequences": self.sequences}


@dataclass
class RunSummary:
    moved: int = 0
    skipped: int = 0
    failed: int = 0

    def text(self) -> str:
        return f"Moved:   {self.moved}\nSkipped: {self.skipped}\nFailed:  {self.failed}"


class Reporter:
    """Centralizes GUI dialogs while retaining useful console output."""

    @staticmethod
    def _with_dialog(callback: Callable[[], object]) -> object | None:
        try:
            import tkinter as tk
        except ImportError:
            return None
        try:
            root: Any = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            try:
                return callback()
            finally:
                root.destroy()
        except (OSError, RuntimeError, tk.TclError):
            return None

    def warning(self, title: str, message: str) -> None:
        print(f"WARNING - {title}: {message}", file=sys.stderr)
        self._with_dialog(lambda: messagebox.showwarning(title, message))

    def extensionless(self, filename: str) -> bool:
        message = f"This file has no extension: {filename}\n\nContinue with MIME fallback?"
        print(f"WARNING - No extension: {message}", file=sys.stderr)
        result = self._with_dialog(
            lambda: messagebox.askyesno("No extension", message, icon="warning")
        )
        if result is None:
            print("No interactive dialog is available; the extensionless file was skipped.", file=sys.stderr)
            return False
        return bool(result)

    def complete(self, summary: RunSummary) -> None:
        message = summary.text()
        print(f"Organization Complete\n{message}")
        self._with_dialog(lambda: messagebox.showinfo("Organization Complete", message))


class OrganizerLock:
    """An advisory OS-level lock scoped to one target folder."""

    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def __enter__(self) -> "OrganizerLock":
        try:
            if is_lexists(self.path) and self.path.is_symlink():
                raise OSError("Lock file must not be a symbolic link.")
            self.handle = open(self.path, "a+b")
            self.handle.seek(0)
            if self.handle.read(1) == b"":
                self.handle.seek(0)
                self.handle.write(b"0")
                self.handle.flush()
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, ImportError) as error:
            self._close()
            raise LockError("Organizer is already running or its lock file cannot be opened.") from error
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        except (OSError, ImportError):
            pass
        finally:
            self._close()

    def mark_established(self) -> None:
        if self.handle is None:
            raise LockError("Organizer lock is not active.")
        try:
            self.handle.seek(0)
            self.handle.write(ESTABLISHED_MARKER)
            self.handle.truncate()
            self.handle.flush()
            os.fsync(self.handle.fileno())
        except OSError as error:
            raise LockError("Cannot persist organizer initialization marker.") from error

    def _close(self) -> None:
        if self.handle is not None:
            self.handle.close()
            self.handle = None


def has_established_marker(lock_path: Path) -> bool:
    try:
        return is_lexists(lock_path) and lock_path.is_file() and lock_path.read_bytes()[:1] == ESTABLISHED_MARKER
    except OSError as error:
        raise LockError("Cannot inspect organizer lock state.") from error


def validate_category_name(name: object) -> str:
    if not isinstance(name, str) or not name or name != name.strip():
        raise ConfigError("Every category name must be a non-empty, trimmed string.")
    if name in {".", ".."} or name.endswith((".", " ")):
        raise ConfigError(f"Unsafe category name: {name!r}")
    if any(character in name for character in '\\/:*?"<>|') or any(ord(character) < 32 for character in name):
        raise ConfigError(f"Unsafe category name: {name!r}")
    if os.path.isabs(name) or Path(name).is_absolute():
        raise ConfigError(f"Unsafe category name: {name!r}")
    if name.split(".", 1)[0].casefold() in WINDOWS_RESERVED_NAMES:
        raise ConfigError(f"Windows-reserved category name: {name!r}")
    return name


def validate_extension(value: object) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not EXTENSION_PATTERN.fullmatch(value)
        or value.count(".") != 1
    ):
        raise ConfigError(f"Invalid extension rule: {value!r}")
    return value.casefold()


def validate_mime_pattern(value: object) -> str:
    if not isinstance(value, str) or value != value.strip() or not MIME_PATTERN.fullmatch(value):
        raise ConfigError(f"Invalid MIME rule: {value!r}")
    return value.casefold()


def mime_patterns_overlap(left: str, right: str) -> bool:
    left_type, left_subtype = left.split("/", 1)
    right_type, right_subtype = right.split("/", 1)
    return (left_type == right_type or "*" in (left_type, right_type)) and (
        left_subtype == right_subtype or "*" in (left_subtype, right_subtype)
    )


def validate_config(data: object) -> ClassificationRules:
    if not isinstance(data, dict):
        raise ConfigError("config.json must contain exactly one object named 'categories'.")
    data = cast(dict[str, Any], data)
    if set(data) != {"categories"} or not isinstance(data["categories"], dict):
        raise ConfigError("config.json must contain exactly one object named 'categories'.")
    
    raw_categories = cast(dict[str, Any], data["categories"])
    if not raw_categories:
        raise ConfigError("config.json must define at least one category.")

    categories: list[str] = []
    extensions: dict[str, str] = {}
    mime_types: list[tuple[str, str]] = []
    rename_by_category: dict[str, bool] = {}
    seen_categories: set[str] = set()

    for raw_name, definition in raw_categories.items():
        category = validate_category_name(raw_name)
        folded_name = category.casefold()
        if folded_name in seen_categories or folded_name in FALLBACK_CATEGORY_NAMES:
            raise ConfigError(f"Duplicate or reserved category name: {category!r}")
        seen_categories.add(folded_name)

        if not isinstance(definition, dict):
            raise ConfigError(f"Invalid definition for category {category!r}.")
        definition = cast(dict[str, Any], definition)
        if set(definition) - {"extensions", "mime_types", "rename"}:
            raise ConfigError(f"Invalid definition for category {category!r}.")
        raw_extensions = definition.get("extensions", [])
        raw_mime_types = definition.get("mime_types", [])
        rename = definition.get("rename", True)
        if not isinstance(rename, bool):
            raise ConfigError(f"'rename' for {category!r} must be a boolean.")

        if not isinstance(raw_extensions, list) or not isinstance(raw_mime_types, list):
            raise ConfigError(f"Rules for {category!r} must be lists.")

        raw_extensions = cast(list[Any], raw_extensions)
        raw_mime_types = cast(list[Any], raw_mime_types)
        categories.append(category)
        rename_by_category[category] = rename
        category_extensions: set[str] = set()
        for value in raw_extensions:
            extension = validate_extension(value)
            if extension in category_extensions:
                raise ConfigError(f"Extension {value!r} is listed more than once.")
            category_extensions.add(extension)
            if extension in extensions:
                raise ConfigError(f"Extension {value!r} is assigned to multiple categories.")
            extensions[extension] = category
        for value in raw_mime_types:
            pattern = validate_mime_pattern(value)
            for existing, existing_category in mime_types:
                if existing_category != category and mime_patterns_overlap(pattern, existing):
                    raise ConfigError(f"MIME rule {value!r} is ambiguous with {existing!r}.")
            mime_types.append((pattern, category))

    return ClassificationRules(extensions, tuple(mime_types), tuple(categories), rename_by_category)


def load_config(path: Path) -> ClassificationRules:
    try:
        with open(path, encoding="utf-8") as file:
            return validate_config(json.load(file))
    except FileNotFoundError as error:
        raise ConfigError(f"Missing required configuration file: {CONFIG_NAME}") from error
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigError(f"Cannot read valid JSON from {CONFIG_NAME}: {error}") from error


def is_lexists(path: Path) -> bool:
    return os.path.lexists(path)


def is_protected_name(name: str) -> bool:
    folded = name.casefold()
    return folded in PROTECTED_NAMES or (
        folded.startswith(STATE_TEMP_PREFIX) and folded.endswith(".tmp")
    )


def find_casefold_entries(directory: Path, name: str) -> list[Path]:
    try:
        with os.scandir(directory) as entries:
            return [Path(entry.path) for entry in entries if entry.name.casefold() == name.casefold()]
    except OSError as error:
        raise PathError(f"Cannot inspect target folder {directory}: {error}") from error


def is_contained(path: Path, target: Path) -> bool:
    try:
        return os.path.commonpath((str(path.resolve(strict=False)), str(target.resolve(strict=False)))) == str(
            target.resolve(strict=False)
        )
    except (OSError, ValueError):
        return False


def validate_category_paths(target: Path, categories: Iterable[str]) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for category in categories:
        matches = find_casefold_entries(target, category)
        if len(matches) > 1:
            raise PathError(f"Multiple case-insensitive matches exist for category {category!r}.")
        path = matches[0] if matches else target / category
        if not is_contained(path, target):
            raise PathError(f"Category path escapes the target folder: {category!r}")
        if is_lexists(path):
            if path.is_symlink() or not path.is_dir():
                raise PathError(f"Category path is not a safe directory: {path.name}")
        paths[category] = path
    return paths


def load_state(path: Path, established: bool) -> tuple[SequenceState, bool]:
    if not is_lexists(path):
        if established:
            raise StateError(f"{STATE_NAME} is missing after an established organizer run.")
        return SequenceState({}), True
    if path.is_symlink() or not path.is_file():
        raise StateError(f"{STATE_NAME} must be a regular file.")
    try:
        with open(path, encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StateError(f"Cannot read valid state from {STATE_NAME}: {error}") from error
    if not isinstance(data, dict):
        raise StateError(f"{STATE_NAME} has an invalid structure.")
    data = cast(dict[str, Any], data)
    if (
        set(data) != {"version", "sequences"}
        or isinstance(data["version"], bool)
        or not isinstance(data["version"], int)
        or data["version"] != 1
    ):
        raise StateError(f"{STATE_NAME} has an invalid structure.")
    sequences = data["sequences"]
    if not isinstance(sequences, dict):
        raise StateError(f"{STATE_NAME} sequences must be an object.")
    sequences = cast(dict[str, Any], sequences)
    normalized_names: set[str] = set()
    checked: dict[str, int] = {}
    for category, number in sequences.items():
        try:
            checked_category = validate_category_name(category)
        except ConfigError as error:
            raise StateError(f"{STATE_NAME} has an invalid category key: {category!r}") from error
        if checked_category.casefold() in normalized_names:
            raise StateError(f"{STATE_NAME} has duplicate category keys.")
        normalized_names.add(checked_category.casefold())
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise StateError(f"{STATE_NAME} contains an invalid sequence value for {category!r}.")
        checked[checked_category] = number
    return SequenceState(checked), False


def persist_state(path: Path, state: SequenceState) -> None:
    descriptor = None
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=".organizer_state_", suffix=".tmp", dir=path.parent)
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
            descriptor = None
            json.dump(state.as_json(), file, indent=2, sort_keys=True)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    except (OSError, TypeError, ValueError) as error:
        raise StateError(f"Cannot safely save {STATE_NAME}: {error}") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def extension_of(filename: str) -> str:
    extension = os.path.splitext(filename)[1]
    return extension if extension not in {"", "."} else ""


def match_mime(mime_type: str | None, rules: ClassificationRules) -> str | None:
    if not isinstance(mime_type, str) or not MIME_PATTERN.fullmatch(mime_type):
        return None
    found: str | None = None
    mime_type = mime_type.casefold()
    for pattern, category in rules.mime_types:
        if mime_patterns_overlap(mime_type, pattern):
            if found is not None and found != category:
                raise ConfigError("MIME configuration produced an ambiguous runtime match.")
            found = category
    return found


def classify(path: Path, rules: ClassificationRules, reporter: Reporter) -> str | None:
    extension = extension_of(path.name)
    if extension:
        category = rules.extensions.get(extension.casefold())
        if category is not None:
            return category
    elif not reporter.extensionless(path.name):
        return None
    try:
        mime_type = mimetypes.guess_type(path.name, strict=False)[0]
    except (TypeError, ValueError) as error:
        reporter.warning("Classification failed", f"Could not classify {path.name}: {error}")
        return None
    category = match_mime(mime_type, rules)
    if category is not None:
        return category
    return "Unknown" if extension else "No Extension"


def snapshot_files(target: Path, reporter: Reporter, summary: RunSummary) -> list[Path]:
    files: list[Path] = []
    try:
        with os.scandir(target) as entries:
            for entry in entries:
                name = entry.name
                if is_protected_name(name):
                    continue
                try:
                    if entry.is_symlink() or name.casefold().endswith(".lnk"):
                        reporter.warning("Link skipped", f"Skipped link-like file: {name}")
                        summary.skipped += 1
                    elif entry.is_file(follow_symlinks=False):
                        files.append(Path(entry.path))
                except OSError as error:
                    reporter.warning("File skipped", f"Could not inspect {name}: {error}")
                    summary.skipped += 1
    except OSError as error:
        raise PathError(f"Cannot snapshot files in {target}: {error}") from error
    return sorted(files, key=lambda path: (path.name.casefold(), path.name))


def ensure_category_directory(path: Path, created_directories: set[Path]) -> None:
    if is_lexists(path):
        if path.is_symlink() or not path.is_dir():
            raise OSError(f"Category path is no longer a usable directory: {path.name}")
        return
    try:
        path.mkdir()
        created_directories.add(path)
    except FileExistsError:
        if path.is_symlink() or not path.is_dir():
            raise OSError(f"Category path is no longer a usable directory: {path.name}")


def number_is_occupied(directory: Path, category: str, number: int) -> bool:
    prefix = f"{category}_{number}".casefold()
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                name = entry.name.casefold()
                if name == prefix or name.startswith(f"{prefix}."):
                    return True
    except OSError as error:
        raise OSError(f"Could not inspect destination {directory.name}: {error}") from error
    return False


def first_safe_number(directory: Path, category: str, state: SequenceState) -> int:
    number = state.next_number(category)
    while number_is_occupied(directory, category, number):
        number += 1
    return number


def source_is_safe_regular_file(path: Path) -> bool:
    try:
        return is_lexists(path) and not path.is_symlink() and path.is_file() and not path.name.casefold().endswith(".lnk")
    except OSError:
        return False


def process_files(
    files: Iterable[Path],
    rules: ClassificationRules,
    paths: dict[str, Path],
    state_path: Path,
    state: SequenceState,
    reporter: Reporter,
    summary: RunSummary,
) -> tuple[set[Path], bool]:
    created_directories: set[Path] = set()
    state_save_failed = False
    for source in files:
        if not source_is_safe_regular_file(source):
            reporter.warning("File skipped", f"File disappeared or became unsafe before processing: {source.name}")
            summary.skipped += 1
            continue
        category = classify(source, rules, reporter)
        if category is None:
            summary.skipped += 1
            continue
        directory = paths[category]

        try:
            ensure_category_directory(directory, created_directories)
        except OSError as error:
            reporter.warning("File move failed", f"Could not prepare {source.name}: {error}")
            summary.failed += 1
            continue
        if not rules.rename_by_category.get(category, True):
            destination = directory / source.name

            if is_lexists(destination):
                reporter.warning(
                    "File move skipped",
                    f"Destination already exists for {source.name}: {destination.name}",
                )
                summary.skipped += 1
                continue

            try:
                os.rename(source, destination)
            except OSError as error:
                reporter.warning("File move failed", f"Could not move {source.name}: {error}")
                summary.failed += 1
                continue

            summary.moved += 1
            continue

        try:
            number = first_safe_number(directory, category, state)
        except OSError as error:
            reporter.warning("File move failed", f"Could not prepare {source.name}: {error}")
            summary.failed += 1
            continue

        destination = directory / f"{category}_{number}{extension_of(source.name)}"
        if is_lexists(destination):
            # A non-serial exact collision is still never safe to overwrite.
            reporter.warning("File move failed", f"Destination already exists for {source.name}: {destination.name}")
            summary.failed += 1
            continue
        state.reserve_through(category, number)
        try:
            persist_state(state_path, state)
        except StateError as error:
            reporter.warning("State save failed", str(error))
            summary.failed += 1
            state_save_failed = True
            break
        try:
            os.rename(source, destination)
        except OSError as error:
            reporter.warning("File move failed", f"Could not move {source.name}: {error}")
            summary.failed += 1
            continue
        summary.moved += 1
    return created_directories, state_save_failed


def clean_empty_directories(paths: Iterable[Path], reporter: Reporter) -> None:
    for path in paths:
        try:
            with os.scandir(path) as entries:
                if next(entries, None) is not None:
                    continue
            path.rmdir()
        except OSError:
            reporter.warning("Cleanup failed", f"Could not remove empty category folder: {path.name}")


def run(target_folder: str | Path, reporter: Reporter | None = None) -> tuple[int, RunSummary | None]:
    reporter = reporter or Reporter()
    target = Path(target_folder).resolve(strict=False)
    if not target.is_dir():
        reporter.warning("Invalid target folder", f"Target folder does not exist: {target}")
        return 1, None
    try:
        lock_path = target / LOCK_NAME
        established = has_established_marker(lock_path)
        with OrganizerLock(target / LOCK_NAME) as lock:
            rules = load_config(target / CONFIG_NAME)
            all_categories = (*rules.categories, *FALLBACK_CATEGORIES)
            state, is_first_run = load_state(target / STATE_NAME, established)
            paths = validate_category_paths(target, all_categories)
            if is_first_run:
                persist_state(target / STATE_NAME, state)
            lock.mark_established()

            summary = RunSummary()
            files = snapshot_files(target, reporter, summary)
            created_directories, state_save_failed = process_files(
                files, rules, paths, target / STATE_NAME, state, reporter, summary
            )
            clean_empty_directories(created_directories, reporter)
            reporter.complete(summary)
            return (1 if state_save_failed else 0), summary
    except OrganizerError as error:
        reporter.warning("Organization aborted", str(error))
        return 1, None


def main(arguments: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if arguments is None else arguments
    if len(arguments) != 1:
        print("Usage: Organizer.py <target-folder>", file=sys.stderr)
        return 2
    return run(arguments[0])[0]


if __name__ == "__main__":
    raise SystemExit(main())
