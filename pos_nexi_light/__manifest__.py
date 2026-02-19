# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.
{
    'name': 'POS NEXI Light',
    'version': '18.0.1.0.0',
    'author': 'Kim Asplund',
    'website': 'https://asplund.kim',
    'category': 'Sales/Point of Sale',
    'sequence': 6,
    'summary': 'Integrate your POS with NEXI payment terminals',
    'description': """
        NEXI POS Payment Terminal Integration
        =====================================
        
        This module integrates NEXI payment terminals with Odoo Point of Sale.
        
        Features:
        - Process card payments through NEXI terminals
        - Automatic payment confirmation
        - Support for refunds
        - Real-time terminal status
        - Secure API communication with encryption
        - Webhook notifications
        
        Configuration:
        1. Go to Point of Sale > Configuration > Payment Methods
        2. Create a payment method and select "NEXI" as the terminal
        3. Enter your terminal ID and API credentials
        4. Assign to your POS configuration
        
        Usage:
        1. Open POS session
        2. Add products to cart
        3. Select NEXI payment method
        4. Click "Send" to terminal
        5. Customer completes payment on terminal
        6. Payment auto-confirms in POS
    """,
    'depends': ['point_of_sale'],
    'data': [
        'views/pos_payment_method_views.xml',
        'data/nexi_data.xml',
    ],
    'assets': {
        'point_of_sale._assets_pos': [
            'pos_nexi_light/static/src/**/*',
        ],
    },
    'installable': True,
    'license': 'LGPL-3',
}
