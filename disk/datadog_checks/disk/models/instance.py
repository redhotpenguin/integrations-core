# (C) Datadog, Inc. 2021-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
from __future__ import annotations

from typing import Any, Mapping, Optional, Tuple

from pydantic import BaseModel, Extra

from datadog_checks.base.utils.functions import identity

from . import defaults, validators


class CreateMount(BaseModel):
    class Config:
        allow_mutation = False
        extra = Extra.allow

    host: Optional[str]
    mountpoint: Optional[str]
    password: Optional[str]
    share: Optional[str]
    type: Optional[str]
    user: Optional[str]


class InstanceConfig(BaseModel):
    class Config:
        allow_mutation = False

    all_partitions: Optional[bool]
    blkid_cache_file: Optional[str]
    create_mounts: Optional[Tuple[CreateMount]]
    device_exclude: Optional[Tuple[str]]
    device_include: Optional[Tuple[str]]
    device_tag_re: Optional[Mapping[str, Any]]
    empty_default_hostname: Optional[bool]
    file_system_exclude: Optional[Tuple[str]]
    file_system_include: Optional[Tuple[str]]
    include_all_devices: Optional[bool]
    min_collection_interval: Optional[float]
    min_disk_size: Optional[float]
    mount_point_exclude: Optional[Tuple[str]]
    mount_point_include: Optional[Tuple[str]]
    service: Optional[str]
    service_check_rw: Optional[bool]
    tag_by_filesystem: Optional[bool]
    tag_by_label: Optional[bool]
    tags: Optional[Tuple[str]]
    timeout: Optional[int]
    use_mount: bool
