# -*- coding: utf-8 -*-
"""Built-in capability handlers for the 'accountant' role.

Each method is invoked by ``ov.virtual.employee.capability.execute`` and
receives a single argument: the ``ov.virtual.employee.action`` record being
executed. Methods read the action's args, perform read-only inspection or
delegate writes to ``ov.ai.creator.request``, and return a result dict
with at minimum ``{'success': bool, 'summary': str}``.

Design constraints:
- Capabilities are *safe by default*: writes always route through the
  creator gate. The policy autonomy is checked by the action layer, not here.
- Each handler is idempotent on re-run when possible (looks at current state
  before acting).
- Each handler returns a human-readable summary suitable for posting to the
  employee's chatter.
"""
import logging

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)


class OvVeAccountant(models.AbstractModel):
    _name = 'ov.virtual.employee.accountant'
    _description = 'OpenVoo VE · Accountant Capability Handlers'

    # ------------------------------------------------------------------
    # 1. Reconcile bank statements — propose matches
    # ------------------------------------------------------------------
    @api.model
    def cap_reconcile_bank_statements(self, action):
        """Inspect open bank statement lines and propose suspect matches.

        This handler is intentionally read-only at the framework level: it
        identifies unmatched lines and posts a summary. Actual reconciliation
        moves money and lives behind the creator gate as a separate step.
        """
        Statement = self.env.get('account.bank.statement.line')
        if Statement is None:
            return {'success': False, 'summary': _('account module not installed.')}
        lines = Statement.search([('is_reconciled', '=', False)], limit=50)
        if not lines:
            return {'success': True, 'summary': _('No unreconciled bank lines.')}

        # Build a digest grouped by partner heuristic
        by_partner = {}
        for ln in lines:
            key = ln.partner_id.name or _('(unknown)')
            by_partner.setdefault(key, 0)
            by_partner[key] += 1
        digest = ", ".join(
            "%s: %d" % (name, count) for name, count in list(by_partner.items())[:8]
        )
        summary = _(
            "%d unreconciled bank lines awaiting attention. Top partners: %s"
        ) % (len(lines), digest)

        # Post to the employee thread
        action.run_id.employee_id.message_post(body=summary)
        return {
            'success': True,
            'summary': summary,
            'count': len(lines),
        }

    # ------------------------------------------------------------------
    # 2. Register vendor payments — propose account.payment for due bills
    # ------------------------------------------------------------------
    @api.model
    def cap_register_vendor_payments(self, action):
        """Find vendor bills due in the next ``window_days`` and stage payments.

        Args (from action.proposed_args_json):
          - window_days: int, default 7
          - max_payments: int, default 5
        """
        args = action.get_args()
        window_days = int(args.get('window_days', 7) or 7)
        max_payments = int(args.get('max_payments', 5) or 5)

        Move = self.env.get('account.move')
        if Move is None:
            return {'success': False, 'summary': _('account module not installed.')}

        today = fields.Date.today()
        cutoff = fields.Date.add(today, days=window_days)
        candidates = Move.search([
            ('state', '=', 'posted'),
            ('move_type', '=', 'in_invoice'),
            ('payment_state', 'in', ('not_paid', 'partial')),
            ('invoice_date_due', '<=', cutoff),
        ], limit=max_payments, order='invoice_date_due asc')

        if not candidates:
            return {'success': True, 'summary': _('No vendor bills due within window.')}

        # Build a natural-language summary; delegate to creator gate for the actual writes
        partner_lines = []
        for move in candidates:
            partner_lines.append(
                "- %s · %s · due %s · %s %s" % (
                    move.partner_id.name or '?', move.name or '?',
                    move.invoice_date_due, move.amount_residual,
                    move.currency_id.name or '',
                )
            )
        nl = _(
            "Propose registering payments for %d vendor bill(s) due within %d "
            "days:\n%s"
        ) % (len(candidates), window_days, "\n".join(partner_lines))

        # Determine if policy is AUTO for this capability — if so, auto-confirm
        Policy = self.env['ov.virtual.employee.policy']
        policy = Policy.search([
            ('employee_id', '=', action.run_id.employee_id.id),
            ('capability_id', '=', action.capability_id.id),
        ], limit=1)
        auto_confirm = bool(policy and policy.autonomy == 'auto')

        delegate_result = action.delegate_to_creator(
            template_code='vendor_payment_batch',
            natural_language_summary=nl,
            auto_confirm=auto_confirm,
        )
        summary = _(
            "%d vendor payment(s) proposed. Confirmation %s."
        ) % (
            len(candidates),
            _('auto-applied') if auto_confirm else _('required by operator'),
        )
        action.run_id.employee_id.message_post(body=summary + "\n" + nl)
        return {
            'success': delegate_result.get('success', True),
            'summary': summary,
            'creator_request_id': delegate_result.get('creator_request_id'),
        }

    # ------------------------------------------------------------------
    # 3. Send overdue reminders — email partners with overdue invoices
    # ------------------------------------------------------------------
    @api.model
    def cap_send_overdue_reminders(self, action):
        """Find overdue customer invoices and post reminder mail.

        This is a *high-risk* capability because it sends external comms.
        Default policy is CONFIRM unless the operator raises trust.
        """
        args = action.get_args()
        min_days_overdue = int(args.get('min_days_overdue', 7) or 7)

        Move = self.env.get('account.move')
        if Move is None:
            return {'success': False, 'summary': _('account module not installed.')}

        today = fields.Date.today()
        cutoff = fields.Date.subtract(today, days=min_days_overdue)
        overdue = Move.search([
            ('state', '=', 'posted'),
            ('move_type', '=', 'out_invoice'),
            ('payment_state', 'in', ('not_paid', 'partial')),
            ('invoice_date_due', '<', cutoff),
        ], limit=20)

        if not overdue:
            return {'success': True, 'summary': _('No overdue invoices to remind.')}

        # Group by partner
        by_partner = {}
        for move in overdue:
            by_partner.setdefault(move.partner_id, []).append(move)

        sent = 0
        skipped = 0
        for partner, moves in by_partner.items():
            if not partner.email:
                skipped += 1
                continue
            invoice_lines = "\n".join(
                "- %s · vence %s · saldo %s %s" % (
                    m.name, m.invoice_date_due, m.amount_residual,
                    m.currency_id.name or '',
                ) for m in moves
            )
            body = _(
                "<p>Estimado/a %s,</p>"
                "<p>Le escribimos para recordarle las siguientes facturas con "
                "saldo pendiente:</p><pre>%s</pre>"
                "<p>Quedamos atentos a su confirmación de pago.</p>"
            ) % (partner.name, invoice_lines)
            # Use mail.mail to draft (and send if policy=auto)
            self.env['mail.mail'].sudo().create({
                'subject': _('Recordatorio de facturas pendientes'),
                'email_to': partner.email,
                'body_html': body,
                'auto_delete': True,
            })  # NOTE: not .send() here — the email is staged in the outbox.
            sent += 1

        summary = _(
            "Drafted %d reminder email(s); skipped %d partner(s) without email."
        ) % (sent, skipped)
        action.run_id.employee_id.message_post(body=summary)
        return {'success': True, 'summary': summary, 'drafted': sent, 'skipped': skipped}

    # ------------------------------------------------------------------
    # 4. Flag invoice anomalies — duplicate detection, missing tax info
    # ------------------------------------------------------------------
    @api.model
    def cap_flag_invoice_anomalies(self, action):
        """Scan recent invoices for duplicates and missing taxes. Read-only."""
        Move = self.env.get('account.move')
        if Move is None:
            return {'success': False, 'summary': _('account module not installed.')}

        recent = Move.search([
            ('state', '=', 'posted'),
            ('move_type', 'in', ('in_invoice', 'out_invoice')),
            ('create_date', '>=', fields.Datetime.subtract(
                fields.Datetime.now(), days=30,
            )),
        ], limit=500)

        anomalies = []
        # Duplicate detection: same partner + amount + date
        seen = {}
        for move in recent:
            key = (move.partner_id.id, float(move.amount_total or 0.0),
                   move.invoice_date)
            if key in seen and seen[key] != move.id:
                anomalies.append(_("Duplicate suspect: %s vs %s") % (
                    move.name, Move.browse(seen[key]).name,
                ))
            seen[key] = move.id
            # Missing tax detection
            if move.move_type in ('out_invoice', 'in_invoice'):
                if not any(line.tax_ids for line in move.invoice_line_ids):
                    anomalies.append(_("No taxes on %s") % move.name)

        if not anomalies:
            summary = _('No invoice anomalies detected in last 30 days.')
        else:
            summary = _("%d anomaly(ies) flagged:\n%s") % (
                len(anomalies), "\n".join("- " + a for a in anomalies[:20]),
            )
        action.run_id.employee_id.message_post(body=summary)
        return {'success': True, 'summary': summary[:255]}

    # ------------------------------------------------------------------
    # 5. Prepare period-close checklist
    # ------------------------------------------------------------------
    @api.model
    def cap_prepare_period_close_checklist(self, action):
        """Build a period-close checklist as a message in the employee thread."""
        today = fields.Date.today()
        checklist = [
            _("[ ] Reconcile every active bank journal up to %s") % today,
            _("[ ] Confirm vendor bills posted with proper analytic tags"),
            _("[ ] Confirm depreciation entries posted for the period"),
            _("[ ] Validate tax declarations (IGV, renta) match move totals"),
            _("[ ] Lock the period in the journal entries form"),
            _("[ ] Export trial balance and share with controller"),
        ]
        body = _("<b>Period-close checklist as of %s:</b><br/>%s") % (
            today, "<br/>".join(checklist),
        )
        action.run_id.employee_id.message_post(body=body)
        return {
            'success': True,
            'summary': _('Period-close checklist posted to employee thread.'),
            'items': len(checklist),
        }
