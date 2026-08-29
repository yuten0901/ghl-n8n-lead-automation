"""The documentation is part of the deliverable, so it is tested like one.

These catch the two ways a README rots: a link that points at a file someone
renamed, and a number that was true when it was written. Both are the kind of
thing a client notices in the first sixty seconds, and neither is caught by any
other test.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from tests.conftest import REPO_ROOT


def _is_ours(path: Path) -> bool:
    """Exclude dependency trees and anything in a dot-directory.

    The dot-directory rule matters more than it looks: pytest writes
    `.pytest_cache/README.md` at the end of a run, so on the *second* run that
    file joined this list. When the list drove a parametrize, that silently
    changed the suite's own test count and broke the count assertion below - a
    green first run and a red second run, and red on every CI checkout after the
    cache was restored.
    """
    parts = path.relative_to(REPO_ROOT).parts
    if any(part.startswith(".") for part in parts):
        return False
    return not {"venv", "node_modules", "site-packages"} & set(parts)


MARKDOWN = sorted(path for path in REPO_ROOT.rglob("*.md") if _is_ours(path))

# [text](target) - skip anchors, mailto and absolute URLs; those cannot be
# checked offline, and a test that needs the network is a test that flakes.
LINK = re.compile(r"\[[^\]]*\]\((?!https?://|mailto:|#)([^)\s]+)\)")


def read(path) -> str:
    return path.read_text(encoding="utf-8")


class TestLinksResolve:
    def test_every_relative_link_points_at_something_that_exists(self) -> None:
        """One test over all documents, deliberately not parametrized.

        Parametrizing over files makes the suite's *size* a function of how many
        documents exist, which then collides with the test-count assertion below:
        adding a single markdown file turned CI red for a reason that had nothing
        to do with the file. Reporting every broken link at once is also more
        useful than failing on the first document that has one.
        """
        broken: list[str] = []
        for document in MARKDOWN:
            for target in LINK.findall(read(document)):
                path_part = target.split("#", 1)[0]
                if not path_part:
                    continue
                if not (document.parent / path_part).resolve().exists():
                    broken.append(f"{document.relative_to(REPO_ROOT)} -> {target}")
        assert not broken, "broken relative links: " + "; ".join(broken)

    def test_readme_links_to_every_document(self) -> None:
        """A doc nobody links to is a doc nobody reads."""
        readme = read(REPO_ROOT / "README.md")
        for document in sorted((REPO_ROOT / "docs").glob("*.md")):
            assert document.name in readme, f"README does not link to docs/{document.name}"


class TestClaimsMatchReality:
    def test_the_advertised_test_count_is_current(self) -> None:
        """The README puts a test count on a badge. If it drifts it is a small
        lie, and small lies are the ones a careful reader finds first."""
        # sys.executable, not "python": the interpreter running the suite is the
        # one with the dependencies installed.
        collected = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", str(REPO_ROOT / "tests")],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        match = re.search(r"(\d+) tests? collected", collected)
        assert match, f"could not read the collected count from pytest:\n{collected[-500:]}"
        actual = int(match.group(1))

        readme = read(REPO_ROOT / "README.md")
        advertised = {int(n) for n in re.findall(r"tests-(\d+)-brightgreen", readme)}
        advertised |= {int(n) for n in re.findall(r"(\d+)\s+(?:automated\s+)?tests\b", readme)}
        assert advertised, "README no longer states a test count"
        assert advertised == {actual}, (
            f"README advertises {sorted(advertised)} tests; the suite has {actual}"
        )

    def test_the_advertised_node_count_matches_the_workflow(self) -> None:
        workflow = json.loads(
            (REPO_ROOT / "n8n" / "workflows" / "01-lead-intake.json").read_text(encoding="utf-8")
        )
        actual = len(workflow["nodes"])
        readme = read(REPO_ROOT / "README.md")
        advertised = {int(n) for n in re.findall(r"n8n-(\d+)%20nodes", readme)}
        advertised |= {int(n) for n in re.findall(r"(\d+)-node n8n workflow", readme)}
        assert advertised == {actual}, f"README says {sorted(advertised)} nodes; there are {actual}"

    def test_every_env_var_the_code_reads_is_documented(self) -> None:
        """A setting that exists but is undocumented is a setting nobody sets."""
        from leadops.config import Settings

        documented = read(REPO_ROOT / ".env.example").upper()
        missing = [name for name in Settings.model_fields if name.upper() not in documented]
        assert not missing, f".env.example does not document: {missing}"

    def test_every_documented_env_var_is_real(self) -> None:
        """The reverse: a documented setting the code ignores is worse than none,
        because someone will set it and expect it to work."""
        from leadops.config import Settings

        known = {name.upper() for name in Settings.model_fields}
        # Read by the n8n workflow rather than by this service.
        n8n_owned = {"LEADOPS_BASE_URL", "GHL_CALENDAR_ID"}
        declared = {
            line.split("=", 1)[0].strip()
            for line in read(REPO_ROOT / ".env.example").splitlines()
            if "=" in line and not line.strip().startswith("#")
        }
        unknown = declared - known - n8n_owned
        assert not unknown, f".env.example documents settings the code never reads: {unknown}"

    def test_the_lead_sources_named_in_the_readme_are_the_ones_implemented(self) -> None:
        from leadops.normalize.adapters import SOURCE_ADAPTERS

        readme = read(REPO_ROOT / "README.md").lower()
        for source in SOURCE_ADAPTERS:
            assert source in readme, f"README does not mention the '{source}' source"

    def test_the_routing_rules_named_in_the_readme_exist(self) -> None:
        from leadops.routing.rules import RoutingTable

        table = RoutingTable.load(REPO_ROOT / "config" / "routing.yml")
        known = {str(rule.get("id")) for rule in table.rules} | {str(table.default.get("id"))}
        readme = read(REPO_ROOT / "README.md")
        for quoted in re.findall(r'"rule_id": "([a-z_]+)"', readme):
            assert quoted in known, f"README quotes rule '{quoted}', which no longer exists"


class TestHonesty:
    """The README makes explicit claims about what is *not* verified. Those
    disclosures are load-bearing - if someone deletes one while tidying, the
    repository starts overstating itself. So they are asserted."""

    def test_the_real_vs_mocked_section_survives(self) -> None:
        readme = read(REPO_ROOT / "README.md")
        assert "What is real, and what is mocked" in readme
        for disclosure in [
            "Not demonstrated",
            "paid GoHighLevel",
            "Not executed",
        ]:
            assert disclosure in readme, f"README lost the '{disclosure}' disclosure"

    def test_no_claim_of_client_or_production_deployment(self) -> None:
        """This is a portfolio project. It must never say otherwise."""
        forbidden = [
            "built this for a client",
            "in production for",
            "deployed for a client",
            "our client",
            "production deployment at",
        ]
        # Checked line by line, skipping lines that are *prohibiting* the phrase.
        # docs/upwork-usage.md lists these phrases precisely to forbid them, and a
        # naive substring check would flag its own guardrail.
        prohibiting = ("never say", "not claim", "wrong:", "❌")

        for document in MARKDOWN:
            for number, line in enumerate(read(document).splitlines(), start=1):
                lowered = line.lower()
                if any(marker in lowered for marker in prohibiting):
                    continue
                for phrase in forbidden:
                    assert phrase not in lowered, (
                        f"{document.relative_to(REPO_ROOT)}:{number} claims '{phrase}': {line.strip()}"
                    )

    def test_docker_compose_is_flagged_as_unverified(self) -> None:
        """It was written but never executed. Anywhere it is offered as an
        instruction, that has to be said."""
        for document in [REPO_ROOT / "docs" / "demo.md", REPO_ROOT / "docker-compose.yml"]:
            body = read(document).lower()
            assert "not executed" in body, f"{document.name} does not flag Compose as unverified"
