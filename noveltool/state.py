"""Pure narrative-position projection and explicit state reduction.

Filter evidence BEFORE merging. A future stable fact or a future human override
must not affect an earlier state. This is knowledge at a narrative position,
not an inferred calendar-time simulation; flashbacks still need author review.
"""
from __future__ import annotations
from collections import defaultdict
from itertools import groupby
from .analysis import compact
from .knowledge import build_graph, apply_entries, normalize_name
from .manuscript import ManuscriptError


def reduce_fields(entity: dict) -> list[dict]:
    stable, changes = {}, defaultdict(list)
    for group in entity["facts"]:
        stable[normalize_name(group["field"])] = group
    for change in entity["states"]:
        changes[normalize_name(change["field"])].append(change)
    output = []
    for key in sorted(set(stable) | set(changes)):
        base = stable.get(key)
        history = list(changes.get(key, []))
        # A dated human fact is authoritative from its anchor, not a timeless
        # initialization which an older state could accidentally overwrite.
        base_values = base["values"] if base else []
        if base and base.get("manual_override"):
            history.extend({**v,"operation":"set","fact_override":True} for v in base["values"])
            base_values = base.get("automatic_values", [])
        history.sort(key=lambda r:(r["at_cp"],r["id"]))
        field = base["field"] if base else history[0]["field"]
        vals = list(dict.fromkeys(v["value"] for v in base_values)) if base else []
        conflicts, evidence = [], [v["id"] for v in base_values]
        status = "known" if len(vals)==1 else "conflict" if vals else "unknown"
        value = vals[0] if len(vals)==1 else None
        if len(vals)>1:
            conflicts.append({"reason":"相对稳定事实存在不同取值", "values":vals})
        for at, group in groupby(history, key=lambda r:r["at_cp"]):
            rows = list(group)
            setters = [r for r in rows if r.get("operation", "set")=="set"]
            manual = [r for r in setters if r["source"]=="manual"]
            setters = manual or setters
            if setters:
                values = list(dict.fromkeys(r["value"] for r in setters))
                evidence = [r["id"] for r in setters]
                if len(values)>1:
                    status,value="conflict",None
                    conflicts=[{"reason":"同一叙述位置有多个不同 set，无法确定先后", "at_cp":at,"values":values}]
                else:
                    value,status=values[0],"known"
                    conflicts=[]
            operations = [r for r in rows if r.get("operation", "set")!="set"]
            adds = {r["value"] for r in operations if r["operation"]=="add"}
            removes = {r["value"] for r in operations if r["operation"]=="remove"}
            if adds & removes:
                status,value="conflict",None
                conflicts.append({"reason":"同一位置同时增加和移除同一项", "at_cp":at,"values":sorted(adds & removes)})
            elif operations and status!="conflict":
                if status=="unknown":
                    status="partial"  # Only known additions; not a complete inventory.
                collection=set(value if isinstance(value,list) else [value] if value is not None else [])
                collection.update(adds);collection.difference_update(removes)
                value=sorted(collection)
                evidence.extend(r["id"] for r in operations)
        output.append({"field":field,"status":status,"value":value,"conflicts":conflicts,
                       "source_ids":list(dict.fromkeys(evidence)),"history":history})
    return output


def latest_slots(items: list[dict], key_fn, value_fn) -> list[dict]:
    grouped=defaultdict(list)
    for item in items:grouped[key_fn(item)].append(item)
    result=[]
    for values in grouped.values():
        latest=max(v["at_cp"] for v in values)
        current=[v for v in values if v["at_cp"]==latest]
        manual=[v for v in current if v["source"]=="manual"]
        current=manual or current
        distinct={compact(value_fn(v)) for v in current}
        if len(distinct)==1:
            result.append({**current[0],"conflict":False})
        else:
            result.append({**current[0],"conflict":True,"alternatives":current})
    return sorted(result,key=lambda r:(r["at_cp"],r["id"]))


def project_at(project_id: str, records: list[dict], profiles, entries: list[dict], at_cp: int) -> dict:
    applicable=[e for e in entries if e["active"] and not e["anchor_stale"] and e["at_cp"]<=at_cp]
    suppress={oid for e in applicable for oid in e["replaces"]}
    prior=[r for r in records if r["at_cp"]<=at_cp and r["id"] not in suppress]
    graph=build_graph(project_id,prior,profiles)
    apply_entries(graph,applicable)
    for entity in graph["entities"]:
        entity["effective_fields"]=reduce_fields(entity)
    graph["relationships"]=latest_slots(graph["relationships"],
        lambda r:(r["a"],r["b"],normalize_name(r["label"])),lambda r:r["description"])
    graph["threads"]=latest_slots(graph["threads"],lambda r:normalize_name(r["title"]),
        lambda r:[r["status"],r["description"],sorted(r["related_entities"])])
    return graph


class StateReducer:
    def __init__(self,session):self.session=session

    def view_locked(self,at_cp:int) -> dict:
        s=self.session
        if type(at_cp) is not int or not 0<=at_cp<=len(s.manuscript.text):
            raise ManuscriptError("状态查询位置超出当前正文")
        s.knowledge.refresh_locked()
        graph=project_at(s.project.data.meta.id,s.knowledge.records,s.settings.profiles.values(),s.settings.entry_views_locked(),at_cp)
        return {**graph,"at_cp":at_cp,"revision_no":s.manuscript.revision_no,"version":s.knowledge.version,
            "notice":"这是叙述截至此处可知的状态，不是自动还原的故事实际时间。自动证据以来源切片末尾为可知位置，倒叙和推测需人工核对。",
            "warnings":[*s.knowledge.errors,*s.settings.load_errors,
                *(["正文已变化，旧自动分析尚未同步，未带入当前状态；请重新分块分析或人工补齐设定"]
                  if s.imports.last_plan is not None and s.imports.last_plan.base_revision_no!=s.manuscript.revision_no else []),
                *["有人工记录的位置已失效，未参与状态计算" for e in s.settings.entry_views_locked() if e["active"] and e["anchor_stale"]]][:50]}

    async def view(self,at_cp:int|None=None):
        async with self.session.lock:
            return self.view_locked(len(self.session.manuscript.text) if at_cp is None else at_cp)
