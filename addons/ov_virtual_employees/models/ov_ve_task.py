# -*- coding: utf-8 -*-
import logging
from datetime import timedelta

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)


class OvVirtualEmployeeTask(models.Model):
    """A recurring or event-bound assignment.

    Tasks are the bridge between triggers (cron tick, Odoo automation rules,
    manual buttons) and runs. A task does NOT itself execute work — it
    spawns a ``ov.virtual.employee.run`` and lets the runtime loop handle
    the perceive→plan→act cycle.
    """

    _name = 'ov.virtual.employee.task'
    _description = 'OpenVoo VE · Task'
    _order = 'employee_id, sequence, id'

    name = fields.Char(string='Task', required=True, translate=True)
    employee_id = fields.Many2one(
        'ov.virtual.employee', string='Employee',
        required=True, ondelete='cascade',
    )
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)

    # ------------------------------------------------------------------
    # Trigger
    # ------------------------------------------------------------------
    trigger = fields.Selection([
        ('cron', 'Scheduled (cron)'),
        ('event', 'Event-driven (automation rule)'),
        ('manual', 'Manual only'),
    ], string='Trigger', default='cron', required=True)

    cadence_hours = fields.Integer(
        string='Cadence (hours)', default=24,
        help='For cron tasks. Minimum hours between runs.',
    )
    last_fired_at = fields.Datetime(string='Last Fired', readonly=True)

    event_model = fields.Char(
        string='Event Model',
        help='For event-driven tasks. The Odoo model whose changes fire this '
             "task. e.g. 'account.bank.statement'.",
    )

    # ------------------------------------------------------------------
    # Scope (what the task asks the planner to focus on)
    # ------------------------------------------------------------------
    objective = fields.Text(
        string='Objective', required=True,
        help='Plain-language description of what this task should accomplish. '
             'Injected into the planner prompt.',
    )
    capability_filter_ids = fields.Many2many(
        'ov.virtual.employee.capability',
        'ov_ve_task_cap_rel', 'task_id', 'capability_id',
        string='Allowed Capabilities',
        help='If set, the planner may only propose actions using these '
             'capabilities. Empty = all capabilities allowed for the employee.',
    )

    # ------------------------------------------------------------------
    # Trigger logic
    # ------------------------------------------------------------------
    def _is_due_now(self):
        self.ensure_one()
        if self.trigger != 'cron':
            return False
        if not self.last_fired_at:
            return True
        delta = fields.Datetime.now() - self.last_fired_at
        return delta >= timedelta(hours=max(1, self.cadence_hours))

    def _fire(self, trigger_kind='manual', event_context=None):
        """Spawn a run for this task and kick the runtime loop."""
        self.ensure_one()
        if not self.employee_id._guard_can_run(raise_on_failure=False):
            return False
        Run = self.env['ov.virtual.employee.run']
        run = Run.create({
            'employee_id': self.employee_id.id,
            'task_id': self.id,
            'trigger_kind': trigger_kind,
            'event_context_json': event_context or '',
        })
        self.last_fired_at = fields.Datetime.now()
        run.action_start()
        return run

    def action_fire_now(self):
        for task in self:
            task._fire(trigger_kind='manual')
