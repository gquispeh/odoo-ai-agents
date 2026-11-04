# -*- coding: utf-8 -*-
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class OvVirtualEmployeeCapability(models.Model):
    """A discrete skill a virtual employee can execute.

    Capabilities are *data*, not subclasses. Third-party modules register a
    capability by inserting a record with ``handler_model`` and
    ``handler_method`` pointing at an Odoo method that implements the work.

    The handler method receives a single argument ``action`` (an
    ``ov.virtual.employee.action`` record), reads context from it, performs
    the work (either directly or by staging a record-creator request), and
    returns a dict with at least ``success`` and ``summary``.
    """

    _name = 'ov.virtual.employee.capability'
    _description = 'OpenVoo VE · Capability'
    _order = 'sequence, name'

    name = fields.Char(string='Capability', required=True, translate=True)
    code = fields.Char(string='Technical Code', required=True, copy=False)
    sequence = fields.Integer(default=10)
    description = fields.Text(string='Description')

    handler_model = fields.Char(
        string='Handler Model', required=True,
        help='The Odoo model whose method implements this capability '
             "(e.g. 'ov.virtual.employee.accountant').",
    )
    handler_method = fields.Char(
        string='Handler Method', required=True,
        help="Method name on the handler model. Must accept a single "
             "'action' argument and return a dict.",
    )

    risk_level = fields.Selection([
        ('low', 'Low — read-only or reversible'),
        ('medium', 'Medium — writes records but easily corrected'),
        ('high', 'High — moves money or sends external communications'),
    ], string='Risk Level', default='medium', required=True)

    requires_confirmation_by_default = fields.Boolean(
        string='Confirmation by Default', default=True,
        help='If True, a fresh employee policy defaults to CONFIRM for this '
             'capability regardless of trust level.',
    )

    supports_role_ids = fields.Many2many(
        'ov.virtual.employee.role',
        'ov_ve_capability_role_rel',
        'capability_id', 'role_id',
        string='Supported Roles',
        help='Roles this capability is intended for. Used to seed default '
             'policies when an employee is hired.',
    )

    creator_template_code = fields.Char(
        string='Creator Template Code',
        help='If this capability writes records, the technical code of the '
             'ov.ai.creator.template used to route the write through the '
             'confirmation gate.',
    )

    active = fields.Boolean(default=True)

    _sql_constraints = [
        ('code_uniq', 'unique(code)',
         'Capability technical code must be unique.'),
    ]

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------
    def execute(self, action):
        """Dispatch this capability to its handler.

        Called by ``ov.virtual.employee.action.execute``. The handler is
        looked up by name; we tolerate missing handlers gracefully and
        return a failure result instead of crashing the whole run.
        """
        self.ensure_one()
        Model = self.env.get(self.handler_model)
        if Model is None:
            return {
                'success': False,
                'summary': _("Handler model '%s' is not installed.") % self.handler_model,
            }
        method = getattr(Model, self.handler_method, None)
        if method is None or not callable(method):
            return {
                'success': False,
                'summary': _("Handler method '%s.%s' not found.") % (
                    self.handler_model, self.handler_method,
                ),
            }
        try:
            result = method(action)
            if not isinstance(result, dict):
                return {
                    'success': False,
                    'summary': _("Handler returned non-dict result: %r") % (result,),
                }
            result.setdefault('success', True)
            result.setdefault('summary', _('Completed.'))
            return result
        except UserError as ue:
            return {'success': False, 'summary': str(ue)}
        except Exception as e:  # noqa: BLE001
            _logger.exception("Capability %s failed", self.code)
            return {
                'success': False,
                'summary': _("Capability raised: %s") % (e,),
            }
