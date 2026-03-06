#!/usr/bin/env -S uv run
# SPDX-License-Identifier: Apache-2.0
#
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "ruamel-yaml>=0.18.16",
# ]
# ///
"""
Scaffold a UDB architecture configuration YAML from a list of implemented extensions.

Reads all 245 UDB param definitions, evaluates which apply to the given extension
set (handling all definedBy patterns: simple extension, allOf, anyOf, param conditions),
then emits a YAML template with one commented-out key per applicable param grouped by
defining extension.

Usage:
  scaffold_config.py \\
    --name cv32e40p \\
    --description "CV32E40P RV32IMCF DUT" \\
    --extensions "I=2.1" "M=2.0" "Zca=1.0.0" "Zicsr=2.0" "Zifencei=2.0" \\
                 "Zicntr=2.0" "Sm=1.11.0" "Smhpm=1.11.0" "F=2.2.0" \\
    --udb-params external/riscv-unified-db/spec/std/isa/param \\
    --output config/cores/my_core/my_core.yaml
"""

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML, YAMLError


# ---------------------------------------------------------------------------
# YAML loading
# ---------------------------------------------------------------------------


def load_yaml(path: Path) -> dict[str, Any]:
    yaml = YAML(typ="safe", pure=True)
    try:
        data = yaml.load(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except YAMLError as e:
        print(f"Warning: Failed to parse {path.name}: {e}", file=sys.stderr)
        return {}


def load_udb_params(udb_dir: Path) -> dict[str, dict[str, Any]]:
    """Load all UDB parameter YAMLs, keyed by param name."""
    params: dict[str, dict[str, Any]] = {}
    for yaml_file in sorted(udb_dir.glob("*.yaml")):
        data = load_yaml(yaml_file)
        if data.get("kind") == "parameter":
            name = data.get("name")
            if name:
                params[name] = data
    return params


# ---------------------------------------------------------------------------
# Semver comparison
# ---------------------------------------------------------------------------


def _parse_ver(ver: str) -> tuple[int, ...]:
    """Parse '1.11.0' or '2.0' into an integer tuple."""
    parts = re.split(r"[.\-]", ver.strip())
    result = []
    for p in parts:
        try:
            result.append(int(p))
        except ValueError:
            break
    return tuple(result) if result else (0,)


def _semver_satisfies(impl_ver: str, constraint: str) -> bool:
    """
    Check whether impl_ver satisfies a constraint like '>= 1.0.0' or '= 2.1'.
    Constraints without operator default to '>='.
    """
    m = re.match(r"^(>=|<=|>|<|==?|~=)?\s*(.+)$", constraint.strip())
    if not m:
        return True
    op, ver_str = m.group(1) or ">=", m.group(2)
    impl = _parse_ver(impl_ver)
    req = _parse_ver(ver_str)
    # Pad to same length
    n = max(len(impl), len(req))
    impl = impl + (0,) * (n - len(impl))
    req = req + (0,) * (n - len(req))
    if op == ">=":
        return impl >= req
    if op == "<=":
        return impl <= req
    if op == ">":
        return impl > req
    if op == "<":
        return impl < req
    if op in ("=", "==", "~="):
        return impl == req
    return True


# ---------------------------------------------------------------------------
# definedBy evaluator
# ---------------------------------------------------------------------------


def _ext_satisfied(ext_node: Any, impls: dict[str, str]) -> bool:
    """
    Evaluate an 'extension' node against the implemented-extensions dict.
    Handles: {name: X}, {name: X, version: '...'}, {allOf: [...]}, {anyOf: [...]}.
    """
    if not isinstance(ext_node, dict):
        return False
    if "allOf" in ext_node:
        return all(_ext_satisfied(e, impls) for e in ext_node["allOf"])
    if "anyOf" in ext_node:
        return any(_ext_satisfied(e, impls) for e in ext_node["anyOf"])
    if "name" in ext_node:
        name = ext_node["name"]
        if name not in impls:
            return False
        ver_constraint = ext_node.get("version")
        if ver_constraint:
            return _semver_satisfies(impls[name], ver_constraint)
        return True
    return False


def evaluate_defined_by(defined_by: Any, impls: dict[str, str]) -> tuple[bool, bool, str]:
    """
    Evaluate a definedBy node against the implemented extension set.

    Returns:
        (applies, is_conditional, condition_note)
        - applies:        True if this param is applicable to this core
        - is_conditional: True if a param-value condition exists (cannot resolve at scaffold time)
        - condition_note: human-readable description of the unresolvable param condition
    """
    if not defined_by:
        return True, False, ""

    if not isinstance(defined_by, dict):
        return True, False, ""

    # Pattern 1: {extension: {...}}
    if "extension" in defined_by:
        ok = _ext_satisfied(defined_by["extension"], impls)
        return ok, False, ""

    # Pattern 2: {allOf: [...]} — may mix extension + param conditions
    if "allOf" in defined_by:
        items = defined_by["allOf"]
        ext_parts = [c for c in items if isinstance(c, dict) and "extension" in c]
        param_parts = [c for c in items if isinstance(c, dict) and "param" in c]

        # All extension conditions must be satisfied first
        if not all(_ext_satisfied(c["extension"], impls) for c in ext_parts):
            return False, False, ""

        # Param conditions cannot be resolved at scaffold time → mark conditional
        if param_parts:
            notes = []
            for pp in param_parts:
                pc = pp["param"]
                # param condition may itself be an allOf list
                if "allOf" in pc:
                    for sub in pc["allOf"]:
                        if isinstance(sub, dict) and "name" in sub:
                            pname = sub["name"]
                            val = sub.get("equal", sub.get("includes", sub.get("value", "?")))
                            notes.append(f"{pname}={val}")
                else:
                    pname = pc.get("name", "?")
                    val = pc.get("equal", pc.get("includes", pc.get("value", "?")))
                    notes.append(f"{pname}={val}")
            return True, True, "; ".join(notes)

        return True, False, ""

    # Pattern 3: {anyOf: [...]} at top level
    if "anyOf" in defined_by:
        ok = any(evaluate_defined_by(item, impls)[0] for item in defined_by["anyOf"])
        return ok, False, ""

    # Unknown pattern — include conservatively
    return True, False, ""


# ---------------------------------------------------------------------------
# Schema → human-readable type string
# ---------------------------------------------------------------------------


_ENUM_DISPLAY_MAX = 8  # Truncate long enums in comments to keep lines readable


def _fmt_enum(vals: list[Any]) -> str:
    """Format an enum list, truncating if very long."""
    strs = [str(v) for v in vals]
    if len(strs) <= _ENUM_DISPLAY_MAX:
        return " | ".join(strs)
    return " | ".join(strs[:_ENUM_DISPLAY_MAX]) + f" | … ({len(strs)} values)"


def schema_to_type_str(schema: Any) -> str:
    """Convert a JSON schema dict to a compact human-readable type annotation."""
    if not isinstance(schema, dict):
        return "?"

    # allOf wrapper (e.g. uint64 ref + constraint): look for a typed sub-schema
    if "allOf" in schema and "type" not in schema:
        for item in schema["allOf"]:
            if isinstance(item, dict) and "type" in item:
                return schema_to_type_str(item)
        return "?"

    t = schema.get("type")

    if t == "boolean":
        return "bool"

    if t == "integer":
        enum = schema.get("enum")
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if enum is not None:
            return f"int [{_fmt_enum(enum)}]"
        if minimum is not None or maximum is not None:
            lo = str(minimum) if minimum is not None else "?"
            hi = str(maximum) if maximum is not None else "?"
            return f"int [{lo}..{hi}]"
        return "int"

    if t == "string":
        enum = schema.get("enum")
        if enum is not None:
            return f"str [{_fmt_enum(enum)}]"
        return "str"

    if t == "array":
        items = schema.get("items", {})
        max_items = schema.get("maxItems", "")
        min_items = schema.get("minItems", "")
        size = str(max_items) if max_items else (str(min_items) if min_items else "")

        if isinstance(items, dict):
            sub = schema_to_type_str(items)
            return f"{sub}[{size}]" if size else f"{sub}[]"

        # items is a list (tuple schema) — use additionalItems type if present
        if isinstance(items, list):
            add = schema.get("additionalItems", {})
            if isinstance(add, dict) and "type" in add:
                sub = schema_to_type_str(add)
            else:
                # Pick first item that has a type
                sub = "?"
                for item in items:
                    if isinstance(item, dict) and "type" in item:
                        sub = schema_to_type_str(item)
                        break
            return f"{sub}[{size}]" if size else f"{sub}[]"

        return f"array[{size}]" if size else "array"

    # Bare enum without type — infer from values
    if "enum" in schema:
        vals = schema["enum"]
        if vals and all(isinstance(v, bool) for v in vals):
            return f"bool [{_fmt_enum(vals)}]"
        if vals and all(isinstance(v, int) for v in vals):
            return f"int [{_fmt_enum(vals)}]"
        if vals and all(isinstance(v, str) for v in vals):
            return f"str [{_fmt_enum(vals)}]"
        return f"[{_fmt_enum(vals)}]"

    # Fallback
    if "const" in schema:
        return type(schema["const"]).__name__

    return "?"


def build_comment(param_data: dict[str, Any]) -> str:
    """Build '  # <human name> — <type>' inline comment for a param line."""
    long_name = " ".join((param_data.get("long_name") or "").split()).strip()
    if not long_name or long_name.upper() == "TODO":
        desc = (param_data.get("description") or "").strip()
        long_name = re.split(r"[.\n]", desc)[0].strip()
        # Collapse whitespace in description fragment
        long_name = " ".join(long_name.split())

    type_str = schema_to_type_str(param_data.get("schema", {}))

    if long_name:
        return f"  # {long_name} — {type_str}"
    return f"  # {type_str}"


# ---------------------------------------------------------------------------
# Extract primary extension name(s) from definedBy (for group labels)
# ---------------------------------------------------------------------------


def get_primary_extensions(defined_by: Any) -> list[str]:
    """Return the extension name(s) that define this param, for use as group header."""
    names: list[str] = []

    def collect(node: Any) -> None:
        if not isinstance(node, dict):
            return
        if "extension" in node:
            ext = node["extension"]
            if isinstance(ext, dict):
                if "name" in ext:
                    names.append(ext["name"])
                elif "allOf" in ext:
                    for e in ext["allOf"]:
                        if isinstance(e, dict) and "name" in e:
                            names.append(e["name"])
                elif "anyOf" in ext:
                    for e in ext["anyOf"]:
                        if isinstance(e, dict) and "name" in e:
                            names.append(e["name"])
        for key in ("allOf", "anyOf"):
            if key in node:
                for item in node[key]:
                    collect(item)

    collect(defined_by)

    # Deduplicate preserving order
    seen: set[str] = set()
    result = []
    for n in names:
        if n not in seen:
            seen.add(n)
            result.append(n)
    return result


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_yaml(
    name: str,
    description: str,
    extensions: list[tuple[str, str]],
    grouped: list[tuple[str, bool, str, list[dict[str, Any]]]],
    schema_rel_path: str,
) -> str:
    """Render the output YAML as a string (no ruamel round-trip to preserve formatting)."""
    lines: list[str] = [
        f"# yaml-language-server: $schema={schema_rel_path}",
        "---",
        "$schema: config_schema.json#",
        "kind: architecture configuration",
        "type: fully configured",
        f"name: {name}",
        f"description: {description}",
        "implemented_extensions:",
    ]

    for ext_name, ext_ver in extensions:
        lines.append(f'  - {{ name: {ext_name}, version: "= {ext_ver}" }}')

    lines.append("")
    lines.append("params:")

    for group_label, is_conditional, cond_note, params in grouped:
        if is_conditional:
            lines.append(f"  # --- {group_label} (conditional: when {cond_note}) ---")
        else:
            lines.append(f"  # --- {group_label} ---")
        for p in params:
            comment = build_comment(p)
            lines.append(f"  {p['name']}:{comment}")
        lines.append("")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_extensions(raw: list[str]) -> list[tuple[str, str]]:
    """Parse 'NAME=VERSION' items; warn and skip malformed ones."""
    result = []
    for item in raw:
        if "=" in item:
            name, ver = item.split("=", 1)
            result.append((name.strip(), ver.strip()))
        else:
            print(
                f"Warning: Extension '{item}' has no version (expected NAME=VER); skipping",
                file=sys.stderr,
            )
    return result


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Scaffold a UDB architecture configuration YAML from an extension list.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--name", required=True, help="Core name (e.g. cv32e40p)")
    ap.add_argument("--description", required=True, help="Human-readable core description")
    ap.add_argument(
        "--extensions",
        nargs="+",
        required=True,
        metavar="NAME=VERSION",
        help="Implemented extensions, e.g.  I=2.1  M=2.0  Sm=1.11.0",
    )
    ap.add_argument(
        "--udb-params",
        default="external/riscv-unified-db/spec/std/isa/param",
        metavar="DIR",
        help="Path to UDB param YAML directory (default: external/riscv-unified-db/spec/std/isa/param)",
    )
    ap.add_argument(
        "--output",
        default="-",
        metavar="FILE",
        help="Output file path; '-' writes to stdout (default: -)",
    )
    ap.add_argument(
        "--schema-path",
        default="../../../../external/riscv-unified-db/spec/schemas/config_schema.json",
        metavar="REL_PATH",
        help="Relative $schema path for the yaml-language-server hint",
    )
    args = ap.parse_args()

    # Resolve UDB param dir (support invocation from repo root or script dir)
    udb_dir = Path(args.udb_params)
    if not udb_dir.exists():
        script_dir = Path(__file__).resolve().parent
        alt = script_dir.parent.parent / args.udb_params
        if alt.exists():
            udb_dir = alt
        else:
            print(f"Error: UDB param directory not found: {args.udb_params}", file=sys.stderr)
            sys.exit(2)

    # Parse extension list
    extensions = parse_extensions(args.extensions)
    if not extensions:
        print("Error: No valid extensions provided.", file=sys.stderr)
        sys.exit(2)
    impls: dict[str, str] = {ext_name: ver for ext_name, ver in extensions}
    impl_set = set(impls)

    # Load all UDB param definitions
    print(f"Loading UDB params from: {udb_dir}", file=sys.stderr)
    all_params = load_udb_params(udb_dir)
    print(f"Loaded {len(all_params)} param definitions", file=sys.stderr)

    # Evaluate applicability of every param
    unconditional: list[tuple[str, dict[str, Any]]] = []  # (group_key, param_data)
    conditional: list[tuple[str, str, dict[str, Any]]] = []  # (group_key, cond_note, param_data)
    skipped = 0

    for _param_name, param_data in sorted(all_params.items()):
        defined_by = param_data.get("definedBy")
        applies, is_cond, cond_note = evaluate_defined_by(defined_by, impls)
        if not applies:
            skipped += 1
            continue

        ext_names = get_primary_extensions(defined_by)
        # Filter group label to only mention extensions we actually implement
        relevant = [e for e in ext_names if e in impl_set]
        group_key = " + ".join(relevant) if relevant else (" + ".join(ext_names) if ext_names else "general")

        if is_cond:
            conditional.append((group_key, cond_note, param_data))
        else:
            unconditional.append((group_key, param_data))

    # Group by extension label
    unc_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for group_key, param_data in unconditional:
        unc_groups[group_key].append(param_data)

    cond_groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for group_key, cond_note, param_data in conditional:
        cond_groups[(group_key, cond_note)].append(param_data)

    # Sort groups in extension-declaration order
    ext_order = {ext_name: i for i, (ext_name, _) in enumerate(extensions)}

    def _group_sort_key(group_key: str) -> tuple[int, str]:
        parts = [g.strip() for g in group_key.split("+")]
        min_idx = min((ext_order.get(part.strip(), 999) for part in parts), default=999)
        return (min_idx, group_key)

    sorted_unc = sorted(unc_groups.items(), key=lambda kv: _group_sort_key(kv[0]))
    sorted_cond = sorted(cond_groups.items(), key=lambda kv: _group_sort_key(kv[0][0]))

    # Assemble grouped list for rendering
    grouped: list[tuple[str, bool, str, list[dict[str, Any]]]] = []
    for group_key, params in sorted_unc:
        grouped.append((group_key, False, "", params))
    for (group_key, cond_note), params in sorted_cond:
        grouped.append((group_key, True, cond_note, params))

    total = sum(len(ps) for *_, ps in grouped)
    cond_count = sum(len(ps) for _, is_c, _, ps in grouped if is_c)
    print(
        f"Applicable params: {total} ({total - cond_count} unconditional, {cond_count} conditional); "
        f"{skipped} skipped (extension not implemented)",
        file=sys.stderr,
    )

    # Render
    yaml_str = render_yaml(args.name, args.description, extensions, grouped, args.schema_path)

    if args.output == "-":
        sys.stdout.write(yaml_str)
    else:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(yaml_str, encoding="utf-8")
        print(f"Written to: {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
