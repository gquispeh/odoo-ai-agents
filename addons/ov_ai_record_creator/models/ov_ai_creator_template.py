# -*- coding: utf-8 -*-
import json
import logging

from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


class OvAiCreatorTemplate(models.Model):
    """Declarative template that describes how to materialize records of a given model.

    A template tells the system:
    - Which Odoo model to create (``target_model``).
    - Which fields are required, optional and computed via defaults.
    - Which related records must be created together (``related_specs_json``).
    - Validation rules to run before writing.

    The LLM is *guided* by these templates: when parsing a user request,
    we tell the model the exact JSON schema it must return. This makes
    parsing cheap and predictable instead of free-form generation.
    """
    _name = 'ov.ai.creator.template'
    _description = 'OpenVoo AI · Record Creation Template'
    _order = 'sequence, id'

    name = fields.Char(string='Template', required=True, translate=True)
    code = fields.Char(string='Technical Code', required=True)
    sequence = fields.Integer(string='Sequence', default=10)
    target_model = fields.Char(
        string='Target Odoo Model', required=True,
        help='Technical model name, e.g. hr.employee, res.partner, product.product.',
    )
    description = fields.Text(string='Description', translate=True)
    active = fields.Boolean(default=True)

    # ------------------------------------------------------------------
    # Declarative schema
    # ------------------------------------------------------------------
    fields_schema_json = fields.Text(
        string='Fields Schema (JSON)', required=True,
        help=(
            "JSON describing fields. Example:\n"
            '[{"name":"name","required":true,"type":"string",'
            '"description":"Full name of the employee"},'
            '{"name":"job_title","required":false,"type":"string"}]'
        ),
    )
    defaults_json = fields.Text(
        string='Default Values (JSON)',
        help='JSON object of default values to apply if the user does not provide them.',
    )
    related_specs_json = fields.Text(
        string='Related Records Spec (JSON)',
        help=(
            "JSON list of related records to create alongside the main one. "
            'Example for hr.employee: '
            '[{"model":"hr.contract","fields_from_main":["employee_id"],"defaults":{"state":"draft"}}]'
        ),
    )
    validators_json = fields.Text(
        string='Validators (JSON)',
        help='JSON list of validator rule names to run before writing.',
    )

    _sql_constraints = [
        ('code_unique', 'unique(code)', 'Template code must be unique.'),
    ]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _get_fields_schema(self):
        self.ensure_one()
        try:
            return json.loads(self.fields_schema_json or '[]')
        except json.JSONDecodeError as exc:
            raise ValidationError(_(
                "Template '%s' has invalid fields_schema_json: %s"
            ) % (self.code, exc))

    def _get_defaults(self):
        self.ensure_one()
        try:
            return json.loads(self.defaults_json or '{}')
        except json.JSONDecodeError:
            return {}

    def _get_related_specs(self):
        self.ensure_one()
        try:
            return json.loads(self.related_specs_json or '[]')
        except json.JSONDecodeError:
            return []

    def build_llm_parser_prompt(self):
        """Compose the system prompt that the LLM uses to parse natural language.

        We tell the LLM exactly what JSON shape we need. This keeps parsing
        deterministic and lets us use a cheap model with high reliability.
        """
        self.ensure_one()
        schema = self._get_fields_schema()
        field_lines = []
        for f in schema:
            req = 'required' if f.get('required') else 'optional'
            desc = f.get('description', '')
            field_lines.append(
                f"  - {f['name']} ({f.get('type', 'string')}, {req}): {desc}"
            )
        fields_block = "\n".join(field_lines)
        return (
            f"You are a structured data extractor for Odoo records.\n"
            f"Target model: {self.target_model}\n"
            f"Extract one OR MORE records from the user's natural language input.\n\n"
            f"Fields:\n{fields_block}\n\n"
            f"Output a JSON object with the shape:\n"
            f'{{ "records": [ {{ ...fields... }} ], "confidence": 0.0-1.0, "warnings": ["..."] }}\n'
            f"If a field cannot be inferred and is required, set it to null and add a "
            f"warning explaining what is missing. Do not invent values."
        )

    # ------------------------------------------------------------------
    # Materialization
    # ------------------------------------------------------------------
    def materialize(self, parsed_records):
        """Create the actual Odoo records from a list of parsed dicts.

        Returns the recordset of main records created.
        Raises UserError if validation fails — caller catches and surfaces.
        """
        self.ensure_one()
        if not parsed_records:
            raise UserError(_("No records to create."))

        Target = self.env[self.target_model]
        defaults = self._get_defaults()
        related_specs = self._get_related_specs()
        schema = {f['name']: f for f in self._get_fields_schema()}

        created = Target.browse()
        for rec_data in parsed_records:
            # Drop unknown keys defensively
            clean = {k: v for k, v in rec_data.items() if k in schema and v is not None}
            # Apply defaults for missing fields
            for k, v in defaults.items():
                clean.setdefault(k, v)
            # Required check
            missing_required = [
                f['name'] for f in self._get_fields_schema()
                if f.get('required') and clean.get(f['name']) in (None, '')
            ]
            if missing_required:
                raise UserError(_(
                    "Missing required fields for %s: %s"
                ) % (self.target_model, ", ".join(missing_required)))
            main = Target.create(clean)
            created |= main
            # Create related records
            for spec in related_specs:
                self._create_related(main, spec)
        return created

    def _create_related(self, main_record, spec):
        """Create a related record described by a spec dict.

        spec example:
            {"model": "hr.contract",
             "field_to_main": "employee_id",
             "defaults": {"state": "draft", "wage": 1025}}
        """
        target_model = spec.get('model')
        if not target_model or target_model not in self.env:
            _logger.warning("Related spec points to unknown model: %s", target_model)
            return False
        Target = self.env[target_model]
        values = dict(spec.get('defaults') or {})
        field_to_main = spec.get('field_to_main')
        if field_to_main:
            values[field_to_main] = main_record.id
        try:
            return Target.create(values)
        except Exception as exc:  # noqa: BLE001
            _logger.exception("Failed to create related %s: %s", target_model, exc)
            return False
