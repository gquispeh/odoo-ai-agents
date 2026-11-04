# -*- coding: utf-8 -*-
import json
import logging
from datetime import datetime, time

import pytz

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


TRUST_AUTONOMY_MATRIX = {
    # trust_level -> default autonomy applied to whitelisted capabilities
    'shadow':     'readonly',
    'assisted':   'confirm',
    'autonomous': 'auto',
    'trusted':    'auto',
}


class OvVirtualEmployee(models.Model):
    """The hire. A virtual worker that perceives Odoo state, plans actions,
    and executes them under scoped policy.
    """

    _name = 'ov.virtual.employee'
    _description = 'OpenVoo · Virtual Employee'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'create_date desc'

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------
    name = fields.Char(string='Name', required=True, tracking=True,
                       help='How the operator refers to this hire. e.g. "Lucía Contable".')
    role_id = fields.Many2one(
        'ov.virtual.employee.role', string='Role',
        required=True, tracking=True,
        help='The template this employee was hired into.',
    )
    role_icon = fields.Char(related='role_id.icon', readonly=True)
    hr_employee_id = fields.Many2one(
        'hr.employee', string='HR Record', ondelete='set null',
        help='Optional link to hr.employee so this hire shows up in the org chart.',
    )
    company_id = fields.Many2one(
        'res.company', string='Company',
        default=lambda self: self.env.company, required=True,
    )
    avatar = fields.Char(
        string='Avatar', default='🤖',
        help='Emoji or short string shown on the employee card.',
    )

    # ------------------------------------------------------------------
    # Trust dial and budgets
    # ------------------------------------------------------------------
    trust_level = fields.Selection([
        ('shadow', 'Shadow — suggest only'),
        ('assisted', 'Assisted — propose, require confirmation'),
        ('autonomous', 'Autonomous — auto for whitelisted, confirm for the rest'),
        ('trusted', 'Trusted — auto for all whitelisted'),
    ], string='Trust Level', default='shadow', required=True, tracking=True)

    max_daily_cost_usd = fields.Float(
        string='Daily Budget (USD)', default=1.00, digits=(8, 4),
        help='Maximum USD spent on LLM calls per UTC day. When hit, the '
             'employee auto-pauses until next day.',
    )
    current_daily_cost_usd = fields.Float(
        string="Today's Cost (USD)", compute='_compute_today_cost',
        digits=(8, 4),
    )
    max_daily_actions = fields.Integer(
        string='Daily Action Cap', default=200,
        help='Maximum actions (record writes) per UTC day. Soft guard.',
    )

    # ------------------------------------------------------------------
    # Schedule
    # ------------------------------------------------------------------
    timezone = fields.Selection(
        '_tz_get', string='Timezone',
        default=lambda self: self.env.user.tz or 'America/Lima',
        required=True,
    )
    work_hour_start = fields.Float(
        string='Working Hours Start', default=8.0,
        help='Local time (decimal hours) when this employee may start working.',
    )
    work_hour_end = fields.Float(
        string='Working Hours End', default=18.0,
        help='Local time (decimal hours) after which scheduled triggers queue '
             'instead of executing.',
    )

    # ------------------------------------------------------------------
    # Lifecycle state
    # ------------------------------------------------------------------
    state = fields.Selection([
        ('draft', 'Draft'),
        ('active', 'Active'),
        ('busy', 'Busy'),
        ('paused', 'Paused'),
        ('over_budget', 'Over Budget'),
        ('archived', 'Archived'),
    ], string='State', default='draft', required=True, tracking=True)

    pause_reason = fields.Char(string='Pause Reason', readonly=True)

    # ------------------------------------------------------------------
    # Relations
    # ------------------------------------------------------------------
    policy_ids = fields.One2many(
        'ov.virtual.employee.policy', 'employee_id', string='Capability Policies',
    )
    task_ids = fields.One2many(
        'ov.virtual.employee.task', 'employee_id', string='Assigned Tasks',
    )
    run_ids = fields.One2many(
        'ov.virtual.employee.run', 'employee_id', string='Runs',
    )
    session_ids = fields.One2many(
        'ov.virtual.employee.session', 'employee_id', string='Supervision Sessions',
    )

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------
    run_count = fields.Integer(string='Runs', compute='_compute_counts')
    pending_action_count = fields.Integer(
        string='Awaiting Confirmation', compute='_compute_counts',
    )
    last_run_summary = fields.Char(string='Last Run Summary', readonly=True)

    # ------------------------------------------------------------------
    # Helpers / computed
    # ------------------------------------------------------------------
    @api.model
    def _tz_get(self):
        return [(tz, tz) for tz in pytz.common_timezones]

    @api.depends('run_ids', 'run_ids.action_ids', 'run_ids.action_ids.state')
    def _compute_counts(self):
        for emp in self:
            emp.run_count = len(emp.run_ids)
            emp.pending_action_count = self.env['ov.virtual.employee.action'].search_count([
                ('run_id.employee_id', '=', emp.id),
                ('state', '=', 'awaiting_confirmation'),
            ])

    def _compute_today_cost(self):
        Audit = self.env['ov.ai.creator.audit']
        today_start = fields.Datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        for emp in self:
            audits = Audit.search([
                ('metadata_json', 'ilike', '"virtual_employee_id": %d' % emp.id),
                ('create_date', '>=', today_start),
            ])
            emp.current_daily_cost_usd = sum(a.cost_usd for a in audits)

    # ------------------------------------------------------------------
    # CRUD — seed policies when hired
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        employees = super().create(vals_list)
        for emp in employees:
            emp._seed_default_policies()
        return employees

    def _seed_default_policies(self):
        """Create a policy row for every capability supported by the role."""
        self.ensure_one()
        Policy = self.env['ov.virtual.employee.policy']
        for cap in self.role_id.default_capability_ids:
            if Policy.search_count([
                ('employee_id', '=', self.id),
                ('capability_id', '=', cap.id),
            ]):
                continue
            # Default autonomy based on trust level and capability risk
            base_autonomy = TRUST_AUTONOMY_MATRIX.get(self.trust_level, 'readonly')
            if cap.requires_confirmation_by_default and base_autonomy == 'auto':
                base_autonomy = 'confirm'
            if cap.risk_level == 'high' and base_autonomy == 'auto':
                base_autonomy = 'confirm'
            Policy.create({
                'employee_id': self.id,
                'capability_id': cap.id,
                'autonomy': base_autonomy,
            })

    # ------------------------------------------------------------------
    # Lifecycle actions
    # ------------------------------------------------------------------
    def action_activate(self):
        for emp in self:
            if not emp.policy_ids:
                emp._seed_default_policies()
            emp.write({'state': 'active', 'pause_reason': False})
            emp.message_post(body=_('Employee activated.'))

    def action_pause(self, reason=None):
        for emp in self:
            emp.write({
                'state': 'paused',
                'pause_reason': reason or _('Paused by operator.'),
            })
            emp.message_post(body=_('Employee paused: %s') % emp.pause_reason)

    def action_resume(self):
        for emp in self:
            emp.write({'state': 'active', 'pause_reason': False})
            emp.message_post(body=_('Employee resumed.'))

    def action_archive_employee(self):
        for emp in self:
            emp.write({'state': 'archived'})

    def action_open_runs(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('%s · Runs') % self.name,
            'res_model': 'ov.virtual.employee.run',
            'view_mode': 'list,form',
            'domain': [('employee_id', '=', self.id)],
            'context': {'default_employee_id': self.id},
        }

    def action_open_chat(self):
        """Open or create a supervision session to talk to this employee."""
        self.ensure_one()
        Session = self.env['ov.virtual.employee.session']
        session = Session.search([
            ('employee_id', '=', self.id),
            ('user_id', '=', self.env.user.id),
        ], limit=1, order='create_date desc')
        if not session:
            session = Session.create({
                'employee_id': self.id,
                'user_id': self.env.user.id,
                'name': _('%s · supervision') % self.name,
            })
        return {
            'type': 'ir.actions.act_window',
            'name': _('Supervise %s') % self.name,
            'res_model': 'ov.virtual.employee.session',
            'res_id': session.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_run_now(self):
        """Manual trigger — run every active task for this employee."""
        for emp in self:
            emp._guard_can_run(raise_on_failure=True)
            for task in emp.task_ids.filtered(lambda t: t.active and t.trigger != 'event'):
                task._fire(trigger_kind='manual')

    # ------------------------------------------------------------------
    # Run-time guards
    # ------------------------------------------------------------------
    def _guard_can_run(self, raise_on_failure=False):
        """Return True if this employee is permitted to start a new run now."""
        self.ensure_one()
        if self.state in ('paused', 'archived', 'draft'):
            msg = _("Employee is %s and cannot run.") % self.state
            if raise_on_failure:
                raise UserError(msg)
            return False
        # Budget
        if self.current_daily_cost_usd >= self.max_daily_cost_usd:
            self.write({
                'state': 'over_budget',
                'pause_reason': _('Daily budget of $%.4f exceeded.') % self.max_daily_cost_usd,
            })
            self.message_post(body=self.pause_reason)
            if raise_on_failure:
                raise UserError(self.pause_reason)
            return False
        # Working hours
        if not self._within_working_hours():
            msg = _("Outside working hours for timezone %s.") % self.timezone
            if raise_on_failure:
                raise UserError(msg)
            return False
        return True

    def _within_working_hours(self):
        self.ensure_one()
        tz = pytz.timezone(self.timezone or 'UTC')
        now_local = fields.Datetime.now().replace(tzinfo=pytz.UTC).astimezone(tz)
        hour = now_local.hour + now_local.minute / 60.0
        return self.work_hour_start <= hour < self.work_hour_end

    # ------------------------------------------------------------------
    # System prompt composition
    # ------------------------------------------------------------------
    def _build_planner_system_prompt(self):
        """Compose the system prompt used by the planner LLM."""
        self.ensure_one()
        role_fragment = self.role_id.system_prompt_fragment or ''
        cap_lines = []
        for pol in self.policy_ids.filtered(lambda p: p.autonomy != 'off'):
            cap_lines.append(
                f"- {pol.capability_id.code} ({pol.autonomy}): {pol.capability_id.name}"
            )
        caps_block = "\n".join(cap_lines) or "(no capabilities)"
        return (
            "Eres un empleado virtual de OpenVoo embebido en Odoo. "
            "Tu mandato:\n"
            f"{role_fragment}\n\n"
            "Capacidades disponibles y su nivel de autonomía:\n"
            f"{caps_block}\n\n"
            "Cuando se te pida planificar un ciclo de trabajo, devuelve "
            "estrictamente un JSON con la forma:\n"
            "{\n"
            '  "actions": [\n'
            '    {"capability_code": "<code>", "args": {...}, "justification": "<frase corta>"}\n'
            "  ]\n"
            "}\n"
            "No incluyas texto fuera del JSON. Si no hay nada que hacer, "
            'devuelve {"actions": []}.'
        )

    # ------------------------------------------------------------------
    # Cron entry-point
    # ------------------------------------------------------------------
    @api.model
    def _cron_tick(self):
        """Called by the scheduler. Fires any cron-triggered task whose
        cadence matches and whose employee is allowed to run.
        """
        employees = self.search([('state', '=', 'active')])
        for emp in employees:
            if not emp._guard_can_run(raise_on_failure=False):
                continue
            for task in emp.task_ids.filtered(
                lambda t: t.active and t.trigger == 'cron' and t._is_due_now()
            ):
                try:
                    task._fire(trigger_kind='cron')
                except Exception:  # noqa: BLE001
                    _logger.exception("Task %s failed on cron tick", task.id)
