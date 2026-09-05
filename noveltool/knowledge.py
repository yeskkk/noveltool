"""Read-only, in-memory projection of validated observations.

SQLite retains history; this catalog selects source-valid, non-overlapping
successful runs across revisions and plans. Failed retries never erase successful results. Textual
name matches are conservative, conflicts stay visible, and no 'confidence number'
is used as proof. M9 builds editable overlays on this same projection.
"""
from __future__ import annotations

from collections import defaultdict
import json
import unicodedata
from uuid import UUID, uuid5

from pydantic import ValidationError
from .analysis import SCHEMAS, compact
from .llm_schemas import (EntityFinding, FactFinding, EventFinding, RelationshipFinding,
                          ThreadFinding, NarrativeFinding)
from .structured_llm import strict_loads, StructuredError

PAYLOADS = {"entity": EntityFinding, "fact": FactFinding, "event": EventFinding,
            "relationship": RelationshipFinding, "thread": ThreadFinding, "narrative": NarrativeFinding}


def normalize_name(value: str) -> str:
    # Do not strip meaningful punctuation or use substring/fuzzy matching.
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def entity_id(project_id: str, kind: str, name: str) -> str:
    return uuid5(UUID(project_id), kind+":"+normalize_name(name)).hex


def load_records(session) -> tuple[list[dict], list[str]]:
    session.semantic.refresh_locked()
    latest = {run['id']: run for run in session.semantic.selected}
    blocks = {b.id: b.text for b in session.manuscript.blocks}
    offsets, cursor = {}, 0
    for b in session.manuscript.blocks:
        offsets[b.id] = cursor
        cursor += len(b.text)
    records, errors = [], []
    for run in latest.values():
        # Evidence and payloads are checked again at the disk -> runtime boundary.
        for row in session.store.connection.execute("SELECT * FROM observations WHERE run_id=? ORDER BY ordinal", (run["id"],)):
            try:
                evidence = strict_loads(row["evidence_json"])
                if not isinstance(evidence, list) or not 1 <= len(evidence) <= 8:
                    raise ValueError("证据数量不合法")
                mapped = []
                at_cp = 0
                for i, e in enumerate(evidence):
                    if not isinstance(e, dict) or set(e) != {"block_id", "start_cp", "end_cp", "scope", "quote"}:
                        raise ValueError("证据字段不合法")
                    text = blocks.get(e["block_id"])
                    a, b = e["start_cp"], e["end_cp"]
                    if text is None or type(a) is not int or type(b) is not int or not 0 <= a < b <= len(text):
                        raise ValueError("证据范围无效")
                    if e["scope"] not in {"core", "overlap"} or not isinstance(e["quote"], str) or not e["quote"].strip() or e["quote"] not in text[a:b]:
                        raise ValueError("摘录与证据不匹配")
                    if e["scope"] == "core":
                        at_cp = max(at_cp, offsets[e["block_id"]] + b)
                    mapped.append({"block": f"B{i+1:03d}", "quote": e["quote"]})
                if not at_cp:
                    raise ValueError("无核心证据")
                payload = strict_loads(row["payload_json"])
                schema = PAYLOADS[row["kind"]]
                schema.model_validate({**payload, "evidence": mapped}, strict=True)
                records.append({"id": row["id"], "kind": row["kind"], "payload": payload,
                    "evidence": evidence, "status": row["status"], "at_cp": at_cp,
                    "chunk_ordinal": run["ordinal"], "run_id": run["id"],
                    "requires_review": bool(run["requires_review"]) and row["status"] != "accepted"})
            except (ValueError, TypeError, KeyError, ValidationError, StructuredError, RecursionError):
                errors.append(f"观察 {row['id']} 无法通过磁盘数据校验，已隔离；正文未改变")
    records.sort(key=lambda r: (r["at_cp"], r["chunk_ordinal"], r["id"]))
    return records, errors


def build_graph(project_id: str, records: list[dict], profiles=()) -> dict:
    entities, by_name, reviews = {}, defaultdict(set), []
    aliases, remap = {}, {}
    for profile in profiles:
        if not profile.active:
            continue
        entities[profile.id] = {"id": profile.id, "name": profile.name, "kind": profile.kind,
            "names": [profile.name, *profile.aliases], "notes": profile.notes, "observation_ids": [],
            "facts": [], "states": [], "source": "manual"}
        for name in [profile.name, *profile.aliases]:
            aliases[(profile.kind, normalize_name(name))] = profile.id
            by_name[normalize_name(name)].add(profile.id)
    usable = []
    for r in records:
        if r["status"] == "rejected":
            continue
        if r["requires_review"]:
            reviews.append({"reason": "输出经过修复，等待人工核对", "observation_ids": [r["id"]]})
            continue
        usable.append(r)
        if r["kind"] == "entity":
            p = r["payload"]
            original_id = entity_id(project_id, p["kind"], p["name"])
            eid = original_id if original_id in entities else aliases.get((p["kind"], normalize_name(p["name"])), original_id)
            remap[original_id] = eid
            entity = entities.setdefault(eid, {"id": eid, "name": p["name"], "kind": p["kind"],
                "names": [], "observation_ids": [], "facts": [], "states": [], "source": "auto"})
            if p["name"] not in entity["names"]:
                entity["names"].append(p["name"])
            entity["observation_ids"].append(r["id"])
            by_name[normalize_name(p["name"])].add(eid)

    def resolve(name: str, record: dict) -> str | None:
        matches = by_name.get(normalize_name(name), set())
        if len(matches) == 1:
            return next(iter(matches))
        reviews.append({"reason": f"实体名称未能唯一匹配：{name}", "observation_ids": [record["id"]]})
        return None

    fact_groups = {}
    events, relations, threads, narrative = [], [], [], []
    seen = {}
    for r in usable:
        p = r["payload"]
        common = {"id": r["id"], "at_cp": r["at_cp"], "evidence": r["evidence"],
                  "observation_ids": [r["id"]], "source": "auto"}
        if r["kind"] == "fact":
            eid = resolve(p["subject"], r)
            if not eid:
                continue
            if p["mode"] == "uncertain":
                reviews.append({"reason": "事实被模型标为不确定", "observation_ids": [r["id"]]})
                continue
            value = {**common, "value": p["value"], "field": p["field"]}
            if p["mode"] == "state":
                entities[eid]["states"].append(value)
            else:
                key = (eid, normalize_name(p["field"]))
                group = fact_groups.setdefault(key, {"field": p["field"], "values": []})
                existing = next((v for v in group["values"] if v["value"] == p["value"]), None)
                if existing:
                    existing["observation_ids"].append(r["id"])
                    existing["evidence"].extend(e for e in r["evidence"] if e not in existing["evidence"])
                    existing["at_cp"] = min(existing["at_cp"], r["at_cp"])
                else:
                    group["values"].append(value)
        elif r["kind"] == "event":
            participants = [resolve(n, r) for n in p["participants"]]
            if any(eid is None for eid in participants):
                continue
            events.append({**common, **p, "participants": participants})
        elif r["kind"] == "relationship":
            a, b = resolve(p["a"], r), resolve(p["b"], r)
            if a is None or b is None or a == b:
                continue
            relations.append({**common, **p, "a": a, "b": b})
        elif r["kind"] == "thread":
            refs = [resolve(n, r) for n in p["related_entities"]]
            if any(eid is None for eid in refs):
                continue
            threads.append({**common, **p, "related_entities": refs})
        elif r["kind"] == "narrative":
            narrative.append({**common, **p})

    for (eid, _), group in fact_groups.items():
        group["conflict"] = len(group["values"]) > 1
        entities[eid]["facts"].append(group)
        if group["conflict"]:
            reviews.append({"reason": f"{entities[eid]['name']} 的 {group['field']} 存在不同取值，不自动覆盖",
                            "observation_ids": [oid for v in group["values"] for oid in v["observation_ids"]]})
    # Dedup only identical payload/evidence at the SAME source position. Do not
    # collapse repeated events at different places or fuzzy names.
    def dedup(items):
        out = []
        known = {}
        for item in items:
            key = compact({k: v for k, v in item.items() if k not in {"id", "observation_ids"}})
            if key in known:
                known[key]["observation_ids"].extend(item["observation_ids"])
            else:
                known[key] = item
                out.append(item)
        return out
    return {"entities": list(entities.values()), "events": dedup(events), "relationships": dedup(relations),
            "threads": dedup(threads), "narrative": dedup(narrative), "review_queue": reviews, "entity_remap": remap}


def apply_entries(graph: dict, entries: list[dict]) -> None:
    """Entries passed here are active and applicable at the chosen narrative position."""
    entities = {e["id"]: e for e in graph["entities"]}
    remap = graph.get("entity_remap", {})
    for e in sorted(entries, key=lambda x: (x["at_cp"], x["id"])):
        p = dict(e["payload"])
        for key in ("entity_id", "a", "b"):
            if key in p:
                p[key] = remap.get(p[key], p[key])
        for key in ("participants", "related_entities"):
            if key in p:
                p[key] = [remap.get(x, x) for x in p[key]]
        refs = ([p["entity_id"]] if e["kind"] in {"fact", "state"} else
                [p["a"], p["b"]] if e["kind"] == "relationship" else
                p.get("participants", p.get("related_entities", [])))
        if any(ref not in entities for ref in refs):
            graph["review_queue"].append({"reason": "人工条目的关联实体缺失", "entry_id": e["id"], "observation_ids": []})
            continue
        common = {"id": e["id"], "at_cp": e["at_cp"], "evidence": [], "observation_ids": [], "source": "manual"}
        if e["kind"] == "fact":
            entity = entities[p["entity_id"]]
            value = {**common, "field": p["field"], "value": p["value"]}
            group = next((f for f in entity["facts"] if normalize_name(f["field"]) == normalize_name(p["field"])), None)
            if group:
                group.setdefault("automatic_values", group["values"] if not group.get("manual_override") else [])
                group.update(values=[value], conflict=False, manual_override=True)
            else:
                entity["facts"].append({"field": p["field"], "values": [value], "conflict": False, "manual_override": True})
        elif e["kind"] == "state":
            entities[p["entity_id"]]["states"].append({**common, **p})
        elif e["kind"] == "event":
            graph["events"].append({**common, **p})
        elif e["kind"] == "relationship":
            graph["relationships"].append({**common, **p})
        elif e["kind"] == "thread":
            graph["threads"].append({**common, **p})
        elif e["kind"] == "note":
            graph["narrative"].append({**common, **p})
    for entity in entities.values():
        entity["states"].sort(key=lambda x: (x["at_cp"], x["source"] == "manual", x["id"]))
    for key in ("events", "relationships", "threads", "narrative"):
        graph[key].sort(key=lambda x: (x["at_cp"], x["source"] == "manual", x["id"]))


class KnowledgeService:
    def __init__(self, session):
        self.session = session
        self.cache_key = None
        self.records: list[dict] = []
        self.graph: dict = {}
        self.errors: list[str] = []
        self.version = ""

    def refresh_locked(self):
        s = self.session
        key = (s.manuscript.revision_no, s.analysis.epoch,
               s.imports.last_plan.id if s.imports.last_plan else None, s.settings.version)
        if key == self.cache_key:
            return
        self.records, self.errors = load_records(s)
        entries = s.settings.entry_views_locked()
        active = [e for e in entries if e["active"] and not e["anchor_stale"]]
        suppress = {oid for e in active for oid in e["replaces"]}
        self.graph = build_graph(s.project.data.meta.id, [r for r in self.records if r["id"] not in suppress], s.settings.profiles.values())
        apply_entries(self.graph, active)
        from hashlib import sha256
        self.version = sha256(compact([s.manuscript.revision_no, key[2], s.settings.version,
                     [(r["id"], r["status"]) for r in self.records]]).encode()).hexdigest()
        self.cache_key = key

    async def view(self) -> dict:
        async with self.session.lock:
            self.refresh_locked()
            return {"revision_no": self.session.manuscript.revision_no,
                    "plan_id": self.session.imports.last_plan.id if self.session.imports.last_plan else None,
                    "record_count": len(self.records), "errors": self.errors + self.session.settings.load_errors, **self.graph,
                    "version": self.version, "profiles": [p.model_dump() for p in self.session.settings.profiles.values()],
                    "entries": self.session.settings.entry_views_locked(), "text_length": len(self.session.manuscript.text),
                    "observations": self.records,
                    "semantic_sync": self.session.semantic.status_locked(),
                    "notice": "人工设定与自动观察分开保存。人工条目不会被分析覆盖；冲突和失效定位需要核对。"}
