"""Infrastructure-as-code misconfiguration detection for Elder (#447, ADR-0007
Track 1 slice 2 - the IaC half; follows the secret-scan half #436).

Flags insecure IaC a PR INTRODUCES on added lines, producing the SAME
`Candidate` shape the SAST/SCA/secret pipeline uses, so the exploitability judge
(`sast.judge_candidates`) and the publish path are reused unchanged - IaC
scanning is just a new candidate SOURCE.

Why a dedicated source: the vendored SAST ruleset is `languages: [python]`, and
SCA/secret cover deps + credentials - an open security group, a privileged k8s
container, a public S3 ACL, or a root Dockerfile is invisible today. This
detector is diff-scoped (added lines only, like the other Track-1 sources) and
FILE-TYPE-AWARE: Terraform patterns fire only on `*.tf`/`*.tfvars`, k8s/compose
patterns only on `*.yaml`/`*.yml`, Dockerfile patterns only on Dockerfiles; a
few cross-cutting patterns (open CIDR, world-writable, pipe-to-shell) fire on
any IaC file.

Recall-liberal by design (a detector miss is unrecoverable; an over-flag is
recovered by the judge, which can confirm an open `0.0.0.0/0` is a public ALB
by design vs an exposed admin port). The snippet carries the misconfig KIND +
the trimmed line - IaC config is not a secret value, so the line is safe to
publish.

Manifest breaks (#1480). Three Kubernetes changes that take production down
show in the diff alone, so a file-level pass over each YAML file's hunks
(context and removed lines too, not just added ones) adds them as candidates:

- a pod affinity or anti-affinity term the PR adds whose labelSelector picks
  another workload (no value the file uses for its own app or name) and that
  names no `namespaces` or `namespaceSelector`, so it only matches pods in
  the pod's own namespace and the placement it promises does nothing;
- a Deployment whose `Recreate` strategy the PR removes for `RollingUpdate`
  while `replicas` is 1 and `maxSurge` is not 0, so a singleton writer runs
  twice during every rollout;
- an `imagePullSecrets` key on a removed line with no `imagePullSecrets` on
  any added line of the same file, so a pod pulling from a private registry
  loses its credentials on the next pull.

Three other shapes from the same audit stay with the LLM review, with the
written reasons in `elder_eval/fixtures/manifest_breaks/corpus.json`: a
misspelled broker config key, a two-way broker bridge with no loop guard,
and docs stating numbers no run produced. Live-state breaks (selectors
against node labels, secret keys, hostnames) belong to Loom's lane-side
live-cluster check.

Pure: no IO, no network, no logging - deterministic + unit-tested.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from .diff_parser import added_lines, DiffHunk
from .sast import IAC_MISCONFIG, Candidate

__all__ = [
    "AFFINITY_NO_NAMESPACE",
    "IAC_MISCONFIG",
    "MANIFEST_BREAK_KINDS",
    "PULL_SECRET_DROPPED",
    "SINGLETON_ROLLING_UPDATE",
    "scan_iac",
]

# Cost bounds, mirroring secret_scan: cap candidates, skip pathological lines,
# and stop once the diff's added text exceeds a total byte budget.
_MAX_FINDINGS = 100
_MAX_LINE_LEN = 4096
_MAX_SCAN_BYTES = 1_048_576

# Published snippet line is trimmed to keep the finding message bounded.
_SNIPPET_MAX = 200


def _is_dockerfile(path: str) -> bool:
    base = path.rsplit("/", 1)[-1].lower()
    return base == "dockerfile" or base.startswith("dockerfile.") or base.endswith(".dockerfile")


def _is_terraform(path: str) -> bool:
    p = path.lower()
    return p.endswith(".tf") or p.endswith(".tfvars")


def _is_yaml(path: str) -> bool:
    p = path.lower()
    return p.endswith(".yaml") or p.endswith(".yml")


def _is_iac(path: str) -> bool:
    return _is_dockerfile(path) or _is_terraform(path) or _is_yaml(path)


@dataclass(frozen=True, slots=True)
class _Rule:
    """A high-signal IaC misconfiguration. `kind` labels it for the snippet,
    `regex` matches the offending construct, `applies` gates it by file type so
    a k8s pattern never fires on Terraform (and vice versa)."""

    kind: str
    regex: re.Pattern[str]
    applies: Callable[[str], bool]


# Each rule is a recognizable insecure construct. Matching one is strong
# evidence; the judge still confirms it is exploitable in context.
_RULES: tuple[_Rule, ...] = (
    # --- cross-cutting (any IaC file) ---
    _Rule("network open to the world (0.0.0.0/0)", re.compile(r"0\.0\.0\.0/0"), _is_iac),
    _Rule("IPv6 network open to the world (::/0)", re.compile(r"(?<![:\w])::/0"), _is_iac),
    _Rule("world-writable permissions (chmod 777)", re.compile(r"chmod\s+(?:-R\s+)?0?777\b"), _is_iac),
    _Rule("pipe-to-shell install", re.compile(r"(?:curl|wget)\b[^|\n]*\|\s*(?:sudo\s+)?(?:ba|z|d)?sh\b"), _is_iac),
    # --- Kubernetes / compose YAML ---
    _Rule("privileged container", re.compile(r"(?i)privileged:\s*true\b"), _is_yaml),
    _Rule("host network namespace", re.compile(r"(?i)hostNetwork:\s*true\b"), _is_yaml),
    _Rule("host PID namespace", re.compile(r"(?i)hostPID:\s*true\b"), _is_yaml),
    _Rule("host IPC namespace", re.compile(r"(?i)hostIPC:\s*true\b"), _is_yaml),
    _Rule("privilege escalation allowed", re.compile(r"(?i)allowPrivilegeEscalation:\s*true\b"), _is_yaml),
    _Rule("container may run as root", re.compile(r"(?i)runAsNonRoot:\s*false\b"), _is_yaml),
    _Rule("writable root filesystem", re.compile(r"(?i)readOnlyRootFilesystem:\s*false\b"), _is_yaml),
    _Rule("TLS verification disabled", re.compile(r"(?i)insecure-?skip-?tls-?verify:\s*true\b"), _is_yaml),
    # --- Terraform ---
    _Rule("public object ACL", re.compile(r"""(?i)acl\s*=\s*["']public-read(?:-write)?["']"""), _is_terraform),
    _Rule("resource publicly accessible", re.compile(r"(?i)publicly_accessible\s*=\s*true\b"), _is_terraform),
    _Rule("encryption disabled", re.compile(r"(?i)(?:encrypted|encryption|storage_encrypted)\s*=\s*false\b"), _is_terraform),
    _Rule("deletion protection disabled", re.compile(r"(?i)(?:deletion_protection|enable_deletion_protection)\s*=\s*false\b"), _is_terraform),
    # --- Dockerfile ---
    _Rule("container runs as root (USER root)", re.compile(r"(?im)^\s*USER\s+root\b"), _is_dockerfile),
    _Rule("unpinned :latest base image", re.compile(r"(?im)^\s*FROM\s+\S+:latest\b"), _is_dockerfile),
    _Rule("remote fetch into image (ADD <url>)", re.compile(r"(?im)^\s*ADD\s+https?://"), _is_dockerfile),
)


@dataclass(frozen=True, slots=True)
class _Misconfig:
    """A detected IaC misconfiguration on an added line. `(file, line)` anchor it
    to the diff; `kind` labels it; `snippet` is the trimmed offending line."""

    file: str
    line: int
    kind: str
    snippet: str


def _added_lines(hunk: DiffHunk) -> list[tuple[int, str]]:
    """[(new_side_line_number, added_text)] for each ADDED line in a hunk:
    the one walk in `diff_parser.added_lines`, so an added line whose
    content starts with `++` is neither dropped nor shifts the numbering of
    everything after it."""
    return added_lines(hunk)


def _detect(path: str, text: str) -> tuple[str, str] | None:
    """Return `(kind, matched)` for the first rule that applies to this file
    type AND matches the line, else None. `matched` is the exact substring, used
    only for in-memory dedup."""
    for rule in _RULES:
        if not rule.applies(path):
            continue
        m = rule.regex.search(text)
        if m:
            return rule.kind, m.group(0)
    return None


def scan_iac(hunks: tuple[DiffHunk, ...]) -> tuple[Candidate, ...]:
    """IaC-misconfig candidate source: a Candidate per insecure IaC construct a
    PR introduces, on added lines of Terraform / k8s-compose YAML / Dockerfiles.
    Diff-scoped, file-type-aware, content-deduped by `(file, kind, matched)` (a
    repeated misconfig in a file is reported once - bounds judge cost), capped at
    `_MAX_FINDINGS`. Same `Candidate` shape the SAST pipeline judges + publishes,
    so the judge decides whether the misconfig is actually exploitable (vs an
    intentional public endpoint)."""
    found: list[_Misconfig] = []
    seen: set[tuple[str, str, str]] = set()
    scanned = 0
    for hunk in hunks:
        if not _is_iac(hunk.file_path):
            continue
        for lineno, text in _added_lines(hunk):
            if len(text) > _MAX_LINE_LEN:
                continue
            scanned += len(text)
            if scanned > _MAX_SCAN_BYTES:
                return _to_candidates(found)
            hit = _detect(hunk.file_path, text)
            if hit is None:
                continue
            kind, matched = hit
            key = (hunk.file_path, kind, matched)
            if key in seen:
                continue
            seen.add(key)
            found.append(
                _Misconfig(file=hunk.file_path, line=lineno, kind=kind, snippet=text.strip()[:_SNIPPET_MAX])
            )
            if len(found) >= _MAX_FINDINGS:
                return _to_candidates(found)
    _merge_manifest_breaks(found, seen, hunks)
    return _to_candidates(found)


def _merge_manifest_breaks(
    found: list[_Misconfig], seen: set[tuple[str, str, str]], hunks: tuple[DiffHunk, ...],
) -> None:
    """Append the #1480 manifest-break hits, deduped and under the cap."""
    for hit in _scan_manifest_breaks(hunks):
        key = (hit.file, hit.kind, hit.snippet)
        if key in seen:
            continue
        seen.add(key)
        found.append(hit)
        if len(found) >= _MAX_FINDINGS:
            return


# --- manifest breaks (#1480) --------------------------------------------------

AFFINITY_NO_NAMESPACE = "pod affinity term selects another workload with no namespaces"
SINGLETON_ROLLING_UPDATE = "singleton Deployment switched from Recreate to RollingUpdate"
# A finding label, not a credential.
PULL_SECRET_DROPPED = "imagePullSecrets removed with no re-add in this file"  # noqa: S105
MANIFEST_BREAK_KINDS: tuple[str, ...] = (
    AFFINITY_NO_NAMESPACE,
    SINGLETON_ROLLING_UPDATE,
    PULL_SECRET_DROPPED,
)

# Bounds on the affinity walk, so a crafted manifest cannot make it costly.
_MAX_TERMS = 50
_MAX_TERM_LINES = 200

_AFFINITY_HEAD_RE = re.compile(r"^(\s*)pod(?:Anti)?Affinity:\s*$")
_LABEL_SELECTOR_RE = re.compile(r"^(\s*)(?:-\s+)?labelSelector:")
_NAMESPACES_RE = re.compile(r"^\s*(?:-\s+)?(?:namespaces|namespaceSelector):")
_OWN_NAME_RE = re.compile(
    r"(?:^|[\s{,])(?:app|name|app\.kubernetes\.io/name|k8s-app):\s*[\"']?([\w.-]+)"
)
_SELECTOR_VALUE_RE = re.compile(r"(?:^|[\s{,])[\w./-]+:\s*[\"']?([\w.-]+)[\"']?\s*(?=[,}]|$)")
_VALUES_LIST_RE = re.compile(r"^\s*values:\s*\[([^\]]*)\]")
_LIST_ITEM_RE = re.compile(r"^\s*-\s+[\"']?([\w.-]+)[\"']?\s*$")
# Term keys whose values are not label values: placement, weight, wrapper.
_TERM_SCAFFOLD_RE = re.compile(r"^\s*(?:-\s+)?(?:topologyKey|weight|podAffinityTerm|matchLabelKeys|mismatchLabelKeys|key|operator):")
_SELECTOR_KEYWORDS = frozenset({
    "matchLabels", "matchExpressions", "labelSelector", "key", "operator",
    "values", "In", "NotIn", "Exists", "DoesNotExist",
})
_RECREATE_RE = re.compile(r"""type:\s*["']?Recreate\b""")
_ROLLING_RE = re.compile(r"""type:\s*["']?RollingUpdate\b""")
_REPLICAS_RE = re.compile(r"^\s*replicas:\s*(\d+)\s*(?:#.*)?$")
_NO_SURGE_RE = re.compile(r"""maxSurge:\s*["']?0+%?["']?(?=[\s,}]|$)""")
_PULL_SECRETS_RE = re.compile(r"^\s*imagePullSecrets:")


@dataclass(frozen=True, slots=True)
class _Line:
    """One hunk body line: `op` is "+", "-" or " "; `new` is its new-side
    number (for a removed line, the new-side line it sits before)."""

    op: str
    new: int
    text: str


def _walk(hunk: DiffHunk) -> list[_Line]:
    out: list[_Line] = []
    lineno = hunk.new_start
    for raw in hunk.body.splitlines():
        if raw.startswith(("@@", chr(92))):
            continue
        if raw.startswith("+"):
            out.append(_Line("+", lineno, raw[1:]))
            lineno += 1
        elif raw.startswith("-"):
            out.append(_Line("-", lineno, raw[1:]))
        else:
            out.append(_Line(" ", lineno, raw[1:]))
            lineno += 1
    return out


def _indent(text: str) -> int:
    return len(text) - len(text.lstrip(" "))


def _hit(path: str, line: int, kind: str, text: str) -> _Misconfig:
    return _Misconfig(file=path, line=line, kind=kind, snippet=text.strip()[:_SNIPPET_MAX])


def _selector_values(term: list[_Line]) -> set[str]:
    values: set[str] = set()
    for ln in term:
        text = ln.text
        if _NAMESPACES_RE.match(text) or _TERM_SCAFFOLD_RE.match(text):
            continue
        m = _VALUES_LIST_RE.match(text)
        if m:
            values.update(v.strip().strip("\"'") for v in m.group(1).split(",") if v.strip())
            continue
        m = _LIST_ITEM_RE.match(text)
        if m:
            values.add(m.group(1))
            continue
        for v in _SELECTOR_VALUE_RE.findall(text):
            values.add(v)
    return {v for v in values - _SELECTOR_KEYWORDS if not v.isdigit()}


def _term_starts(new_side: list[_Line]) -> list[int]:
    """Indices of the list items holding a labelSelector under a
    podAffinity/podAntiAffinity key, in one linear walk. A stack of open list
    items (index, dash column) gives each labelSelector its enclosing item
    without walking back; a set dedupes items holding several selectors."""
    starts: dict[int, None] = {}
    open_items: list[tuple[int, int]] = []
    section: int | None = None
    section_at = -1
    for i, ln in enumerate(new_side):
        if not ln.text.strip():
            continue
        ind = _indent(ln.text)
        while open_items and ind <= open_items[-1][1]:
            open_items.pop()
        if ln.text.lstrip().startswith("-"):
            open_items.append((i, ind))
        if _AFFINITY_HEAD_RE.match(ln.text):
            section, section_at = ind, i
            continue
        if section is not None and ind <= section:
            section = None
        # The enclosing item must open inside the affinity key, not around it.
        in_term = section is not None and open_items and open_items[-1][0] > section_at
        if in_term and _LABEL_SELECTOR_RE.match(ln.text):
            starts.setdefault(open_items[-1][0], None)
            if len(starts) >= _MAX_TERMS:
                break
    return list(starts)


def _term_lines(new_side: list[_Line], start: int) -> list[_Line]:
    """The list item opening at `start`: every line deeper than its dash."""
    dash = _indent(new_side[start].text)
    term = [new_side[start]]
    for nxt in new_side[start + 1:start + _MAX_TERM_LINES]:
        if nxt.text.strip() and _indent(nxt.text) <= dash:
            break
        term.append(nxt)
    return term


def _affinity_terms(lines: list[_Line]) -> list[list[_Line]]:
    """The affinity terms on the new side of one hunk."""
    new_side = [ln for ln in lines if ln.op != "-"]
    return [_term_lines(new_side, i) for i in _term_starts(new_side)]


def _own_names(path: str, lines: list[_Line], term_lines: set[int]) -> set[str]:
    names = {seg for seg in re.split(r"[/.]", path) if seg}
    for ln in lines:
        if ln.op == "-" or ln.new in term_lines:
            continue
        names.update(_OWN_NAME_RE.findall(ln.text))
    return names


def _scan_affinity(path: str, per_hunk: list[list[_Line]]) -> list[_Misconfig]:
    terms = [t for lines in per_hunk for t in _affinity_terms(lines)]
    term_lines = {ln.new for t in terms for ln in t if ln.op == "+"}
    flat = [ln for lines in per_hunk for ln in lines]
    own = _own_names(path, flat, term_lines)
    out: list[_Misconfig] = []
    for term in terms:
        added = [ln for ln in term if ln.op == "+"]
        if not added or any(_NAMESPACES_RE.match(ln.text) for ln in term):
            continue
        selected = _selector_values(term)
        if not selected or selected <= own:
            continue
        anchor = next((ln for ln in added if _LABEL_SELECTOR_RE.match(ln.text)), added[0])
        others = ", ".join(sorted(selected - own))
        out.append(_hit(path, anchor.new, AFFINITY_NO_NAMESPACE,
                        f"selects {others}; matches only this pod's own namespace"))
    return out


def _scan_singleton_rolling(path: str, flat: list[_Line]) -> list[_Misconfig]:
    if not any(ln.op == "-" and _RECREATE_RE.search(ln.text) for ln in flat):
        return []
    rolling = next((ln for ln in flat if ln.op == "+" and _ROLLING_RE.search(ln.text)), None)
    if rolling is None:
        return []
    replicas = {int(m.group(1)) for ln in flat if ln.op != "-"
                for m in [_REPLICAS_RE.match(ln.text)] if m}
    if replicas != {1}:
        return []
    if any(ln.op != "-" and _NO_SURGE_RE.search(ln.text) for ln in flat):
        return []
    return [_hit(path, rolling.new, SINGLETON_ROLLING_UPDATE,
                 f"replicas 1, maxSurge not 0: {rolling.text.strip()}")]


def _scan_pull_secret_dropped(path: str, flat: list[_Line]) -> list[_Misconfig]:
    removed = next((ln for ln in flat if ln.op == "-" and _PULL_SECRETS_RE.match(ln.text)), None)
    if removed is None or any(ln.op == "+" and "imagePullSecrets" in ln.text for ln in flat):
        return []
    added = [ln.new for ln in flat if ln.op == "+"]
    if not added:
        # Guard's findings must sit on an added line; a removal-only file
        # has none, so the LLM review keeps that case.
        return []
    line = min(added, key=lambda n: abs(n - removed.new))
    return [_hit(path, line, PULL_SECRET_DROPPED, f"removed `{removed.text.strip()}`")]


def _scan_manifest_breaks(hunks: tuple[DiffHunk, ...]) -> list[_Misconfig]:
    """The #1480 manifest-break detectors, one pass per YAML file.

    One byte budget covers every hunk byte this pass reads, context and
    removed lines included; a file with a line over `_MAX_LINE_LEN` is
    skipped whole, as the line rules skip such a line."""
    by_file: dict[str, list[list[_Line]]] = {}
    skipped: set[str] = set()
    budget = _MAX_SCAN_BYTES
    for hunk in hunks:
        if not _is_yaml(hunk.file_path):
            continue
        budget -= len(hunk.body)
        if budget < 0:
            break
        lines = _walk(hunk)
        if any(len(ln.text) > _MAX_LINE_LEN for ln in lines):
            skipped.add(hunk.file_path)
        by_file.setdefault(hunk.file_path, []).append(lines)
    out: list[_Misconfig] = []
    for path, per_hunk in by_file.items():
        if path in skipped:
            continue
        flat = [ln for lines in per_hunk for ln in lines]
        out.extend(_scan_affinity(path, per_hunk))
        out.extend(_scan_singleton_rolling(path, flat))
        out.extend(_scan_pull_secret_dropped(path, flat))
    return out


def _to_candidates(found: list[_Misconfig]) -> tuple[Candidate, ...]:
    return tuple(
        Candidate(
            vuln_class=IAC_MISCONFIG,
            file=m.file,
            line=m.line,
            snippet=f"{m.kind}: {m.snippet}",
        )
        for m in found
    )
