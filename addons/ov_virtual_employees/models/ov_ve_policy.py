# -*- coding: utf-8 -*-
from odoo import _, api, fields, models


class OvVirtualEmployeePolicy(models.Model):
    """Per-employee, per-capability autonomy rule.

    The intersection of trust dial (which sets a default per employee) and
    the operator's manual overrides (which scope individual capabilities).
    """

    _name = 'ov.virtual.employee.policy'
    _description = 'OpenVoo VE · Policy'
    _order = 'employee_id, sequence, id'

    employee_id = fields.Many2one(
        'ov.virtual.employee', string='Employee',
        required=True, ondelete='cascade',
    )
    capability_id = fields.Many2one(
        'ov.virtual.employee.capability', string='Capability',
        required=True, ondelete='cascade',
    )
    capability_risk = fields.Selection(
        related='capability_id.risk_level', readonly=True, store=False,
    )
    sequence = fields.Integer(default=10)

    autonomy = fields.Selection([
        ('off', 'Off — capability disabled'),
        ('readonly', 'Read-only — may suggest, never propose writes'),
        ('confirm', 'Confirm — propose writes, require human approval'),
        ('auto', 'Auto — execute immediately (audit stays)'),
    ], string='Autonomy', default='confirm', required=True)

    daily_quota = fields.Integer(
        string='Daily Quota', default=0,
        help='0 = unlimited. Maximum invocations of this capability per UTC day.',
    )
    daily_used = fields.Integer(
        string='Used Today', compute='_compute_daily_used',
    )

    note = fields.Char(string='Internal Note')

    _sql_constraints = [
        ('emp_cap_uniq', 'unique(employee_id, capability_id)',
         'One policy per (employee, capability) only.'),
    ]

    @api.depends('employee_id', 'capability_id')
    def _compute_daily_used(self):
        """Count actions executed today for this (employee, capability) pair."""
        Action = self.env['ov.virtual.employee.action']
        today_start = fields.Datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        for pol in self:
            pol.daily_used = Action.search_count([
                ('run_id.employee_id', '=', pol.employee_id.id),
                ('capability_id', '=', pol.capability_id.id),
                ('state', 'in', ('executed', 'confirmed')),
                ('create_date', '>=', today_start),
            ])

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------
    def is_within_quota(self):
        self.ensure_one()
        if not self.daily_quota:
            return True
        return self.daily_used < self.daily_quota
