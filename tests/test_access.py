# Copyright 2026 The RLinf Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Who may do what (``domain/access.py``, design 2.7): a person's role in a
repository, and the permissions it and ``config/access.yaml`` give them."""

from __future__ import annotations

import pytest
import yaml

from auto_maintainer.domain.access import (
    AccessConfig,
    Facts,
    Grant,
    PeopleRole,
    Role,
    resolve,
)
from auto_maintainer.domain.permissions import Permission

REPO = "acme/widget"
CONTRIBUTOR = PeopleRole(id="contributor", name="Contributor")
AREA_OWNER = PeopleRole(
    id="area-owner", name="Area Owner", github_permission=Permission.WRITE
)


def _facts(**kw: object) -> Facts:
    return Facts(login="ana", repo=REPO, **kw)  # type: ignore[arg-type]


def _config(text: str = "") -> AccessConfig:
    return AccessConfig.model_validate(yaml.safe_load(text) or {})


@pytest.mark.parametrize(
    ("facts", "role"),
    [
        (_facts(permission=Permission.ADMIN, maintains=True), Role.MAINTAINER),
        (_facts(permission=Permission.WRITE), Role.REVIEWER),
        (
            _facts(permission=Permission.READ, people_role=CONTRIBUTOR),
            Role.GUEST_REVIEWER,
        ),
        (_facts(permission=Permission.READ, people_role=AREA_OWNER), Role.REVIEWER),
        # GitHub write access is never less than a reviewer's.
        (_facts(permission=Permission.WRITE, people_role=CONTRIBUTOR), Role.REVIEWER),
        (_facts(permission=Permission.READ), None),
        # A People record place counts only with access to the repository.
        (_facts(permission=Permission.NONE, people_role=CONTRIBUTOR), None),
    ],
)
def test_a_role_comes_from_the_gate_the_people_record_and_github(
    facts: Facts, role: Role | None
) -> None:
    assert resolve(_config(), facts).role == role


def test_the_repositorys_mapping_and_the_file_set_roles() -> None:
    config = _config(
        """
        repos: {acme/widget: {people_roles: {contributor: viewer}}}
        people:
          Bob: {role: blocked}
          cara: {role: viewer, repos: {acme/widget: maintainer}}
        """
    )
    ana = resolve(config, _facts(permission=Permission.READ, people_role=CONTRIBUTOR))
    assert ana.role == Role.VIEWER and "People record" in ana.role_from
    assert ana.grants.keys() == {Grant.SEE_PRS}
    bob = resolve(config, Facts(login="bob", repo=REPO, permission=Permission.ADMIN))
    assert bob.role == Role.BLOCKED and not bob.grants, "even with admin access"
    assert config.blocked("BOB") and config.blocked("bob", REPO)
    cara = resolve(config, Facts(login="cara", repo=REPO))
    assert cara.role == Role.MAINTAINER and cara.role_from == f"set for them in {REPO}"


def test_a_guest_uses_none_of_the_services_agents_or_machines_unless_enabled() -> None:
    facts = _facts(permission=Permission.READ, people_role=CONTRIBUTOR)
    guest = resolve(_config(), facts)
    assert guest.can(Grant.REVIEW_ASSIGNED) and guest.can(Grant.OWN_AGENTS)
    for grant in (Grant.USE_AGENTS, Grant.USE_MACHINES, Grant.REVIEW_ANY):
        assert not guest.can(grant)
    enabled = resolve(
        _config(
            "people: {ana: {grants: {use.agents: true}, limits: {jobs_at_once: 1}}}"
        ),
        facts,
    )
    assert enabled.grants[Grant.USE_AGENTS] == "set for them"
    assert enabled.limits.jobs_at_once == 1
    by_role = resolve(
        _config(
            "roles: {guest_reviewer: {grants: {use.machines: true}, "
            "limits: {tokens_per_day: 100}}}"
        ),
        facts,
    )
    assert by_role.can(Grant.USE_MACHINES) and by_role.limits.tokens_per_day == 100


def test_no_file_gets_around_the_configuration_gate() -> None:
    config = _config("people: {ana: {role: maintainer, grants: {manage.access: true}}}")
    ana = resolve(config, _facts(permission=Permission.WRITE))
    assert ana.role == Role.MAINTAINER
    assert not ana.can(Grant.MERGE_SCHEDULE), "merges stay with who passes the gate"
    assert Grant.MERGE_SCHEDULE in ana.withheld
    assert not ana.can(Grant.MANAGE_ACCESS), "only the owner and service admins"
    maintainer = resolve(_config(), _facts(permission=Permission.ADMIN, maintains=True))
    assert maintainer.can(Grant.MERGE_SCHEDULE)


def test_the_owner_and_service_admins_manage_access_where_they_have_a_role() -> None:
    config = _config("admins: [Ana]")
    admin = resolve(config, _facts(permission=Permission.WRITE))
    assert admin.admin and admin.can(Grant.MANAGE_ACCESS) and admin.can(Grant.SEE_AUDIT)
    owner = resolve(_config(), _facts(permission=Permission.WRITE, owner=True))
    assert owner.can(Grant.MANAGE_ACCESS)
    operator = resolve(_config(), _facts(permission=Permission.WRITE, operator=True))
    assert operator.can(Grant.SEE_INTERNALS) and not operator.can(Grant.MANAGE_ACCESS)


def test_the_file_refuses_what_it_does_not_know() -> None:
    with pytest.raises(ValueError):
        _config("people: {ana: {role: emperor}}")
    with pytest.raises(ValueError):
        _config("people: {ana: {grants: {fly: true}}}")
    with pytest.raises(ValueError):
        _config("people: {ana: {}, ANA: {}}")
