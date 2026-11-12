"""`ame` command line: check-graph, backup, restore, migrate (ADR-011, ADR-014); Phase 4 adds
epoch, review, distill and decay (user-only, ADR-029..032).

Exit codes: 0 ok, 1 warnings, 2 errors, 3 cannot check / operation failed, 64 usage error.
Never prints memory content; paths are printed for the operator only.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn

from ame.application.integrity_service import (
    EXIT_CANNOT,
    EXIT_ERROR,
    EXIT_OK,
    EXIT_USAGE,
    IntegrityService,
)
from ame.application.schemas import Actor
from ame.config import Settings
from ame.domain.enums import ActorType
from ame.errors import AMEError

CLI_USER = Actor(actor_type=ActorType.USER, actor_id="user:cli")


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:  # argparse default is exit 2; we reserve 2
        self.print_usage(sys.stderr)
        sys.stderr.write(f"ame: error: {message}\n")
        raise SystemExit(EXIT_USAGE)


def _db(arg: str | None) -> Path:
    return Path(arg) if arg else Settings().db_path


def _check_graph(ns: argparse.Namespace) -> int:
    report = IntegrityService(_db(ns.db)).run()
    if ns.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        return report.exit_code
    print(f"AME check-graph  db={report.db}  revision={report.revision}")
    if report.fatal:
        print(f"CANNOT CHECK: {report.fatal}")
        return report.exit_code
    for f in report.findings:
        print(f"[{f.severity.upper()}] {f.code} {f.message} ({len(f.items)})")
        if ns.verbose:
            for it in f.items:
                print(f"    - {it}")
        if ns.repair_dry_run and f.repair:
            print(f"    repair (dry-run, nothing changed): {f.repair}")
    if not report.findings:
        print(f"OK: {', '.join(sorted(set(report.checked)))} passed")
    print(f"exit={report.exit_code}")
    return report.exit_code


def _backup(ns: argparse.Namespace) -> int:
    from ame.infrastructure.backup import backup_database

    out = backup_database(_db(ns.db), Path(ns.dest) if ns.dest else None)
    print(f"backup written: {out}")
    return EXIT_OK


def _restore(ns: argparse.Namespace) -> int:
    from ame.infrastructure.backup import restore_database

    if not ns.yes:
        print("restore overwrites the database; stop AME first and re-run with --yes")
        return EXIT_USAGE
    saved = restore_database(Path(ns.backup), _db(ns.db))
    print(f"restored from {ns.backup}; previous database saved as {saved}")
    return EXIT_OK


def _migrate(ns: argparse.Namespace) -> int:
    from ame.infrastructure.migration_runner import MigrationRunner

    rep = MigrationRunner(_db(ns.db)).upgrade()
    print(f"migrated {rep.from_revision} -> {rep.to_revision}; backup={rep.backup}")
    return EXIT_OK


def _graph_order(ns: argparse.Namespace) -> int:
    from ame.application.topology_service import TopologyService
    from ame.domain.enums import EdgeType

    types = [EdgeType(t) for t in ns.edge_type] if ns.edge_type else None
    res = TopologyService(_db(ns.db)).order(types, ns.limit)
    if ns.json:
        print(json.dumps(res.model_dump(mode="json"), ensure_ascii=False, indent=2))
    else:
        print(f"AME graph-order  nodes={res.node_count} edges={res.edge_count} "
              f"truncated={res.truncated}")  # fmt: skip
        for i, mid in enumerate(res.memory_ids, 1):
            print(f"{i}\t{mid}")
    return EXIT_OK


def _space(ns: argparse.Namespace) -> int:
    from ame.application.schemas import SpaceCreate
    from ame.application.space_service import SpaceService
    from ame.infrastructure.database import create_sqlite_engine
    from ame.infrastructure.unit_of_work import make_session_factory

    settings = _settings_for(ns.db)
    if not settings.db_path.exists():
        print("database file not found; start AME or run `ame migrate` first", file=sys.stderr)
        return EXIT_CANNOT
    engine = create_sqlite_engine(settings)
    try:
        svc = SpaceService(make_session_factory(engine))
        if ns.space_cmd == "create":
            cmd = SpaceCreate(space_id=ns.space_id, name=ns.name or ns.space_id,
                              scope_type=ns.scope_type)  # fmt: skip
            s = svc.create_space(cmd, CLI_USER)
            print(f"space created: {s.space_id} ({s.scope_type})")
        elif ns.space_cmd == "grant":
            from ame.application.schemas import SpaceGrant

            m = svc.grant(
                ns.space_id,
                SpaceGrant(agent_id=ns.agent_id, role=ns.role, reason=ns.reason),
                CLI_USER,
            )
            print(f"granted: {m.agent_id} -> {m.space_id} ({m.role})")
        elif ns.space_cmd == "revoke":
            m = svc.revoke(ns.space_id, ns.agent_id, ns.reason, CLI_USER)
            print(f"revoked: {m.agent_id} -> {m.space_id}")
        elif ns.space_cmd == "members":
            for m in svc.list_members(ns.space_id, CLI_USER):
                state = "revoked" if m.revoked_at else "active"
                print(f"{m.agent_id}\t{m.role}\t{state}")
        else:
            for s in svc.list_spaces():
                print(f"{s.space_id}\t{s.scope_type}\t{s.name}")
    finally:
        engine.dispose()
    return EXIT_OK


def _settings_for(db: str | None) -> Settings:
    if db:
        p = Path(db)
        return Settings(data_dir=p.parent, db_filename=p.name)
    return Settings()


def _agent(ns: argparse.Namespace) -> int:
    """Phase 3a (ADR-024): only the local user (this CLI) issues agent keys."""
    from ame.application.schemas import AgentCreate
    from ame.application.space_service import AgentService
    from ame.infrastructure.database import create_sqlite_engine
    from ame.infrastructure.unit_of_work import make_session_factory

    settings = _settings_for(ns.db)
    if not settings.db_path.exists():
        print("database file not found; start AME or run `ame migrate` first", file=sys.stderr)
        return EXIT_CANNOT
    engine = create_sqlite_engine(settings)
    try:
        svc = AgentService(make_session_factory(engine))
        if ns.agent_cmd == "create":
            a = svc.register_agent(
                AgentCreate(agent_id=ns.agent_id, display_name=ns.name or ns.agent_id,
                            agent_type=ns.agent_type), CLI_USER)  # fmt: skip
            print(f"agent registered: {a.agent_id}")
            if not ns.no_key:
                cred = svc.issue_credential(a.agent_id, CLI_USER, "initial key")
                print("API key (shown ONCE, store it now):")
                print(cred.api_key)
        elif ns.agent_cmd == "rotate-key":
            cred = svc.issue_credential(ns.agent_id, CLI_USER, ns.reason)
            print("new API key (shown ONCE; the old key no longer works):")
            print(cred.api_key)
        elif ns.agent_cmd in ("disable", "enable"):
            a = svc.set_enabled(ns.agent_id, ns.agent_cmd == "enable", CLI_USER, ns.reason)
            print(f"agent {a.agent_id}: {'enabled' if a.enabled else 'disabled'}")
        else:
            for a in svc.list_agents():
                key = "key" if a.has_credential else "no-key"
                print(f"{a.agent_id}\t{'enabled' if a.enabled else 'disabled'}\t{key}")
    finally:
        engine.dispose()
    return EXIT_OK


def _token(ns: argparse.Namespace) -> int:
    """User token: `rotate` makes a new one (applied on the next request); `show` prints it."""
    from ame.infrastructure.token_store import (
        default_protector,
        load_or_create_token,
        rotate_token,
    )

    settings = Settings()
    prot = default_protector()
    if ns.token_cmd == "rotate":
        rotate_token(settings.data_dir, prot)
        print("user token rotated; a running AME uses it from the next request (no restart).")
        print("run `ame token show` to read it.")
    else:
        print(load_or_create_token(settings.data_dir, prot))
    return EXIT_OK


def _index(ns: argparse.Namespace) -> int:
    from ame.application.index_worker import IndexWorker
    from ame.infrastructure.database import create_sqlite_engine
    from ame.infrastructure.embeddings import build_vector_backend
    from ame.infrastructure.unit_of_work import make_session_factory

    settings = Settings()
    if ns.db:
        p = Path(ns.db)
        settings = Settings(data_dir=p.parent, db_filename=p.name)
    if not settings.db_path.exists():
        print("database file not found; start AME or run `ame migrate` first", file=sys.stderr)
        return EXIT_CANNOT
    engine = create_sqlite_engine(settings)
    try:
        w = IndexWorker(make_session_factory(engine), backend=build_vector_backend(settings))
        if ns.index_cmd == "status":
            st = w.status()
            if ns.json:
                print(json.dumps(st, ensure_ascii=False, indent=2))
            else:
                print(f"AME index  mode={st['mode']}")
                print(f"outbox: {st['outbox']}")
                print(f"vector_state: {st['vector_state']}")
                print(f"oldest_pending_at: {st['oldest_pending_at']}")
        elif ns.index_cmd == "run":
            r = w.run_until_idle()
            print(f"processed={r.done} retried={r.retried} failed={r.failed} mode={w.mode}")
            return EXIT_OK if r.failed == 0 else EXIT_ERROR
        elif ns.index_cmd == "rebuild":
            n = w.rebuild()
            print(f"re-enqueued {n} memories (memory.reindex); run `ame index run` or start AME")
        elif ns.index_cmd == "retry-failed":
            print(f"reset {w.retry_failed()} failed events to pending")
        else:
            print(f"lexical index rebuilt from SQLite: {w.rebuild_lexical()} rows")
    finally:
        engine.dispose()
    return EXIT_OK


def _idempotency(ns: argparse.Namespace) -> int:
    """Phase 3b (ADR-027): replay records only; audit rows are never touched."""
    from ame.application.idempotency_service import IdempotencyService
    from ame.infrastructure.database import create_sqlite_engine
    from ame.infrastructure.unit_of_work import make_session_factory

    settings = _settings_for(ns.db)
    if not settings.db_path.exists():
        print("database file not found; start AME or run `ame migrate` first", file=sys.stderr)
        return EXIT_CANNOT
    engine = create_sqlite_engine(settings)
    try:
        svc = IdempotencyService(make_session_factory(engine), settings.idempotency_ttl_hours)
        if ns.idem_cmd == "purge":
            print(f"purged {svc.purge_expired()} expired idempotency records")
        else:
            st = svc.status()
            print(f"idempotency records: done={st.get('done', 0)} pending={st.get('pending', 0)}")
    finally:
        engine.dispose()
    return EXIT_OK


_MCP_SNIPPETS = """\
# ---- Codex: ~/.codex/config.toml   (UNVERIFIED on Windows) ----
[mcp_servers.ame]
command = "ame-mcp"
args = ["--agent", "{agent}"]
env = {{
  AME_MCP_ENABLED = "true",
  AME_MCP_READ_ONLY = "{ro}",
  AME_AUTO_APPROVE_AGENT_WRITES = "true"
}}

# ---- Claude Desktop: %APPDATA%\\Claude\\claude_desktop_config.json   (UNVERIFIED) ----
# ---- Cursor: %USERPROFILE%\\.cursor\\mcp.json   (UNVERIFIED) ----
{{"mcpServers": {{"ame": {{"command": "ame-mcp", "args": ["--agent", "{agent}"],
  "env": {{"AME_MCP_ENABLED": "true", "AME_MCP_READ_ONLY": "{ro}",
          "AME_AUTO_APPROVE_AGENT_WRITES": "true"}}}}}}}}
"""


def _mcp(ns: argparse.Namespace) -> int:
    """Phase 3c (ADR-028): issue a fresh key for an agent, store it locally (DPAPI on Windows,
    0600 elsewhere) and print client config snippets that contain NO key."""
    from ame.adapters.mcp.keystore import platform_protector, save_key
    from ame.application.space_service import AgentService
    from ame.infrastructure.database import create_sqlite_engine
    from ame.infrastructure.unit_of_work import make_session_factory

    settings = _settings_for(ns.db)
    if not settings.db_path.exists():
        print("database file not found; start AME or run `ame migrate` first", file=sys.stderr)
        return EXIT_CANNOT
    engine = create_sqlite_engine(settings)
    try:
        svc = AgentService(make_session_factory(engine))
        cred = svc.issue_credential(ns.agent, CLI_USER, "mcp setup")
    finally:
        engine.dispose()
    path = save_key(settings.data_dir, ns.agent, cred.api_key, platform_protector())
    print(f"key for {ns.agent} stored at {path} (the previous key of this agent no longer works)")
    print("paste ONE of these into your AI tool (no key inside):\n")
    print(_MCP_SNIPPETS.format(agent=ns.agent, ro="false"))
    print("also set AME_MCP_ENABLED=true and AME_MCP_READ_ONLY=false for the AME service itself.")
    return EXIT_OK


def _p4_open(ns: argparse.Namespace) -> tuple[Settings, object] | None:
    from ame.infrastructure.database import create_sqlite_engine

    settings = _settings_for(ns.db)
    if not settings.db_path.exists():
        print("database file not found; start AME or run `ame migrate` first", file=sys.stderr)
        return None
    return settings, create_sqlite_engine(settings)


def _epoch(ns: argparse.Namespace) -> int:
    """Phase 4 (ADR-030): only the local user (this CLI) creates or changes epochs."""
    from sqlalchemy.engine import Engine

    from ame.application.phase4_service import EpochService
    from ame.application.schemas import EpochClose, EpochCreate, EpochMemberChange
    from ame.infrastructure.unit_of_work import make_session_factory

    opened = _p4_open(ns)
    if opened is None:
        return EXIT_CANNOT
    _, engine = opened
    assert isinstance(engine, Engine)
    try:
        svc = EpochService(make_session_factory(engine))
        if ns.epoch_cmd == "create":
            e = svc.create(EpochCreate(name=ns.name, space_id=ns.space, reason=ns.reason),
                           CLI_USER)  # fmt: skip
            print(f"epoch created: {e.epoch_id}\t{e.name}")
        elif ns.epoch_cmd == "close":
            e = svc.close(ns.epoch_id, EpochClose(reason=ns.reason), CLI_USER)
            print(f"epoch closed: {e.epoch_id}\t{e.end_at}")
        elif ns.epoch_cmd in ("add", "remove"):
            ch = EpochMemberChange(memory_id=ns.memory_id, reason=ns.reason)
            fn = svc.add_member if ns.epoch_cmd == "add" else svc.remove_member
            e = fn(ns.epoch_id, ch, CLI_USER)
            print(f"epoch {e.epoch_id}: {len(e.member_ids)} active members")
        elif ns.epoch_cmd == "derive":
            from ame.application.phase4_service import SpineService

            sp_svc = SpineService(make_session_factory(engine))
            sp_res = sp_svc.spine(ns.scope, 200, None, CLI_USER)
            if ns.json:
                print(
                    json.dumps(
                        [ep.model_dump(mode="json") for ep in sp_res.derived_epochs],
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            else:
                for ep in sp_res.derived_epochs:
                    piv = ep.pivot_memory_id or "unanchored"
                    print(f"epoch[{ep.index}]\tpivot={piv}\tmembers={','.join(ep.memory_ids)}")
        else:
            for e in svc.list(CLI_USER):
                print(f"{e.epoch_id}\t{e.status}\t{e.space_id or '-'}\t{len(e.member_ids)}"
                      f"\t{e.name}")  # fmt: skip
    finally:
        engine.dispose()
    return EXIT_OK


def _review(ns: argparse.Namespace) -> int:
    """Phase 4 (ADR-032): only the user promotes or rejects candidate memories."""
    from sqlalchemy.engine import Engine

    from ame.application.phase4_service import ReviewService
    from ame.application.schemas import PromoteRequest
    from ame.infrastructure.unit_of_work import make_session_factory

    opened = _p4_open(ns)
    if opened is None:
        return EXIT_CANNOT
    _, engine = opened
    assert isinstance(engine, Engine)
    try:
        svc = ReviewService(make_session_factory(engine))
        if ns.review_cmd == "list":
            for it in svc.list(ns.scope, CLI_USER):
                kind = "distill" if it.is_distill_draft else "candidate"
                print(f"{it.memory_id}\t{kind}\t{it.scope_id}\t{it.created_by}")
        else:
            fn = svc.promote if ns.review_cmd == "promote" else svc.reject
            m = fn(ns.memory_id, PromoteRequest(reason=ns.reason), CLI_USER)
            print(f"{m.memory_id}: {m.status}")
    finally:
        engine.dispose()
    return EXIT_OK


def _distill(ns: argparse.Namespace) -> int:
    """Phase 4 (ADR-032): manual, deterministic distill; creates candidate drafts only."""
    from sqlalchemy.engine import Engine

    from ame.application.phase4_service import DistillService
    from ame.application.schemas import DistillRequest
    from ame.infrastructure.unit_of_work import make_session_factory

    opened = _p4_open(ns)
    if opened is None:
        return EXIT_CANNOT
    settings, engine = opened
    assert isinstance(engine, Engine)
    try:
        svc = DistillService(make_session_factory(engine), settings.distill_threshold)
        res = svc.run(DistillRequest(scope_id=ns.scope, tag=ns.tag, threshold=ns.threshold,
                                     reason=ns.reason), CLI_USER)  # fmt: skip
        print(f"distill: groups={res.groups_considered} created={len(res.created)} "
              f"skipped_existing={res.skipped_existing}")  # fmt: skip
        for d in res.created:
            print(f"{d.memory_id}\t{len(d.source_memory_ids)} sources\t(candidate)")
    finally:
        engine.dispose()
    return EXIT_OK


def _decay(ns: argparse.Namespace) -> int:
    """Phase 4 (ADR-029): suggestions are read-only; archive needs an explicit user command."""
    from sqlalchemy.engine import Engine

    from ame.application.phase4_service import DecayService
    from ame.application.schemas import DecayArchive
    from ame.infrastructure.unit_of_work import make_session_factory

    opened = _p4_open(ns)
    if opened is None:
        return EXIT_CANNOT
    settings, engine = opened
    assert isinstance(engine, Engine)
    try:
        svc = DecayService(make_session_factory(engine), settings.decay_half_life_days,
                           settings.decay_threshold,
                           settings.decay_protect_importance)  # fmt: skip
        if ns.decay_cmd == "suggest":
            r = svc.suggest(ns.scope, ns.threshold, ns.limit, CLI_USER)
            print(f"decay suggestions: {len(r.suggestions)} (threshold={r.threshold:g}, "
                  f"half-life={r.half_life_days:g} days; nothing archived)")  # fmt: skip
            for sgg in r.suggestions:
                print(f"{sgg.memory_id}\t{sgg.status}\tscore={sgg.score:.3f}"
                      f"\tage={sgg.age_days:.0f}d")  # fmt: skip
        else:
            m = svc.archive_suggested(DecayArchive(memory_id=ns.memory_id, reason=ns.reason),
                                      CLI_USER)  # fmt: skip
            print(f"{m.memory_id}: {m.status}")
    finally:
        engine.dispose()
    return EXIT_OK


def _spine(ns: argparse.Namespace) -> int:
    """Conflux heaviest-subtree Pivot Chain, derived epochs and total order (ADR-031/033)."""
    from sqlalchemy.engine import Engine

    from ame.application.phase4_service import SpineService
    from ame.infrastructure.unit_of_work import make_session_factory

    opened = _p4_open(ns)
    if opened is None:
        return EXIT_CANNOT
    settings, engine = opened
    assert isinstance(engine, Engine)
    try:
        svc = SpineService(
            make_session_factory(engine),
            settings.spine_weight_values(),
            settings.spine_freshness_half_life_days,
        )
        res = svc.spine(ns.scope, ns.limit, ns.epoch, CLI_USER)
        if ns.json:
            print(json.dumps(res.model_dump(mode="json"), ensure_ascii=False, indent=2))
        else:
            print(f"pivot_chain: {' -> '.join(res.pivot_chain) if res.pivot_chain else '(empty)'}")
            print(f"total_order: {', '.join(res.total_order) if res.total_order else '(empty)'}")
            for it in res.items:
                piv = "PIVOT" if it.on_pivot_chain else "-"
                print(
                    f"{it.memory_id}\tscore={it.score:.3f}\tsubW={it.subtree_weight:.3f}"
                    f"\t{piv}\tepoch={it.epoch_index}\trank={it.total_order_rank}"
                )
    finally:
        engine.dispose()
    return EXIT_OK


def _export_graph(ns: argparse.Namespace) -> int:
    """Static self-contained HTML/JSON graph export (ADR-034)."""
    from sqlalchemy.engine import Engine

    from ame.application.graph_export_service import GraphExportService
    from ame.infrastructure.unit_of_work import make_session_factory

    opened = _p4_open(ns)
    if opened is None:
        return EXIT_CANNOT
    settings, engine = opened
    assert isinstance(engine, Engine)
    try:
        svc = GraphExportService(
            make_session_factory(engine),
            settings.spine_weight_values(),
            settings.spine_freshness_half_life_days,
        )
        out_path = Path(ns.out)
        data = svc.export_file(out_path, scope_id=ns.scope, fmt=ns.format, actor=CLI_USER)
        print(
            f"graph exported: {out_path} (format={ns.format}, "
            f"nodes={len(data['nodes'])}, edges={len(data['edges'])})"
        )
    finally:
        engine.dispose()
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(prog="ame", description="Agent Memory Engine maintenance")
    sub = p.add_subparsers(dest="cmd", required=True, parser_class=_Parser)
    cg = sub.add_parser("check-graph", help="read-only integrity check (G001-G016)")
    cg.add_argument("--db")
    cg.add_argument("--verbose", "-v", action="store_true")
    cg.add_argument("--json", action="store_true")
    cg.add_argument("--repair-dry-run", action="store_true", help="show repairs, change nothing")
    cg.set_defaults(fn=_check_graph)
    b = sub.add_parser("backup", help="online backup (SQLite backup API, WAL-safe)")
    b.add_argument("--db")
    b.add_argument("--dest")
    b.set_defaults(fn=_backup)
    r = sub.add_parser("restore", help="restore a backup (current DB is saved first)")
    r.add_argument("backup")
    r.add_argument("--db")
    r.add_argument("--yes", action="store_true")
    r.set_defaults(fn=_restore)
    m = sub.add_parser("migrate", help="safe upgrade to head (backup + checks + auto-restore)")
    m.add_argument("--db")
    m.set_defaults(fn=_migrate)
    g = sub.add_parser("graph-order", help="read-only topological order of active structural edges")
    g.add_argument("--db")
    g.add_argument("--json", action="store_true")
    g.add_argument("--limit", type=int, default=1000)
    g.add_argument("--edge-type", action="append",
                   choices=["PARENT", "REFERENCE", "SUPERSEDES", "DERIVED_FROM"])  # fmt: skip
    g.set_defaults(fn=_graph_order)
    sp = sub.add_parser("space", help="spaces: create, list, grant, revoke, members (user only)")
    sp.add_argument("--db")
    ssub = sp.add_subparsers(dest="space_cmd", required=True, parser_class=_Parser)
    sc = ssub.add_parser("create")
    sc.add_argument("space_id")
    sc.add_argument("--scope-type", required=True, choices=["private", "project", "shared"])
    sc.add_argument("--name")
    ssub.add_parser("list")
    for name in ("grant", "revoke"):
        sg = ssub.add_parser(name)
        sg.add_argument("space_id")
        sg.add_argument("agent_id")
        if name == "grant":
            sg.add_argument("--role", required=True, choices=["reader", "editor", "owner"])
        sg.add_argument("--reason", required=True)
    ssub.add_parser("members").add_argument("space_id")
    sp.set_defaults(fn=_space)
    ag = sub.add_parser("agent", help="register agents and issue their keys (user only)")
    ag.add_argument("--db")
    asub = ag.add_subparsers(dest="agent_cmd", required=True, parser_class=_Parser)
    ac = asub.add_parser("create")
    ac.add_argument("agent_id")
    ac.add_argument("--agent-type", required=True)
    ac.add_argument("--name")
    ac.add_argument("--no-key", action="store_true")
    for name in ("rotate-key", "disable", "enable"):
        x = asub.add_parser(name)
        x.add_argument("agent_id")
        x.add_argument("--reason", required=True)
    asub.add_parser("list")
    ag.set_defaults(fn=_agent)
    tk = sub.add_parser("token", help="local user token: rotate / show")
    tsub = tk.add_subparsers(dest="token_cmd", required=True, parser_class=_Parser)
    tsub.add_parser("rotate")
    tsub.add_parser("show")
    tk.set_defaults(fn=_token)
    ix = sub.add_parser("index", help="derived search index: status/run/rebuild (SQLite is truth)")
    isub = ix.add_subparsers(dest="index_cmd", required=True, parser_class=_Parser)
    ist = isub.add_parser("status")
    ist.add_argument("--json", action="store_true")
    ist.add_argument("--db")
    for name in ("run", "rebuild", "rebuild-lexical", "retry-failed"):
        isub.add_parser(name).add_argument("--db")
    ix.set_defaults(fn=_index)
    idp = sub.add_parser("idempotency", help="Idempotency-Key records: status / purge expired")
    idsub = idp.add_subparsers(dest="idem_cmd", required=True, parser_class=_Parser)
    for name in ("status", "purge"):
        idsub.add_parser(name).add_argument("--db")
    idp.set_defaults(fn=_idempotency)
    mp = sub.add_parser("mcp", help="MCP adapter: setup an agent key for an AI tool (ADR-028)")
    msub = mp.add_subparsers(dest="mcp_cmd", required=True, parser_class=_Parser)
    ms = msub.add_parser("setup")
    ms.add_argument("--agent", required=True)
    ms.add_argument("--db")
    mp.set_defaults(fn=_mcp)
    ep = sub.add_parser("epoch", help="epochs (periods): create/list/close/add/remove (user only)")
    ep.add_argument("--db")
    esub = ep.add_subparsers(dest="epoch_cmd", required=True, parser_class=_Parser)
    ec = esub.add_parser("create")
    ec.add_argument("name")
    ec.add_argument("--space")
    ec.add_argument("--reason", required=True)
    esub.add_parser("list")
    esub.add_parser("close").add_argument("epoch_id")
    for name in ("add", "remove"):
        em = esub.add_parser(name)
        em.add_argument("epoch_id")
        em.add_argument("memory_id")
    for name in ("close", "add", "remove"):
        esub.choices[name].add_argument("--reason", required=True)
    ed = esub.add_parser("derive", help="Conflux auto-derived epochs from Pivot Chain")
    ed.add_argument("--scope")
    ed.add_argument("--json", action="store_true")
    ep.set_defaults(fn=_epoch)
    rv = sub.add_parser("review", help="review candidate memories: list/promote/reject (user only)")
    rv.add_argument("--db")
    rsub = rv.add_subparsers(dest="review_cmd", required=True, parser_class=_Parser)
    rsub.add_parser("list").add_argument("--scope")
    for name in ("promote", "reject"):
        rp = rsub.add_parser(name)
        rp.add_argument("memory_id")
        rp.add_argument("--reason", required=True)
    rv.set_defaults(fn=_review)
    ds = sub.add_parser("distill", help="manual distill into candidate drafts (no LLM)")
    ds.add_argument("--db")
    dsub = ds.add_subparsers(dest="distill_cmd", required=True, parser_class=_Parser)
    dr = dsub.add_parser("run")
    dr.add_argument("--scope", required=True)
    dr.add_argument("--tag")
    dr.add_argument("--threshold", type=float)
    dr.add_argument("--reason", required=True)
    ds.set_defaults(fn=_distill)
    dc = sub.add_parser("decay", help="decay: read-only suggestions; archive one on request")
    dc.add_argument("--db")
    dcsub = dc.add_subparsers(dest="decay_cmd", required=True, parser_class=_Parser)
    dsg = dcsub.add_parser("suggest")
    dsg.add_argument("--scope")
    dsg.add_argument("--threshold", type=float)
    dsg.add_argument("--limit", type=int, default=50)
    da = dcsub.add_parser("archive")
    da.add_argument("memory_id")
    da.add_argument("--reason", required=True)
    dc.set_defaults(fn=_decay)
    # plan wording (PHASE_4_REVIEW 4f): `ame archive --from-suggestion <id>` = `ame decay archive`
    arc = sub.add_parser("archive", help="archive ONE memory that is a current decay suggestion")
    arc.add_argument("--db")
    arc.add_argument("--from-suggestion", dest="memory_id", required=True, metavar="MEMORY_ID")
    arc.add_argument("--reason", required=True)
    arc.set_defaults(fn=_decay, decay_cmd="archive")
    spn = sub.add_parser("spine", help="Conflux heaviest-subtree Pivot Chain & total order")
    spn.add_argument("--db")
    spn.add_argument("--scope")
    spn.add_argument("--epoch")
    spn.add_argument("--limit", type=int, default=20)
    spn.add_argument("--json", action="store_true")
    spn.set_defaults(fn=_spine)
    eg = sub.add_parser("export-graph", help="export static HTML/JSON graph snapshot (zero CDN)")
    eg.add_argument("--db")
    eg.add_argument("--out", required=True, help="output file path (.html or .json)")
    eg.add_argument("--scope")
    eg.add_argument("--format", choices=["html", "json"], default="html")
    eg.set_defaults(fn=_export_graph)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    ns = build_parser().parse_args(argv)
    try:
        return int(ns.fn(ns))
    except AMEError as exc:
        print(f"ame: {exc.code}: {exc}", file=sys.stderr)
        return EXIT_CANNOT


if __name__ == "__main__":
    raise SystemExit(main())
