# -*- coding: utf-8 -*-
{
    'name': 'OpenVoo · AI Record Creator',
    'version': '19.0.1.0.0',
    'category': 'Services/OpenVoo',
    'summary': 'Natural language to Odoo records. Templates, preview, confirmation gate, audit trail.',
    'description': """
OpenVoo AI Record Creator
=========================

Generic engine to materialize Odoo records from natural language. The user
describes what they want in plain Spanish (or English); the engine parses
intent, builds a preview, and only writes after explicit confirmation.

Architectural principles:
- Declarative templates per model (hr.employee, res.partner, product.product...)
- Cost-aware LLM routing: cheap models for parsing, expensive only on ambiguity
- Mandatory confirmation gate before any write
- Full audit trail of every LLM call (tokens, cost, latency)
- Graceful fallback to forms when LLM is unavailable

Designed to be invoked from ``ov_implementation_copilot`` when the user
expresses an intent to create records, but usable standalone.

License: LGPL-3
""",
    'author': 'OpenVoo',
    'website': 'https://openvoo.pe',
    'license': 'Other OSI approved licence',
    'depends': [
        'base',
        'mail',
        'hr',
        'product',
        'sdr_chatbot',
    ],
    'data': [
        'security/ir.model.access.csv',
        'data/ov_ai_creator_template_data.xml',
        'views/ov_ai_creator_template_views.xml',
        'views/ov_ai_creator_request_views.xml',
        'views/ov_ai_creator_audit_views.xml',
        'views/ov_ai_creator_menus.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
