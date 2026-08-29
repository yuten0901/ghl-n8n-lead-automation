"""Routing rules and the retry policy."""

from __future__ import annotations

import pytest

from leadops.errors import RateLimited, UpstreamRejected, UpstreamUnavailable, ValidationFailed
from leadops.ghl.mapping import GHLMapping
from leadops.models import Priority, Qualification
from leadops.reliability.retry import RetryPolicy, call_with_retry
from leadops.routing.rules import RoutingTable, coerce_category, route
from tests.conftest import REPO_ROOT


@pytest.fixture
def table() -> RoutingTable:
    return RoutingTable.load(REPO_ROOT / "config" / "routing.yml")


def qual(**overrides) -> Qualification:
    base = {
        "intent": "test",
        "service_category": "general_enquiry",
        "priority": Priority.MEDIUM,
        "qualification_score": 55,
        "missing_information": [],
        "summary": "s",
        "recommended_action": "a",
    }
    return Qualification(**{**base, **overrides})


class TestRoutingIsTotal:
    def test_every_score_and_category_combination_gets_a_destination(self, table) -> None:
        """A lead with no destination is invisible in the CRM, which means lost.

        Exhaustive rather than sampled: 7 categories x 3 priorities x 11 scores.
        """
        for category in table.service_categories:
            for priority in Priority:
                for score in range(0, 101, 10):
                    decision = route(
                        qual(
                            service_category=category, priority=priority, qualification_score=score
                        ),
                        table,
                    )
                    assert decision.pipeline_stage, (category, priority, score)
                    assert decision.rule_id, (category, priority, score)

    def test_unmatched_leads_land_on_the_default_rule(self, table) -> None:
        decision = route(qual(qualification_score=40, service_category="general_enquiry"), table)
        assert decision.rule_id == "default_nurture"
        assert decision.pipeline_stage == "nurture"


class TestRoutingIsDeterministic:
    def test_the_same_input_always_routes_the_same_way(self, table) -> None:
        q = qual(
            service_category="emergency_repair", priority=Priority.HIGH, qualification_score=90
        )
        decisions = {route(q, table).model_dump_json() for _ in range(20)}
        assert len(decisions) == 1


class TestSpecificRules:
    def test_emergency_high_priority_notifies_sales_with_a_tight_sla(self, table) -> None:
        decision = route(
            qual(
                service_category="emergency_repair", priority=Priority.HIGH, qualification_score=90
            ),
            table,
        )
        assert decision.rule_id == "emergency_high_priority"
        assert decision.pipeline_stage == "hot_lead"
        assert decision.notify_sales is True
        assert decision.sla_minutes == 5

    def test_low_scores_are_routed_out_before_any_other_rule_can_match(self, table) -> None:
        # First-match-wins ordering matters: an "emergency" spam message must be
        # caught by the score floor, not by the emergency rule.
        decision = route(
            qual(
                service_category="emergency_repair", priority=Priority.HIGH, qualification_score=5
            ),
            table,
        )
        assert decision.rule_id == "spam_or_unqualified"
        assert decision.notify_sales is False
        assert decision.follow_up_sequence == ""  # no customer contact for spam

    def test_high_value_installation_is_escalated(self, table) -> None:
        decision = route(qual(service_category="installation", qualification_score=80), table)
        assert decision.rule_id == "high_value_installation"
        assert decision.monetary_value == 6500

    def test_missing_information_routes_to_a_detail_gathering_branch(self, table) -> None:
        decision = route(
            qual(qualification_score=60, missing_information=["service address"]), table
        )
        assert decision.rule_id == "missing_contact_details"
        assert decision.pipeline_stage == "needs_info"
        assert decision.follow_up_sequence == "ask_for_missing_details"


class TestRuleSafety:
    def test_an_unrecognised_predicate_makes_the_rule_fail_closed(self) -> None:
        """A typo in the config must not match everything.

        If `qualification_score_gte` were mistyped, a fail-open rule would route
        every lead - including spam - to the owner's phone at 5-minute SLA.
        """
        broken = RoutingTable.from_dict(
            {
                "rules": [
                    {
                        "id": "typo_rule",
                        "when": {"qualification_scoore_gte": 50},
                        "then": {"pipeline_stage": "hot_lead"},
                    }
                ],
                "default": {"id": "default", "then": {"pipeline_stage": "nurture"}},
            }
        )
        assert route(qual(qualification_score=99), broken).rule_id == "default"

    def test_a_vocabulary_without_other_gets_one_added(self) -> None:
        # A closed vocabulary with no exit forces unknown values into a
        # wrong-but-valid category, which corrupts the data quietly.
        loaded = RoutingTable.from_dict({"service_categories": ["installation"]})
        assert "other" in loaded.service_categories

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("installation", "installation"),
            ("Installation", "installation"),
            ("emergency-repair", "emergency_repair"),
            ("Emergency Repair", "emergency_repair"),
            ("urgent_dragon_removal", "other"),
            ("", "other"),
        ],
    )
    def test_category_coercion(self, table, raw: str, expected: str) -> None:
        assert coerce_category(raw, table)[0] == expected


class TestMapping:
    def test_unknown_stage_names_fall_back_to_a_visible_stage(self) -> None:
        mapping = GHLMapping.load(REPO_ROOT / "config" / "ghl-mapping.json")
        assert mapping.stage_id("stage_that_was_renamed_in_ghl") == mapping.stage_id("new_lead")

    def test_unmapped_custom_fields_are_skipped_not_sent(self) -> None:
        # Sending an unknown field id is a 422 that fails the whole contact write.
        # Losing one analytics field is not worth losing the lead.
        mapping = GHLMapping.load(REPO_ROOT / "config" / "ghl-mapping.json")
        payload = mapping.custom_field_payload({"lead_score": 80, "not_a_real_field": "x"})
        assert len(payload) == 1
        assert mapping.unmapped_fields({"lead_score": 80, "not_a_real_field": "x"}) == [
            "not_a_real_field"
        ]

    def test_tags_are_deduplicated_and_order_stable(self) -> None:
        mapping = GHLMapping.load(REPO_ROOT / "config" / "ghl-mapping.json")
        first = mapping.tags_for(
            source="website", priority="high", extra=["emergency", "leadops"], degraded=False
        )
        second = mapping.tags_for(
            source="website", priority="high", extra=["emergency", "leadops"], degraded=False
        )
        assert first == second
        assert len(first) == len(set(first))

    def test_degraded_qualification_is_tagged_so_it_is_filterable_in_ghl(self) -> None:
        mapping = GHLMapping.load(REPO_ROOT / "config" / "ghl-mapping.json")
        tags = mapping.tags_for(source="meta", priority="low", extra=[], degraded=True)
        assert "ai-fallback-used" in tags


class TestRetryPolicy:
    async def test_a_permanent_error_is_never_retried(self) -> None:
        calls = 0

        async def op():
            nonlocal calls
            calls += 1
            raise UpstreamRejected("400 bad request")

        with pytest.raises(UpstreamRejected):
            await call_with_retry(op, RetryPolicy(max_attempts=4, base_seconds=0, max_seconds=0))
        assert calls == 1  # retrying a 400 turns one bug into four

    async def test_a_retryable_error_is_retried_up_to_the_limit(self) -> None:
        calls = 0

        async def op():
            nonlocal calls
            calls += 1
            raise UpstreamUnavailable("503")

        with pytest.raises(UpstreamUnavailable):
            await call_with_retry(op, RetryPolicy(max_attempts=4, base_seconds=0, max_seconds=0))
        assert calls == 4

    async def test_a_transient_failure_that_recovers_returns_the_value(self) -> None:
        calls = 0

        async def op():
            nonlocal calls
            calls += 1
            if calls < 3:
                raise UpstreamUnavailable("503")
            return "recovered"

        result = await call_with_retry(
            op, RetryPolicy(max_attempts=4, base_seconds=0, max_seconds=0)
        )
        assert result.value == "recovered"
        assert result.attempts == 3

    async def test_validation_errors_are_not_retryable(self) -> None:
        async def op():
            raise ValidationFailed("bad payload")

        with pytest.raises(ValidationFailed):
            await call_with_retry(op, RetryPolicy(max_attempts=3, base_seconds=0, max_seconds=0))

    def test_backoff_grows_and_is_capped(self) -> None:
        policy = RetryPolicy(base_seconds=1.0, max_seconds=8.0, jitter=lambda _lo, hi: hi)
        assert [policy.delay_for(n) for n in range(1, 6)] == [1.0, 2.0, 4.0, 8.0, 8.0]

    def test_jitter_spreads_retries_rather_than_synchronising_them(self) -> None:
        """Without jitter, every pending lead retries at the same instant after a
        rate-limit and the burst simply repeats."""
        policy = RetryPolicy(base_seconds=1.0, max_seconds=8.0)
        samples = {policy.delay_for(3) for _ in range(50)}
        assert len(samples) > 40
        assert all(0.0 <= s <= 4.0 for s in samples)

    def test_server_supplied_retry_after_overrides_our_backoff(self) -> None:
        policy = RetryPolicy(base_seconds=1.0, max_seconds=8.0)
        assert policy.delay_for(1, retry_after=2.5) == 2.5

    async def test_rate_limit_uses_retry_after_before_sleeping(self) -> None:
        slept: list[float] = []

        async def sleeper(seconds: float) -> None:
            slept.append(seconds)

        calls = 0

        async def op():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RateLimited("429", retry_after=3.0)
            return "ok"

        await call_with_retry(
            op, RetryPolicy(max_attempts=3, base_seconds=0.1, max_seconds=1.0, sleeper=sleeper)
        )
        assert slept == [3.0]
