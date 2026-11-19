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

"""Playbooks: one workflow each (design 5.5)."""

from __future__ import annotations

from .auto_schedule import AutoSchedule
from .base import Playbook
from .calibrate import Calibrate
from .ci_doctor import CiDoctor
from .ci_rules import CiRulesPlaybook
from .commands import Commands
from .comment_check import CommentCheckPlaybook, CommentScan
from .evaluate import EvaluatePlaybook
from .fix import FixPlaybook
from .fix_ci import FixCiPlaybook
from .flake_learn import FlakeLearn
from .followup import Followup
from .governance import GovernancePlaybook
from .housekeeping import Journal, LabTest, Stale
from .hygiene import Hygiene
from .learn import LearnFromRejection
from .merge import MergePlaybook
from .merge_base import MergeBasePlaybook
from .onboard import Onboard, ProfileDrift
from .person_notes import LearnPersonNotes, PersonNotesScan
from .quality import (
    QualityPlaybook,
    QualityScan,
    QuickLearnPlaybook,
    QuickQualityPlaybook,
)
from .reproduce import ReproducePlaybook
from .retrospect import Retrospect
from .review import ReviewPlaybook
from .review_scan import ReviewScan
from .self_improve import SelfImprove
from .triage import Triage


def all_playbooks() -> list[Playbook]:
    return [
        Triage(),
        ReviewPlaybook(),
        ReviewScan(),
        Followup(),
        CommentCheckPlaybook(),
        CommentScan(),
        EvaluatePlaybook(),
        Calibrate(),
        LearnFromRejection(),
        Hygiene(),
        CiDoctor(),
        MergePlaybook(),
        AutoSchedule(),
        CiRulesPlaybook(),
        FlakeLearn(),
        Commands(),
        Onboard(),
        ProfileDrift(),
        Stale(),
        LabTest(),
        Journal(),
        SelfImprove(),
        FixPlaybook(),
        FixCiPlaybook(),
        MergeBasePlaybook(),
        GovernancePlaybook(),
        ReproducePlaybook(),
        Retrospect(),
        QualityPlaybook(),
        QuickQualityPlaybook(),
        QuickLearnPlaybook(),
        QualityScan(),
        LearnPersonNotes(),
        PersonNotesScan(),
    ]


__all__ = ["Playbook", "all_playbooks"]
