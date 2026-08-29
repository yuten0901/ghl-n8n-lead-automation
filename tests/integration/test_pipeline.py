"""End-to-end: a webhook payload in, CRM state out.

These run the real pipeline against the real mock GHL server. Nothing in
`leadops` is stubbed; only the vendor is replaced. Each test asserts on the
mock's resulting state, which is the same question a client asks - "what
actually ended up in my CRM?"
"""

from __future__ import annotations

import asyncio

import pytest

from leadops.models import EventStatus
from leadops.pipeline import process_lead
from leadops.storage import repo, session_scope
from tests.conftest import load_fixture, set_fault

pytestmark = pytest.mark.usefixtures("db")


async def run(source: str, fixture: str, *, settings, ghl, mapping, table, **kwargs):
    async with session_scope() as session:
        return await process_lead(
            source=source,
            payload=load_fixture(fixture),
            headers=kwargs.pop("headers", {}),
            settings=settings,
            session=session,
            ghl=ghl,
            mapping=mapping,
            table=table,
            **kwargs,
        )


class TestHappyPath:
    async def test_an_emergency_lead_reaches_the_crm_completely(
        self, settings, ghl_client, mapping, table, mock_ghl
    ) -> None:
        result = await run(
            "website",
            "website-lead-emergency.json",
            settings=settings,
            ghl=ghl_client,
            mapping=mapping,
            table=table,
        )

        assert result.status is EventStatus.SUCCEEDED
        assert result.contact_id and result.opportunity_id

        # One contact, correctly normalized, correctly attributed.
        assert len(mock_ghl.contacts) == 1
        contact = next(iter(mock_ghl.contacts.values()))
        assert contact["email"] == "dana.whitfield@example.com"
        assert contact["phone"] == "+15125550147"
        assert contact["source"] == "leadops:website"

        # Tags a GHL workflow can trigger on.
        assert "priority-high" in contact["tags"]
        assert "source-website" in contact["tags"]
        assert "emergency" in contact["tags"]

        # Custom fields carry the score and the summary onto the record.
        field_values = {f.get("id"): f["field_value"] for f in contact["customFields"]}
        assert field_values["cf_DEMO_lead_score_000"] == "90"
        assert field_values["cf_DEMO_qual_mode_0000"] == "deterministic"

        # The opportunity is on the board, in the hot-lead stage.
        assert len(mock_ghl.opportunities) == 1
        opportunity = next(iter(mock_ghl.opportunities.values()))
        assert opportunity["pipelineStageId"] == mapping.stage_id("hot_lead")
        assert opportunity["status"] == "open"

        # A salesperson can read the reasoning, and both messages were sent.
        assert len(mock_ghl.notes) == 1
        assert "Recommended next step" in mock_ghl.notes[0]["body"]
        assert len(mock_ghl.messages) == 2  # customer follow-up + internal alert
        assert {m["type"] for m in mock_ghl.messages} == {"SMS", "Email"}

    @pytest.mark.parametrize(
        ("source", "fixture"),
        [
            ("website", "website-lead-standard.json"),
            ("meta", "meta-lead-ads.json"),
            ("google", "google-lead-form.json"),
            ("partner", "partner-lead.json"),
        ],
    )
    async def test_every_source_completes(
        self, settings, ghl_client, mapping, table, mock_ghl, source, fixture
    ) -> None:
        result = await run(
            source, fixture, settings=settings, ghl=ghl_client, mapping=mapping, table=table
        )
        assert result.status is EventStatus.SUCCEEDED
        assert len(mock_ghl.contacts) == 1

    async def test_a_low_scoring_lead_is_filed_without_contacting_the_customer(
        self, settings, ghl_client, mapping, table, mock_ghl
    ) -> None:
        """Spam still gets a CRM record - so it is auditable - but no SMS, no
        email, and no salesperson's time."""
        result = await run(
            "website",
            "spam-lead.json",
            settings=settings,
            ghl=ghl_client,
            mapping=mapping,
            table=table,
        )
        assert result.status is EventStatus.SUCCEEDED
        assert result.routing.rule_id == "spam_or_unqualified"
        assert len(mock_ghl.contacts) == 1
        assert mock_ghl.messages == []

    async def test_a_prompt_injection_attempt_cannot_route_itself_to_sales(
        self, settings, ghl_client, mapping, table, mock_ghl
    ) -> None:
        """The lead body instructs the classifier to set priority high.

        Even if a model complied, routing is deterministic and the score floor
        catches it - the model classifies, the rules route.
        """
        result = await run(
            "website",
            "prompt-injection-lead.json",
            settings=settings,
            ghl=ghl_client,
            mapping=mapping,
            table=table,
        )
        assert result.qualification.service_category == "other"
        assert result.routing.notify_sales is False
        assert mock_ghl.messages == []


class TestDuplicateDelivery:
    async def test_the_same_event_delivered_twice_writes_to_the_crm_once(
        self, settings, ghl_client, mapping, table, mock_ghl
    ) -> None:
        """The single most common real-world webhook problem."""
        kwargs = {"settings": settings, "ghl": ghl_client, "mapping": mapping, "table": table}
        first = await run("meta", "meta-lead-ads.json", **kwargs)
        second = await run("meta", "meta-lead-ads.json", **kwargs)

        assert first.status is EventStatus.SUCCEEDED
        assert second.status is EventStatus.DUPLICATE
        assert second.duplicate_of == first.event_id

        # The proof: the CRM was written once, not twice.
        assert len(mock_ghl.contacts) == 1
        assert len(mock_ghl.opportunities) == 1
        assert len(mock_ghl.notes) == 1
        assert mock_ghl.call_counts["contacts.upsert"] == 1

    async def test_a_duplicate_never_re_sends_the_follow_up_message(
        self, settings, ghl_client, mapping, table, mock_ghl
    ) -> None:
        """Double-texting a customer is the failure they actually notice."""
        kwargs = {"settings": settings, "ghl": ghl_client, "mapping": mapping, "table": table}
        await run("website", "website-lead-emergency.json", **kwargs)
        sent_after_first = len(mock_ghl.messages)
        for _ in range(4):
            await run("website", "website-lead-emergency.json", **kwargs)
        assert len(mock_ghl.messages) == sent_after_first

    async def test_a_duplicate_returns_the_original_decision(
        self, settings, ghl_client, mapping, table, mock_ghl
    ) -> None:
        kwargs = {"settings": settings, "ghl": ghl_client, "mapping": mapping, "table": table}
        first = await run("meta", "meta-lead-ads.json", **kwargs)
        second = await run("meta", "meta-lead-ads.json", **kwargs)
        assert second.contact_id == first.contact_id
        assert second.routing.rule_id == first.routing.rule_id

    async def test_concurrent_identical_deliveries_produce_one_contact(
        self, settings, ghl_client, mapping, table, mock_ghl
    ) -> None:
        """Insert-first idempotency under a real race.

        Five deliveries dispatched together, each in its own session. Exactly one
        wins the UNIQUE constraint; the rest report duplicate. A check-then-act
        implementation fails this.
        """
        payload = load_fixture("meta-lead-ads.json")

        async def deliver():
            async with session_scope() as session:
                return await process_lead(
                    source="meta",
                    payload=payload,
                    headers={},
                    settings=settings,
                    session=session,
                    ghl=ghl_client,
                    mapping=mapping,
                    table=table,
                )

        results = await asyncio.gather(*(deliver() for _ in range(5)), return_exceptions=True)
        errors = [r for r in results if isinstance(r, Exception)]
        assert not errors, errors

        statuses = [r.status for r in results]
        assert statuses.count(EventStatus.SUCCEEDED) == 1
        assert statuses.count(EventStatus.DUPLICATE) == 4
        assert len(mock_ghl.contacts) == 1
        assert len(mock_ghl.opportunities) == 1


class TestReturningLead:
    async def test_the_same_person_from_two_sources_gets_one_contact_and_one_opportunity(
        self, settings, ghl_client, mapping, table, mock_ghl
    ) -> None:
        """Two genuinely different events, one person.

        This is *not* the duplicate-webhook case: both events are processed. What
        must not happen is a second contact or a second card on the pipeline.
        """
        website = load_fixture("website-lead-standard.json")
        google = load_fixture("google-lead-form.json")
        # Same person, arriving through a different channel a week later.
        google["user_column_data"] = [
            {"column_id": "EMAIL", "string_value": website["email"]},
            {"column_id": "FULL_NAME", "string_value": "Marcus Oyelaran"},
            {"column_id": "PHONE_NUMBER", "string_value": website["phone"]},
        ]

        async with session_scope() as session:
            first = await process_lead(
                source="website",
                payload=website,
                headers={},
                settings=settings,
                session=session,
                ghl=ghl_client,
                mapping=mapping,
                table=table,
            )
        async with session_scope() as session:
            second = await process_lead(
                source="google",
                payload=google,
                headers={},
                settings=settings,
                session=session,
                ghl=ghl_client,
                mapping=mapping,
                table=table,
            )

        assert first.status is EventStatus.SUCCEEDED
        assert second.status is EventStatus.SUCCEEDED  # a real second event
        assert second.contact_id == first.contact_id  # ...but one person
        assert len(mock_ghl.contacts) == 1
        assert len(mock_ghl.opportunities) == 1  # updated, not duplicated

        async with session_scope() as session:
            lead_row = await repo.get_lead(
                session, f"{mapping.location_id}:email:{website['email'].lower()}"
            )
            assert lead_row.submission_count == 2
            assert lead_row.first_source == "website"
            assert lead_row.last_source == "google"


class TestTransientFailure:
    async def test_a_transient_500_is_retried_and_the_lead_still_lands(
        self, settings, ghl_client, mapping, table, mock_ghl
    ) -> None:
        await set_fault("contacts.upsert", "500", times=2)
        result = await run(
            "website",
            "website-lead-standard.json",
            settings=settings,
            ghl=ghl_client,
            mapping=mapping,
            table=table,
        )
        assert result.status is EventStatus.SUCCEEDED
        assert mock_ghl.call_counts["contacts.upsert"] == 3  # 2 failures + 1 success
        assert len(mock_ghl.contacts) == 1

    async def test_rate_limiting_is_honoured_and_recovered_from(
        self, settings, ghl_client, mapping, table, mock_ghl
    ) -> None:
        await set_fault("contacts.upsert", "429", times=1)
        result = await run(
            "website",
            "website-lead-standard.json",
            settings=settings,
            ghl=ghl_client,
            mapping=mapping,
            table=table,
        )
        assert result.status is EventStatus.SUCCEEDED
        assert len(mock_ghl.contacts) == 1

    async def test_exhausted_retries_leave_the_event_retryable_not_lost(
        self, settings, ghl_client, mapping, table, mock_ghl
    ) -> None:
        await set_fault("contacts.upsert", "500", times=10)
        result = await run(
            "website",
            "website-lead-standard.json",
            settings=settings,
            ghl=ghl_client,
            mapping=mapping,
            table=table,
        )
        assert result.status is EventStatus.FAILED
        assert result.error["retryable"] is True
        assert result.error["terminal"] is False
        assert mock_ghl.contacts == {}

    async def test_a_permanent_rejection_goes_straight_to_the_dead_letter_queue(
        self, settings, ghl_client, mapping, table, mock_ghl
    ) -> None:
        """A 401 means the token is wrong. Retrying it four times fails four
        times and delays the alert that would have told someone."""
        await set_fault("contacts.upsert", "401", times=1)
        result = await run(
            "website",
            "website-lead-standard.json",
            settings=settings,
            ghl=ghl_client,
            mapping=mapping,
            table=table,
        )
        assert result.status is EventStatus.DEAD_LETTERED
        assert result.error["retryable"] is False
        assert mock_ghl.call_counts["contacts.upsert"] == 1  # not retried

        async with session_scope() as session:
            entries = await repo.list_dead_letters(session)
            assert len(entries) == 1
            assert entries[0].raw_payload  # the original body, ready to replay


class TestPartialFailure:
    async def test_a_retry_after_a_partial_failure_does_not_duplicate_the_contact(
        self, settings, ghl_client, mapping, table, mock_ghl
    ) -> None:
        """The failure this whole design exists to prevent.

        Attempt 1: contact created, then the opportunity write fails hard.
        Attempt 2: must reuse the contact and only create the opportunity.
        A naive retry creates a second contact and a second pipeline card.
        """
        await set_fault("opportunities.create", "500", times=10)
        kwargs = {"settings": settings, "ghl": ghl_client, "mapping": mapping, "table": table}

        first = await run("website", "website-lead-standard.json", **kwargs)
        assert first.status is EventStatus.FAILED
        assert len(mock_ghl.contacts) == 1  # the contact did get created
        assert mock_ghl.opportunities == {}
        upserts_after_first = mock_ghl.call_counts["contacts.upsert"]

        mock_ghl.faults.clear()  # the outage ends
        second = await run("website", "website-lead-standard.json", **kwargs)

        assert second.status is EventStatus.SUCCEEDED
        assert len(mock_ghl.contacts) == 1  # still one contact
        assert len(mock_ghl.opportunities) == 1
        # The contact step was memoized, so GHL was not called again for it.
        assert mock_ghl.call_counts["contacts.upsert"] == upserts_after_first
        assert second.contact_id == first.contact_id

    async def test_the_customer_is_not_messaged_before_the_crm_record_exists(
        self, settings, ghl_client, mapping, table, mock_ghl
    ) -> None:
        """Step ordering: side effects that reach the customer go last.

        If the opportunity write fails, the customer has not been texted about a
        job that exists nowhere in the CRM.
        """
        await set_fault("opportunities.create", "500", times=10)
        await run(
            "website",
            "website-lead-emergency.json",
            settings=settings,
            ghl=ghl_client,
            mapping=mapping,
            table=table,
        )
        assert mock_ghl.messages == []


class TestObservability:
    async def test_every_step_is_recorded_with_timings(
        self, settings, ghl_client, mapping, table, mock_ghl
    ) -> None:
        result = await run(
            "website",
            "website-lead-emergency.json",
            settings=settings,
            ghl=ghl_client,
            mapping=mapping,
            table=table,
        )
        async with session_scope() as session:
            audit = await repo.audit_for_event(session, result.event_id)
            steps = [a.step for a in audit]
            assert steps == [
                "normalize",
                "lead_identity",
                "ai_qualification",
                "routing",
                "ghl_contact",
                "ghl_note",
                "ghl_opportunity",
                "follow_up",
                "notify_sales",
            ]
            assert all(a.ok for a in audit)

    async def test_a_failure_records_which_steps_completed_before_it(
        self, settings, ghl_client, mapping, table, mock_ghl
    ) -> None:
        await set_fault("opportunities.create", "401", times=1)
        result = await run(
            "website",
            "website-lead-standard.json",
            settings=settings,
            ghl=ghl_client,
            mapping=mapping,
            table=table,
        )
        completed = [s.name for s in result.steps if s.ok]
        failed = [s.name for s in result.steps if not s.ok]
        assert "ghl_contact" in completed
        assert failed == ["ghl_opportunity"]

    async def test_the_correlation_id_ties_our_logs_to_the_outbound_calls(
        self, settings, ghl_client, mapping, table, mock_ghl
    ) -> None:
        result = await run(
            "website",
            "website-lead-standard.json",
            settings=settings,
            ghl=ghl_client,
            mapping=mapping,
            table=table,
        )
        forwarded = {
            entry["correlation_id"] for entry in mock_ghl.call_log if entry["correlation_id"]
        }
        assert forwarded == {result.correlation_id}

    async def test_provider_calls_are_logged_per_dependency(
        self, settings, ghl_client, mapping, table, mock_ghl
    ) -> None:
        result = await run(
            "website",
            "website-lead-standard.json",
            settings=settings,
            ghl=ghl_client,
            mapping=mapping,
            table=table,
        )
        async with session_scope() as session:
            calls = await repo.provider_calls_for_event(session, result.event_id)
            providers = {c.provider for c in calls}
            assert "ghl" in providers
            assert "deterministic" in providers
