"""
schema.py — Pydantic models for the row schema.
Spec reference: composio-final-spec.md lines 40-61

Exact schema from spec:
{
  "app": "Sherlock",
  "category": "Data, SEO and Scraping",
  "one_liner": "OSINT username enumeration across social platforms",
  "composio_toolkit_match": null,
  "fields": {
    "auth_method": {
      "pass_a": { "value": "OAuth2", "confidence": "Low", "source": "generic search result" },
      "pass_b": { "value": "None - local CLI", "confidence": "High", "source": "github.com/..." },
      "agree": false,
      "status": "escalated",
      "final": "No auth - confirmed via repo README.",
      "resolved_by": "human"
    }
    // Same shape for: self_serve_or_gated, api_surface, buildability_verdict
    // Plus: category, one_liner, evidence_url  (7 fields x 100 apps = 700 field-checks)
  }
}

Confidence and evidence live at the FIELD level, not one score per app.
"""
from __future__ import annotations
from typing import Literal, Optional
from pydantic import BaseModel


Confidence = Literal["High", "Medium", "Low"]
Status = Literal["auto_accepted", "escalated", "human_resolved", "agent_resolved"]


class FieldPass(BaseModel):
    """One pass's output for a single field."""
    value: str
    confidence: Confidence
    source: str


class FieldResult(BaseModel):
    """Dual-pass result for a single field, with auto-diff outcome."""
    pass_a: FieldPass
    pass_b: FieldPass
    agree: bool
    status: Status
    final: str                              # resolved value (auto or human)
    reasoning: str                          # human justification ("why", which source is more credible)
    resolved_by: Literal["auto", "human", "agent", "agent_verified", "unverifiable"]


# The 7 fields that are checked per app (spec line 70: "7 -> 700 field-checks total")
FIELD_NAMES = [
    "category",
    "one_liner",
    "auth_method",
    "self_serve_or_gated",
    "api_surface",
    "buildability_verdict",
    "evidence_url",
]


class AppResult(BaseModel):
    """Complete research result for one app."""
    app: str
    category: str                           # convenience: final value of fields.category
    one_liner: str                          # convenience: final value of fields.one_liner
    composio_toolkit_match: Optional[str] = None
    fields: dict[str, FieldResult]          # keyed by FIELD_NAMES
