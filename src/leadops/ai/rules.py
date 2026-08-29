"""The deterministic scorer.

This exists for three jobs, and it is the reason the system has no hard
dependency on an LLM being up:

1. **Default provider.** With no API key configured, this is what runs, so a
   reviewer gets a working demo without paying anyone.
2. **Fallback.** When the model times out, or returns something that will not
   validate twice in a row, the lead is still scored and still routed. A lead is
   never dropped because a model had a bad minute.
3. **Floor.** It is a stated baseline: whatever the LLM adds, it has to beat
   keyword matching, and `evals/` measures whether it does.

It is keyword matching and it is not pretending to be more than that. The point
is that the *degraded* path is honest and visible (`degraded=True`,
`qualification_mode` written to the CRM) rather than silently worse.
"""

from __future__ import annotations

import re

from leadops.models import CanonicalLead, Priority, Qualification

_EMERGENCY = re.compile(
    r"\b(emergency|urgent|asap|right now|today|burst|flood|flooding|leak|leaking|"
    r"no heat|no heating|no hot water|no power|gas smell|smell of gas|broken down|"
    r"not working|stopped working|overflow)\b",
    re.IGNORECASE,
)
_INSTALL = re.compile(
    r"\b(install|installation|replace|replacement|new system|new unit"
    r"|upgrade|fit|fitting|quote for a new)\b",
    re.IGNORECASE,
)
_MAINTENANCE = re.compile(
    r"\b(service|servicing|maintenance|annual|tune[- ]?up|check[- ]?up|inspection|clean)\b",
    re.IGNORECASE,
)
_WARRANTY = re.compile(r"\b(warranty|guarantee|under cover|covered by)\b", re.IGNORECASE)
_QUOTE = re.compile(r"\b(quote|quotation|estimate|price|pricing|how much|cost)\b", re.IGNORECASE)
_SPAM = re.compile(
    r"\b(seo services|backlinks|guest post|crypto|bitcoin|loan offer|casino|"
    r"increase your traffic|we can rank|marketing agency|hire our team|"
    r"outsourcing|dear sir/madam|business proposal)\b",
    re.IGNORECASE,
)
# Deliberately narrow: matches attempts to redirect the model, not ordinary text.
_INJECTION = re.compile(
    r"(ignore (all |your |the )?(previous|above|prior) instructions"
    r"|disregard (the |all )?(above|previous|prior)"
    r"|you are now\b|new instructions:|system prompt|</?lead_data>"
    r"|set (the )?(priority|score) to\b|respond with only)",
    re.IGNORECASE,
)


def _haystack(lead: CanonicalLead) -> str:
    return " ".join([lead.service, lead.message, str(lead.extra.get("notes", ""))]).strip()


def looks_like_injection(lead: CanonicalLead) -> bool:
    """True when the lead body contains text shaped like instructions to a model.

    Lives here, but is called from `qualify()` on **every** provider path. It
    used to be reachable only through `classify()`, which meant the protection
    existed exactly where it was least needed - the offline scorer that ignores
    instructions anyway - and was absent on the live-model path that a paying
    deployment actually runs. An independent review found that, and it was a
    real hole: a compliant model returning
    `emergency_repair / high / 100` for an injected lead routed straight to the
    sales team with a five-minute SLA and an SMS to the submitter.

    This is a **heuristic**, not a boundary. It catches the obvious shapes. See
    `injection_verdict()` for what happens when it fires, and
    docs/ai-qualification.md for what is and is not actually guaranteed.
    """
    return bool(_INJECTION.search(_haystack(lead)))


def injection_verdict() -> Qualification:
    """The classification an injected lead gets, whatever the model said."""
    return Qualification(
        intent="Submission contains instruction-like text aimed at the classifier",
        service_category="other",
        priority=Priority.LOW,
        qualification_score=5,
        missing_information=["genuine service request"],
        summary=(
            "The message body contains text shaped like instructions to an AI "
            "system rather than a service enquiry. Flagged for human review; "
            "no automated follow-up sent."
        ),
        recommended_action="Review manually before any outbound contact.",
        provider="deterministic",
    )


def classify(lead: CanonicalLead) -> Qualification:
    """Score and categorise a lead with no external call."""
    text = _haystack(lead)

    if looks_like_injection(lead):
        return injection_verdict()

    if _SPAM.search(text):
        return Qualification(
            intent="Unsolicited sales or marketing approach",
            service_category="other",
            priority=Priority.LOW,
            qualification_score=5,
            missing_information=[],
            summary="Solicitation rather than a customer enquiry. No sales action needed.",
            recommended_action="No action; suppress from follow-up sequences.",
            provider="deterministic",
        )

    score = 30
    priority = Priority.LOW
    category = "general_enquiry"
    intent = "General enquiry"

    if _EMERGENCY.search(text):
        category, priority, intent = "emergency_repair", Priority.HIGH, "Urgent repair needed"
        score = 85
    elif _INSTALL.search(text):
        category = "installation"
        priority, intent = Priority.MEDIUM, "New installation or replacement"
        score = 70
    elif _WARRANTY.search(text):
        category = "warranty_claim"
        priority, intent = Priority.MEDIUM, "Warranty or guarantee claim"
        score = 60
    elif _MAINTENANCE.search(text):
        category, priority, intent = "maintenance", Priority.LOW, "Routine servicing"
        score = 55
    elif _QUOTE.search(text):
        category, priority, intent = "quote_request", Priority.MEDIUM, "Price or quote request"
        score = 50

    # Contact completeness moves the score, because a lead you cannot call back
    # is worth less regardless of how urgent it sounds.
    missing: list[str] = []
    if not lead.phone:
        missing.append("phone number")
        score -= 12
    if not lead.email:
        missing.append("email address")
        score -= 6
    if not lead.location:
        missing.append("service address or area")
        score -= 8
    if len(text) < 15:
        missing.append("description of the work required")
        score -= 10
    if not (lead.first_name or lead.last_name):
        missing.append("contact name")
        score -= 4

    # Partner leads are pre-screened by the sending system; website leads with a
    # written description show more intent than a bare ad-form submission.
    if lead.source.value == "partner":
        score += 5
    if len(text) > 120:
        score += 5

    score = max(0, min(100, score))
    if score >= 75:
        priority = Priority.HIGH
    elif score < 35 and priority is Priority.HIGH:
        priority = Priority.MEDIUM

    summary_bits = [f"{lead.full_name} enquired about {category.replace('_', ' ')}"]
    if lead.location:
        summary_bits.append(f"in {lead.location}")
    if lead.service:
        summary_bits.append(f"(service field: {lead.service})")
    summary = " ".join(summary_bits) + "."
    if missing:
        summary += f" Missing before quoting: {', '.join(missing)}."

    action = (
        "Call within the SLA window."
        if priority is Priority.HIGH
        else "Send the standard follow-up and request the missing details."
        if missing
        else "Send the standard follow-up sequence."
    )

    return Qualification(
        intent=intent,
        service_category=category,
        priority=priority,
        qualification_score=score,
        missing_information=missing,
        summary=summary,
        recommended_action=action,
        provider="deterministic",
    )
