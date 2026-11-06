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

from .base import Playbook
from .calibrate import Calibrate
from .ci_doctor import CiDoctor
from .commands import Commands
from .evaluate import EvaluatePlaybook
from .fix import FixPlaybook
from .followup import Followup
from .housekeeping import Journal, LabTest, Stale
from .hygiene import Hygiene
from .merge import MergePlaybook
from .onboard import Onboard, ProfileDrift
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
        EvaluatePlaybook(),
        Calibrate(),
        Hygiene(),
        CiDoctor(),
        MergePlaybook(),
        Commands(),
        Onboard(),
        ProfileDrift(),
        Stale(),
        LabTest(),
        Journal(),
        SelfImprove(),
        FixPlaybook(),
    ]


__all__ = ["Playbook", "all_playbooks"]
