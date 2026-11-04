# -*- coding: utf-8 -*-
from odoo import _, api, fields, models


class OvVirtualEmployeeSession(models.Model):
    """Conversational supervision channel.

    The operator chats with a specific virtual employee. Each user message
    is interpreted as either a question (the employee summarizes status) or
    an imperative (the employee creates a manual task and fires a run).
    Inherits from sdr.chatbot.session so we reuse the full chat infra.
    """

    _name = 'ov.virtual.employee.session'
    _description = 'OpenVoo VE · Supervision Session'
    _inherit = 'sdr.ai.conversation'

    employee_id = fields.Many2one(
        'ov.virtual.employee', string='Employee',
        required=False, ondelete='cascade',
    )

    # ------------------------------------------------------------------
    # System prompt — composes role context onto the chat session
    # ------------------------------------------------------------------
    def _build_system_prompt(self):
        """Override to inject the bound employee's identity and capability set."""
        base = super()._build_system_prompt() if hasattr(
            super(), '_build_system_prompt'
        ) else ''
        if not self.employee_id:
            return base
        emp = self.employee_id
        emp_prompt = (
            f"\n\nEres {emp.name}, un empleado virtual con rol "
            f"'{emp.role_id.name}' dentro de Odoo. Tu nivel de confianza actual "
            f"es {emp.trust_level}. Cuando el operador te da una instrucción, "
            "puedes responder con información leída de Odoo, o crear una tarea "
            "manual y ejecutar un ciclo. No prometas acciones fuera del alcance "
            "de tus capacidades configuradas.\n"
            f"{emp.role_id.system_prompt_fragment or ''}"
        )
        return (base or '') + emp_prompt
