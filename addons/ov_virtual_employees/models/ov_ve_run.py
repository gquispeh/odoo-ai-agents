# -*- coding: utf-8 -*-
import json
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class OvVirtualEmployeeRun(models.Model):
    """One perceive → plan → act cycle.

    A Run is the unit of work the operator inspects. It holds the LLM-produced
    plan (an editable JSON checklist), the cost incurred, and the resulting
    actions. The Run state machine:

      queued → planning → awaiting_approval → executing → completed
                                            ↘ rejected
                                            ↘ failed
    """

    _name = 'ov.virtual.employee.run'
    _description = 'OpenVoo VE · Run'
    _inherit = ['mail.thread']
    _order = 'create_date desc'

    name = fields.Char(string='Reference', default=lambda self: _('New'), copy=False)
    employee_id = fields.Many2one(
        'ov.virtual.employee', string='Employee',
        required=True, ondelete='cascade', tracking=True,
    )
    task_id = fields.Many2one(
        'ov.virtual.employee.task', string='Task', ondelete='set null',
    )

    trigger_kind = fields.Selection([
        ('cron', 'Scheduled'),
        ('event', 'Event'),
        ('manual', 'Manual'),
    ], string='Trigger', default='manual', required=True)
    event_context_json = fields.Text(string='Event Context (JSON)')

    state = fields.Selection([
        ('queued', 'Queued'),
        ('planning', 'Planning'),
        ('awaiting_approval', 'Awaiting Approval'),
        ('executing', 'Executing'),
        ('completed', 'Completed'),
        ('rejected', 'Rejected'),
        ('failed', 'Failed'),
    ], string='State', default='queued', required=True, tracking=True)

    # Perception
    perceive_summary = fields.Text(
        string='Perception Summary',
        help='What the employee read from Odoo before planning.',
    )

    # Plan
    plan_json = fields.Text(
        string='Plan (JSON)',
        help='LLM-produced plan as an editable JSON checklist. The operator '
             'may edit before approval if any action requires confirmation.',
    )
    requires_human_approval = fields.Boolean(
        string='Needs Human Approval', readonly=True,
    )

    # Reflection
    reflection = fields.Text(string='Reflection', readonly=True)

    # Cost
    total_cost_usd = fields.Float(
        string='Total Cost (USD)', compute='_compute_cost',
        store=True, digits=(8, 4),
    )

    # Relations
    action_ids = fields.One2many(
        'ov.virtual.employee.action', 'run_id', string='Actions',
    )
    audit_ids = fields.Many2many(
        'ov.ai.creator.audit', 'ov_ve_run_audit_rel',
        'run_id', 'audit_id', string='LLM Calls',
    )

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'ov.virtual.employee.run'
                ) or '/'
        return super().create(vals_list)

    @api.depends('audit_ids.cost_usd')
    def _compute_cost(self):
        for run in self:
            run.total_cost_usd = sum(run.audit_ids.mapped('cost_usd'))

    # ------------------------------------------------------------------
    # Loop entry — perceive then plan
    # ------------------------------------------------------------------
    def action_start(self):
        """Begin the cycle. Transitions queued → planning → awaiting_approval (or executing)."""
        for run in self:
            if run.state != 'queued':
                raise UserError(_("Run %s is not queued.") % run.name)
            run.state = 'planning'
            run.employee_id.write({'state': 'busy'})
            try:
                run._perceive()
                run._plan()
                run._materialize_actions()
            except Exception as e:  # noqa: BLE001
                _logger.exception("Run %s failed during planning", run.name)
                run.write({
                    'state': 'failed',
                    'reflection': _("Planning error: %s") % (e,),
                })
                run.employee_id.write({'state': 'active'})
                continue
            # Decide whether to wait for approval or execute now
            if run._has_actions_needing_confirmation():
                run.state = 'awaiting_approval'
                run.requires_human_approval = True
                run.message_post(body=_(
                    'Plan ready. %d action(s) require approval.'
                ) % len(run.action_ids))
                run.employee_id.write({'state': 'active'})
            else:
                run.state = 'executing'
                run.requires_human_approval = False
                run._execute_all()

    def _perceive(self):
        """Build a short context string from Odoo state. Called before planning.

        Most of the actual perception is delegated to the capability handlers
        themselves when they run — this stage just collects high-level
        snapshot info so the planner can prioritize.
        """
        self.ensure_one()
        Move = self.env.get('account.move')
        snapshot = []
        if Move is not None:
            overdue = Move.search_count([
                ('state', '=', 'posted'),
                ('move_type', '=', 'out_invoice'),
                ('payment_state', 'in', ('not_paid', 'partial')),
                ('invoice_date_due', '<', fields.Date.today()),
            ])
            due_bills = Move.search_count([
                ('state', '=', 'posted'),
                ('move_type', '=', 'in_invoice'),
                ('payment_state', 'in', ('not_paid', 'partial')),
            ])
            snapshot.append("Overdue customer invoices: %d" % overdue)
            snapshot.append("Unpaid vendor bills: %d" % due_bills)
        Statement = self.env.get('account.bank.statement')
        if Statement is not None:
            unrec = Statement.search_count([('state', '=', 'open')])
            snapshot.append("Open bank statements: %d" % unrec)
        if self.event_context_json:
            snapshot.append("Event context: %s" % self.event_context_json)
        self.perceive_summary = "\n".join(snapshot) or _("(no signals)")

    def _plan(self):
        """Ask the planner LLM for a JSON checklist of actions."""
        self.ensure_one()
        emp = self.employee_id
        Router = self.env['ov.ai.llm.router']

        system = emp._build_planner_system_prompt()
        objective = (self.task_id.objective if self.task_id else
                     _('Sweep the inbox and propose what to do next.'))

        user_message = (
            f"Objetivo del ciclo:\n{objective}\n\n"
            f"Lo que percibes ahora:\n{self.perceive_summary}\n\n"
            "Devuelve el JSON de plan."
        )

        result = Router.complete(
            system=system,
            user_message=user_message,
            tier='cheap',
            require_json=True,
            max_tokens=800,
            audit_metadata={
                'virtual_employee_id': emp.id,
                'run_id': self.id,
                'stage': 'plan',
            },
        )
        # Attach the audit row if returned
        if result.get('audit_id'):
            self.audit_ids = [(4, result['audit_id'])]

        raw = result.get('content', '').strip()
        try:
            parsed = json.loads(raw or '{"actions": []}')
            actions = parsed.get('actions', []) if isinstance(parsed, dict) else []
        except json.JSONDecodeError:
            actions = []
            self.message_post(body=_(
                'Planner returned non-JSON; treating as empty plan.'
            ))
        self.plan_json = json.dumps(
            {'actions': actions}, indent=2, ensure_ascii=False,
        )

    def _materialize_actions(self):
        """Create ov.virtual.employee.action records from the planned JSON."""
        self.ensure_one()
        try:
            plan = json.loads(self.plan_json or '{"actions": []}')
        except json.JSONDecodeError:
            plan = {'actions': []}
        Action = self.env['ov.virtual.employee.action']
        Capability = self.env['ov.virtual.employee.capability']
        Policy = self.env['ov.virtual.employee.policy']

        emp = self.employee_id
        allowed_caps = (self.task_id.capability_filter_ids
                        or emp.policy_ids.mapped('capability_id'))

        seq = 0
        for proposed in plan.get('actions', []):
            seq += 10
            code = (proposed.get('capability_code') or '').strip()
            cap = Capability.search([('code', '=', code)], limit=1) if code else False
            if not cap or cap not in allowed_caps:
                Action.create({
                    'run_id': self.id,
                    'sequence': seq,
                    'capability_id': cap.id if cap else False,
                    'proposed_args_json': json.dumps(proposed.get('args') or {}, ensure_ascii=False),
                    'justification': proposed.get('justification') or '',
                    'state': 'rejected',
                    'result_summary': _("Capability '%s' not in policy.") % code,
                })
                continue
            policy = Policy.search([
                ('employee_id', '=', emp.id),
                ('capability_id', '=', cap.id),
            ], limit=1)
            if not policy or policy.autonomy == 'off':
                Action.create({
                    'run_id': self.id,
                    'sequence': seq,
                    'capability_id': cap.id,
                    'proposed_args_json': json.dumps(proposed.get('args') or {}, ensure_ascii=False),
                    'justification': proposed.get('justification') or '',
                    'state': 'rejected',
                    'result_summary': _('Policy disabled.'),
                })
                continue
            if not policy.is_within_quota():
                Action.create({
                    'run_id': self.id,
                    'sequence': seq,
                    'capability_id': cap.id,
                    'proposed_args_json': json.dumps(proposed.get('args') or {}, ensure_ascii=False),
                    'justification': proposed.get('justification') or '',
                    'state': 'rejected',
                    'result_summary': _('Daily quota exhausted for this capability.'),
                })
                continue

            if policy.autonomy == 'readonly':
                act_state = 'suggested'
            elif policy.autonomy == 'auto':
                act_state = 'planned'
            else:  # confirm
                act_state = 'awaiting_confirmation'

            Action.create({
                'run_id': self.id,
                'sequence': seq,
                'capability_id': cap.id,
                'proposed_args_json': json.dumps(proposed.get('args') or {}, ensure_ascii=False),
                'justification': proposed.get('justification') or '',
                'state': act_state,
            })

    def _has_actions_needing_confirmation(self):
        self.ensure_one()
        return any(a.state == 'awaiting_confirmation' for a in self.action_ids)

    # ------------------------------------------------------------------
    # Approval gates
    # ------------------------------------------------------------------
    def action_approve_and_execute(self):
        """Operator approves the plan. Move all awaiting_confirmation → planned, then execute."""
        for run in self:
            if run.state != 'awaiting_approval':
                raise UserError(_("Run %s is not awaiting approval.") % run.name)
            for act in run.action_ids.filtered(lambda a: a.state == 'awaiting_confirmation'):
                act.state = 'planned'
            run.state = 'executing'
            run.employee_id.write({'state': 'busy'})
            run._execute_all()

    def action_reject(self):
        for run in self:
            for act in run.action_ids.filtered(
                lambda a: a.state in ('awaiting_confirmation', 'planned', 'suggested')
            ):
                act.state = 'rejected'
                act.result_summary = _('Rejected by operator.')
            run.write({'state': 'rejected'})
            run.employee_id.write({'state': 'active'})
            run.message_post(body=_('Plan rejected by operator.'))

    # ------------------------------------------------------------------
    # Execute all planned actions and reflect
    # ------------------------------------------------------------------
    def _execute_all(self):
        self.ensure_one()
        results = []
        for act in self.action_ids.filtered(lambda a: a.state == 'planned').sorted('sequence'):
            res = act.execute()
            results.append(
                f"[{'OK' if res.get('success') else 'FAIL'}] "
                f"{act.capability_id.code}: {res.get('summary', '')}"
            )
        # Suggestions are also reflected (read-only proposals)
        for act in self.action_ids.filtered(lambda a: a.state == 'suggested'):
            results.append(f"[SUGGEST] {act.capability_id.code}: {act.justification}")

        ok_count = sum(1 for a in self.action_ids if a.state == 'executed')
        fail_count = sum(1 for a in self.action_ids if a.state == 'failed')
        suggest_count = sum(1 for a in self.action_ids if a.state == 'suggested')
        reject_count = sum(1 for a in self.action_ids if a.state == 'rejected')

        summary = _(
            "%(ok)d executed · %(fail)d failed · %(sug)d suggestions · %(rej)d rejected"
        ) % {'ok': ok_count, 'fail': fail_count, 'sug': suggest_count, 'rej': reject_count}

        self.write({
            'state': 'completed' if fail_count == 0 else 'failed',
            'reflection': "\n".join(results) or _('Nothing to do.'),
        })
        self.employee_id.write({
            'state': 'active',
            'last_run_summary': summary,
        })
        self.employee_id.message_post(body=_(
            'Run %s finished — %s'
        ) % (self.name, summary))
