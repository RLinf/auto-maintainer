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

"""Policy decisions (design 5.8) and the leak guard."""

from __future__ import annotations

import pytest

from auto_maintainer.domain.actions import (
    Action,
    ActionHints,
    ActionKind,
    Autonomy,
    DecisionKind,
)
from auto_maintainer.domain.profile import RepoProfile
from auto_maintainer.domain.visibility import PublicText
from auto_maintainer.policy.engine import PolicyFacts, decide
from auto_maintainer.policy.leak_guard import (
    TIER_BLIND,
    LeakError,
    check_text,
    make_public,
)

REPO = "acme/widget"


def _profile(**policy: dict) -> RepoProfile:
    return RepoProfile.model_validate({"repo": REPO, "policy": policy})


def _action(kind: ActionKind = ActionKind.LABEL_APPLY, **hints: object) -> Action:
    return Action(
        kind=kind, repo=REPO, number=1, head_sha="a" * 40, hints=ActionHints(**hints)
    )


@pytest.mark.parametrize(
    ("level", "approved", "expected"),
    [
        (Autonomy.OFF, None, DecisionKind.DENY),
        (Autonomy.SHADOW, None, DecisionKind.SHADOW),
        (Autonomy.SHADOW, "maria", DecisionKind.SHADOW),
        (Autonomy.PROPOSE, None, DecisionKind.PROPOSE),
        (Autonomy.PROPOSE, "maria", DecisionKind.EXECUTE),
        (Autonomy.AUTO, None, DecisionKind.EXECUTE),
    ],
)
def test_levels(level: Autonomy, approved: str | None, expected: DecisionKind) -> None:
    profile = _profile(**{"label.apply": {"level": level}})
    assert (
        decide(_action(), profile, PolicyFacts(approved_by=approved)).kind == expected
    )


def test_conditions_fall_back_to_otherwise() -> None:
    profile = _profile(
        **{
            "label.ci_gate": {
                "level": "auto",
                "conditions": {"author": "team", "recommendation": "ready"},
                "otherwise": "propose",
            }
        }
    )
    ok = _action(ActionKind.LABEL_CI_GATE, author_is_team=True, recommendation="ready")
    assert (
        decide(
            ok, profile, PolicyFacts(author_is_team=True, recommendation="ready")
        ).kind
        == DecisionKind.EXECUTE
    )
    assert (
        decide(
            ok, profile, PolicyFacts(author_is_team=False, recommendation="ready")
        ).kind
        == DecisionKind.PROPOSE
    )
    assert (
        decide(
            ok, profile, PolicyFacts(author_is_team=True, recommendation="changes")
        ).kind
        == DecisionKind.PROPOSE
    )


def test_path_conditions() -> None:
    profile = _profile(
        **{
            "label.apply": {
                "level": "auto",
                "conditions": {"paths": ["docs/**", "*.md"]},
            }
        }
    )
    assert (
        decide(
            _action(), profile, PolicyFacts(changed_paths=["docs/a.md", "README.md"])
        ).kind
        == DecisionKind.EXECUTE
    )
    assert (
        decide(
            _action(), profile, PolicyFacts(changed_paths=["docs/a.md", "src/x.py"])
        ).kind
        == DecisionKind.SHADOW
    )


def test_hard_limits() -> None:
    profile = _profile(
        **{"review.post": {"level": "auto"}, "label.apply": {"level": "auto"}}
    )
    assert (
        decide(
            _action(ActionKind.REVIEW_POST), profile, PolicyFacts(executed_on_sha=1)
        ).kind
        == DecisionKind.DENY
    )
    capped = PolicyFacts(public_writes_last_hour=500)
    assert decide(_action(), profile, capped).rule == "limit.write_cap"
    internal = Action(kind=ActionKind.NOTIFY, repo=REPO)
    assert (
        decide(internal, _profile(notify={"level": "auto"}), capped).kind
        == DecisionKind.EXECUTE
    )


def test_pause_and_dev_mode() -> None:
    profile = _profile(**{"label.apply": {"level": "auto"}})
    assert (
        decide(_action(), profile, PolicyFacts(paused=True)).kind
        == DecisionKind.PROPOSE
    )
    assert (
        decide(_action(), profile, PolicyFacts(paused=True, approved_by="m")).kind
        == DecisionKind.PROPOSE
    )
    assert (
        decide(_action(), profile, PolicyFacts(dev_mode=True)).kind
        == DecisionKind.SHADOW
    )


def test_a_standby_host_records_forge_writes_nobody_approved() -> None:
    profile = _profile(
        **{
            "label.apply": {"level": "auto"},
            "comment.upsert": {"level": "propose"},
            "lab.review": {"level": "auto"},
            "notify": {"level": "auto"},
        }
    )
    standby = PolicyFacts(standby=True)
    label = decide(_action(), profile, standby)
    assert label.kind == DecisionKind.SHADOW
    assert label.rule == "standby records label.apply"
    approved = PolicyFacts(standby=True, approved_by="maria")
    assert decide(_action(), profile, approved).kind == DecisionKind.EXECUTE
    comment = _action(ActionKind.COMMENT_UPSERT)
    assert decide(comment, profile, standby).kind == DecisionKind.PROPOSE
    assert decide(comment, profile, approved).kind == DecisionKind.EXECUTE
    lab = _action(ActionKind.LAB_REVIEW)
    assert decide(lab, profile, standby).kind == DecisionKind.EXECUTE, (
        "a test machine's session writes nothing to the repository"
    )
    internal = Action(kind=ActionKind.NOTIFY, repo=REPO)
    assert decide(internal, profile, standby).kind == DecisionKind.EXECUTE


def test_leak_guard_rejects_internal_content() -> None:
    profile = RepoProfile.model_validate(
        {"repo": REPO, "confidentiality": {"internal_entities": ["Contoso Robotics"]}}
    )
    entities = ["Contoso Robotics"]
    for text in [
        "This is a P0 for us.",
        "Our rubric ranks this highly.",
        "Done for contoso robotics.",
        "score: 78 overall",
        "cc @someone",
        "这个修改很好，谢谢你的贡献，我们会尽快合并。",
    ]:
        assert not check_text(text, profile, {"alice"}, entities).ok, text
    ok = check_text(
        "Thanks @alice. `P0` in code is fine, and so is <!-- P1 -->.",
        profile,
        {"alice"},
        entities,
    )
    assert ok.ok, ok.errors
    with pytest.raises(LeakError):
        make_public("P1 now", profile, set(), entities)
    public, warnings = make_public(
        "We leverage this seamlessly.", profile, set(), entities
    )
    assert isinstance(public, PublicText) and warnings


def test_public_text_cannot_be_built_without_the_guard() -> None:
    with pytest.raises(TypeError):
        PublicText("hello", REPO, _key=object())


def test_leak_guard_rejects_credentials_even_in_code_blocks() -> None:
    profile = RepoProfile(repo=REPO)
    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"
    for text in [
        f"Here is the config:\n```\noauth_token: {token}\n```",
        "-----BEGIN OPENSSH PRIVATE KEY-----",
        "clone https://user:hunter2hunter2@example.com/x.git",
    ]:
        result = check_text(text, profile, set(), [])
        assert any("looks like" in e for e in result.errors), text


def test_tier_names_are_refused_only_where_the_writer_knew_the_tier() -> None:
    profile = RepoProfile(repo=REPO)
    finding = "- **P2 (`F1`)**: VPP2 fails at step zero."
    assert not check_text(finding, profile, set(), []).ok
    assert check_text(finding, profile, set(), [], tiers=False).ok
    assert not check_text("score: 78 overall", profile, set(), [], tiers=False).ok
    assert ActionKind.REVIEW_POST in TIER_BLIND
    assert ActionKind.COMMENT_UPSERT not in TIER_BLIND
