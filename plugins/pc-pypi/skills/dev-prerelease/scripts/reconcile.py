#!/usr/bin/env python3
"""Deterministic code <-> feature-table reconciliation — the no-model layer.

Verifies that a library's feature documentation does not *lie* about its code, using only
determinism. The model layer that judges semantic DIVERGE (does the code really do what the prose
claims) is orchestrated separately by the skill body — this module never calls a model or the
network.

Two things it produces:

1. **Deterministic findings** (`scan`) — parses the feature table's ``| Feature | Module | Status |``
   rows, extracts the backtick-quoted Python symbols in each Feature cell (SQL ALL_CAPS names filtered
   out), and checks each against its module's source:
     - ``stable`` row, symbol **absent** from the module -> LIE: claimed stable, not implemented.
     - ``planned`` row, symbol **present** -> LIE: built but still marked planned.
     - module column names a file that does not exist -> structural divergence.
     - ``stable`` row, all symbols present -> *residue*: a model must judge the prose (determinism
       can refute a stable row but never confirm it).

2. **A brief** (`build_selective_brief`) — the document plus a compact structural index of the whole
   package plus the full source of only the referenced modules — the bounded input the skill body
   hands to the model layer.

3. **A MANQUANT post-filter** (`drop_already_documented`) — applied by the skill body to the model's
   *output*: a "capability X is missing from the table" finding is dropped when X's module already
   has a row, or when X is named inside another row's Feature cell. Kills the recurring
   already-documented false positive without being able to hide a real omission.

The feature-doc path and table header are **arguments**, never hardcoded — the flow generalises to
any PyPI library with a capability table.

`not_applicable` gates the run: an absent or unreadable feature table and an unresolvable package
are both "cannot refute", reported as *not applicable* (exit 2) — never as a clean pass.

Usage:
    reconcile.py --repo PATH --features-doc REL_PATH   # deterministic scan, report to stdout
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

_BACKTICK = re.compile(r"`([^`]+)`")
_ALL_CAPS = re.compile(
    r"[A-Z0-9_]+"
)  # SQL constant / function name -> not a Python symbol
_CAPWORD = re.compile(r"[A-Z][A-Za-z0-9]*")  # class-shaped
_SNAKE = re.compile(r"[a-z_][a-z0-9_]*")  # function/method-shaped
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")

DEFAULT_HEADER = ("feature", "module", "status")


@dataclass(frozen=True)
class Row:
    """One data row of a recognised feature table, status normalised to stable/planned/other."""

    feature: str
    module: str
    status: str


@dataclass(frozen=True)
class Finding:
    """One deterministic divergence between a table row and the code it points at."""

    kind: str  # "stable-absent" | "planned-present" | "module-missing"
    module: str
    symbol: str
    detail: str


@dataclass(frozen=True)
class ScanResult:
    """The deterministic pass over a whole feature table: refuted rows, and rows left for a model."""

    findings: list[Finding]
    residue: list[Row]


# --- table parsing -------------------------------------------------------------------------------


def parse_feature_rows(
    md: str, header: tuple[str, str, str] = DEFAULT_HEADER
) -> list[Row]:
    """Extract data rows from every 3-column table in `md` whose header matches `header`.

    Abstains on any other table shape: a feature doc is not standardised across libraries, so a row
    is only read once its table's header has been recognised. An unknown format is not "nothing to
    report" — it is "determinism cannot refute this", and the row falls to the model layer.

    Args:
        md: the feature document's markdown.
        header: the lowercased 3-tuple the table's header row must equal to be read.

    Returns:
        The recognised data rows, in document order.
    """
    rows: list[Row] = []
    in_table = False
    for raw in md.splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            in_table = False  # any non-table line closes the current table
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) != 3:
            in_table = False
            continue
        if tuple(c.lower() for c in cells) == header:
            in_table = True
            continue
        if not in_table:
            continue  # a 3-column table we do not recognise -> not ours to judge
        feature, module, status = cells
        if set(feature) <= {"-", ":", " "}:  # separator row
            continue
        module = module.strip("`").strip()
        low = status.lower()
        status_norm = (
            "stable"
            if low.startswith("stable")
            else ("planned" if low.startswith("planned") else low)
        )
        rows.append(Row(feature=feature, module=module, status=status_norm))
    return rows


def classify_symbol(tok: str) -> tuple[str, str] | None:
    """Map a backtick tok to ``("class"|"def", name)`` to check, or None to skip.

    Args:
        tok: the raw text between a pair of backticks in a Feature cell.

    Returns:
        The kind and name to presence-check, or None for SQL / ambiguous / dotted-SQL toks.
    """
    tok = tok.strip()
    if "." in tok:
        parts = tok.split(".")
        if all(_ALL_CAPS.fullmatch(p) for p in parts):
            return None  # SNOWFLAKE.CORTEX.COUNT_TOKENS -> SQL
        if _CAPWORD.fullmatch(parts[0]) and _SNAKE.fullmatch(parts[-1]):
            return ("def", parts[-1])  # Class.method -> method presence
        return None  # ambiguous / dotted-SQL
    if _ALL_CAPS.fullmatch(tok):
        return None  # AI_EMBED, TO_FILE -> SQL
    if _CAPWORD.fullmatch(tok):
        return ("class", tok)  # PerUserQuota -> class
    if _SNAKE.fullmatch(tok):
        return ("def", tok)  # get_ai, purge_expired -> function
    return None


def extract_symbols(feature_cell: str) -> list[tuple[str, str]]:
    """All checkable ``(kind, name)`` Python symbols named in a Feature cell's backticks.

    Args:
        feature_cell: the text of a table row's Feature column.

    Returns:
        One ``(kind, name)`` pair per backtick token that classifies as a Python symbol.
    """
    out: list[tuple[str, str]] = []
    for tok in _BACKTICK.findall(feature_cell):
        classified = classify_symbol(tok)
        if classified is not None:
            out.append(classified)
    return out


def symbol_present(src: str, kind: str, name: str) -> bool:
    """True if ``def name`` (async allowed) or ``class name`` appears at any indent in `src`.

    Args:
        src: a module's source text.
        kind: ``"class"`` or ``"def"``.
        name: the identifier to look for.

    Returns:
        Whether a matching definition line exists.
    """
    if kind == "class":
        pat = rf"^\s*class\s+{re.escape(name)}\b"
    else:
        pat = rf"^\s*(?:async\s+)?def\s+{re.escape(name)}\b"
    return re.search(pat, src, re.MULTILINE) is not None


def check_row(row: Row, module_src: str | None) -> tuple[list[Finding], bool]:
    """Check one row against its module source.

    Args:
        row: the table row to check.
        module_src: the module's source text, or None when the module file is missing.

    Returns:
        ``(findings, has_residue)`` — deterministic divergences for this row, and whether the row
        is a still-open stable claim a model must judge.
    """
    findings: list[Finding] = []
    if module_src is None:
        findings.append(
            Finding(
                "module-missing",
                row.module,
                "",
                f"module `{row.module}` has no source file",
            )
        )
        return findings, False
    for kind, name in extract_symbols(row.feature):
        present = symbol_present(module_src, kind, name)
        if row.status == "stable" and not present:
            findings.append(
                Finding(
                    "stable-absent",
                    row.module,
                    name,
                    f"`{name}` marked stable but absent from `{row.module}`",
                )
            )
        elif row.status == "planned" and present:
            findings.append(
                Finding(
                    "planned-present",
                    row.module,
                    name,
                    f"`{name}` marked planned but already exists in `{row.module}`",
                )
            )
    has_residue = row.status == "stable" and not any(
        f.kind == "stable-absent" for f in findings
    )
    return findings, has_residue


# --- package introspection ---------------------------------------------------------------------


def package_dir(repo: Path) -> Path | None:
    """The single importable package under ``src/`` (the dir carrying ``__init__.py``).

    Args:
        repo: the library checkout root.

    Returns:
        The package directory, or None when ``src/`` is absent or holds more than one package.
    """
    src = repo / "src"
    if not src.is_dir():
        return None
    pkgs = [
        d for d in sorted(src.iterdir()) if d.is_dir() and (d / "__init__.py").exists()
    ]
    return pkgs[0] if len(pkgs) == 1 else None


def collect_sources(repo: Path) -> dict[str, str]:
    """Every ``.py`` under the repo's package, keyed by repo-relative POSIX path (recursive).

    Args:
        repo: the library checkout root.

    Returns:
        A path -> source-text map, empty when no single package resolves.
    """
    pkg = package_dir(repo)
    if pkg is None:
        return {}
    return {
        p.relative_to(repo).as_posix(): p.read_text(encoding="utf-8", errors="ignore")
        for p in sorted(pkg.rglob("*.py"))
        if p.name != "__init__.py" or p.stat().st_size > 0
    }


def defined_symbols(src: str) -> list[str]:
    """Top-level classes/functions plus methods defined in a source, via AST (best-effort).

    Args:
        src: a module's source text.

    Returns:
        The defined names, or an empty list when the source does not parse.
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            out.append(node.name)
    return out


def build_structural_index(sources: dict[str, str]) -> str:
    """One compact line per file: ``path: Sym1, Sym2, …`` — the whole-package presence map.

    Args:
        sources: the path -> source map from `collect_sources`.

    Returns:
        A newline-joined index, one line per file.
    """
    lines = []
    for path in sorted(sources):
        syms = defined_symbols(sources[path])
        lines.append(f"{path}: {', '.join(syms)}" if syms else path)
    return "\n".join(lines)


def _module_stem(path: str) -> str:
    """``resources/account/api_integration.py`` -> ``api_integration``.

    Args:
        path: a repo-relative source path.

    Returns:
        The bare module stem.
    """
    return path.rsplit("/", 1)[-1][:-3]


def referenced_files(doc_md: str, sources: dict[str, str]) -> list[str]:
    """The source files a document points at — generous on purpose (over-including is safe).

    Args:
        doc_md: the feature document's markdown.
        sources: the path -> source map from `collect_sources`.

    Returns:
        The sorted repo-relative paths of referenced modules.
    """
    stem_to_file = {_module_stem(p): p for p in sources}
    symbol_to_files: dict[str, set[str]] = {}
    for path, src in sources.items():
        for sym in defined_symbols(src):
            symbol_to_files.setdefault(sym, set()).add(path)

    hit: set[str] = set()
    for tok in _IDENT.findall(doc_md):
        if tok in stem_to_file:
            hit.add(stem_to_file[tok])
        if tok and tok[0].isupper() and tok in symbol_to_files:
            hit.update(symbol_to_files[tok])
    for raw in _BACKTICK.findall(doc_md):
        classified = classify_symbol(raw)
        if classified is None:
            continue
        name = classified[1]
        if name in symbol_to_files:
            hit.update(symbol_to_files[name])
        stem = raw.strip().split(".")[0]
        if stem in stem_to_file:
            hit.add(stem_to_file[stem])
    return sorted(hit)


def build_selective_brief(doc_rel: str, doc_md: str, sources: dict[str, str]) -> str:
    """Document + full-package structural index + full source of only the referenced modules.

    Args:
        doc_rel: the feature document's repo-relative path, for the header.
        doc_md: the feature document's markdown.
        sources: the path -> source map from `collect_sources`.

    Returns:
        The bounded brief text handed to the model layer.
    """
    referenced = referenced_files(doc_md, sources)
    parts = [
        f"--- DOCUMENT À RÉCONCILIER ({doc_rel}) ---\n{doc_md}",
        "--- INDEX DU PACKAGE (tous les fichiers, symboles publics — pour repérer le code "
        "implémenté mais ABSENT du document) ---\n" + build_structural_index(sources),
    ]
    if referenced:
        parts.append(
            "--- CODE DES MODULES CITÉS PAR LE DOCUMENT (source complète — pour juger si le code "
            "fait ce que le document affirme) ---"
        )
        for path in referenced:
            parts.append(f"--- CODE: {path} ---\n```python\n{sources[path]}\n```")
    return "\n\n".join(parts)


# --- MANQUANT post-filter ----------------------------------------------------------------------


def _documented_modules(table_md: str, header: tuple[str, str, str]) -> set[str]:
    """The set of module names that already have a row in the recognised feature table.

    Args:
        table_md: the feature document's markdown.
        header: the table header tuple to recognise.

    Returns:
        Lowercased module names and their bare stems.
    """
    out: set[str] = set()
    for row in parse_feature_rows(table_md, header):
        mod = row.module.strip().lower()
        if mod:
            out.add(mod)
            out.add(mod.rsplit("/", 1)[-1].removesuffix(".py"))
    return out


def drop_already_documented(
    missing: list[str],
    table_md: str,
    header: tuple[str, str, str] = DEFAULT_HEADER,
) -> list[str]:
    """Drop every MANQUANT finding whose capability is already in the table.

    A capability is "already documented" when its own module has a table row, or when the
    capability name appears verbatim inside any row's Feature cell (the table describes features,
    not signatures — a method named inside a broader feature's description is documented).

    Module-level dropping is deliberate: a per-symbol row is not what the table is for, so a
    MANQUANT for any capability of an already-listed module is discarded by design. This cannot
    silence the class it targets (a demand for a dedicated row of something the table names), and
    it never touches a MANQUANT for a module with no row at all.

    Args:
        missing: the model's MANQUANT findings, each ``"module: capability"`` or just a capability
            name.
        table_md: the feature document's markdown.
        header: the table header tuple to recognise.

    Returns:
        The findings that survive the filter, in input order.
    """
    documented_mods = _documented_modules(table_md, header)
    rows = parse_feature_rows(table_md, header)
    feature_blob = " ".join(r.feature for r in rows)
    kept: list[str] = []
    for finding in missing:
        module, _, capability = finding.partition(":")
        module = module.strip().lower()
        capability = (capability or module).strip()
        if module and module in documented_mods:
            continue
        if capability and re.search(rf"`{re.escape(capability)}`", feature_blob):
            continue
        kept.append(finding)
    return kept


# --- scan + CLI --------------------------------------------------------------------------------


def scan(
    repo: Path, features_rel: str, header: tuple[str, str, str] = DEFAULT_HEADER
) -> ScanResult:
    """Run the deterministic grep pre-pass over a whole feature table.

    Args:
        repo: the library checkout root.
        features_rel: the feature document's repo-relative path.
        header: the table header tuple to recognise.

    Returns:
        The deterministic findings and the residue rows a model must judge.
    """
    pkg = package_dir(repo)
    md = (repo / features_rel).read_text(encoding="utf-8")
    findings: list[Finding] = []
    residue: list[Row] = []
    src_cache: dict[str, str | None] = {}
    for row in parse_feature_rows(md, header):
        if row.module not in src_cache:
            f = (pkg / f"{row.module}.py") if pkg else None
            src_cache[row.module] = (
                f.read_text(encoding="utf-8") if f and f.exists() else None
            )
        row_findings, has_residue = check_row(row, src_cache[row.module])
        findings.extend(row_findings)
        if has_residue:
            residue.append(row)
    return ScanResult(findings, residue)


def not_applicable(
    repo: Path, features_rel: str, header: tuple[str, str, str] = DEFAULT_HEADER
) -> str | None:
    """Why the deterministic reconcile does not apply to this repo, or None when it does.

    Every "not applicable" case is a "cannot refute", never a "clean": an absent or unreadable
    feature table and an unresolvable package both mean the deterministic layer has nothing to
    check — reporting "no divergences" there would be a false green.

    Args:
        repo: the library checkout root.
        features_rel: the feature document's repo-relative path.
        header: the table header tuple to recognise.

    Returns:
        A short reason string, or None when the reconcile can run.
    """
    doc = repo / features_rel
    if not doc.exists():
        return f"no {features_rel} in {repo}"
    if not parse_feature_rows(doc.read_text(encoding="utf-8"), header):
        return f"{features_rel} has no recognised | {' | '.join(header)} | table"
    if package_dir(repo) is None:
        return "no single importable package under src/"
    return None


def render(repo: Path, result: ScanResult) -> str:
    """Render a scan result as a plain-text report.

    Args:
        repo: the library checkout root, for the title.
        result: the scan output.

    Returns:
        The report text.
    """
    lines = [f"# Reconcile (deterministic) — {repo.name}", ""]
    if result.findings:
        lines.append(
            f"## Deterministic divergences ({len(result.findings)}) — fix these"
        )
        lines.extend(f"- **{f.kind}** · {f.detail}" for f in result.findings)
    else:
        lines.append("## Deterministic divergences: none")
    lines.append("")
    lines.append(
        f"## Model residue ({len(result.residue)} rows) — present, semantic correctness unverified"
    )
    lines.extend(f"- `{r.module}` · {r.feature}" for r in result.residue)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for the deterministic scan.

    Args:
        argv: argument vector, or None to read ``sys.argv``.

    Returns:
        int: 0 clean, 1 when deterministic divergences were found, 2 on a usage error.
    """
    ap = argparse.ArgumentParser(
        description="Deterministic code<->feature-table reconciliation."
    )
    ap.add_argument("--repo", required=True, type=Path)
    ap.add_argument(
        "--features-doc",
        required=True,
        help="feature document, repo-relative (e.g. docs/features.md)",
    )
    args = ap.parse_args(argv)

    repo: Path = args.repo
    reason = not_applicable(repo, args.features_doc)
    if reason is not None:
        print(f"[reconcile] not applicable — {reason}", file=sys.stderr)
        return 2

    result = scan(repo, args.features_doc)
    print(render(repo, result))
    return 1 if result.findings else 0


if __name__ == "__main__":
    sys.exit(main())
