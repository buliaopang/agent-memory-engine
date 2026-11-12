"""IntegrityService: the logic behind `ame check-graph` (ADR-011). Read-only by default.

Codes:
  G001 cycle in active structural edges (error)      G002 self-loop (error)
  G003 edge/relation with missing endpoint (error)   G004 duplicate active edge (error)
  G005 undirected relation not normalized (error)    G006 superseded w/o SUPERSEDES (warning)
  G007 guard trigger missing / fingerprint changed   G008 guard triggers do not fire (error)
  G009 integrity_check / foreign_key_check (error)   G010 revoked link w/o metadata (error)
  G011 conflict bookkeeping: conflicted w/o open conflict (warning, user should confirm T6);
       open conflict on a revoked relation (error)
  G012 lexical index (memory_fts) out of sync with the current versions (error, Phase 2)
  G013 compact CJK index (memory_fts_compact) out of sync (error, Phase 2.1, ADR-023)
  G014 space_members rows invalid: dangling space/agent, missing grant metadata, or
       half-filled revoke metadata (error, Phase 3a, ADR-025)
  G015 feedback / epoch rows invalid: dangling memory/space/epoch, half-filled removal,
       membership changed after the epoch closed (error, Phase 4, ADR-029/030)
  G016 distill draft invalid: output missing, missing DERIVED_FROM edge, or no longer
       candidate without a user status change in the audit (error, Phase 4, ADR-032)
  G017 memory has multiple active incoming PARENT edges (warning, ADR-033, Conflux parent tree)
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ame.infrastructure.integrity_repository import IntegrityRepository, fingerprint
from ame.infrastructure.schema_guards import HEAD, REVISIONS, required_triggers

EXIT_OK, EXIT_WARN, EXIT_ERROR, EXIT_CANNOT, EXIT_USAGE = 0, 1, 2, 3, 64
MIN_REVISION = "0005_conflicts_audit"


@dataclass
class Finding:
    code: str
    severity: str  # "error" | "warning"
    message: str
    items: list[str] = field(default_factory=list)
    repair: str | None = None


@dataclass
class Report:
    db: str
    revision: str | None
    findings: list[Finding] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)
    fatal: str | None = None

    @property
    def exit_code(self) -> int:
        if self.fatal:
            return EXIT_CANNOT
        if any(f.severity == "error" for f in self.findings):
            return EXIT_ERROR
        return EXIT_WARN if self.findings else EXIT_OK

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["exit_code"] = self.exit_code
        return d


def find_cycle_nodes(edges: list[tuple[str, str, str, str]]) -> list[str]:
    """Kahn's algorithm: nodes left over are on (or downstream-locked by) a cycle."""
    indeg: dict[str, int] = defaultdict(int)
    adj: dict[str, list[str]] = defaultdict(list)
    nodes: set[str] = set()
    for _eid, a, b, _t in edges:
        if a == b:
            continue
        adj[a].append(b)
        indeg[b] += 1
        nodes.update((a, b))
    q = deque(n for n in nodes if indeg[n] == 0)
    seen = 0
    while q:
        n = q.popleft()
        seen += 1
        for m in adj[n]:
            indeg[m] -= 1
            if indeg[m] == 0:
                q.append(m)
    return sorted(n for n in nodes if indeg[n] > 0)


def _probe_triggers(schema: list[str]) -> list[str]:
    """G008: replay the schema into :memory: and verify the guards really fire."""
    c = sqlite3.connect(":memory:")
    failures: list[str] = []
    try:
        c.execute("PRAGMA foreign_keys=ON")
        for sql in schema:
            c.execute(sql)
        has_spaces = any(x.startswith("CREATE TABLE spaces") for x in schema)
        if has_spaces:
            c.execute(
                "INSERT INTO spaces (space_id, name, scope_type, created_at) "
                "VALUES ('shared', 'shared', 'shared', 't')"
            )
        cols = "memory_id, memory_type, layer, scope_type, scope_id, created_by, created_at, "
        cols += "updated_at, importance, confidence, status, current_version, metadata_json"
        for mid in ("A", "B", "C"):
            c.execute(
                f"INSERT INTO memories ({cols}) VALUES (?, 'semantic', 'L2', 'shared', "  # noqa: S608
                "'shared', 'u', 't', 't', 0.5, 0.5, 'active', 1, '{}')",
                (mid,),
            )
        ecols = "edge_id, from_memory_id, to_memory_id, edge_type, status, created_by, "
        ecols += "created_at, metadata_json"
        ins = f"INSERT INTO memory_edges ({ecols}) VALUES (?, ?, ?, 'PARENT', 'active', 'u', 't', '{{}}')"  # noqa: S608,E501
        c.execute(ins, ("e1", "A", "B"))
        probes = {
            "cycle_insert": (ins, ("e2", "B", "A"), "DAG_CYCLE_DETECTED"),
            "endpoint_missing": (ins, ("e3", "A", "ZZ"), ""),
        }
        if any("trg_memory_edges_single_parent" in x for x in schema):
            probes["single_parent"] = (ins, ("e4", "C", "B"), "PARENT_ALREADY_EXISTS")
        for name, (sql, args, marker) in probes.items():
            try:
                c.execute(sql, args)
                failures.append(name)
            except sqlite3.DatabaseError as exc:
                if marker and marker not in str(exc):
                    failures.append(f"{name}: unexpected {exc}")
        c.execute(
            "INSERT INTO audit_events (event_id, seq, event_type, entity_type, entity_id, "
            "actor_type, actor_id, occurred_at, reason, correlation_id) VALUES "
            "('ev', 1, 'MEMORY_CREATED', 'memory', 'A', 'user', 'u', 't', 'r', 'c')"
        )
        try:
            c.execute("UPDATE audit_events SET reason='x'")
            failures.append("audit_append_only")
        except sqlite3.DatabaseError:
            pass
        try:
            c.execute("UPDATE memories SET status='conflicted' WHERE memory_id='A'")
            failures.append("conflicted_requires_evidence")
        except sqlite3.DatabaseError:
            pass
        if has_spaces:
            try:
                c.execute("UPDATE memories SET scope_id='nope' WHERE memory_id='A'")
                failures.append("memory_space_required")
            except sqlite3.DatabaseError as exc:
                if "SPACE_NOT_FOUND" not in str(exc):
                    failures.append(f"memory_space_required: unexpected {exc}")
        if any(x.startswith("CREATE VIRTUAL TABLE memory_fts") for x in schema):
            c.execute(
                "INSERT INTO memory_versions (version_id, memory_id, version, content, summary, "
                "content_hash, created_by, created_at, change_reason) VALUES "
                "('v1', 'A', 1, 'probe text', NULL, 'h', 'u', 't', NULL)"
            )
            if c.execute("SELECT COUNT(*) FROM memory_fts WHERE memory_id='A'").fetchone()[0] != 1:
                failures.append("fts_sync")
            if any(x.startswith("CREATE VIRTUAL TABLE memory_fts_compact") for x in schema):
                row = c.execute("SELECT content FROM memory_fts_compact WHERE memory_id='A'")
                if [r[0] for r in row.fetchall()] != ["probetext"]:
                    failures.append("fts_compact_sync")
            n_out = c.execute("SELECT COUNT(*) FROM outbox_events WHERE aggregate_id='A'")
            if n_out.fetchone()[0] != 1:
                failures.append("outbox_enqueue")
            try:
                c.execute("DELETE FROM outbox_events")
                failures.append("outbox_no_delete")
            except sqlite3.DatabaseError:
                pass
        if any(x.startswith("CREATE TABLE memory_feedback") for x in schema):
            c.execute(
                "INSERT INTO memory_feedback VALUES ('f1', 'A', 'user', 'u', 'useful', 'r', 't')"
            )
            for name, sql in (
                ("feedback_no_update", "UPDATE memory_feedback SET reason='x'"),
                ("feedback_no_delete", "DELETE FROM memory_feedback"),
            ):
                try:
                    c.execute(sql)
                    failures.append(name)
                except sqlite3.DatabaseError:
                    pass
        if any(x.startswith("CREATE TABLE epoch_members") for x in schema):
            c.execute(
                "INSERT INTO epochs VALUES ('ep', 'n', NULL, 't', 't2', 'closed', 'u', 't', '{}')"
            )
            try:
                c.execute("INSERT INTO epoch_members VALUES ('ep', 'A', 't', 'u', NULL, NULL)")
                failures.append("epoch_closed_insert")
            except sqlite3.DatabaseError as exc:
                if "EPOCH_CLOSED" not in str(exc):
                    failures.append(f"epoch_closed_insert: unexpected {exc}")
            try:
                c.execute("UPDATE epochs SET status='open', end_at=NULL")
                failures.append("epoch_closed_final")
            except sqlite3.DatabaseError:
                pass
    finally:
        c.close()
    return failures


class IntegrityService:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def run(self) -> Report:
        report = Report(db=self.db_path.name, revision=None)
        if not self.db_path.exists():
            report.fatal = "database file not found"
            return report
        try:
            repo = IntegrityRepository(self.db_path)
        except sqlite3.Error as exc:
            report.fatal = f"cannot open database: {type(exc).__name__}"
            return report
        try:
            report.revision = repo.revision()
            if report.revision not in REVISIONS or REVISIONS.index(
                report.revision
            ) < REVISIONS.index(MIN_REVISION):
                report.fatal = (
                    f"schema revision {report.revision} < {MIN_REVISION}; run migrations first"
                )
                return report
            self._checks(repo, report)
        except sqlite3.DatabaseError as exc:
            report.fatal = f"database unreadable: {type(exc).__name__}: {exc}"
        finally:
            repo.close()
        return report

    def _add(self, r: Report, code: str, sev: str, msg: str, items: list[str], fix: str) -> None:
        if items:
            r.findings.append(Finding(code, sev, msg, items, fix))

    def _checks(self, repo: IntegrityRepository, r: Report) -> None:
        integ = repo.integrity_check()
        fks = repo.foreign_key_check()
        bad = [] if integ == ["ok"] else integ
        bad += [f"fk:{t}:rowid={rid}->{p}" for t, rid, p, _ in fks]
        self._add(r, "G009", "error", "integrity_check/foreign_key_check failed", bad,
                  "restore the latest good backup (ame restore)")  # fmt: skip
        r.checked.append("G009")
        edges = repo.active_structural_edges()
        cyc = find_cycle_nodes(edges)
        self._add(r, "G001", "error", "cycle in active structural edges", cyc,
                  "revoke one edge on the cycle (keep history; never delete)")  # fmt: skip
        loops = [e for e, a, b, _ in edges if a == b]
        self._add(r, "G002", "error", "self-loop edge", loops, "revoke the self-loop edge")
        dang = [f"edge:{x[0]}" for x in repo.dangling_edges()]
        dang += [f"relation:{x[0]}" for x in repo.dangling_relations()]
        self._add(r, "G003", "error", "link endpoint missing", dang,
                  "restore the missing memory from backup, or revoke the link")  # fmt: skip
        dup = [f"{a}->{b}:{t}x{n}" for a, b, t, n in repo.duplicate_active_edges()]
        self._add(r, "G004", "error", "duplicate active edge", dup, "revoke the extra edges")
        un = [f"{rid}:{t}" for rid, t in repo.unnormalized_undirected_relations()]
        self._add(r, "G005", "error", "undirected relation not normalized", un,
                  "revoke and re-create the relation via the service")  # fmt: skip
        sup = [x[0] for x in repo.superseded_without_edge()]
        self._add(r, "G006", "warning", "superseded memory without active SUPERSEDES edge", sup,
                  "user decides: T11 (back to active) or re-link the successor")  # fmt: skip
        trig = repo.triggers()
        stored = repo.stored_fingerprints()
        need = required_triggers(r.revision or HEAD)
        g7 = [f"missing:{n}" for n in need if n not in trig]
        g7 += [
            f"changed:{n}"
            for n in need
            if n in trig and stored.get(n) not in (None, fingerprint(trig[n]))
        ]
        g7 += [f"no-fingerprint:{n}" for n in need if n in trig and n not in stored]
        self._add(r, "G007", "error", "guard trigger missing or modified", g7,
                  "restore from backup; triggers are defined only by migrations")  # fmt: skip
        failures = _probe_triggers(repo.schema_sql())
        self._add(r, "G008", "error", "guard triggers did not fire on probe", failures,
                  "restore from backup")  # fmt: skip
        rv = [f"{k}:{i}" for k, i in repo.revoked_links_missing_metadata()]
        self._add(r, "G010", "error", "revoked link without revoke metadata", rv,
                  "manual review; history rows are never deleted")  # fmt: skip
        g11w = [x[0] for x in repo.conflicted_without_open_conflict()]
        self._add(r, "G011", "warning", "conflicted memory has no open conflict", g11w,
                  "user confirms T6 (conflicted -> active) or T7")  # fmt: skip
        g11e = [f"{c}:{rel}" for c, rel in repo.open_conflicts_on_revoked_relation()]
        self._add(r, "G011", "error", "open conflict on a revoked relation", g11e,
                  "resolve the conflict as relation_revoked via the service")  # fmt: skip
        r.checked += [f"G{n:03d}" for n in range(1, 12) if n != 9]
        if "memory_fts" in repo.tables():
            g12 = repo.lexical_index_mismatches()
            self._add(r, "G012", "error", "lexical index out of sync with current versions", g12,
                      "run `ame index rebuild-lexical` (rebuilds it from SQLite)")  # fmt: skip
            r.checked.append("G012")
        if "memory_fts_compact" in repo.tables():
            g13 = repo.compact_index_mismatches()
            self._add(r, "G013", "error", "compact CJK index out of sync with current versions",
                      g13, "run `ame index rebuild-lexical` (rebuilds it from SQLite)")  # fmt: skip
            r.checked.append("G013")
        if "granted_at" in repo.columns("space_members"):
            g14 = repo.bad_space_members()
            self._add(r, "G014", "error", "invalid space_members rows (access list)",
                      g14, "fix the listed grants with `ame space grant/revoke`")  # fmt: skip
            r.checked.append("G014")
        if "epochs" in repo.tables() or "memory_feedback" in repo.tables():
            g15 = repo.bad_phase4_rows()
            self._add(r, "G015", "error", "invalid feedback / epoch rows", g15,
                      "inspect the listed rows; restore a backup if edited by hand")  # fmt: skip
            r.checked.append("G015")
        if "distill_jobs" in repo.tables():
            g16 = repo.bad_distill_jobs()
            self._add(r, "G016", "error", "invalid distill drafts", g16,
                      "drafts must stay candidate until the user promotes them")  # fmt: skip
            r.checked.append("G016")
        if "trg_memory_edges_single_parent" in need:
            g17 = repo.multiple_active_parents()
            self._add(
                r,
                "G017",
                "warning",
                "memory has multiple active PARENT edges (parent tree requires at most one)",
                g17,
                "revoke the extra PARENT edges and re-create them as REFERENCE",
            )
            r.checked.append("G017")
