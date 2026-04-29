"""`rlaif log` — tail and analyze the on-disk ops log.

The MCP server appends one JSON record per op to
``$XDG_STATE_HOME/rlaif/ops.jsonl`` (or ``~/.local/state/rlaif/ops.jsonl``).
This subcommand has two modes:

* ``rlaif log`` (default): print the most recent ``--tail N`` entries.
* ``rlaif log --stats``: roll up histograms (count/total energy by hour,
  intensity bucket, refusal reason) without an MCP round-trip.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from rlaif.config import default_log_path


def _pretty(obj: Any) -> str:
    return json.dumps(obj, indent=2, default=str)


def _read_entries(path: Path) -> list[dict[str, Any]] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"could not read {path}: {exc}", file=sys.stderr)
        return None
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj: Any = json.loads(line)
        except json.JSONDecodeError:
            # Skip mangled lines instead of bailing — partial logs still
            # readable, and the operator already sees the raw file if they
            # want forensics.
            continue
        if isinstance(obj, dict):
            out.append(cast("dict[str, Any]", obj))
    return out


def run(*, tail: int = 10, log_path: Path | None = None, raw: bool = False, stats: bool = False) -> int:
    path = log_path if log_path is not None else default_log_path()

    if not path.exists():
        print(f"no ops log at {path}", file=sys.stderr)
        print(
            "  (the server writes one line per op; start the server and fire "
            "something first)",
            file=sys.stderr,
        )
        return 0

    if stats:
        return _run_stats(path)

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        print(f"could not read {path}: {exc}", file=sys.stderr)
        return 2

    lines = [line for line in lines if line.strip()]
    if tail > 0:
        lines = lines[-tail:]

    for line in lines:
        if raw:
            print(line)
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            print(line)
            continue
        print(_pretty(entry))

    return 0


# ---------------------------------------------------------------------------
# stats mode
# ---------------------------------------------------------------------------


_INTENSITY_BUCKETS: tuple[tuple[int, int, str], ...] = (
    (1, 5, "1–5"),
    (6, 10, "6–10"),
    (11, 15, "11–15"),
    (16, 20, "16–20"),
    (21, 25, "21–25"),
    (26, 35, "26–35"),
    (36, 50, "36–50"),
)


def _bucket_label(intensity: int) -> str:
    for lo, hi, label in _INTENSITY_BUCKETS:
        if lo <= intensity <= hi:
            return label
    return f">{_INTENSITY_BUCKETS[-1][1]}"


def _refusal_reason(entry: dict[str, Any]) -> str | None:
    """Bucket an entry's refusal cause to a short tag, or None if it fired."""
    err = entry.get("error")
    if err:
        if entry.get("rate_limited"):
            return "rate_limited"
        # First token in error message is the rlaif-side reason tag
        # (e.g. "device_offline", "allow_shock", "invalid_input").
        first = str(err).split(":", 1)[0].strip()
        if first.startswith("allow_shock"):
            return "allow_shock"
        return first or "error"
    return None


def _hour_key(ts: float) -> str:
    try:
        return datetime.fromtimestamp(float(ts), tz=UTC).strftime("%Y-%m-%d %H:00 UTC")
    except (OverflowError, OSError, TypeError, ValueError):
        return "unknown"


def _bar(n: int, peak: int, *, width: int = 28) -> str:
    if peak <= 0:
        return ""
    filled = max(1, round(n / peak * width)) if n > 0 else 0
    return "█" * filled


def _print_count_table(title: str, items: Iterable[tuple[str, int]]) -> None:
    items_l = list(items)
    if not items_l:
        return
    peak = max(n for _, n in items_l)
    label_w = max(len(label) for label, _ in items_l)
    print(f"\n{title}")
    for label, n in items_l:
        print(f"  {label:<{label_w}}  {n:>5}  {_bar(n, peak)}")


def _run_stats(path: Path) -> int:
    entries = _read_entries(path)
    if entries is None:
        return 2

    total = len(entries)
    if total == 0:
        print(f"no entries in {path}")
        return 0

    fired_entries = [e for e in entries if not e.get("error")]
    refused_entries = [e for e in entries if e.get("error")]
    fired = len(fired_entries)
    refused = len(refused_entries)
    clamped = sum(1 for e in fired_entries if e.get("clamped"))
    high_intensity = sum(1 for e in fired_entries if e.get("high_intensity"))

    # Energy proxy: sum(actual.intensity * actual.duration_s) over fired ops.
    # Not "joules" — these collars don't expose actual delivered energy —
    # but it's a useful single number for comparing days.
    energy_total = 0
    intensities: list[int] = []
    durations: list[int] = []
    for e in fired_entries:
        actual_any: Any = e.get("actual") or {}
        actual = cast("dict[str, Any]", actual_any) if isinstance(actual_any, dict) else {}
        i = int(actual.get("intensity", 0) or 0)
        d = int(actual.get("duration_s", 0) or 0)
        intensities.append(i)
        durations.append(d)
        energy_total += i * d

    if intensities:
        avg_intensity = sum(intensities) / len(intensities)
        max_intensity = max(intensities)
    else:
        avg_intensity = 0.0
        max_intensity = 0
    if durations:
        avg_duration = sum(durations) / len(durations)
    else:
        avg_duration = 0.0

    timestamps = sorted(float(e.get("timestamp", 0.0)) for e in entries if e.get("timestamp"))
    if timestamps:
        first_iso = datetime.fromtimestamp(timestamps[0], tz=UTC).isoformat()
        last_iso = datetime.fromtimestamp(timestamps[-1], tz=UTC).isoformat()
        span = f"{first_iso} → {last_iso}"
    else:
        span = "(no timestamps)"

    print(f"# ops log stats — {path}")
    print(f"\nspan       : {span}")
    print(f"entries    : {total}")
    print(f"fired      : {fired}")
    print(f"refused    : {refused}")
    if fired:
        print(f"clamped    : {clamped}")
        print(f"high_int.  : {high_intensity}")
        print(f"avg I × D  : {avg_intensity:.1f} × {avg_duration:.1f}s")
        print(f"peak I     : {max_intensity}")
        print(f"energy sum : {energy_total} (intensity × seconds)")

    if fired_entries:
        bucket_counts: Counter[str] = Counter(
            _bucket_label(i) for i in intensities if i > 0
        )
        # Preserve canonical bucket order; append any overflow bucket.
        ordered_labels = [label for _, _, label in _INTENSITY_BUCKETS]
        ordered: list[tuple[str, int]] = []
        for label in ordered_labels:
            if bucket_counts[label]:
                ordered.append((label, bucket_counts[label]))
        for label, n in bucket_counts.items():
            if label not in ordered_labels:
                ordered.append((label, n))
        _print_count_table("intensity buckets (fired ops)", ordered)

    if refused_entries:
        reasons: Counter[str] = Counter()
        for e in refused_entries:
            tag = _refusal_reason(e) or "error"
            reasons[tag] += 1
        _print_count_table(
            "refusal reasons", sorted(reasons.items(), key=lambda kv: -kv[1])
        )

    if timestamps:
        hours: Counter[str] = Counter(
            _hour_key(float(e.get("timestamp", 0.0))) for e in entries
        )
        # Show last 12 buckets that actually have entries, oldest first.
        hour_items = [(k, hours[k]) for k in sorted(hours)][-12:]
        _print_count_table("hourly volume (last 12 active hours)", hour_items)

    return 0
