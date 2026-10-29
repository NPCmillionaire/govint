"""SAM.gov: entity registrations and federal exclusions (debarments). Needs a SAM.gov API key."""

from __future__ import annotations

from .. import names
from ..graph import COMPANY, SANCTION, Edge, Node, Result
from .base import Ctx, cite

API = "https://api.sam.gov/entity-information/v4"
SRC = "SAM.gov"
NO_KEY = ("SAM.gov skipped: no API key. Get one under Account Details at https://sam.gov "
          "and set GOVINT_SAM_KEY.")


def entities(ctx: Ctx, name: str, threshold: float = 88.0) -> list[dict]:
    data = ctx.http.get(f"{API}/entities", params={
        "api_key": ctx.settings.sam_key, "legalBusinessName": name,
        "includeSections": "entityRegistration,coreData",
    })
    out = []
    for e in data.get("entityData", []) or []:
        reg = e.get("entityRegistration") or {}
        if names.score(name, reg.get("legalBusinessName") or "") >= threshold:
            out.append(e)
    return out


def exclusions(ctx: Ctx, name: str, threshold: float = 88.0) -> list[dict]:
    data = ctx.http.get(f"{API}/exclusions", params={
        "api_key": ctx.settings.sam_key, "exclusionName": name,
    })
    out = []
    for x in data.get("excludedEntity", []) or []:
        ident = x.get("exclusionIdentification") or {}
        label = ident.get("entityName") or " ".join(
            p for p in (ident.get("firstName"), ident.get("lastName")) if p)
        if label and names.score(name, label) >= threshold:
            out.append({**x, "_label": label})
    return out


def result(ctx: Ctx, subject: Node) -> Result:
    if not ctx.settings.sam_key:
        return Result(notes=[NO_KEY])
    r = Result()
    for e in entities(ctx, subject.label)[:3]:
        reg = e.get("entityRegistration") or {}
        uei = reg.get("ueiSAM")
        src = cite(SRC, f"https://sam.gov/entity/{uei}/coreData" if uei else "https://sam.gov")
        core = e.get("coreData") or {}
        info = core.get("entityInformation") or {}
        n = Node(id=f"company:uei:{uei}", kind=COMPANY, label=reg.get("legalBusinessName") or subject.label,
                 props={"uei": uei, "cage": reg.get("cageCode"), "sam_status": reg.get("registrationStatus"),
                        "sam_expires": reg.get("registrationExpirationDate"),
                        "dba": reg.get("dbaName"), "entity_start": info.get("entityStartDate"),
                        "exclusion_flag": reg.get("exclusionStatusFlag")},
                 sources=[src])
        if reg.get("exclusionStatusFlag") == "Y":
            n.flag("sam-excluded")
        r.nodes.append(n)
        r.edges.append(Edge(subject.id, n.id, "same_as", source=src))
    for x in exclusions(ctx, subject.label)[:5]:
        det = x.get("exclusionDetails") or {}
        src = cite(SRC, "https://sam.gov/search/?index=ex")
        n = Node(id=f"sanction:sam:{names.normalize(x['_label'])}", kind=SANCTION, label=x["_label"],
                 props={"list": "SAM.gov exclusion", "type": det.get("exclusionType"),
                        "program": det.get("exclusionProgram"), "agency": det.get("excludingAgencyName")},
                 sources=[src], flags=["excluded"])
        r.nodes.append(n)
        r.edges.append(Edge(subject.id, n.id, "possible_match", source=src))
        subject.flag("sam-exclusion-possible")
    return r
