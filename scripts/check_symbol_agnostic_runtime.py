from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOTS = [Path("harvester.py"), Path("scripts"), Path(".github/workflows")]
DEFAULT_PROOF_SYMBOLS = ["BZ", "QFIN", "ACAD", "TSLA", "JHG", "INVA", "CPRX", "SPG", "AUPH", "INCY"]
TEXT_EXTENSIONS = {".py", ".json", ".yml", ".yaml"}
SELF = Path("scripts/check_symbol_agnostic_runtime.py")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def configured_symbols() -> list[str]:
    raw = os.environ.get("SYMBOL_AGNOSTIC_FORBIDDEN_SYMBOLS") or ",".join(DEFAULT_PROOF_SYMBOLS)
    return [part.strip().upper() for part in raw.split(",") if part.strip()]


def walk(target: Path) -> list[Path]:
    if not target.exists():
        return []
    if target.is_file():
        return [target]
    out: list[Path] = []
    for child in target.iterdir():
        if child.is_dir():
            out.extend(walk(child))
        else:
            out.append(child)
    return out


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def main() -> int:
    forbidden_symbols = configured_symbols()
    pattern = re.compile(r"\b(" + "|".join(re.escape(symbol) for symbol in forbidden_symbols) + r")\b") if forbidden_symbols else None
    checked_files: list[str] = []
    findings: list[dict[str, Any]] = []

    if pattern:
        for root in ROOTS:
            for file_path in walk(root):
                normalized = Path(file_path).as_posix()
                if Path(normalized) == SELF:
                    continue
                if file_path.suffix not in TEXT_EXTENSIONS:
                    continue
                checked_files.append(normalized)
                text = file_path.read_text(encoding="utf-8")
                for line_number, line in enumerate(text.splitlines(), start=1):
                    matches = sorted(set(pattern.findall(line)))
                    if matches:
                        findings.append(
                            {
                                "filePath": normalized,
                                "line": line_number,
                                "symbols": matches,
                                "text": line.strip()[:240],
                            }
                        )

    report = {
        "generatedAt": utc_now(),
        "overall": "pass" if not findings else "fail",
        "scope": "harvester_runtime_code_and_workflows_docs_and_testdata_excluded",
        "forbiddenSymbols": forbidden_symbols,
        "checkedRoots": [root.as_posix() for root in ROOTS],
        "checkedFileCount": len(checked_files),
        "findings": findings,
        "invariant": "Harvester universe and failed-ticker handling must be data-driven, not hard-coded to current proof/sample symbols.",
    }

    write_json(Path("state/symbol-agnostic-runtime-check.json"), report)
    md_lines = [
        "## Symbol-Agnostic Runtime Check",
        f"- generatedAt: `{report['generatedAt']}`",
        f"- overall: `{report['overall'].upper()}`",
        f"- scope: `{report['scope']}`",
        f"- forbiddenSymbols: `{','.join(forbidden_symbols) or 'N/A'}`",
        f"- checkedFiles: `{len(checked_files)}`",
        f"- findings: `{len(findings)}`",
        "- invariant: harvester runtime must not hard-code current proof/sample symbols.",
    ]
    md_lines.extend(
        f"  - finding {row['filePath']}:{row['line']} symbols={','.join(row['symbols'])} text={row['text']}"
        for row in findings
    )
    Path("state/symbol-agnostic-runtime-check.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    print(f"[SYMBOL_AGNOSTIC_RUNTIME_CHECK] overall={report['overall']} findings={len(findings)}")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
