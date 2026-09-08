#!/usr/bin/env python3
"""Repo-local doc rendering: README sections (badges/tagline/documentation/license) + docs/index.md.

Standalone module — stdlib only, no dependency on the workspace's distribution machinery.
Distributed as-is into every repo's scripts/hooks/ (always-on, like generate_ci_badges.py) so the
`generate-docs` hook can regenerate at commit time without the full pinky-workspace checkout.
`pinky-workspace/setup.py` imports this same module (CORE_HOOKS_DIR on sys.path) so the two never
duplicate the render logic — see the `generate-docs-hook` kanban card.

Instance identity (suite header image, suite line, GitHub org, pitch manifesto/pills) is NOT
hardcoded here — this module is the generic, shippable engine (`core/`). It is loaded from a
repo-local `docs_identity.json` (distributed by setup.py, sourced from the repo's group config —
see `_group_docs_identity`), so `core/` carries no `pinky` literal and the engine stays
instance-agnostic (de-leak,
2026-07-10). This includes the '## Why <suite>?' pitch section (2026-07-11): the manifesto/pills text used to be read live from pinky-studio's landing page HTML at
distribution time, a cross-repo dependency a per-repo hook can't reach reliably — that was the
ONLY identity field not already sourced from docs_identity.json. It now joins the others there
(`pitch_heading`/`pitch_paragraphs`/`pitch_pills`, one-time migrated from the HTML), so this
module renders it locally like every other section — no hybrid split, no cross-repo read.
"""

import json
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

# Windows' default console codepage (cp1252, not UTF-8) can't encode this hook's ✓/⚠ output and
# crashes with UnicodeEncodeError (found by the windows-latest CI job, CHANTIER0001
# portage-windows-du-moteur). Same fix as setup/__init__.py, applied here because this script runs
# standalone (invoked directly by pre-commit) and never imports that package.
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

README_BADGES_MARK_START = "<!-- pinky:readme-badges:start -->"
README_BADGES_MARK_END = "<!-- pinky:readme-badges:end -->"
README_LICENSE_MARK_START = "<!-- pinky:readme-license:start -->"
README_LICENSE_MARK_END = "<!-- pinky:readme-license:end -->"
README_DOCS_MARK_START = "<!-- pinky:readme-docs:start -->"
README_DOCS_MARK_END = "<!-- pinky:readme-docs:end -->"
README_TAGLINE_MARK_START = "<!-- pinky:readme-tagline:start -->"
README_TAGLINE_MARK_END = "<!-- pinky:readme-tagline:end -->"
README_WHY_MARK_START = "<!-- pinky:readme-why:start -->"
README_WHY_MARK_END = "<!-- pinky:readme-why:end -->"


@dataclass(frozen=True)
class SuiteIdentity:
    """Instance identity injected into generated docs — deliberately NOT hardcoded in this module
    (the generic, shippable engine). Resolved from the repo's group `docs_identity.json` (see
    `_group_docs_identity`) and distributed per-repo as `scripts/hooks/docs_identity.json` by
    setup.py, so the commit-time hook has it locally without reaching the workspace. Keeps `core/`
    free of any suite/instance literal.
    """

    header_image_url: str  # hosted suite header image (icon + wordmark lockup)
    header_alt: (
        str  # its alt text, HTML-ready (may contain entities, e.g. "pinky &amp; co")
    )
    suite_line: str  # the fixed "Part of the [<org>](<url>) suite." markdown line
    github_org: str  # GitHub org for the CI badge URL
    pitch_heading: (
        str  # the README '## <heading>' pitch section title, e.g. "Why pinky?"
    )
    pitch_paragraphs: tuple[str, ...]  # pitch manifesto, one markdown paragraph each
    pitch_pills: tuple[
        str, ...
    ]  # short positioning pills, rendered "· "-joined and bolded

    _FIELDS = (
        "header_image_url",
        "header_alt",
        "suite_line",
        "github_org",
        "pitch_heading",
        "pitch_paragraphs",
        "pitch_pills",
    )

    # Seul `github_org` est structurellement requis : l'URL du badge CI se construit avec, il n'a pas
    # de forme dégradée. Les six autres décrivent une SUITE — bandeau, ligne d'appartenance, pitch —
    # et un profil peut légitimement n'en être pas une : une org qui livre des applications plutôt
    # que des libs publiées n'a rien à y mettre, et un repo outil n'a jamais rendu de pitch. Devoir
    # déclarer six champs vides pour dire « je n'en suis pas une » était un contournement propre,
    # pas une réponse (card `profil-sans-identite-de-suite`).
    _REQUIRED = ("github_org",)

    @classmethod
    def from_dict(cls, data: dict) -> "SuiteIdentity":
        """Build from the docs_identity.json mapping (the ONE place the field names are read).

        JSON arrays decode as lists; ``pitch_paragraphs``/``pitch_pills`` are coerced to tuples so
        every instance stays hashable (this dataclass is frozen).

        An ABSENT optional field yields an empty value, and each renderer then omits the block it
        feeds — the block becomes unavailable rather than rendered empty. Same reversal as
        ADR-0058 → 0062: what is not declared does not exist.

        Args:
            data: the parsed `docs_identity.json` mapping.

        Returns:
            The `SuiteIdentity` built from `data`.

        Raises:
            KeyError: when a structurally required field is missing (see ``_REQUIRED``).
        """
        missing = [k for k in cls._REQUIRED if not data.get(k)]
        if missing:
            raise KeyError(
                f"docs_identity.json: missing required field(s) {', '.join(missing)}"
            )
        seqs = ("pitch_paragraphs", "pitch_pills")
        return cls(
            **{  # type: ignore[arg-type]  # mypy can't verify a **dict unpack field-by-field
                # against SuiteIdentity's per-field types; the ternary above already keys the
                # tuple-vs-str split off `seqs`, so runtime shape is correct — mypy's gap, not a
                # real defect.
                k: tuple(data.get(k) or ()) if k in seqs else (data.get(k) or "")
                for k in cls._FIELDS
            }
        )

    def to_json(self) -> str:
        """The docs_identity.json text — what setup.py distributes into each repo's scripts/hooks/."""
        return json.dumps({k: getattr(self, k) for k in self._FIELDS}, indent=2) + "\n"


def _load_identity() -> SuiteIdentity:
    """Load the repo-local instance identity distributed alongside this module. Fails loud if
    absent — a generated hook without its identity config is a distribution bug, not a silent
    fallback to some hardcoded default (there is none here, by design — that is the de-leak)."""
    path = Path(__file__).with_name("docs_identity.json")
    if not path.exists():
        sys.exit(f"✗ {path} missing — run setup.py to distribute docs_identity")
    return SuiteIdentity.from_dict(json.loads(path.read_text(encoding="utf-8")))


_LICENSE_VERSION = re.compile(r"^Version (\d+\.\d+)")


def _repo_license_name(repo: Path) -> str:
    """License name derived from the repo's own LICENSE file (e.g. 'MIT License' → 'MIT').

    Reads the first NON-BLANK line, not line 0: the canonical Apache-2.0 text opens with an empty
    line, so the previous version returned an empty name for it — and the README's generated License
    section would have said "` — see LICENSE`" on every repo adopting Apache (found 2026-08-09,
    adding one to the moteur).

    When the following line carries a version (Apache's `Version 2.0, January 2004`), it joins the
    name: "Apache-2.0", the SPDX form, rather than a bare "Apache" that names three licenses.
    """
    license_file = repo / "LICENSE"
    if not license_file.exists():
        return "UNKNOWN"
    lines = [
        ln.strip()
        for ln in license_file.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    if not lines:
        return "UNKNOWN"
    name = lines[0].removesuffix(" License").strip() or lines[0]
    version = _LICENSE_VERSION.match(lines[1]) if len(lines) > 1 else None
    return f"{name}-{version.group(1)}" if version else name


def _shields_escape(text: str) -> str:
    """Escape a shields.io static-badge segment: literal `-` must become `--`, spaces `_`
    (shields.io's `/badge/<label>-<message>-<color>` splits on bare hyphens — a license name like
    'Creative Commons Attribution-NonCommercial-NoDerivatives 4.0 International' breaks unescaped).
    """
    return text.replace("-", "--").replace(" ", "_")


def _frontmatter_title(path: Path) -> str | None:
    """The `title:` field from a file's YAML frontmatter (see doc_conventions.md), or None if
    the file has no frontmatter or no `title:` line in it."""
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            return None
        m = re.match(r"^title:\s*(.+)$", line)
        if m:
            return m.group(1).strip()
    return None


def _doc_links(dir_path: Path) -> list[tuple[str, str]]:
    """(title, filename) for every direct .md child of dir_path carrying a title: frontmatter.

    Sorted by filename — numbered prefixes (1-design.md, 2-features.md, …) order naturally. A file
    without title: is silently skipped (not an error — see doc_conventions.md's own carve-out).
    """
    if not dir_path.is_dir():
        return []
    links = []
    for f in sorted(dir_path.iterdir()):
        if f.is_file() and f.suffix == ".md":
            title = _frontmatter_title(f)
            if title:
                links.append((title, f.name))
    return links


# Extra exclusions for render_tree() on top of what git already ignores — deterministic, never
# grown ad hoc. `docs/` is excluded wholesale: the '**Explanation**'/'**Reference**'/'**Decisions**'
# sections right above the tree already enumerate it (title-frontmatter links, ADRs) — relisting it
# raw here is redundant, not structure worth showing twice. Everything else noisy (.cache, .venv,
# __pycache__, build artifacts…) is caught by .gitignore itself (see _tracked_or_unignored_files).
_TREE_EXCLUDE_PATHS = {"docs"}


def _tracked_or_unignored_files(repo: Path) -> list[str]:
    """Repo-relative file paths git would let you `git add`: tracked files + untracked files NOT
    matched by .gitignore (nested .gitignore files and the global excludes file all respected
    natively by git — no reimplementing gitignore parsing here). Empty list if this isn't a git
    repo or git isn't on PATH (defensive only — the hook that calls this always runs under git)."""
    git = shutil.which("git")
    if git is None:
        return []
    try:
        # Bandit S603 suppressed below: fixed argument list (no user input interpolated),
        # no shell=True, git resolved via shutil.which() above (not a bare literal on PATH).
        result = subprocess.run(  # noqa: S603
            [git, "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
            encoding="utf-8",
        )
    except (subprocess.CalledProcessError, OSError):
        return []
    return [line for line in result.stdout.splitlines() if line]


def _build_tree(paths: list[str]) -> dict:
    """Nested dict from repo-relative POSIX paths: directories map to dict, files map to None."""
    root: dict = {}
    for rel in paths:
        parts = rel.split("/")
        if any(
            "/".join(parts[: i + 1]) in _TREE_EXCLUDE_PATHS for i in range(len(parts))
        ):
            continue
        node = root
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = None
    return root


def _render_tree_dict(node: dict, prefix: str = "") -> list[str]:
    """ASCII tree lines from a _build_tree() dict — fixed sort (dirs first, then alpha
    case-insensitive) so re-generation never churns on dict/filesystem iteration order."""
    entries = sorted(node.items(), key=lambda kv: (kv[1] is None, kv[0].lower()))
    lines = []
    for i, (name, sub) in enumerate(entries):
        last = i == len(entries) - 1
        connector = "└── " if last else "├── "
        is_dir = sub is not None
        lines.append(f"{prefix}{connector}{name}{'/' if is_dir else ''}")
        if is_dir:
            lines.extend(_render_tree_dict(sub, prefix + ("    " if last else "│   ")))
    return lines


def render_tree(repo: Path) -> str:
    """Deterministic directory tree for the '**Structure**' section — built from the files git
    would let you add (.gitignore-respecting, via _tracked_or_unignored_files), so cache/build
    noise never needs a hardcoded exclusion list. Fixed sort, never churns commit to commit."""
    tree = _build_tree(_tracked_or_unignored_files(repo))
    lines = [f"{repo.name}/", *_render_tree_dict(tree)]
    return "```text\n" + "\n".join(lines) + "\n```"


def render_readme_documentation(repo: Path) -> str:
    """The '## Documentation' section body — 100% derived from the real filesystem.

    Enumerates docs/explanation/ and docs/reference/ (top-level .md files only — api/ and
    generated/ are separate, always-fixed subsections) via each file's title: frontmatter.
    """
    sections: list[str] = []

    explanation = _doc_links(repo / "docs" / "explanation")
    if explanation:
        lines = ["**Explanation**", ""]
        lines += [
            f"- [{title}](docs/explanation/{name})" for title, name in explanation
        ]
        sections.append("\n".join(lines))

    reference = _doc_links(repo / "docs" / "reference")
    if reference:
        lines = ["**Reference**", ""]
        lines += [f"- [{title}](docs/reference/{name})" for title, name in reference]
        sections.append("\n".join(lines))

    if (repo / "docs" / "reference" / "api").is_dir():
        sections.append(
            "**API reference**\n\n- [Generated API docs](docs/reference/api/)"
        )

    if (repo / "docs" / "adr").is_dir():
        sections.append("**Decisions**\n\n- [ADRs](docs/adr/)")

    sections.append(
        "**Structure**\n\n<details>\n<summary>Show the directory tree</summary>\n\n"
        + render_tree(repo)
        + "\n\n</details>"
    )

    body = "\n\n".join(sections)
    return f"{README_DOCS_MARK_START}\n\n{body}\n\n{README_DOCS_MARK_END}"


_API_STUB_TEMPLATE = (
    "::: {dotted}\n"
    "    options:\n"
    "      show_root_heading: true\n"
    "      show_root_full_path: false\n"
    "      heading_level: 1\n"
)


def render_missing_api_stubs(repo: Path) -> list[str]:
    """Create missing docs/reference/api/<module>.md stubs for top-level public modules under
    src/<pkg>/*.py — mkdocstrings only renders content INTO an existing stub, it never creates
    one, so a new module silently never appears on the site until someone remembers to add its
    stub by hand (observed on pinky-core: html.py and lint.py went unstubbed for a while).

    Deliberately non-recursive (src/<pkg>/*.py, not **/*.py): repos with subpackages
    (pinky-streamlit, pinky-provider) hand-curate their stubs — grouping several submodules under
    one heading, picking non-default heading levels — which doesn't map 1:1 to a module name. This
    only fills the flat-package gap; it never fights a curated one.

    Returns the repo-relative paths of the stubs it created (empty if none, or if the repo has no
    mkdocs site / no docs/reference/api/ yet — same "opt-in" gate as _render_badges' conditions).
    """
    api_dir = repo / "docs" / "reference" / "api"
    if not (repo / "docs" / "mkdocs.yml").exists() or not api_dir.is_dir():
        return []
    src_dir = repo / "src"
    if not src_dir.is_dir():
        return []
    existing_stems = {f.stem for f in api_dir.glob("*.md")}
    created = []
    for pkg_dir in sorted(d for d in src_dir.iterdir() if d.is_dir()):
        for module in sorted(pkg_dir.glob("*.py")):
            if module.stem.startswith("_") or module.stem in existing_stems:
                continue
            stub = api_dir / f"{module.stem}.md"
            stub.write_text(
                _API_STUB_TEMPLATE.format(dotted=f"{pkg_dir.name}.{module.stem}"),
                encoding="utf-8",
            )
            created.append(f"docs/reference/api/{module.stem}.md")
    return created


def _load_badges() -> list[dict]:
    """Load the generic badge catalog distributed alongside this module (core/hooks/badges.toml, copied
    verbatim by setup.py's distribute() — see the module docstring for why TOML, not YAML). Fails
    loud if absent, same posture as _load_identity()."""
    path = Path(__file__).with_name("badges.toml")
    if not path.exists():
        sys.exit(f"✗ {path} missing — run setup.py to distribute the badge catalog")
    with path.open("rb") as f:
        return tomllib.load(f)["badge"]


def _render_badges(repo: Path, identity: SuiteIdentity, surface: str) -> list[str]:
    """Badge markdown lines for ``surface`` ("readme" or "index"), in catalog order, each gated on
    its own `condition` path existing in the repo — presence-gated, not extras-gated, so a
    non-Python repo (assets, infra) still gets its own badges back (license, prek) without a
    dangling PyPI/coverage badge pointing at something that doesn't exist there."""
    ctx = {
        "name": repo.name,
        "license": _shields_escape(_repo_license_name(repo)),
        "github_org": identity.github_org,
    }
    lines = []
    for entry in _load_badges():
        if surface not in entry["surfaces"]:
            continue
        if (repo / entry["condition"]).exists():
            lines.append(entry[surface].format(**ctx))
    return lines


def render_readme_badges(repo: Path, identity: SuiteIdentity) -> str:
    """Suite icon + quality/release badges (from core/hooks/badges.toml) + a centered quick-nav line."""
    lines: list[str] = ['<div align="center">', ""]
    # Pas de bandeau déclaré = pas de bandeau. Un `<img src="">` rendrait une image cassée, ce qui
    # est pire que l'absence (card `profil-sans-identite-de-suite`).
    if identity.header_image_url:
        lines += [
            f'<img src="{identity.header_image_url}" width="380" alt="{identity.header_alt}">',
            "",
        ]
    lines += _render_badges(repo, identity, "readme")
    lines += [
        "",
        "**[Install](#install) • [Documentation](#documentation) • [License](#license)**",
        "",
        "</div>",
    ]
    body = "\n".join(lines)
    return f"{README_BADGES_MARK_START}\n\n{body}\n{README_BADGES_MARK_END}"


def render_readme_license(repo: Path) -> str:
    """The '## License' section body — 100% mechanical, derived from the repo's own LICENSE file."""
    body = f"{_repo_license_name(repo)} — see [LICENSE](LICENSE)."
    return f"{README_LICENSE_MARK_START}\n\n{body}\n\n{README_LICENSE_MARK_END}"


def _repo_description(repo: Path) -> str:
    """The one-line description from the repo's pyproject.toml [project].description.

    Fails loud if absent — a missing required source is a bug to surface, not paper over.
    """
    pyproject = repo / "pyproject.toml"
    if not pyproject.exists():
        sys.exit(f"✗ no pyproject.toml in {repo} — cannot render the tagline")
    with pyproject.open("rb") as f:
        desc = tomllib.load(f).get("project", {}).get("description", "").strip()
    if not desc:
        sys.exit(
            f"✗ no [project].description in {pyproject} — it is the tagline source"
        )
    return desc


def _tagline_body(repo: Path, identity: SuiteIdentity) -> str:
    """Description paragraph + the fixed suite line — shared verbatim by README and index.md."""
    description = f"{_repo_description(repo).rstrip('.')}."
    # Un profil sans suite n'a pas de ligne d'appartenance à afficher.
    return (
        f"{description}\n\n{identity.suite_line}"
        if identity.suite_line
        else description
    )


def render_readme_tagline(repo: Path, identity: SuiteIdentity) -> str:
    """The README.md tagline block (description + suite line), marker-wrapped for idempotence."""
    return f"{README_TAGLINE_MARK_START}\n\n{_tagline_body(repo, identity)}\n\n{README_TAGLINE_MARK_END}"


def render_readme_why(identity: SuiteIdentity) -> str:
    """The '## <pitch_heading>' section — suite-level positioning, identical across every README.

    Sourced from ``identity.pitch_paragraphs``/``pitch_pills`` (docs_identity.json) — repo-local,
    like every other section here, since 2026-07-11 (see the module docstring).
    """
    if not identity.pitch_paragraphs and not identity.pitch_pills:
        # Rien à dire : on rend les marqueurs vides plutôt qu'un titre suivi de blanc. Les marqueurs
        # restent pour que l'injection soit idempotente et qu'un pitch ajouté plus tard atterrisse.
        # La ligne blanche entre les deux n'est pas cosmétique : sans elle, mdformat en ajoute une à
        # chaque commit et generate-docs la retire à chaque fois — boucle qui bloque `git commit`
        # indéfiniment sur tout repo dont l'identité de groupe n'a pas encore de pitch.
        return f"{README_WHY_MARK_START}\n\n{README_WHY_MARK_END}"
    parts = ["\n\n".join(identity.pitch_paragraphs)]
    if identity.pitch_pills:
        parts.append(f"**{' · '.join(identity.pitch_pills)}**")
    body = "\n\n".join(p for p in parts if p)
    return f"{README_WHY_MARK_START}\n\n{body}\n\n{README_WHY_MARK_END}"


def has_generated_index(repo: Path) -> bool:
    """Whether ``repo`` wants `docs/index.md` generated (the mkdocs homepage) rather than left
    hand-maintained. Single predicate shared by both call sites (`_main()` below, `distribute()`
    in setup/precommit.py — same imported function, see setup/common.py) so the exception can't
    drift between the commit-time hook and the one-time distribution render.

    True whenever `docs/mkdocs.yml` exists — UNLESS `docs/.no-generated-index` (empty, committed
    marker) opts a repo out. Written for `pinky-workspace` itself: its `docs/index.md` is a
    hand-curated agent navigation index over 70+ ADRs, richer than anything the generated homepage
    or the README's generated Documentation section (which only links the `docs/adr/` folder, not
    per-file) can reproduce — see kanban card `site-mkdocs-sur-le-moteur`. A marker file, not a
    hardcoded repo path, because `core/hooks/render_docs.py` is distributed byte-identical to every
    repo (ADR-0031) — any future repo with the same corpus-scale mismatch can opt out the same way.
    """
    return (repo / "docs" / "mkdocs.yml").exists() and not (
        repo / "docs" / ".no-generated-index"
    ).exists()


def render_index_home(repo: Path, identity: SuiteIdentity) -> str:
    """The full docs/index.md — the mkdocs homepage, 100% generated (do not hand-edit).

    Same conditional-badge principle as render_readme_badges(). No CI badge here regardless (it
    404s unauthenticated on a private repo — the mkdocs site has no GitHub session to pass through).
    No '*Update date*' tag: a fully-generated file's freshness is the git history, not a hand-typed
    line (exempted in doc_conventions.md).

    The wrapping `<div>` needs `markdown="1"`: unlike GitHub's renderer (used for README.md), mkdocs'
    Python-Markdown treats raw HTML blocks as opaque and won't parse the `![...](...)` badge syntax
    inside them without it — that also requires the `md_in_html` extension enabled in mkdocs.yml,
    which is a per-repo file this hook does not own.
    """
    name = repo.name
    lines: list[str] = [
        '<div align="center" markdown="1">',
        "",
        f'<img src="{identity.header_image_url}" width="380" alt="{identity.header_alt}">',
        "",
    ]
    lines += _render_badges(repo, identity, "index")
    lines += ["", "</div>"]
    badges = "\n".join(lines)
    return "\n".join(
        [
            "---",
            "title: Home",
            "---",
            f"# {name}",
            "",
            badges,
            "",
            _tagline_body(repo, identity),
            "",
        ]
    )


def inject_readme_sections_local(
    readme: str, repo: Path, identity: SuiteIdentity, *, allow_first_run: bool = False
) -> str | None:
    """Return README.md text with the repo-local sections (badges/tagline/documentation/license)
    injected/updated, or None if unchanged.

    Idempotent, marker-wrapped. Updates content between EXISTING markers only. First-run wrapping of
    hand-typed sections (the `elif` branches below) is destructive — it can drop hand-typed content
    that a doc's missing `title:` frontmatter makes invisible to the enumeration (the pinky-studio
    incident, 2026-07-11: `## Documentation` lost its Design/Features links on the adoption commit) —
    so it is gated behind ``allow_first_run`` and OFF by default. The commit-time `generate-docs`
    hook calls with the default (never wraps hand-typed content silently); only setup.py's reviewed
    distribution passes ``allow_first_run=True`` for the one-time adoption, where the diff is seen
    before it lands.
    """
    new = readme

    badges = render_readme_badges(repo, identity)
    if README_BADGES_MARK_START in new and README_BADGES_MARK_END in new:
        new = re.sub(
            re.escape(README_BADGES_MARK_START)
            + r".*?"
            + re.escape(README_BADGES_MARK_END),
            badges,
            new,
            count=1,
            flags=re.DOTALL,
        )
    elif allow_first_run and re.search(r"^!\[CI\]", new, flags=re.MULTILINE):
        new = re.sub(
            r"^!\[CI\]\(.*(?:\n.*)*?(?=\n\n)",
            badges,
            new,
            count=1,
            flags=re.MULTILINE,
        )

    # Tagline needs pyproject.toml's [project].description — skip on repos without one (assets,
    # infra) rather than crash them every commit; no source, no section, same rule as the badges.
    if (repo / "pyproject.toml").exists():
        tagline = render_readme_tagline(repo, identity)
        if README_TAGLINE_MARK_START in new and README_TAGLINE_MARK_END in new:
            new = re.sub(
                re.escape(README_TAGLINE_MARK_START)
                + r".*?"
                + re.escape(README_TAGLINE_MARK_END),
                tagline,
                new,
                count=1,
                flags=re.DOTALL,
            )
        elif allow_first_run and README_BADGES_MARK_END in new:
            new = re.sub(
                re.escape(README_BADGES_MARK_END) + r"\n\n(?:.*\n)*?(?=_{10,})",
                f"{README_BADGES_MARK_END}\n\n{tagline}\n\n",
                new,
                count=1,
            )

    why = render_readme_why(identity)
    if README_WHY_MARK_START in new and README_WHY_MARK_END in new:
        new = re.sub(
            re.escape(README_WHY_MARK_START) + r".*?" + re.escape(README_WHY_MARK_END),
            why,
            new,
            count=1,
            flags=re.DOTALL,
        )
    elif allow_first_run and re.search(r"^## Install[ \t]*$", new, flags=re.MULTILINE):
        # First run: insert as a new section right before ## Install — no prior anchor to replace.
        # Reuses the file's own separator style (mdformat's rendering of `___`), not a bare `---`.
        sep = "_" * 70
        new = re.sub(
            r"^## Install[ \t]*$",
            f"## {identity.pitch_heading}\n\n{why}\n\n{sep}\n\n## Install",
            new,
            count=1,
            flags=re.MULTILINE,
        )

    license_block = render_readme_license(repo)
    if README_LICENSE_MARK_START in new and README_LICENSE_MARK_END in new:
        new = re.sub(
            re.escape(README_LICENSE_MARK_START)
            + r".*?"
            + re.escape(README_LICENSE_MARK_END),
            license_block,
            new,
            count=1,
            flags=re.DOTALL,
        )
    elif allow_first_run and re.search(r"^## License[ \t]*$", new, flags=re.MULTILINE):
        new = re.sub(
            r"^## License[ \t]*\n(?:.*\n?)*\Z",
            f"## License\n\n{license_block}\n",
            new,
            flags=re.MULTILINE,
        )

    docs_block = render_readme_documentation(repo)
    if README_DOCS_MARK_START in new and README_DOCS_MARK_END in new:
        new = re.sub(
            re.escape(README_DOCS_MARK_START)
            + r".*?"
            + re.escape(README_DOCS_MARK_END),
            docs_block,
            new,
            count=1,
            flags=re.DOTALL,
        )
    elif allow_first_run and re.search(
        r"^## Documentation[ \t]*$", new, flags=re.MULTILINE
    ):
        new = re.sub(
            r"^## Documentation[ \t]*\n(?:.*\n)*?(?=_{10,}\n\n## License)",
            f"## Documentation\n\n{docs_block}\n\n",
            new,
            count=1,
            flags=re.MULTILINE,
        )

    # Strip the '*Update date*' tag: it exists (doc_conventions.md) to resolve conflicts between
    # diverging HAND-TYPED copies, and README no longer qualifies — badges/tagline/documentation/
    # license are generator-owned, each derived from a single source that can't diverge that way.
    # Git history is the freshness signal now, same reasoning as docs/index.md.
    new = re.sub(r"^\*Update date :.*\*\n\n", "", new, count=1, flags=re.MULTILINE)
    return new if new != readme else None


def _main() -> None:
    """CLI entry point for the `generate-docs` hook — regenerate README.md + docs/index.md in
    the current working directory (the repo the hook runs in) and print which files changed."""
    repo = Path.cwd()
    identity = _load_identity()
    changed: list[str] = []

    readme_path = repo / "README.md"
    if readme_path.exists():
        updated = inject_readme_sections_local(
            readme_path.read_text(encoding="utf-8"), repo, identity
        )
        if updated is not None:
            readme_path.write_text(updated, encoding="utf-8")
            changed.append("README.md")

    if has_generated_index(repo):
        index_path = repo / "docs" / "index.md"
        generated = render_index_home(repo, identity)
        current = (
            index_path.read_text(encoding="utf-8") if index_path.exists() else None
        )
        if generated != current:
            index_path.write_text(generated, encoding="utf-8")
            changed.append("docs/index.md")

    changed += render_missing_api_stubs(repo)

    for path in changed:
        print(f"  regenerated {path}")


if __name__ == "__main__":
    _main()
