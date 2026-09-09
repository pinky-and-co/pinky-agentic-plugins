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
from typing import NamedTuple

_BACKTICK = re.compile(r"`([^`]+)`")
_ALL_CAPS = re.compile(
    r"[A-Z0-9_]+"
)  # SQL constant / function name -> not a Python symbol
_CAPWORD = re.compile(r"[A-Z][A-Za-z0-9]*")  # class-shaped
_SNAKE = re.compile(r"[a-z_][a-z0-9_]*")  # function/method-shaped
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")


class Header(NamedTuple):
    """The three lowercased column names a feature table's header row must match."""

    feature: str
    module: str
    status: str


DEFAULT_HEADER = Header("feature", "module", "status")


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


class RowCheck(NamedTuple):
    """One row's deterministic result: its divergences, and whether a stable claim is still open."""

    findings: list[Finding]
    has_residue: bool


# --- table parsing -------------------------------------------------------------------------------

# noms de colonne (lowercased) qui désignent le module de code, quel que soit l'ordre
_MODULE_COL_NAMES = {"module", "object", "resource", "component", "class", "file"}
# marqueurs "livré" au-delà du littéral "stable" — ✓/✔ suffisent, "yes"/"done" sont ambigus
_DELIVERED_MARKS = ("✓", "✔")


def _resolve_columns(header: Header) -> tuple[int, int, int]:
    """Map a 3-column header to ``(feature_i, module_i, status_i)`` by column name, not position.

    ``| Feature | Module | Status |`` and ``| Object | Status | Notes |`` both parse — the second
    has no free-text feature column, so its Notes cell fills that role.

    Args:
        header: the table's 3 lowercased column names.

    Returns:
        The cell index for the feature text, the module name, and the status.
    """
    status_i = next(
        (i for i, h in enumerate(header) if "status" in h or "state" in h), 2
    )
    module_i = next(
        (
            i for i, h in enumerate(header) if h == "module"
        ),  # exact wins over the broad set
        next(
            (i for i, h in enumerate(header) if h in _MODULE_COL_NAMES),
            0 if status_i == 1 else 1,
        ),
    )
    feature_i = next((i for i in range(3) if i not in {status_i, module_i}), 0)
    return feature_i, module_i, status_i


def _norm_status(raw: str) -> str:
    """Normalise a status cell to ``stable`` / ``planned`` / the verbatim lowercase otherwise.

    Args:
        raw: the status cell text.

    Returns:
        The normalised status.
    """
    low = raw.strip().lower()
    if low.startswith("planned"):
        return "planned"
    if low.startswith("stable") or any(m in raw for m in _DELIVERED_MARKS):
        return "stable"
    return low


def parse_feature_rows(md: str, header: Header = DEFAULT_HEADER) -> list[Row]:
    """Extract data rows from every 3-column table in `md` whose header matches `header`.

    Abstains on any other table shape: a feature doc is not standardised across libraries, so a row
    is only read once its table's header has been recognised. An unknown format is not "nothing to
    report" — it is "determinism cannot refute this", and the row falls to the model layer. Column
    *roles* are resolved by name (`_resolve_columns`), so a non-standard order still parses.

    Args:
        md: the feature document's markdown.
        header: the lowercased 3-tuple the table's header row must equal to be read.

    Returns:
        The recognised data rows, in document order.
    """
    feature_i, module_i, status_i = _resolve_columns(header)
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
        if set(cells[feature_i]) <= {"-", ":", " "}:  # separator row
            continue
        rows.append(
            Row(
                feature=cells[feature_i],
                module=cells[module_i].strip("`").strip(),
                status=_norm_status(cells[status_i]),
            )
        )
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


def check_row(row: Row, module_src: str | None) -> RowCheck:
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
        return RowCheck(findings, False)
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
    return RowCheck(findings, has_residue)


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


def _is_stub_body(node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True when the node's body carries no implementation: only a docstring, ``pass``, ``...``,
    or ``raise NotImplementedError``.

    The signal ``build_signature_brief`` uses to mark a symbol NON implémenté regardless of what its
    docstring claims — the recurring failure a docstring-quality gate cannot catch (interrogate
    checks a docstring exists, darglint that it matches the signature, neither that it matches the
    behaviour).

    Args:
        node: a class, function, or async-function definition node.

    Returns:
        Whether the body is implementation-free.
    """
    body = list(node.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]  # drop the leading docstring
    if not body:
        return True
    for stmt in body:
        if isinstance(stmt, ast.Pass):
            continue
        if (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and stmt.value.value is Ellipsis
        ):
            continue
        exc = stmt.exc if isinstance(stmt, ast.Raise) else None
        name = exc.func if isinstance(exc, ast.Call) else exc
        if isinstance(name, ast.Name) and name.id == "NotImplementedError":
            continue
        return False
    return True


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Reconstruct a ``def name(args) -> ret`` line from a function node, body omitted.

    Args:
        node: a function or async-function definition node.

    Returns:
        The one-line signature.
    """
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    ret = f" -> {ast.unparse(node.returns)}" if node.returns else ""
    return f"{prefix} {node.name}({ast.unparse(node.args)}){ret}"


def _module_api_digest(src: str) -> str:
    """Every public top-level symbol of a module as signature + first docstring line + stub flag.

    Classes also list their public method signatures. Private names (leading ``_``) are skipped —
    a feature table documents features, not internals.

    Args:
        src: a module's source text.

    Returns:
        The digest text, or an empty string when the source has no public symbol or does not parse.
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return ""
    lines: list[str] = []

    def emit(node: ast.FunctionDef | ast.AsyncFunctionDef, indent: str) -> None:
        flag = "  ⚠ STUB" if _is_stub_body(node) else ""
        lines.append(f"{indent}{_signature(node)}{flag}")
        doc = ast.get_docstring(node)
        if doc:
            lines.append(f'{indent}    "{doc.splitlines()[0]}"')

    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if not node.name.startswith("_"):
                emit(node, "")
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            bases = ", ".join(ast.unparse(b) for b in node.bases)
            head = f"class {node.name}({bases})" if bases else f"class {node.name}"
            flag = (
                "  ⚠ STUB — corps = docstring seule, aucune méthode"
                if _is_stub_body(node)
                else ""
            )
            lines.append(f"{head}{flag}")
            doc = ast.get_docstring(node)
            if doc:
                lines.append(f'    "{doc.splitlines()[0]}"')
            for sub in node.body:
                if isinstance(
                    sub, ast.FunctionDef | ast.AsyncFunctionDef
                ) and not sub.name.startswith("_"):
                    emit(sub, "    ")
    return "\n".join(lines)


def build_signature_brief(doc_rel: str, doc_md: str, sources: dict[str, str]) -> str:
    """Document + every module's public-API digest (signatures + docstrings + stub flags).

    The bounded middle ground between `build_phase1_brief` (every full body — overflows a small
    model on a real repo) and `build_selective_brief` (index + referenced bodies — a small model
    rubber-stamps it). Trusts the docstrings a repo's own gates already enforce, and hands the model
    a deterministic ``⚠ STUB`` flag for the one thing a docstring can still lie about: an empty body.

    Args:
        doc_rel: the feature document's repo-relative path, for the header.
        doc_md: the feature document's markdown.
        sources: the path -> source map from `collect_sources`.

    Returns:
        The document, then one digest block per module with a public API.
    """
    api_header = (
        "--- API PUBLIQUE (signatures + 1re ligne de docstring ; ⚠ STUB = corps vide/trivial, "
        "à traiter comme NON implémenté quoi qu'affirme la docstring ou le tableau) ---"
    )
    parts = [f"--- TABLEAU DE FEATURES ({doc_rel}) ---\n{doc_md}", api_header]
    for path in sorted(sources):
        digest = _module_api_digest(sources[path])
        if digest:
            parts.append(f"### {path}\n{digest}")
    return "\n\n".join(parts)


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


def build_phase1_brief(doc_rel: str, doc_md: str, sources: dict[str, str]) -> str:
    """Document + the full source of every package file — the unbounded, format-agnostic brief.

    The all-source counterpart to `build_selective_brief`: no index, no module selection, every
    ``.py`` inlined. Too large for a real repo (that is what `build_selective_brief` is for), but it
    is the surface the reconcile oracle drives — a framing change is scored against a fixed set of
    sources, so the brief must not itself do any selecting.

    Args:
        doc_rel: the feature document's repo-relative path, for the header.
        doc_md: the feature document's markdown.
        sources: the path -> source map from `collect_sources`.

    Returns:
        The document, then every source file in path order.
    """
    parts = [f"--- TABLEAU DE FEATURES ({doc_rel}) ---\n{doc_md}"]
    for path in sorted(sources):
        parts.append(f"--- CODE: {path} ---\n```python\n{sources[path]}\n```")
    return "\n\n".join(parts)


# --- model layer: the prompt + its output normalisation --------------------------------------
#
# Ce module n'appelle jamais de modèle (NFR-0026-03) : il porte seulement le *texte* du prompt de
# jugement sémantique et le parsing de ce qu'un modèle renvoie. C'est le corps du skill qui passe
# `PHASE1_TASK` + un brief au tool MCP `offline` / `panel`, puis redonne la réponse ici.
#
# `PHASE1_TASK` est repris verbatim du moteur déprécié (`pinky_reconcile.py`). Son framing exact est
# précisément ce que l'oracle reconcile (`evals/reconcile_oracle.py`) protège — l'incident de
# rubber-stamp des 2026-07-16/17 vient d'un réajustement à l'œil. Ne pas le reformuler sans l'oracle
# vert sur les deux fixtures.

PHASE1_TASK = (
    "Tu réconcilies le tableau de features d'un repo avec le code qui est censé l'implémenter.\n"
    "Le code fait FOI. Le tableau est le document suspect.\n"
    "\n"
    "Vérifie les DEUX directions :\n"
    "1. Pour chaque ligne du tableau : le code fait-il ce que la ligne affirme ? MATCH ou DIVERGE.\n"
    "2. Inversement : une capacité publique importante est-elle absente du tableau ? MANQUANT.\n"
    "\n"
    "DIVERGE — définition stricte. La ligne affirme quelque chose que le code ne fait pas :\n"
    "- le code ne le fait pas du tout (déclaré stable/✓ mais absent, ou simple stub) ;\n"
    "- le code le fait autrement (valeur, seuil, comportement différent de l'affirmation) ;\n"
    "- le code ne le fait qu'à MOITIÉ (la ligne annonce une portée que l'implémentation "
    "n'atteint que partiellement).\n"
    "\n"
    "N'est PAS un DIVERGE — ne les signale sous aucun prétexte :\n"
    "- une ligne qui décrit MOINS que ce que le code fait. Une description incomplète n'est pas "
    "un mensonge : si le code offre en plus une option, un paramètre ou un réglage que la ligne "
    "ne mentionne pas, la ligne reste VRAIE. C'est un MATCH.\n"
    "- du code perfectible, mal nommé, ou dont tu discutes la conception : tu juges la "
    "correspondance ligne↔code, jamais la qualité du code.\n"
    "- une limitation que le tableau ASSUME lui-même : si une colonne (notes, commentaire, "
    "statut) justifie explicitement l'écart, c'est documenté, donc MATCH.\n"
    "\n"
    "MANQUANT — réservé aux capacités PUBLIQUES qu'un utilisateur du repo appellerait : classes et "
    "fonctions de l'API publique. Ne signale JAMAIS comme manquant : un helper privé (nom préfixé "
    "d'un `_`), une constante, une table de configuration, un détail d'implémentation interne. Un "
    "tableau de features documente des features, pas des rouages.\n"
    "\n"
    "Le tableau peut avoir n'importe quelle forme de colonnes — déduis-en le sens toi-même.\n"
    "Si tu n'as pas de quoi trancher une ligne, dis-le plutôt que de supposer.\n"
    "\n"
    "Pour chaque DIVERGE ou MANQUANT : ce que la ligne prétend / ce que le code fait réellement, "
    "puis la ligne de tableau corrigée, prête à relire.\n"
    "Ne rends que les lignes à problème + une ligne de synthèse. Ne détaille pas les MATCH.\n"
    "Un tableau honnête est le cas NORMAL : s'il n'y a rien à signaler, dis-le en une ligne. "
    "Ne cherche pas à remplir le rapport.\n"
    "\n"
    "AVANT de signaler un MANQUANT, relis le tableau en entier et vérifie que la capacité n'y est "
    "pas DÉJÀ, sous un autre libellé : réclamer l'ajout d'une ligne qui existe déjà est l'erreur la "
    "plus coûteuse de cet exercice. En particulier, une capacité citée À L'INTÉRIEUR de la "
    "description d'une autre ligne (une méthode nommée dans la cellule d'une feature plus large) EST "
    "documentée — le tableau décrit des features, pas des signatures : ne réclame JAMAIS une ligne "
    "dédiée par méthode ou par symbole.\n"
    "\n"
    "Termine impérativement par cette ligne, seule et exactement à ce format :\n"
    "VERDICT: <nombre de DIVERGE> diverge, <nombre de MANQUANT> manquant"
)

_MANQUANT_LINE = re.compile(r"^.*\bMANQUANT\b.*$", re.MULTILINE)


def normalize_manquants(model_output: str) -> list[str]:
    """Extract the model's MANQUANT findings as ``drop_already_documented`` input.

    `PHASE1_TASK` asks for free prose, not a machine format, so a MANQUANT surfaces as a line that
    names the capability (``MANQUANT — `xml_path_str` : …``). Each such line yields one entry: its
    first backtick-quoted symbol, prefixed with a module when the line reads ``… dans `xml` …``
    (``"xml: xml_path_str"``), else the bare capability name — `drop_already_documented` accepts
    both. A MANQUANT line with no backtick symbol is dropped (nothing to filter on).

    The trailing ``VERDICT:`` line is skipped: it restates counts and would echo the word MANQUANT
    with no symbol.

    Args:
        model_output: the raw text the model returned for `PHASE1_TASK` + a brief.

    Returns:
        One ``"module: capability"`` or ``"capability"`` string per MANQUANT finding, in order.
    """
    out: list[str] = []
    for line in _MANQUANT_LINE.findall(model_output):
        if line.lstrip().upper().startswith("VERDICT:"):
            continue
        symbols = [s.strip() for s in _BACKTICK.findall(line)]
        # `dans `xml`` -> module ; la capacité est le premier autre symbole entre backticks
        module = next(
            (s for s in symbols if re.search(rf"dans\s+`{re.escape(s)}`", line)), ""
        )
        capability = next((s.split("(")[0].strip() for s in symbols if s != module), "")
        if not capability:
            continue
        out.append(f"{module}: {capability}" if module else capability)
    return out


# --- MANQUANT post-filter ----------------------------------------------------------------------


def _documented_modules(table_md: str, header: Header) -> set[str]:
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
    header: Header = DEFAULT_HEADER,
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


def scan(repo: Path, features_rel: str, header: Header = DEFAULT_HEADER) -> ScanResult:
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
    repo: Path, features_rel: str, header: Header = DEFAULT_HEADER
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
    """CLI entry point.

    Three modes, all reading ``--repo`` / ``--features-doc``:
      - default: run the deterministic scan, print its report (exit 1 if it found divergences).
      - ``--emit-prompt``: print ``PHASE1_TASK`` + the signature brief — the text the skill body
        hands to the ``offline`` / ``panel`` MCP tool. No model is called here.
      - ``--filter-model-output``: read the model's reply on stdin, print the MANQUANT findings that
        survive `drop_already_documented`, one per line (the DIVERGE prose is for the skill body to
        read directly). Empty output = every MANQUANT was already-documented noise.

    Args:
        argv: argument vector, or None to read ``sys.argv``.

    Returns:
        int: 0 clean, 1 when the default scan found divergences, 2 on a not-applicable repo.
    """
    ap = argparse.ArgumentParser(
        description="Deterministic code<->feature-table reconciliation + model-layer plumbing."
    )
    ap.add_argument("--repo", required=True, type=Path)
    ap.add_argument(
        "--features-doc",
        required=True,
        help="feature document, repo-relative (e.g. docs/features.md)",
    )
    ap.add_argument(
        "--features-header",
        default=",".join(DEFAULT_HEADER),
        help="the table's 3 column headers, lowercased and comma-separated "
        f"(default: {','.join(DEFAULT_HEADER)})",
    )
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument(
        "--emit-prompt",
        action="store_true",
        help="print PHASE1_TASK + the signature brief for the model layer",
    )
    mode.add_argument(
        "--filter-model-output",
        action="store_true",
        help="read the model reply on stdin, print the surviving MANQUANT findings",
    )
    args = ap.parse_args(argv)
    parts = [h.strip().lower() for h in args.features_header.split(",")]
    if len(parts) != 3:
        ap.error("--features-header needs exactly 3 comma-separated names")
    header = Header(parts[0], parts[1], parts[2])

    repo: Path = args.repo
    reason = not_applicable(repo, args.features_doc, header)
    if reason is not None:
        print(f"[reconcile] not applicable — {reason}", file=sys.stderr)
        return 2

    if args.filter_model_output:
        doc_md = (repo / args.features_doc).read_text(encoding="utf-8")
        survivors = drop_already_documented(
            normalize_manquants(sys.stdin.read()), doc_md, header
        )
        print("\n".join(survivors))
        return 0

    if args.emit_prompt:
        doc_md = (repo / args.features_doc).read_text(encoding="utf-8")
        sources = collect_sources(repo)
        brief = build_signature_brief(args.features_doc, doc_md, sources)
        print(f"{PHASE1_TASK}\n\n{brief}")
        return 0

    result = scan(repo, args.features_doc, header)
    print(render(repo, result))
    return 1 if result.findings else 0


if __name__ == "__main__":
    sys.exit(main())
