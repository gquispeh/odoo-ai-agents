# -*- coding: utf-8 -*-
import json
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class OvVirtualEmployeeAction(models.Model):
    """One atomic step within a run.

    Possible states:
      planned             -> capability whitelisted; will execute on _execute_all
      awaiting_confirmation -> operator must approve before this becomes 'planned'
      suggested           -> read-only proposal; never executed
      executed            -> handler ran successfully
      failed              -> handler raised or returned success=False
      rejected            -> dropped (operator rejected the plan or policy denied)
      confirmed           -> executed via creator gate; record written
    """

    _name = 'ov.virtual.employee.action'
    _description = 'OpenVoo VE · Action'
    _order = 'run_id, sequence, id'

    run_id = fields.Many2one(
        'ov.virtual.employee.run', string='Run',
        required=True, ondelete='cascade',
    )
    sequence = fields.Integer(default=10)
    capability_id = fields.Many2one(
        'ov.virtual.employee.capability', string='Capability',
        ondelete='restrict',
    )
    capability_code = fields.Char(
        related='capability_id.code', readonly=True, store=False,
    )
    proposed_args_json = fields.Text(string='Proposed Args (JSON)')
    justification = fields.Text(string='Justification')

    state = fields.Selection([
        ('planned', 'Planned'),
        ('awaiting_confirmation', 'Awaiting Confirmation'),
        ('suggested', 'Suggested (read-only)'),
        ('confirmed', 'Confirmed'),
        ('executed', 'Executed'),
        ('failed', 'Failed'),
        ('rejected', 'Rejected'),
    ], string='State', default='planned', required=True)

    creator_request_id = fields.Many2one(
        'ov.ai.creator.request', string='Creator Request',
        ondelete='set null',
        help='If this action proposed records, the staged request that will '
             'apply the writes.',
    )
    result_summary = fields.Char(string='Result')
    result_payload_json = fields.Text(string='Result Payload (JSON)')

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def get_args(self):
        self.ensure_one()
        if not self.proposed_args_json:
            return {}
        try:
            return json.loads(self.proposed_args_json)
        except json.JSONDecodeError:
            return {}

    def set_result(self, result_dict):
        self.ensure_one()
        self.result_summary = (result_dict.get('summary') or '')[:255]
        try:
            self.result_payload_json = json.dumps(
                result_dict, indent=2, ensure_ascii=False, default=str,
            )
        except (TypeError, ValueError):
            self.result_payload_json = json.dumps(
                {'summary': self.result_summary}, ensure_ascii=False,
            )

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    def execute(self):
        """Dispatch this action through its capability handler.

        Returns the handler's result dict.
        """
        self.ensure_one()
        if self.state != 'planned':
            return {'success': False, 'summary': _("Action not in 'planned' state.")}
        if not self.capability_id:
            self.state = 'failed'
            return {'success': False, 'summary': _('No capability bound to action.')}
        result = self.capability_id.execute(self)
        self.set_result(result)
        if result.get('success'):
            # If a creator request was staged and auto-confirmed, mark confirmed
            if self.creator_request_id and self.creator_request_id.state == 'executed':
                self.state = 'confirmed'
            else:
                self.state = 'executed'
        else:
            self.state = 'failed'
        return result

    # ------------------------------------------------------------------
    # Helper: hand off to ov.ai.creator.request for writes
    # ------------------------------------------------------------------
    def delegate_to_creator(self, template_code, natural_language_summary,
                            auto_confirm=False):
        """Stage a record-creator request for this action.

        ``auto_confirm`` should only be passed True when the policy autonomy
        is AUTO for this capability. Even in that case, the creator request
        row remains for audit.
        """
        self.ensure_one()
        Template = self.env['ov.ai.creator.template'].search(
            [('code', '=', template_code)], limit=1,
        )
        if not Template:
            return {
                'success': False,
                'summary': _("Creator template '%s' not found.") % template_code,
            }
        request = self.env['ov.ai.creator.request'].create({
            'user_input': natural_language_summary,
            'template_id': Template.id,
        })
        self.creator_request_id = request.id

        request.action_parse()

        if auto_confirm and request.state == 'preview':
            request.action_confirm_and_execute()

        return {
            'success': request.state in ('preview', 'executed'),
            'summary': _("Creator request %s -> %s") % (request.name, request.state),
            'creator_request_id': request.id,
        }
