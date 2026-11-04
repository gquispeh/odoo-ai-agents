# -*- coding: utf-8 -*-
{
    'name': 'OpenVoo · Virtual Employees',
    'version': '19.0.1.0.0',
    'category': 'Services/OpenVoo',
    'summary': 'Hire virtual employees that work inside Odoo. Trust dial, scoped autonomy, budget guards, full audit.',
    'description': """
OpenVoo Virtual Employees
=========================

Productized virtual labor inside Odoo. The operator browses a roster of roles
(accountant, sales follow-up, inventory clerk, etc.), hires one, scopes its
authority per capability, and watches it work in an inbox-style thread.

Architectural pillars:
- Role / Employee / Capability / Policy / Task / Run / Action data model
- Four-level trust dial: Shadow, Assisted, Autonomous, Trusted
- Per-capability autonomy override (AUTO / CONFIRM / READONLY / OFF) with daily quotas
- Two trigger paradigms: cron-scheduled and Odoo automation-rule events
- Working-hours and timezone respect
- Daily USD budget per employee with auto-pause on overrun
- Every write routes through ov_ai_record_creator's confirmation gate (audit-by-design)
- Conversational supervision via sdr_chatbot session

Bundled role: accountant. Bundled capabilities: bank statement reconciliation
proposals, vendor payment registration, overdue invoice reminders, invoice
anomaly flagging, period-close checklist preparation.

License: LGPL-3
""",
    'author': 'OpenVoo',
    'website': 'https://openvoo.pe',
    'license': 'Other OSI approved licence',
    'depends': [
        'base',
        'mail',
        'hr',
        'account',
        'sdr_chatbot',
        'ov_ai_record_creator',
    ],
    'data': [
        'security/ov_ve_security.xml',
        'security/ir.model.access.csv',
        'data/ov_ve_capability_data.xml',
        'data/ov_ve_role_data.xml',
        'data/ov_ve_cron_data.xml',
        'views/ov_ve_capability_views.xml',
        'views/ov_ve_role_views.xml',
        'views/ov_ve_run_views.xml',
        'views/ov_ve_employee_views.xml',
        'views/ov_ve_menus.xml',
    ],
    'installable': True,
    'application': True,
    'auto_install': False,
}
