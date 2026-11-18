"""Add response_model=None to DELETE endpoints that use HTTP 204."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app" / "api"


def patch_text(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        full = match.group(0)
        if "response_model" in full:
            return full
        # Insert response_model=None before the closing paren of decorator args.
        return full[:-1] + ", response_model=None)"

    return re.sub(
        r"@router\.delete\([^)]*status_code=(?:status\.HTTP_204_NO_CONTENT|204)[^)]*\)",
        repl,
        text,
    )


def main() -> None:
    for path in sorted(ROOT.glob("*.py")):
        original = path.read_text(encoding="utf-8")
        updated = patch_text(original)
        if updated != original:
            path.write_text(updated, encoding="utf-8")
            print(f"updated {path.name}")
        else:
            for idx, line in enumerate(original.splitlines(), 1):
                if "HTTP_204" in line or "status_code=204" in line:
                    print(f"{path.name}:{idx}: {line.strip()}")


if __name__ == "__main__":
    main()
