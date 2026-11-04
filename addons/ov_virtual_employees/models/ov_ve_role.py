# -*- coding: utf-8 -*-
from odoo import fields, models


class OvVirtualEmployeeRole(models.Model):
    """A template a virtual employee can be hired into.

    Holds the role-specific system-prompt fragment (in Spanish, since that's
    OpenVoo's primary market) and the default set of capabilities the role
    arrives with. Operators can override per-employee.
    """

    _name = 'ov.virtual.employee.role'
    _description = 'OpenVoo VE · Role Template'
    _order = 'sequence, name'

    name = fields.Char(string='Role', required=True, translate=True)
    code = fields.Char(string='Technical Code', required=True, copy=False)
    sequence = fields.Integer(default=10)
    icon = fields.Char(
        string='Icon',
        help='Optional emoji or single character used for badging.',
        default='👤',
    )

    short_pitch = fields.Char(
        string='One-Line Pitch', translate=True,
        help='Shown on the role card during hiring.',
    )
    description = fields.Text(
        string='Role Description', translate=True,
        help='Full description of what this role does day-to-day. Shown on '
             'the hire form.',
    )
    system_prompt_fragment = fields.Text(
        string='System-Prompt Fragment',
        help='Injected into the planner LLM prompt. Should describe the '
             "role's mandate, constraints, and tone in Spanish.",
    )

    default_capability_ids = fields.Many2many(
        'ov.virtual.employee.capability',
        'ov_ve_role_default_cap_rel',
        'role_id', 'capability_id',
        string='Default Capabilities',
        help='Capabilities seeded on every employee hired into this role.',
    )

    default_trust_level = fields.Selection([
        ('shadow', 'Shadow — suggest only'),
        ('assisted', 'Assisted — propose, require confirmation'),
        ('autonomous', 'Autonomous — auto for whitelisted, confirm for the rest'),
        ('trusted', 'Trusted — auto for all whitelisted'),
    ], string='Default Trust Level', default='shadow', required=True)

    active = fields.Boolean(default=True)

    _sql_constraints = [
        ('code_uniq', 'unique(code)', 'Role technical code must be unique.'),
    ]
