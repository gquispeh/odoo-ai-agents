# -*- coding: utf-8 -*-
from odoo import models, fields


class OvAiCreatorAudit(models.Model):
    """Audit row for every LLM call made by the record creator.

    Without this, the business cannot manage AI cost at scale. Every call
    writes one row: which model, how many tokens in/out, cost in USD,
    latency, and excerpts of the prompt and response for traceability.
    """
    _name = 'ov.ai.creator.audit'
    _description = 'OpenVoo AI · LLM Call Audit'
    _order = 'create_date desc'
    _rec_name = 'model_name'

    model_name = fields.Char(string='LLM Model', readonly=True, required=True)
    system_prompt_excerpt = fields.Text(string='System Prompt (first 500c)', readonly=True)
    user_message_excerpt = fields.Text(string='User Message (first 500c)', readonly=True)
    response_excerpt = fields.Text(string='Response (first 500c)', readonly=True)
    tokens_in = fields.Integer(string='Tokens In', readonly=True)
    tokens_out = fields.Integer(string='Tokens Out', readonly=True)
    cost_usd = fields.Float(string='Cost (USD)', digits=(12, 6), readonly=True)
    latency_ms = fields.Integer(string='Latency (ms)', readonly=True)
    error = fields.Char(string='Error', readonly=True)
    metadata_json = fields.Text(string='Metadata (JSON)', readonly=True)
