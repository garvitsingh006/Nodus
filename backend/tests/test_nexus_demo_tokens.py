"""Standard-library checks for readable local-demo token aliases.

The existing integration suite exercises the database resolver and effective
source grants. These checks guard the alias-to-seed mapping without needing
Neo4j, PostgreSQL, FastAPI, or third-party test dependencies.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from backend.app.governance.demo_tokens import DEMO_TOKEN_ALIASES, canonical_demo_token


DATASET = Path(__file__).resolve().parents[2] / "nexus_src" / "7A" / "NEXUS_Stage_7A"
PROJECT_SUFFIX = {
    "NEX-PRJ-PAY": "pay", "NEX-PRJ-COM": "com",
    "NEX-PRJ-ID": "id", "NEX-PRJ-DATA": "data",
}


class DemoTokenTests(unittest.TestCase):
    def test_existing_demo_tokens_remain_unchanged(self):
        for token in ("4", "D02", "D09", "I10", "I12"):
            self.assertEqual(canonical_demo_token(token), token)

    def test_unrecognized_tokens_are_not_mapped_to_an_identity(self):
        for token in ("dev-10-A1", "dev-02-ID", "dev-02-pay-extra", "", "unknown"):
            self.assertEqual(canonical_demo_token(token), token)

    def test_aliases_match_real_employee_roles_and_project_assignments(self):
        with (DATASET / "roles_and_demo_identities.json").open(encoding="utf-8") as file:
            roles_doc = json.load(file)
        with (DATASET / "memberships.json").open(encoding="utf-8") as file:
            memberships = json.load(file)

        role_by_id = {item["id"]: item["role_code"] for item in roles_doc["roles"]}
        demo_by_token = {item["demo_token"]: item for item in roles_doc["demo_identities"]}
        projects_by_employee: dict[str, list[str]] = {}
        for assignment in memberships["project_assignments"]:
            projects_by_employee.setdefault(assignment["employee_id"], []).append(assignment["project_id"])

        self.assertEqual(len(DEMO_TOKEN_ALIASES), len(roles_doc["demo_identities"]) - 1)
        self.assertEqual(len(set(DEMO_TOKEN_ALIASES.values())), len(DEMO_TOKEN_ALIASES))
        self.assertEqual(role_by_id[demo_by_token["4"]["role_id"]], "ADMIN")
        for alias, canonical in DEMO_TOKEN_ALIASES.items():
            person = demo_by_token[canonical]
            role = role_by_id[person["role_id"]]
            self.assertIn(role, ("DEVELOPER", "INTERN"))
            employee = person["employee_id"]
            projects = projects_by_employee[employee]
            self.assertEqual(len(projects), 1)
            prefix = "dev" if role == "DEVELOPER" else "intern"
            employee_number = int(employee.rsplit("-", 1)[-1])
            expected = f"{prefix}-{employee_number:02d}-{PROJECT_SUFFIX[projects[0]]}"
            self.assertEqual(alias, expected)
            self.assertEqual(canonical_demo_token(alias), canonical)


if __name__ == "__main__":
    unittest.main()
