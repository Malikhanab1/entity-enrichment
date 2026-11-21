"""
Crash-safe checkpoint/resume support.

Results are appended to a JSONL file as they complete (one JSON object per
processed row). On restart, load_checkpoint() reconstructs a dict of
already-processed row indices so main() can skip them entirely.

JSONL is used instead of re-writing the whole Excel file every N rows
because appends are cheap and atomic-ish (a partial last line just gets
skipped on parse), which keeps us safe against a mid-write crash.
"""
from __future__ import annotations

import json
import os
import threading

from logging_setup import logger


class CheckpointManager:
    def __init__(self, checkpoint_file: str):
        self.checkpoint_file = checkpoint_file
        self._lock = threading.Lock()
        self._buffer: list[dict] = []

    def load_checkpoint(self) -> dict[int, dict]:
        """Read existing checkpoint file and return {row_index: result_dict}."""
        results: dict[int, dict] = {}
        if not os.path.exists(self.checkpoint_file):
            return results

        with open(self.checkpoint_file, "r", encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    results[record["row_index"]] = record
                except (json.JSONDecodeError, KeyError):
                    logger.warning(
                        f"Skipping corrupt checkpoint line {line_num} "
                        f"(likely from an interrupted write)."
                    )
                    continue

        logger.info(
            f"Resuming from checkpoint: {len(results)} rows already processed."
        )
        return results

    def append_result(self, result: dict) -> None:
        """Buffer a single completed row's result (thread/async-safe)."""
        with self._lock:
            self._buffer.append(result)

    def flush(self) -> None:
        """Write buffered results to disk and clear the buffer."""
        with self._lock:
            if not self._buffer:
                return
            with open(self.checkpoint_file, "a", encoding="utf-8") as f:
                for record in self._buffer:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._buffer.clear()

    def buffered_count(self) -> int:
        with self._lock:
            return len(self._buffer)
