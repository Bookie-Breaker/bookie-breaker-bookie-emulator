"""Export the FastAPI-generated OpenAPI spec as deterministic YAML (ADR-021).

The generated spec is the source of truth for this service's API; the
exported artifact is committed to bookie-breaker-docs/api-contracts/openapi/.

FastAPI emits OpenAPI 3.1, but the ADR-009 codegen pipeline (oapi-codegen
for Go clients) only accepts 3.0, so the export downgrades the handful of
3.1 constructs FastAPI produces (numeric exclusive bounds, null-type anyOf
members, const) to their 3.0 equivalents.

Usage:
    uv run python scripts/export_openapi.py [--out PATH]
"""

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bookie_emulator.config import Settings  # noqa: E402
from bookie_emulator.main import create_app  # noqa: E402


def _downgrade_schema(node: Any) -> Any:
    """Convert FastAPI's OpenAPI 3.1 constructs to 3.0 equivalents in place."""
    if isinstance(node, list):
        return [_downgrade_schema(item) for item in node]
    if not isinstance(node, dict):
        return node

    # 3.1 numeric exclusive bounds -> 3.0 boolean form
    if isinstance(node.get("exclusiveMinimum"), int | float):
        node["minimum"] = node.pop("exclusiveMinimum")
        node["exclusiveMinimum"] = True
    if isinstance(node.get("exclusiveMaximum"), int | float):
        node["maximum"] = node.pop("exclusiveMaximum")
        node["exclusiveMaximum"] = True

    # anyOf [X, {type: null}] -> nullable X
    any_of = node.get("anyOf")
    if isinstance(any_of, list) and {"type": "null"} in any_of:
        remaining = [s for s in any_of if s != {"type": "null"}]
        node.pop("anyOf")
        if len(remaining) == 1 and isinstance(remaining[0], dict):
            node.update(remaining[0])
        elif remaining:
            node["anyOf"] = remaining
        node["nullable"] = True

    # const -> single-value enum
    if "const" in node:
        node["enum"] = [node.pop("const")]

    result = {key: _downgrade_schema(value) for key, value in node.items()}

    # 3.0 forbids $ref siblings; wrap them in allOf
    if "$ref" in result and len(result) > 1:
        ref = result.pop("$ref")
        return {"allOf": [{"$ref": ref}], **result}
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=None, help="Output file path (default: stdout)")
    args = parser.parse_args()

    app = create_app(Settings())
    spec: dict[str, Any] = _downgrade_schema(app.openapi())
    spec["openapi"] = "3.0.3"
    rendered = yaml.safe_dump(spec, sort_keys=True, allow_unicode=True, default_flow_style=False)

    if args.out is None:
        sys.stdout.write(rendered)
    else:
        args.out.write_text(rendered)
        sys.stderr.write(f"wrote {args.out}\n")


if __name__ == "__main__":
    main()
