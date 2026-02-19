{
    'name': 'NEXI POS Payment',
    'version': '18.0.1.0.0',
    'category': 'Point of Sale',
    'summary': 'Integrate NEXI payment terminals with Odoo POS',
    'description': """
        NEXI POS Payment Integration
        ============================
        Seamlessly integrate NEXI payment terminals with Odoo Point of Sale.
        
        Features:
        - Process card payments through NEXI terminals
        - Automatic reconciliation
        - Real-time transaction status
        - Support for refunds
        - Multi-terminal support
        - Secure API communication
    """,
    'author': 'Kim Asplund',
    'website': 'https://asplund.kim',
    'license': 'LGPL-3',
    'depends': [
        'base',
        'mail',
        'point_of_sale',
        'account',
    ],
    'data': [
        # Security (groups must load before rules)
        'security/nexi_groups.xml',
        'security/nexi_security.xml',
        'security/ir.model.access.csv',
        # Data
        'data/nexi_sequence.xml',
        'data/nexi_cron.xml',
        # Views
        'views/nexi_terminal_views.xml',
        'views/nexi_transaction_views.xml',
        'views/pos_payment_method_views.xml',
        'views/pos_payment_views.xml',
        'views/pos_order_views.xml',
        'views/nexi_menu.xml',
    ],
    'assets': {
        'point_of_sale._assets_pos': [
            'pos_nexi_payment/static/src/css/nexi_payment.css',
            'pos_nexi_payment/static/src/js/nexi_models.js',
            'pos_nexi_payment/static/src/js/nexi_payment_button.js',
            'pos_nexi_payment/static/src/js/nexi_payment_screen.js',
            'pos_nexi_payment/static/src/js/nexi_terminal_status.js',
            'pos_nexi_payment/static/src/xml/nexi_payment_templates.xml',
        ],
        'web.assets_backend': [
            'pos_nexi_payment/static/src/css/nexi_payment.css',
        ],
        'web.report_assets_common': [
            'pos_nexi_payment/static/src/css/nexi_payment.css',
        ],
    },
    'demo': [],
    'installable': True,
    'application': False,
    'auto_install': False,
}
