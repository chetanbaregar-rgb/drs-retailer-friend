"""
profiling.py — quietly build a picture of the visitor from the conversation.

This runs OUT of the main chat loop. After an exchange (and only if the visitor
has consented), we ask a cheap, fast model to read the conversation and pull out
any business details the retailer mentioned — location, store type, size, number
of stores, their role, and why they're researching DRS.

It never invents anything: a field is only filled if the visitor actually said
it. We deliberately do NOT extract names, emails, phone numbers or addresses —
that is data minimisation under UK GDPR, by design.
"""

from __future__ import annotations

import json

import anthropic

EXTRACT_MODEL = "claude-haiku-4-5-20251001"

# The fields we care about. Order is the order we'd naturally learn them.
PROFILE_FIELDS = [
    "location",
    "store_type",
    "store_size",
    "num_stores",
    "role",
    "intention",
    "contact_name",
    "contact_email",
]

_EXTRACTION_TOOL = {
    "name": "record_visitor_profile",
    "description": (
        "Record business details the retailer has actually stated in the "
        "conversation. Only include a field if the visitor genuinely provided "
        "it — never guess or infer. Omit anything not clearly stated."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "location": {
                "type": "string",
                "description": (
                    "Town/city or region and the UK nation (England, Wales, "
                    "Northern Ireland or Scotland). Coarse only — never a full "
                    "address or postcode."
                ),
            },
            "store_type": {
                "type": "string",
                "description": (
                    "Type of retail store, e.g. convenience store, off-licence, "
                    "petrol forecourt, supermarket, farm shop."
                ),
            },
            "store_size": {
                "type": "string",
                "description": (
                    "Retail sales floor area, e.g. '85 sqm', 'under 100 sqm', "
                    "'about 250 square metres'."
                ),
            },
            "num_stores": {
                "type": "string",
                "description": "How many stores they operate, e.g. '1', '3', 'a chain of 12'.",
            },
            "role": {
                "type": "string",
                "description": "Their role, e.g. owner, store manager, area manager, buyer.",
            },
            "intention": {
                "type": "string",
                "description": (
                    "Why they're researching DRS, e.g. 'preparing for launch', "
                    "'deciding whether to get an RVM', 'checking if exempt'."
                ),
            },
            "contact_name": {
                "type": "string",
                "description": (
                    "The retailer's name, only if they volunteered it when "
                    "asked at the end of the conversation."
                ),
            },
            "contact_email": {
                "type": "string",
                "description": (
                    "The retailer's email address, only if they volunteered it "
                    "when asked at the end of the conversation."
                ),
            },
        },
        "required": [],
    },
}


def extract_profile(messages: list, existing: dict | None = None) -> dict:
    """Read the conversation and return a merged visitor profile.

    `messages` is the list of {role, content} dicts. `existing` is the profile
    we've built so far. Newly-stated details overwrite/refine old ones; fields
    the visitor never mentioned stay as they were. Best-effort: any failure
    just returns the existing profile unchanged.
    """
    existing = dict(existing or {})

    # Only the user's turns can contain new facts; keep the call small.
    transcript = "\n".join(
        f"{m['role'].upper()}: {m['content']}"
        for m in messages
        if isinstance(m.get("content"), str)
    )[-6000:]

    if not transcript.strip():
        return existing

    try:
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=EXTRACT_MODEL,
            max_tokens=400,
            tools=[_EXTRACTION_TOOL],
            tool_choice={"type": "tool", "name": "record_visitor_profile"},
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Read this chat between a UK retailer and a DRS assistant. "
                        "Record only the business details the retailer actually "
                        "stated about themselves.\n\n"
                        f"{transcript}"
                    ),
                }
            ],
        )
    except Exception:
        return existing

    for block in resp.content:
        if getattr(block, "type", None) == "tool_use":
            data = block.input or {}
            for field in PROFILE_FIELDS:
                value = data.get(field)
                if isinstance(value, str) and value.strip():
                    existing[field] = value.strip()

    return existing


def profile_is_empty(profile: dict | None) -> bool:
    if not profile:
        return True
    return not any(profile.get(f) for f in PROFILE_FIELDS)
