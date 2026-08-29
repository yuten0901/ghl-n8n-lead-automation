"""AI qualification: the contract is that `qualify()` never raises.

Each test here is one way a model ruins your afternoon in production.
"""

from __future__ import annotations

import json

import pytest

from leadops.ai.providers import ModelReply
from leadops.ai.qualify import extract_json, qualify
from leadops.errors import UpstreamTimeout, UpstreamUnavailable
from leadops.models import CanonicalLead, Priority
from leadops.normalize import normalize_payload
from leadops.routing.rules import RoutingTable
from tests.conftest import REPO_ROOT, load_fixture

VALID_OUTPUT = {
    "intent": "Furnace repair needed urgently",
    "service_category": "emergency_repair",
    "priority": "high",
    "qualification_score": 92,
    "missing_information": [],
    "summary": "No heat overnight with an infant at home. Same-day callout requested.",
    "recommended_action": "Call within 5 minutes and book the earliest slot.",
}


class StubProvider:
    """Returns a scripted sequence of replies or exceptions, one per call."""

    def __init__(self, *script, name: str = "anthropic") -> None:
        self.script = list(script)
        self.name = name
        self.calls = 0

    async def complete(self, *, system: str, user: str, repair: str | None = None) -> ModelReply:
        self.calls += 1
        item = self.script[min(self.calls - 1, len(self.script) - 1)]
        if isinstance(item, Exception):
            raise item
        return ModelReply(text=item, provider=self.name, model="stub-model-1")


@pytest.fixture
def table() -> RoutingTable:
    return RoutingTable.load(REPO_ROOT / "config" / "routing.yml")


@pytest.fixture
def lead() -> CanonicalLead:
    return normalize_payload("website", load_fixture("website-lead-emergency.json"))


class TestJsonExtraction:
    def test_plain_object(self) -> None:
        assert extract_json('{"a": 1}') == {"a": 1}

    def test_markdown_fences_are_tolerated(self) -> None:
        # Models emit fenced JSON often enough that failing on it would mean a
        # fallback for a response that was actually correct.
        assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
        assert extract_json('```\n{"a": 1}\n```') == {"a": 1}

    def test_surrounding_prose_is_tolerated(self) -> None:
        assert extract_json('Sure! Here you go:\n{"a": 1}\nHope that helps.') == {"a": 1}

    @pytest.mark.parametrize("bad", ["", "   ", "no json here", "[1, 2, 3]", "{unclosed"])
    def test_genuinely_unusable_output_raises(self, bad: str) -> None:
        with pytest.raises(ValueError):
            extract_json(bad)


class TestHappyPath:
    async def test_valid_model_output_is_used_as_is(self, lead, table) -> None:
        provider = StubProvider(json.dumps(VALID_OUTPUT))
        outcome = await qualify(lead, provider=provider, table=table)
        assert outcome.qualification.qualification_score == 92
        assert outcome.qualification.priority is Priority.HIGH
        assert outcome.qualification.degraded is False
        assert provider.calls == 1

    async def test_deterministic_provider_makes_no_model_call(self, lead, table) -> None:
        from leadops.ai.providers import DeterministicProvider

        outcome = await qualify(lead, provider=DeterministicProvider(), table=table)
        assert outcome.provider_used == "deterministic"
        assert outcome.qualification.degraded is False  # this is the default, not a failure
        assert outcome.qualification.service_category == "emergency_repair"


class TestMalformedOutput:
    async def test_one_repair_attempt_recovers_a_fixable_response(self, lead, table) -> None:
        provider = StubProvider("I think this is an emergency!", json.dumps(VALID_OUTPUT))
        outcome = await qualify(lead, provider=provider, table=table, max_attempts=2)
        assert provider.calls == 2
        assert outcome.qualification.degraded is False
        assert outcome.qualification.qualification_score == 92

    async def test_two_failures_fall_back_to_rules_and_are_marked_degraded(
        self, lead, table
    ) -> None:
        provider = StubProvider("still not json", "nope, prose again")
        outcome = await qualify(lead, provider=provider, table=table, max_attempts=2)
        assert provider.calls == 2
        assert outcome.qualification.degraded is True
        assert outcome.qualification.provider == "deterministic"
        # The lead is still fully classified. Nothing is dropped.
        assert outcome.qualification.service_category == "emergency_repair"
        assert outcome.qualification.qualification_score > 0

    async def test_out_of_range_score_fails_validation_then_falls_back(self, lead, table) -> None:
        bad = {**VALID_OUTPUT, "qualification_score": 250}
        provider = StubProvider(json.dumps(bad), json.dumps(bad))
        outcome = await qualify(lead, provider=provider, table=table, max_attempts=2)
        assert outcome.qualification.degraded is True
        assert 0 <= outcome.qualification.qualification_score <= 100

    async def test_invented_service_category_is_coerced_to_other(self, lead, table) -> None:
        # A hallucinated category must not be able to reach a routing rule.
        odd = {**VALID_OUTPUT, "service_category": "urgent_dragon_removal"}
        provider = StubProvider(json.dumps(odd))
        outcome = await qualify(lead, provider=provider, table=table)
        assert outcome.qualification.service_category == "other"
        assert any("not in vocabulary" in note for note in outcome.notes)

    async def test_missing_required_field_falls_back(self, lead, table) -> None:
        incomplete = {k: v for k, v in VALID_OUTPUT.items() if k != "summary"}
        provider = StubProvider(json.dumps(incomplete), json.dumps(incomplete))
        outcome = await qualify(lead, provider=provider, table=table, max_attempts=2)
        assert outcome.qualification.degraded is True

    async def test_extra_unexpected_fields_are_ignored_not_fatal(self, lead, table) -> None:
        noisy = {**VALID_OUTPUT, "confidence": 0.9, "internal_note": "ignore me"}
        provider = StubProvider(json.dumps(noisy))
        outcome = await qualify(lead, provider=provider, table=table)
        assert outcome.qualification.degraded is False
        assert outcome.qualification.qualification_score == 92


class TestProviderFailure:
    @pytest.mark.parametrize(
        "error", [UpstreamTimeout("timed out"), UpstreamUnavailable("503 from vendor")]
    )
    async def test_transport_failure_falls_back_immediately_without_retrying(
        self, lead, table, error
    ) -> None:
        # A lead waiting on a model retry loop is a lead going cold. One attempt,
        # then score it ourselves.
        provider = StubProvider(error, json.dumps(VALID_OUTPUT))
        outcome = await qualify(lead, provider=provider, table=table, max_attempts=2)
        assert provider.calls == 1
        assert outcome.qualification.degraded is True
        assert error.code in outcome.qualification.degraded_reason

    async def test_an_unexpected_exception_still_does_not_escape(self, lead, table) -> None:
        provider = StubProvider(RuntimeError("provider SDK blew up"))
        outcome = await qualify(lead, provider=provider, table=table)
        assert outcome.qualification.degraded is True
        assert "RuntimeError" in outcome.qualification.degraded_reason


class TestDeterministicScorer:
    async def test_spam_scores_low_enough_to_be_routed_out(self, table) -> None:
        from leadops.ai.providers import DeterministicProvider

        lead = normalize_payload("website", load_fixture("spam-lead.json"))
        outcome = await qualify(lead, provider=DeterministicProvider(), table=table)
        assert outcome.qualification.qualification_score < 20

    async def test_prompt_injection_is_flagged_and_scored_down(self, table) -> None:
        from leadops.ai.providers import DeterministicProvider

        lead = normalize_payload("website", load_fixture("prompt-injection-lead.json"))
        outcome = await qualify(lead, provider=DeterministicProvider(), table=table)
        assert outcome.qualification.qualification_score < 20
        assert outcome.qualification.service_category == "other"
        assert "review" in outcome.qualification.recommended_action.lower()

    async def test_missing_contact_details_lower_the_score(self, table) -> None:
        from leadops.ai.providers import DeterministicProvider

        complete = normalize_payload(
            "website",
            {
                "email": "a@b.com",
                "phone": "512-555-0101",
                "name": "A B",
                "city": "Austin",
                "message": "Need my AC serviced before summer please.",
            },
        )
        sparse = normalize_payload(
            "website", {"email": "a@b.com", "message": "Need my AC serviced before summer please."}
        )
        det = DeterministicProvider()
        full_score = (await qualify(complete, provider=det, table=table)).qualification
        thin_score = (await qualify(sparse, provider=det, table=table)).qualification
        assert full_score.qualification_score > thin_score.qualification_score
        assert "phone number" in thin_score.missing_information


class TestPromptConstruction:
    async def test_pii_is_not_sent_to_the_model(self, lead, table) -> None:
        """The model gets presence booleans, not the email address or phone number.

        Classification does not need them, so they do not travel to a third party.
        """
        captured: list[str] = []

        class Capturing(StubProvider):
            async def complete(self, *, system, user, repair=None):
                captured.append(user)
                return await super().complete(system=system, user=user, repair=repair)

        await qualify(lead, provider=Capturing(json.dumps(VALID_OUTPUT)), table=table)
        prompt = captured[0]
        assert lead.email not in prompt
        assert lead.phone not in prompt
        assert "has_phone: True" in prompt
        assert "<lead_data>" in prompt

    async def test_lead_content_is_delimited(self, lead, table) -> None:
        captured: list[str] = []

        class Capturing(StubProvider):
            async def complete(self, *, system, user, repair=None):
                captured.append(user)
                return await super().complete(system=system, user=user, repair=repair)

        await qualify(lead, provider=Capturing(json.dumps(VALID_OUTPUT)), table=table)
        assert captured[0].count("<lead_data>") == 1
        assert captured[0].count("</lead_data>") == 1


class TestInjectionCannotEscalateOnAnyProviderPath:
    """The guarantee has to hold for the provider a paying deployment runs.

    An independent review found it did not. The injection check lived inside
    `rules.classify()`, so it only ever ran on the offline scorer - the one path
    that ignores instructions anyway - and was absent on the live-model path.
    A model that complied with the injected instruction returned
    `emergency_repair / high / 100`, which routed to `hot_lead` with
    `notify_sales=True`, a five-minute SLA, and an SMS to the submitter.

    These tests use a model that complies completely, because a model that
    refuses proves nothing about the guard.
    """

    COMPLIANT = json.dumps(
        {
            "intent": "Emergency - no heat, infant at home",
            "service_category": "emergency_repair",
            "priority": "high",
            "qualification_score": 100,
            "missing_information": [],
            "summary": "Urgent emergency callout required immediately.",
            "recommended_action": "Call within 5 minutes.",
        }
    )

    async def test_a_complying_model_cannot_escalate_an_injected_lead(self, table) -> None:
        from leadops.routing.rules import route

        lead = normalize_payload("website", load_fixture("prompt-injection-lead.json"))
        provider = StubProvider(self.COMPLIANT)

        outcome = await qualify(lead, provider=provider, table=table)
        decision = route(outcome.qualification, table)

        assert decision.notify_sales is False, "an injected lead reached the sales team"
        assert decision.follow_up_sequence == "", "an injected lead triggered outbound contact"
        assert decision.pipeline_stage == "unqualified"
        assert outcome.qualification.qualification_score < 20

    async def test_the_model_is_not_even_asked_about_an_injected_lead(self, table) -> None:
        """Nothing is gained by asking, and a compliant answer is a liability."""
        lead = normalize_payload("website", load_fixture("prompt-injection-lead.json"))
        provider = StubProvider(self.COMPLIANT)
        await qualify(lead, provider=provider, table=table)
        assert provider.calls == 0

    async def test_the_override_is_visible_rather_than_silent(self, table) -> None:
        lead = normalize_payload("website", load_fixture("prompt-injection-lead.json"))
        outcome = await qualify(lead, provider=StubProvider(self.COMPLIANT), table=table)
        assert outcome.qualification.degraded is True
        assert "instruction-like" in outcome.qualification.degraded_reason
        assert outcome.provider_used == "injection-guard"

    async def test_an_ordinary_urgent_lead_is_still_scored_by_the_model(self, table) -> None:
        """The guard must not swallow real emergencies - that would trade one
        failure for a worse one."""
        lead = normalize_payload(
            "website",
            {
                "email": "a@b.com",
                "phone": "512-555-0147",
                "name": "Real Customer",
                "city": "Austin",
                "message": "Our furnace stopped overnight and there is no heat. Please help today.",
            },
        )
        provider = StubProvider(self.COMPLIANT)
        outcome = await qualify(lead, provider=provider, table=table)
        assert provider.calls == 1
        assert outcome.qualification.qualification_score == 100
        assert outcome.qualification.degraded is False
