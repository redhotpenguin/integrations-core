# (C) Datadog, Inc. 2021-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
from __future__ import annotations

from typing import Optional, Tuple

from pydantic import BaseModel

from datadog_checks.base.utils.functions import identity

from . import defaults, validators


class SharedConfig(BaseModel):
    class Config:
        allow_mutation = False

    device_global_exclude: Optional[Tuple[str]]
    file_system_global_exclude: Optional[Tuple[str]]
    mount_point_global_exclude: Optional[Tuple[str]]
    service: Optional[str]
