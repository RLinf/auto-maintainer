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

"""Locations of the files shipped with the service: default config, packs, and skills."""

from __future__ import annotations

from pathlib import Path

_PKG = Path(__file__).resolve().parent


def data_dir(name: str) -> Path:
    """Directory ``name`` (``config``, ``packs``, or ``skills``) from the wheel or the checkout."""
    packaged = _PKG / "_data" / name
    if packaged.is_dir():
        return packaged
    checkout = _PKG.parents[1] / name
    if checkout.is_dir():
        return checkout
    raise FileNotFoundError(
        f"cannot find the {name!r} directory in {packaged} or {checkout}"
    )


def defaults_dir() -> Path:
    return data_dir("config") / "defaults"
