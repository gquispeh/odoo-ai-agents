# -*- coding: utf-8 -*-
from odoo import models, fields


class OvAiCreatorExecution(models.Model):
    """Log of a completed materialization.

    One row per materialization, regardless of how many records were created.
    Links back to the request, the template, and the audit rows of the LLM
    calls used. This is the table to read when answering "what did the AI
    create last week, and how much did it cost?".
    """
    _name = 'ov.ai.creator.execution'
    _description = 'OpenVoo AI · Materialization Execution Log'
    _order = 'create_date desc'

    request_id = fields.Many2one(
        'ov.ai.creator.request', string='Source Request', ondelete='set null',
    )
    template_id = fields.Many2one(
        'ov.ai.creator.template', string='Template Used', required=True,
    )
    target_model = fields.Char(
        string='Target Model', related='template_id.target_model', store=True,
    )
    record_count = fields.Integer(string='Records Created')
    record_ids_json = fields.Text(
        string='Record IDs (JSON)',
        help='JSON list of IDs created in the target model.',
    )
    audit_ids = fields.Many2many(
        'ov.ai.creator.audit', string='LLM Calls Audit',
    )
    total_cost_usd = fields.Float(
        string='Total Cost (USD)', digits=(12, 6),
        compute='_compute_total_cost', store=True,
    )
    user_id = fields.Many2one(
        'res.users', string='Triggered By',
        default=lambda self: self.env.user,
    )
    success = fields.Boolean(string='Success', default=True)
    error = fields.Text(string='Error')

    def _compute_total_cost(self):
        for rec in self:
            rec.total_cost_usd = sum(rec.audit_ids.mapped('cost_usd'))
