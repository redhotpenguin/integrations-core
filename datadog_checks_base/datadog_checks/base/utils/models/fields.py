# (C) Datadog, Inc. 2021-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
from immutables import Map
from pydantic.fields import SHAPE_MAPPING, SHAPE_SINGLETON, SHAPE_TUPLE


def get_default_field_value(field, value):
    if field.shape == SHAPE_MAPPING:
        return Map()
    elif field.shape == SHAPE_TUPLE:
        return ()
    elif field.shape == SHAPE_SINGLETON:
        field_type = field.type_
        if field_type in (float, int, str):
            return field_type()

    return value
