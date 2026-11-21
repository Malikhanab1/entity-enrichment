"""Safe, dedicated-workspace file tools for the enrichment agent.

Only paths below ``MCP_FILE_WORKSPACE`` are addressable; it defaults to the
repository's ``workspace/`` folder and is independent of the agent's cwd.
"""

from __future__ import annotations

import csv
import fnmatch
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any


_MCP_ROOT = Path(__file__).resolve().parents[3]
_workspace_value = Path(os.getenv("MCP_FILE_WORKSPACE", "../workspace"))
# Relative configuration is anchored to mcp/, not to the agent's working
# directory. This keeps the boundary stable when MCP is started over stdio.
WORKSPACE = (_workspace_value if _workspace_value.is_absolute() else _MCP_ROOT / _workspace_value).resolve()
MAX_BYTES = int(os.getenv("MCP_FILE_MAX_BYTES", str(10 * 1024 * 1024)))
MAX_RESULTS = int(os.getenv("MCP_FILE_MAX_RESULTS", "100"))
TEXT_SUFFIXES = {".txt", ".md", ".csv", ".tsv", ".json", ".yaml", ".yml", ".xml", ".html", ".py", ".js", ".ts", ".css", ".sql", ".toml", ".ini", ".log"}
SHEET_SUFFIXES = {".xlsx", ".xls", ".csv", ".tsv"}
SKIP_PARTS = {".git", ".venv", "node_modules", "__pycache__"}
INTERNAL_WORKSPACE_FILES = {".enrichment_contacts.sqlite3", ".enrichment_contacts.sqlite3-wal", ".enrichment_contacts.sqlite3-shm"}


def _path(user_path: str, *, for_write: bool = False) -> Path:
    if not isinstance(user_path, str) or not user_path.strip():
        raise ValueError("file_path must be a non-empty path relative to the configured workspace.")
    candidate = Path(user_path)
    if candidate.is_absolute():
        try:
            candidate.relative_to(WORKSPACE)
        except ValueError as exc:
            raise ValueError("Absolute paths must be inside the configured workspace.") from exc
    else:
        candidate = WORKSPACE / candidate
    # strict=False resolves existing symlinks and prevents writes through a
    # symlinked parent that points outside the mounted workspace.
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(WORKSPACE)
    except ValueError as exc:
        raise ValueError("Path traversal and paths outside the configured workspace are blocked.") from exc
    if for_write:
        parent = resolved.parent.resolve(strict=False)
        try:
            parent.relative_to(WORKSPACE)
        except ValueError as exc:
            raise ValueError("Write target parent is outside the configured workspace.") from exc
    return resolved


def _relative(path: Path) -> str:
    return str(path.relative_to(WORKSPACE)) or "."


def _check_file(path: Path) -> None:
    if not path.exists() or not path.is_file():
        raise ValueError(f"File not found: {_relative(path)}")
    if path.stat().st_size > MAX_BYTES:
        raise ValueError(f"File exceeds MCP_FILE_MAX_BYTES ({MAX_BYTES} bytes).")


def list_files(path: str = ".", pattern: str = "*", max_depth: int = 3) -> dict[str, Any]:
    """List non-hidden workspace files without following directories outside it."""
    root = _path(path)
    if not root.exists() or not root.is_dir():
        raise ValueError("path must be an existing directory inside the configured workspace.")
    max_depth = max(0, min(int(max_depth), 12))
    matches: list[dict[str, Any]] = []
    for candidate in root.rglob("*"):
        if any(part in SKIP_PARTS for part in candidate.parts) or candidate.name in INTERNAL_WORKSPACE_FILES:
            continue
        try:
            relative = candidate.relative_to(root)
        except ValueError:
            continue
        if len(relative.parts) > max_depth + 1 or not candidate.is_file() or not fnmatch.fnmatch(candidate.name, pattern):
            continue
        matches.append({"path": _relative(candidate.resolve()), "size_bytes": candidate.stat().st_size, "extension": candidate.suffix.lower()})
        if len(matches) >= MAX_RESULTS:
            break
    return {"workspace": str(WORKSPACE), "files": matches, "truncated": len(matches) >= MAX_RESULTS}


def file_info(file_path: str) -> dict[str, Any]:
    """Return metadata for a permitted workspace file."""
    path = _path(file_path)
    _check_file(path)
    stat = path.stat()
    return {"path": _relative(path), "size_bytes": stat.st_size, "extension": path.suffix.lower(), "modified_epoch": int(stat.st_mtime), "spreadsheet": path.suffix.lower() in SHEET_SUFFIXES}


def read_text_file(file_path: str, start_line: int = 1, max_lines: int = 200) -> dict[str, Any]:
    """Read a bounded section of a UTF-8 text, Markdown, CSV, or source file."""
    path = _path(file_path)
    _check_file(path)
    if path.suffix.lower() not in TEXT_SUFFIXES:
        raise ValueError("Use read_spreadsheet for Excel files; this tool only reads supported text formats.")
    start_line, max_lines = max(1, int(start_line)), max(1, min(int(max_lines), 2_000))
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ValueError("File is not UTF-8 text.") from exc
    selected = lines[start_line - 1 : start_line - 1 + max_lines]
    return {"path": _relative(path), "start_line": start_line, "total_lines": len(lines), "content": "\n".join(selected), "truncated": start_line - 1 + len(selected) < len(lines)}


def search_files(query: str, path: str = ".", pattern: str = "*", max_matches: int = 50, regex: bool = False) -> dict[str, Any]:
    """Search UTF-8 text files by literal text or regular expression."""
    if not query:
        raise ValueError("query must not be empty.")
    # Tool-calling models often send an explicit empty string for an optional
    # parameter.  Treat that exactly like the documented workspace-root default.
    root = _path(path or ".")
    if not root.is_dir():
        raise ValueError("path must be a directory.")
    matcher = re.compile(query if regex else re.escape(query), re.IGNORECASE)
    max_matches = max(1, min(int(max_matches), MAX_RESULTS))
    matches: list[dict[str, Any]] = []
    for candidate in root.rglob("*"):
        if any(part in SKIP_PARTS for part in candidate.parts) or not candidate.is_file() or not fnmatch.fnmatch(candidate.name, pattern) or candidate.suffix.lower() not in TEXT_SUFFIXES:
            continue
        if candidate.stat().st_size > MAX_BYTES:
            continue
        try:
            for line_number, line in enumerate(candidate.read_text(encoding="utf-8").splitlines(), 1):
                if matcher.search(line):
                    matches.append({"path": _relative(candidate.resolve()), "line": line_number, "text": line[:500]})
                    if len(matches) >= max_matches:
                        return {"matches": matches, "truncated": True}
        except UnicodeDecodeError:
            continue
    return {"matches": matches, "truncated": False}


def write_text_file(file_path: str, content: str, overwrite: bool = False) -> dict[str, Any]:
    """Create a UTF-8 text/Markdown/CSV file, or explicitly overwrite one."""
    if not isinstance(content, str):
        raise ValueError("content must be a string.")
    if len(content.encode("utf-8")) > MAX_BYTES:
        raise ValueError("content exceeds MCP_FILE_MAX_BYTES.")
    path = _path(file_path, for_write=True)
    if path.suffix.lower() not in TEXT_SUFFIXES:
        raise ValueError("write_text_file supports text formats only; use write_spreadsheet for .xlsx/.xls.")
    existed = path.exists()
    if existed and not overwrite:
        raise ValueError("File already exists. Set overwrite=true to replace it.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return {"path": _relative(path), "created": not existed, "bytes_written": len(content.encode("utf-8"))}


def replace_in_text_file(file_path: str, find: str, replace: str, expected_replacements: int | None = None) -> dict[str, Any]:
    """Make an exact, validated text edit; avoids arbitrary shell execution."""
    if not find:
        raise ValueError("find must not be empty.")
    path = _path(file_path, for_write=True)
    _check_file(path)
    if path.suffix.lower() not in TEXT_SUFFIXES:
        raise ValueError("replace_in_text_file supports text formats only.")
    content = path.read_text(encoding="utf-8")
    count = content.count(find)
    if count == 0:
        raise ValueError("find text does not occur in the file.")
    if expected_replacements is not None and count != int(expected_replacements):
        raise ValueError(f"Expected {expected_replacements} replacements, found {count}; file left unchanged.")
    updated = content.replace(find, replace)
    if len(updated.encode("utf-8")) > MAX_BYTES:
        raise ValueError("Edited content exceeds MCP_FILE_MAX_BYTES.")
    path.write_text(updated, encoding="utf-8")
    return {"path": _relative(path), "replacements": count}


def read_spreadsheet(file_path: str, sheet: str | None = None, start_row: int = 1, max_rows: int = 100, max_columns: int = 30) -> dict[str, Any]:
    """Read CSV/TSV/XLS/XLSX rows with strict output bounds."""
    path = _path(file_path)
    _check_file(path)
    suffix = path.suffix.lower()
    start_row, max_rows, max_columns = max(1, int(start_row)), max(1, min(int(max_rows), 500)), max(1, min(int(max_columns), 100))
    if suffix in {".csv", ".tsv"}:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.reader(handle, delimiter="\t" if suffix == ".tsv" else ","))
        return {"path": _relative(path), "sheet": None, "total_rows": len(rows), "rows": [row[:max_columns] for row in rows[start_row - 1 : start_row - 1 + max_rows]]}
    if suffix == ".xlsx":
        from openpyxl import load_workbook
        book = load_workbook(path, read_only=True, data_only=False)
        worksheet = book[sheet] if sheet else book.active
        rows = [[cell.value for cell in row[:max_columns]] for row in worksheet.iter_rows(min_row=start_row, max_row=start_row + max_rows - 1)]
        return {"path": _relative(path), "sheet": worksheet.title, "sheets": book.sheetnames, "rows": rows}
    if suffix == ".xls":
        import xlrd
        book = xlrd.open_workbook(path)
        worksheet = book.sheet_by_name(sheet) if sheet else book.sheet_by_index(0)
        rows = [worksheet.row_values(index, end_colx=min(worksheet.ncols, max_columns)) for index in range(start_row - 1, min(worksheet.nrows, start_row - 1 + max_rows))]
        return {"path": _relative(path), "sheet": worksheet.name, "sheets": book.sheet_names(), "rows": rows}
    raise ValueError("Supported spreadsheet formats are .csv, .tsv, .xlsx, and .xls.")


def write_spreadsheet(file_path: str, rows: list[list[Any]], sheet: str = "Sheet1", overwrite: bool = False) -> dict[str, Any]:
    """Create a CSV, XLSX, or legacy XLS spreadsheet from tabular rows."""
    if not isinstance(rows, list) or any(not isinstance(row, list) for row in rows):
        raise ValueError("rows must be a list of row lists.")
    if len(rows) > 10_000 or any(len(row) > 100 for row in rows):
        raise ValueError("Maximum spreadsheet size is 10,000 rows by 100 columns.")
    path = _path(file_path, for_write=True)
    if path.exists() and not overwrite:
        raise ValueError("File already exists. Set overwrite=true to replace it.")
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower()
    if suffix in {".csv", ".tsv"}:
        with path.open("w", newline="", encoding="utf-8") as handle:
            csv.writer(handle, delimiter="\t" if suffix == ".tsv" else ",").writerows(rows)
    elif suffix == ".xlsx":
        from openpyxl import Workbook
        book = Workbook()
        worksheet = book.active
        worksheet.title = sheet[:31] or "Sheet1"
        for row in rows:
            worksheet.append(row)
        book.save(path)
    elif suffix == ".xls":
        import xlwt
        book = xlwt.Workbook()
        worksheet = book.add_sheet(sheet[:31] or "Sheet1")
        for row_index, row in enumerate(rows):
            for column_index, value in enumerate(row):
                worksheet.write(row_index, column_index, value)
        book.save(str(path))
    else:
        raise ValueError("Supported output formats are .csv, .tsv, .xlsx, and .xls.")
    return {"path": _relative(path), "rows_written": len(rows), "format": suffix}


def _spreadsheet_rows(path: Path, sheet: str | None) -> tuple[list[list[Any]], str | None]:
    """Load values for a patch operation.  XLSX formulas remain formula values."""
    suffix = path.suffix.lower()
    if suffix in {".csv", ".tsv"}:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            return list(csv.reader(handle, delimiter="\t" if suffix == ".tsv" else ",")), None
    if suffix == ".xlsx":
        from openpyxl import load_workbook

        book = load_workbook(path, data_only=False)
        worksheet = book[sheet] if sheet else book.active
        return [[cell.value for cell in row] for row in worksheet.iter_rows()], worksheet.title
    if suffix == ".xls":
        import xlrd

        book = xlrd.open_workbook(path)
        worksheet = book.sheet_by_name(sheet) if sheet else book.sheet_by_index(0)
        return [worksheet.row_values(index) for index in range(worksheet.nrows)], worksheet.name
    raise ValueError("Supported spreadsheet formats are .csv, .tsv, .xlsx, and .xls.")


def _save_spreadsheet_rows(path: Path, rows: list[list[Any]], sheet: str | None) -> None:
    """Persist a bounded patch atomically where the format permits it."""
    if len(rows) > 10_000 or any(len(row) > 100 for row in rows):
        raise ValueError("Maximum spreadsheet size is 10,000 rows by 100 columns.")
    suffix = path.suffix.lower()
    if suffix in {".csv", ".tsv"}:
        with tempfile.NamedTemporaryFile("w", newline="", encoding="utf-8", dir=path.parent, delete=False) as handle:
            csv.writer(handle, delimiter="\t" if suffix == ".tsv" else ",").writerows(rows)
            temporary = Path(handle.name)
        temporary.replace(path)
        return
    if suffix == ".xlsx":
        from openpyxl import load_workbook

        book = load_workbook(path, data_only=False)
        worksheet = book[sheet] if sheet else book.active
        for row_number, row in enumerate(rows, 1):
            for column_number, value in enumerate(row, 1):
                worksheet.cell(row_number, column_number).value = value
        with tempfile.NamedTemporaryFile(suffix=".xlsx", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
        try:
            book.save(temporary)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return
    if suffix == ".xls":
        # xlrd cannot edit in place.  Legacy XLS formatting is not retained,
        # but all values on the selected sheet are patched safely.
        import xlwt

        book = xlwt.Workbook()
        worksheet = book.add_sheet((sheet or "Sheet1")[:31])
        for row_number, row in enumerate(rows):
            for column_number, value in enumerate(row):
                worksheet.write(row_number, column_number, value)
        with tempfile.NamedTemporaryFile(suffix=".xls", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
        try:
            book.save(str(temporary))
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return
    raise ValueError("Supported spreadsheet formats are .csv, .tsv, .xlsx, and .xls.")


def _headers(rows: list[list[Any]]) -> tuple[list[str], dict[str, int]]:
    if not rows or not rows[0]:
        raise ValueError("Spreadsheet must have a non-empty header row before it can be patched.")
    headers = [str(value).strip() if value is not None else "" for value in rows[0]]
    if not all(headers) or len(set(headers)) != len(headers):
        raise ValueError("Header names must be non-empty and unique before patching a spreadsheet.")
    return headers, {header: index for index, header in enumerate(headers)}


def _pad_rows(rows: list[list[Any]], width: int) -> None:
    for row in rows:
        row.extend([""] * (width - len(row)))


def add_spreadsheet_column(file_path: str, column_name: str, default_value: Any = "", sheet: str | None = None) -> dict[str, Any]:
    """Add one named column to an existing CSV, TSV, XLSX, or XLS spreadsheet."""
    if not isinstance(column_name, str) or not column_name.strip():
        raise ValueError("column_name must be a non-empty string.")
    path = _path(file_path, for_write=True)
    _check_file(path)
    rows, selected_sheet = _spreadsheet_rows(path, sheet)
    headers, _ = _headers(rows)
    column_name = column_name.strip()
    if column_name in headers:
        raise ValueError(f"Column already exists: {column_name}")
    _pad_rows(rows, len(headers))
    rows[0].append(column_name)
    for row in rows[1:]:
        row.append(default_value)
    _save_spreadsheet_rows(path, rows, selected_sheet)
    return {"path": _relative(path), "sheet": selected_sheet, "column_added": column_name, "rows_updated": max(0, len(rows) - 1)}


def update_spreadsheet_rows(file_path: str, match_column: str, match_value: Any, updates: dict[str, Any], sheet: str | None = None, expected_matches: int | None = None) -> dict[str, Any]:
    """Update cells in rows whose named column exactly matches a value.

    Set ``expected_matches`` for guarded edits when the caller knows how many
    rows should change; a mismatch leaves the spreadsheet untouched.
    """
    if not isinstance(updates, dict) or not updates:
        raise ValueError("updates must be a non-empty object mapping existing column names to values.")
    path = _path(file_path, for_write=True)
    _check_file(path)
    rows, selected_sheet = _spreadsheet_rows(path, sheet)
    headers, positions = _headers(rows)
    if match_column not in positions:
        raise ValueError(f"Unknown match_column: {match_column}")
    unknown = set(updates) - set(headers)
    if unknown:
        raise ValueError(f"Unknown update column(s): {', '.join(sorted(unknown))}")
    _pad_rows(rows, len(headers))
    matched = [row for row in rows[1:] if row[positions[match_column]] == match_value or str(row[positions[match_column]]) == str(match_value)]
    if not matched:
        raise ValueError("No rows matched; spreadsheet left unchanged.")
    if expected_matches is not None and len(matched) != int(expected_matches):
        raise ValueError(f"Expected {expected_matches} matching rows, found {len(matched)}; spreadsheet left unchanged.")
    for row in matched:
        for column, value in updates.items():
            row[positions[column]] = value
    _save_spreadsheet_rows(path, rows, selected_sheet)
    return {"path": _relative(path), "sheet": selected_sheet, "rows_updated": len(matched), "updated_columns": list(updates)}


def append_spreadsheet_rows(file_path: str, rows: list[Any], sheet: str | None = None) -> dict[str, Any]:
    """Append list rows or header-keyed object rows to an existing spreadsheet."""
    if not isinstance(rows, list) or not rows:
        raise ValueError("rows must be a non-empty list of row lists or header-keyed objects.")
    path = _path(file_path, for_write=True)
    _check_file(path)
    existing, selected_sheet = _spreadsheet_rows(path, sheet)
    headers, _ = _headers(existing)
    additions: list[list[Any]] = []
    for row in rows:
        if isinstance(row, dict):
            unknown = set(row) - set(headers)
            if unknown:
                raise ValueError(f"Unknown column(s) in appended row: {', '.join(sorted(unknown))}")
            additions.append([row.get(header, "") for header in headers])
        elif isinstance(row, list):
            if len(row) > len(headers):
                raise ValueError("An appended row has more values than the header has columns.")
            additions.append(row + [""] * (len(headers) - len(row)))
        else:
            raise ValueError("Each appended row must be a list or object.")
    _pad_rows(existing, len(headers))
    existing.extend(additions)
    _save_spreadsheet_rows(path, existing, selected_sheet)
    return {"path": _relative(path), "sheet": selected_sheet, "rows_appended": len(additions), "total_rows": len(existing)}


def copy_workspace_file(source_path: str, destination_path: str, overwrite: bool = False) -> dict[str, Any]:
    """Copy one file inside the workspace; never follows an external path."""
    source, destination = _path(source_path), _path(destination_path, for_write=True)
    _check_file(source)
    if destination.exists() and not overwrite:
        raise ValueError("Destination exists. Set overwrite=true to replace it.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return {"source": _relative(source), "destination": _relative(destination)}
