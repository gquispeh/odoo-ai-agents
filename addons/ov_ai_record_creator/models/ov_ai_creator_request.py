# -*- coding: utf-8 -*-
import json
import logging

from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class OvAiCreatorRequest(models.Model):
    """The user-facing record for "I want to create something via natural language".

    Lifecycle:
        draft → parsing → preview → confirmed → executed
                     ↘ failed (with reason)

    The preview state is the confirmation gate: NO writes happen until the
    user explicitly approves the previewed records. This is non-negotiable
    architecture per the design session.
    """
    _name = 'ov.ai.creator.request'
    _description = 'OpenVoo AI · Record Creation Request'
    _inherit = ['mail.thread']
    _order = 'create_date desc'

    name = fields.Char(string='Reference', default=lambda self: _('New'), copy=False)
    user_input = fields.Text(
        string='Natural Language Input', required=True,
        help='What the user wrote, e.g. "crea 5 empleados del área comercial con sueldo 2500 PEN".',
    )
    template_id = fields.Many2one(
        'ov.ai.creator.template', string='Template',
        help='If empty, the system tries to auto-detect from the input.',
    )
    target_model = fields.Char(
        string='Target Model', related='template_id.target_model', store=True,
    )
    state = fields.Selection([
        ('draft', 'Draft'),
        ('parsing', 'Parsing'),
        ('preview', 'Ready for Confirmation'),
        ('confirmed', 'Confirmed'),
        ('executed', 'Executed'),
        ('failed', 'Failed'),
    ], string='Status', default='draft', tracking=True, required=True)
    parsed_json = fields.Text(
        string='Parsed Intent (JSON)',
        help='LLM-parsed structured representation of the user input.',
    )
    confidence = fields.Float(string='Parser Confidence', digits=(3, 2))
    warnings = fields.Text(string='Warnings')
    execution_id = fields.Many2one(
        'ov.ai.creator.execution', string='Execution Log', readonly=True,
    )
    session_id = fields.Many2one(
        'ov.copilot.session', string='Source Copilot Session', ondelete='set null',
    )
    implementation_id = fields.Many2one(
        'ov.copilot.implementation', string='Implementation', ondelete='set null',
    )
    cost_usd = fields.Float(
        string='Parsing Cost (USD)', digits=(12, 6), readonly=True,
    )

    # ------------------------------------------------------------------
    # CRUD overrides
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'ov.ai.creator.request'
                ) or '/'
        return super().create(vals_list)

    # ------------------------------------------------------------------
    # Public entry — called from sessions and direct UI
    # ------------------------------------------------------------------
    @api.model
    def create_from_natural_language(self, text, implementation=None,
                                     session=None, template=None):
        """High-level entry point used by the copilot session.

        Creates a request, parses the input, transitions to preview.
        Returns the request so the caller can open its preview action.
        """
        vals = {'user_input': text, 'state': 'draft'}
        if implementation:
            vals['implementation_id'] = implementation.id
        if session:
            vals['session_id'] = session.id
        if template:
            vals['template_id'] = template.id
        request = self.create(vals)
        request.action_parse()
        return request

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------
    def action_parse(self):
        """Run the LLM parser and transition to preview.

        Uses the cheap tier by default. If the parser returns low confidence
        OR warnings about missing required fields, the user is shown the
        preview anyway and asked to fill in the gaps.
        """
        for rec in self:
            rec.state = 'parsing'
            template = rec._auto_detect_template_if_needed()
            if not template:
                rec.state = 'failed'
                rec.warnings = _(
                    "Could not detect a template for the input. "
                    "Pick one manually or extend the template registry."
                )
                continue
            try:
                rec._invoke_parser(template, tier='cheap')
            except Exception as exc:  # noqa: BLE001
                rec.state = 'failed'
                rec.warnings = str(exc)
                _logger.exception("Parsing failed: %s", exc)
                continue
            rec.state = 'preview'

    def _auto_detect_template_if_needed(self):
        """If no template is set, try to guess one from keywords in the input.

        This avoids paying for an LLM call just to classify intent when
        the user already wrote "empleados" or "productos" explicitly.
        """
        self.ensure_one()
        if self.template_id:
            return self.template_id
        text = (self.user_input or '').lower()
        Template = self.env['ov.ai.creator.template']
        # Cheap keyword routing
        rules = [
            (('empleado', 'employee', 'trabajador', 'colaborador'), 'hr_employee'),
            (('cliente', 'proveedor', 'partner', 'customer', 'vendor'), 'res_partner'),
            (('producto', 'product', 'artículo', 'sku'), 'product_product'),
        ]
        for keywords, code in rules:
            if any(k in text for k in keywords):
                template = Template.search([('code', '=', code)], limit=1)
                if template:
                    self.template_id = template
                    return template
        return False

    def _invoke_parser(self, template, tier='cheap'):
        """Run the LLM parser using the template's schema.

        Stores the parsed JSON, confidence, warnings, and cost.
        """
        self.ensure_one()
        Router = self.env['ov.ai.llm.router']
        system_prompt = template.build_llm_parser_prompt()
        result = Router.complete(
            system=system_prompt,
            user_message=self.user_input,
            tier=tier,
            require_json=True,
            max_tokens=1024,
            audit_metadata={
                'request_id': self.id,
                'template_code': template.code,
            },
        )
        try:
            data = json.loads(result['text'])
        except json.JSONDecodeError:
            raise UserError(_(
                "Parser returned non-JSON output. Aborting. Raw: %s"
            ) % (result['text'][:200]))
        records = data.get('records') or []
        confidence = float(data.get('confidence') or 0.0)
        warnings = data.get('warnings') or []
        self.write({
            'parsed_json': json.dumps(records, indent=2, ensure_ascii=False),
            'confidence': confidence,
            'warnings': "\n".join(warnings) if warnings else False,
            'cost_usd': result['cost_usd'],
        })

    # ------------------------------------------------------------------
    # The confirmation gate
    # ------------------------------------------------------------------
    def action_confirm_and_execute(self):
        """User has reviewed the preview. Materialize the records.

        This is the only path that writes to the target model. Everything
        before is read/preview only.
        """
        self.ensure_one()
        if self.state != 'preview':
            raise UserError(_("Request is not in preview state."))
        try:
            parsed = json.loads(self.parsed_json or '[]')
        except json.JSONDecodeError as exc:
            raise UserError(_("Cannot parse preview JSON: %s") % exc)
        if not parsed:
            raise UserError(_("No records to create."))

        Execution = self.env['ov.ai.creator.execution']
        try:
            created = self.template_id.materialize(parsed)
            execution = Execution.create({
                'request_id': self.id,
                'template_id': self.template_id.id,
                'record_count': len(created),
                'record_ids_json': json.dumps(created.ids),
                'success': True,
            })
            self.write({
                'state': 'executed',
                'execution_id': execution.id,
            })
            self.message_post(body=_(
                "Created %(count)s record(s) on model %(model)s."
            ) % {'count': len(created), 'model': self.target_model})
            return self._action_open_created_records(created)
        except Exception as exc:  # noqa: BLE001
            execution = Execution.create({
                'request_id': self.id,
                'template_id': self.template_id.id,
                'record_count': 0,
                'success': False,
                'error': str(exc),
            })
            self.write({'state': 'failed', 'execution_id': execution.id})
            raise UserError(_("Materialization failed: %s") % exc) from exc

    def action_reject(self):
        """User rejected the preview. Mark as failed with reason 'rejected'."""
        for rec in self:
            rec.write({'state': 'failed', 'warnings': _("Rejected by user.")})
            rec.message_post(body=_("Preview rejected by user."))

    def action_edit_parsed(self):
        """Open the parsed JSON for manual editing before confirmation."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'ov.ai.creator.request',
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def _get_preview_action(self):
        """Open the preview view, used by the copilot session."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Confirm Record Creation'),
            'res_model': 'ov.ai.creator.request',
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def _action_open_created_records(self, created):
        """After execution, open the freshly created records for inspection."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Created Records'),
            'res_model': self.target_model,
            'view_mode': 'list,form',
            'domain': [('id', 'in', created.ids)],
            'target': 'current',
        }
