# -*- coding: utf-8 -*-
"""Cost-aware LLM router.

Encapsulates model selection so the rest of the module does not hardcode
specific models. The router routes parsing tasks to cheap models and
ambiguity-resolution tasks to expensive ones, and writes an audit row
for every call.

Implementation note: the actual HTTP call is delegated to ``sdr_chatbot``'s
connector if it provides one (``sdr.chatbot.connector.complete``). If not,
we fall back to a direct Anthropic API call. This keeps the dependency
graph clean while remaining functional out of the box.
"""
import json
import logging
import time

from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


# Approximate USD price per million tokens (May 2026 list prices).
# These are reference values, not authoritative — overridable in settings.
MODEL_REGISTRY = {
    'cheap': {
        'name': 'claude-haiku-4-5-20251001',
        'in_per_mtok': 1.0,
        'out_per_mtok': 5.0,
    },
    'balanced': {
        'name': 'claude-sonnet-4-6',
        'in_per_mtok': 3.0,
        'out_per_mtok': 15.0,
    },
    'expensive': {
        'name': 'claude-opus-4-7',
        'in_per_mtok': 15.0,
        'out_per_mtok': 75.0,
    },
}


class LlmRouter(models.AbstractModel):
    """Stateless LLM router. Use ``self.env['ov.ai.llm.router'].complete(...)``."""
    _name = 'ov.ai.llm.router'
    _description = 'OpenVoo AI · Cost-Aware LLM Router'

    @api.model
    def complete(self, system, user_message, tier='cheap', context=None,
                 max_tokens=1024, require_json=False, audit_metadata=None):
        """Run a completion through the appropriate model.

        :param system: System prompt string.
        :param user_message: User message string (or list of messages).
        :param tier: One of ``cheap``, ``balanced``, ``expensive``.
        :param context: Optional dict carried into the audit row.
        :param max_tokens: Token cap for the response.
        :param require_json: If True, the prompt is wrapped to force JSON output.
        :param audit_metadata: Extra fields to store on the audit row.
        :returns: dict with ``text``, ``raw``, ``tokens_in``, ``tokens_out``,
                  ``cost_usd``, ``latency_ms``, ``model``, ``audit_id``.
        """
        if tier not in MODEL_REGISTRY:
            raise UserError(_("Unknown LLM tier: %s") % tier)
        model_cfg = MODEL_REGISTRY[tier]

        if require_json:
            system = (system or '') + (
                "\n\nReply with valid JSON only. No prose, no markdown fences. "
                "If you cannot produce JSON, return an object with key 'error'."
            )

        start_ts = time.time()
        try:
            response = self._dispatch(model_cfg['name'], system, user_message, max_tokens)
        except Exception as exc:  # noqa: BLE001
            _logger.exception("LLM call failed: %s", exc)
            self._write_audit(model_cfg, system, user_message, '', 0, 0,
                              latency_ms=int((time.time() - start_ts) * 1000),
                              error=str(exc), audit_metadata=audit_metadata)
            raise UserError(_(
                "LLM call failed: %s. "
                "Falling back to manual form is recommended."
            ) % exc) from exc

        latency_ms = int((time.time() - start_ts) * 1000)
        text = response.get('text', '')
        tokens_in = response.get('tokens_in', 0)
        tokens_out = response.get('tokens_out', 0)
        cost_usd = (
            tokens_in / 1_000_000 * model_cfg['in_per_mtok']
            + tokens_out / 1_000_000 * model_cfg['out_per_mtok']
        )
        audit = self._write_audit(
            model_cfg, system, user_message, text,
            tokens_in, tokens_out, latency_ms,
            cost_usd=cost_usd, audit_metadata=audit_metadata,
        )
        return {
            'text': text,
            'raw': response,
            'tokens_in': tokens_in,
            'tokens_out': tokens_out,
            'cost_usd': cost_usd,
            'latency_ms': latency_ms,
            'model': model_cfg['name'],
            'audit_id': audit.id if audit else False,
        }

    # ------------------------------------------------------------------
    # Dispatch — prefer sdr_chatbot connector if it exists.
    # ------------------------------------------------------------------
    def _dispatch(self, model_name, system, user_message, max_tokens):
        """Choose how to actually reach the LLM.

        Order of preference:
        1. ``sdr.chatbot.connector`` if installed (centralized API key, retries).
        2. Direct HTTP via Anthropic SDK (fallback).
        """
        if 'sdr.chatbot.connector' in self.env:
            connector = self.env['sdr.chatbot.connector'].sudo()
            if hasattr(connector, 'complete'):
                return connector.complete(
                    model=model_name,
                    system=system,
                    messages=[{'role': 'user', 'content': user_message}],
                    max_tokens=max_tokens,
                )
        return self._direct_anthropic_call(model_name, system, user_message, max_tokens)

    def _direct_anthropic_call(self, model_name, system, user_message, max_tokens):
        """Fallback when sdr_chatbot connector is not available.

        Reads the API key from ``ir.config_parameter`` key ``ov_ai_creator.anthropic_api_key``.
        """
        api_key = self.env['ir.config_parameter'].sudo().get_param(
            'ov_ai_creator.anthropic_api_key'
        )
        if not api_key:
            raise UserError(_(
                "No Anthropic API key configured. "
                "Set 'ov_ai_creator.anthropic_api_key' in System Parameters."
            ))
        # We avoid hard-importing the anthropic SDK at module load to keep
        # the module installable even without the SDK present.
        try:
            import anthropic  # type: ignore
        except ImportError as exc:
            raise UserError(_(
                "anthropic Python package not installed. "
                "Run: pip install anthropic"
            )) from exc
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=model_name,
            max_tokens=max_tokens,
            system=system or "",
            messages=[{'role': 'user', 'content': user_message}],
        )
        text = ''
        for block in msg.content:
            if getattr(block, 'type', None) == 'text':
                text += block.text
        return {
            'text': text,
            'tokens_in': getattr(msg.usage, 'input_tokens', 0),
            'tokens_out': getattr(msg.usage, 'output_tokens', 0),
            'raw': msg.model_dump() if hasattr(msg, 'model_dump') else {},
        }

    # ------------------------------------------------------------------
    # Audit
    # ------------------------------------------------------------------
    def _write_audit(self, model_cfg, system, user_message, response,
                     tokens_in, tokens_out, latency_ms,
                     cost_usd=0.0, error=None, audit_metadata=None):
        try:
            return self.env['ov.ai.creator.audit'].sudo().create({
                'model_name': model_cfg['name'],
                'system_prompt_excerpt': (system or '')[:500],
                'user_message_excerpt': (user_message or '')[:500],
                'response_excerpt': (response or '')[:500],
                'tokens_in': tokens_in,
                'tokens_out': tokens_out,
                'cost_usd': cost_usd,
                'latency_ms': latency_ms,
                'error': error or False,
                'metadata_json': json.dumps(audit_metadata or {}),
            })
        except Exception as exc:  # noqa: BLE001
            # Never let audit failures break the actual response
            _logger.warning("Could not write audit row: %s", exc)
            return False
