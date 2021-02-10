# (C) Datadog, Inc. 2021-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
import re

import yaml

from datamodel_code_generator.format import CodeFormatter, PythonVersion
from datamodel_code_generator.parser.openapi import OpenAPIParser

from ..utils import sanitize_openapi_object_properties

PYTHON_VERSION = PythonVersion.PY_38
EXTRA_TYPE_FIELDS = ('compact_example', 'default', 'example')


def process_model_file(model_file_contents):
    """
    We modify the model files in the following ways:

    - Ensure that every model has a Config with `allow_mutation = False`
    - Convert all `list` types to `tuple`
    - Convert all `dict` types to `immutables.Map`
    - Add the necessary imports for default values and custom validators
    """
    lines = model_file_contents.splitlines()

    # TODO: look for builtin types when the Agent upgrades Python to 3.9
    dict_pattern = r'(?<=\b)Dict(?=\b)'
    list_pattern = r'(?<=\b)List(?=\b)'

    dict_used = False
    list_used = False
    import_lines = []

    # Keep track of each model
    model_data = []
    temp_model_data = []

    for i, line in enumerate(lines):
        if line.startswith('from '):
            import_lines.append(i)

        if line.startswith('class '):
            if temp_model_data:
                model_data.append(temp_model_data)

            # (<where Config class should be defined>, <whether or not `immutables.Map` is used as a type>)
            temp_model_data = [i + 1, False]
        elif temp_model_data:
            if not re.match(r' {4}\w+: ', line):
                continue

            field_part, sep, type_part = line.partition(': ')

            new_type_part = re.sub(dict_pattern, 'Mapping', type_part)
            if new_type_part != type_part:
                dict_used = True
                temp_model_data[1] = True

            final_type_part = re.sub(list_pattern, 'Tuple', new_type_part)
            if final_type_part != new_type_part:
                list_used = True

            lines[i] = f'{field_part}{sep}{final_type_part}'

    if temp_model_data:
        model_data.append(temp_model_data)

    # Perform all modifications from this point on in reverse line order
    for config_def_location, map_used in reversed(model_data):
        config_start_location = config_def_location + 1
        config_def_line = lines[config_def_location]

        config_def = '    class Config:'
        if not config_def_line.startswith(config_def):
            lines.insert(config_def_location, '')
            lines.insert(config_def_location, config_def)

        lines.insert(config_start_location, '        allow_mutation = False')

    final_import_line = import_lines[-1]

    local_import_start_location = final_import_line + 1
    for line in reversed(
        (
            '',
            'from datadog_checks.base.utils.functions import identity',
            '',
            'from . import defaults, validators',
        )
    ):
        lines.insert(local_import_start_location, line)

    # TODO: uncomment when the Agent upgrades Python to 3.9
    # if dict_used:
    #     if len(import_lines) == 2:
    #         lines.insert(final_import_line, 'from collections.abc import Mapping')
    #         lines.insert(final_import_line, '')
    #     else:
    #         lines.insert(import_lines[1], 'from collections.abc import Mapping')

    # TODO: remove when the Agent upgrades Python to 3.9
    if dict_used or list_used:
        line_diff = 2
        typing_import_line = final_import_line - line_diff
        line = lines[typing_import_line]
        package_part, sep, imports_part = line.partition(' import ')

        imports = set(imports_part.split(', '))
        for t in ('Dict', 'List'):
            imports.discard(t)

        if dict_used:
            imports.add('Mapping')

        if list_used:
            imports.add('Tuple')

        if imports:
            final_imports_part = ', '.join(sorted(imports))
            lines[typing_import_line] = f'{package_part}{sep}{final_imports_part}'
        else:
            for _ in range(line_diff):
                del lines[typing_import_line]

    lines.append('')
    return '\n'.join(lines)


class ModelConsumer:
    def __init__(self, spec):
        self.spec = spec

    def render(self):
        files = {}

        for file in self.spec['files']:
            model_files = {
                '__init__.py': ('from .instance import InstanceConfig\nfrom .shared import SharedConfig\n', []),
            }

            for section in file['options']:
                errors = []

                section_name = section['name']
                if section_name == 'init_config':
                    model_file_name = 'shared.py'
                    schema_name = 'SharedConfig'
                elif section_name == 'instances':
                    model_file_name = 'instance.py'
                    schema_name = 'InstanceConfig'
                # Skip anything checks don't use directly
                else:
                    continue

                # We want to create something like:
                #
                # components:
                #   schemas:
                #     InstanceConfig:
                #       required:
                #         - endpoint
                #       properties:
                #         endpoint:
                #           type: string
                #         timeout:
                #           type: number
                #         ...
                openapi_document = {'components': {'schemas': {}}}
                schema = openapi_document['components']['schemas'][schema_name] = {}

                required_options = schema['required'] = []
                options = schema['properties'] = {}

                for option in section['options']:
                    option_name = option['name']
                    if option['required']:
                        required_options.append(option_name)

                    type_data = option['value']
                    options[option_name] = type_data

                    # Remove fields that aren't part of the OpenAPI specification
                    for extra_field in EXTRA_TYPE_FIELDS:
                        type_data.pop(extra_field, None)

                    sanitize_openapi_object_properties(type_data)

                try:
                    parser = OpenAPIParser(
                        yaml.safe_dump(openapi_document),
                        target_python_version=PythonVersion.PY_38,
                        # TODO: uncomment when the Agent upgrades Python to 3.9
                        # use_standard_collections=True,
                        strip_default_none=True,
                        # https://github.com/koxudaxi/datamodel-code-generator/pull/173
                        field_constraints=True,
                    )
                    model_file_contents = parser.parse()
                except Exception as e:
                    errors.append(f'Error parsing the OpenAPI schema `{schema_name}`: {e}')
                    model_files[model_file_name] = ('', errors)
                else:
                    model_files[model_file_name] = (process_model_file(model_file_contents), errors)

            files[file['name']] = model_files

        return files
