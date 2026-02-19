# -*- coding: utf-8 -*-
"""NEXI Security Module - Authorization, Audit, and Rate Limiting.

This module provides security controls for NEXI payment operations:
- Multi-layer authorization for refunds
- Audit logging for all sensitive operations
- Rate limiting to prevent abuse
- Permission validation

Security Levels:
- group_nexi_user: Basic POS operations
- group_nexi_manager: Refunds, terminal config changes
- group_nexi_admin: API key management, full access
"""

import functools
import hashlib
import logging
import re
import time
from datetime import timedelta
from typing import Any, Callable, Optional

from odoo import _, api, fields, models
from odoo.exceptions import AccessDenied, UserError, ValidationError

_logger = logging.getLogger(__name__)

# Rate limiting storage (in-memory, per-process)
_rate_limit_store = {}

# Default refund approval threshold (amounts above require manager approval)
DEFAULT_REFUND_THRESHOLD = 100.0

# Maximum failed attempts before temporary lockout
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_DURATION_SECONDS = 300  # 5 minutes


class NexiSecurityError(AccessDenied):
    """Custom exception for NEXI security violations."""
    pass


def require_nexi_manager(approval_threshold: Optional[float] = None):
    """Decorator to require NEXI manager permission for operations.
    
    This decorator enforces role-based access control for sensitive operations
    like refunds. It supports multi-layer authorization where amounts above
    a threshold require additional approval.
    
    Args:
        approval_threshold: Amount above which manager approval is required.
                          If None, always requires manager approval.
    
    Usage:
        @require_nexi_manager(approval_threshold=100.0)
        def action_refund(self, amount):
            # This will check authorization
            pass
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(self, *args, **kwargs):
            # Get amount from args or kwargs if provided
            amount = kwargs.get('amount')
            if amount is None and args:
                try:
                    amount = float(args[0])
                except (IndexError, ValueError, TypeError):
                    amount = None
            
            # Check authorization
            check_refund_authorization(self.env, amount, approval_threshold)
            
            # Log the access
            _audit_log(
                self.env,
                'refund_access_granted',
                f"User {self.env.user.login} accessed {func.__name__} "
                f"with amount {amount}",
                {'amount': amount, 'method': func.__name__}
            )
            
            return func(self, *args, **kwargs)
        return wrapper
    return decorator


def check_refund_authorization(env, amount: Optional[float] = None, 
                               threshold: Optional[float] = None) -> bool:
    """Check if the current user is authorized to perform a refund.
    
    Implements multi-layer authorization:
    1. Check if user has basic refund permission
    2. Check amount against threshold
    3. Check for manager approval if needed
    4. Check rate limits
    5. Log the attempt
    
    Args:
        env: Odoo environment
        amount: The refund amount (optional)
        threshold: Override threshold amount (optional)
        
    Returns:
        True if authorized
        
    Raises:
        NexiSecurityError: If not authorized
    """
    user = env.user
    
    # Check if user has any refund permission
    has_refund_perm = (
        user.has_group('pos_nexi_payment.group_nexi_manager') or
        user.has_group('pos_nexi_payment.group_nexi_admin') or
        user.has_group('point_of_sale.group_pos_manager')
    )
    
    if not has_refund_perm:
        _audit_log(
            env,
            'refund_access_denied',
            f"User {user.login} denied refund access - insufficient permissions",
            {'amount': amount}
        )
        raise NexiSecurityError(
            _('You do not have permission to process refunds. '
              'Please contact a manager.')
        )
    
    # Get threshold from config or use default
    if threshold is None:
        threshold = float(
            env['ir.config_parameter'].sudo().get_param(
                'nexi_payment.refund_threshold',
                str(DEFAULT_REFUND_THRESHOLD)
            )
        )
    
    # Check if amount exceeds threshold and requires manager approval
    if amount is not None and amount > threshold:
        # Check if user is a manager
        is_manager = (
            user.has_group('pos_nexi_payment.group_nexi_manager') or
            user.has_group('pos_nexi_payment.group_nexi_admin')
        )
        
        if not is_manager:
            _audit_log(
                env,
                'refund_approval_required',
                f"User {user.login} attempted refund of {amount} "
                f"exceeding threshold {threshold}",
                {'amount': amount, 'threshold': threshold}
            )
            raise NexiSecurityError(
                _('Refund amount %.2f exceeds the threshold (%.2f). '
                  'Manager approval required.') % (amount, threshold)
            )
    
    # Check rate limiting
    if not _check_rate_limit(env, 'refund'):
        raise NexiSecurityError(
            _('Too many refund attempts. Please try again later.')
        )
    
    return True


def _audit_log(env, action: str, message: str, details: Optional[dict] = None):
    """Write a security audit log entry.
    
    Args:
        env: Odoo environment
        action: The action being logged
        message: Human-readable log message
        details: Additional structured data
    """
    user = env.user
    log_message = (
        f"[NEXI AUDIT] Action: {action} | User: {user.login} (ID: {user.id}) | "
        f"{message}"
    )
    
    # Log to Odoo logger
    _logger.info(log_message)
    
    # Create audit log record if model exists
    try:
        AuditLog = env['nexi.audit.log'].sudo()
        if AuditLog is not None:
            AuditLog.create({
                'action': action,
                'user_id': user.id,
                'message': message,
                'details': str(details) if details else False,
                'ip_address': _get_client_ip(env),
            })
    except Exception:
        # Don't fail if audit logging fails
        pass


def _check_rate_limit(env, action: str, max_attempts: int = 10, 
                      window_seconds: int = 60) -> bool:
    """Check if the current request is within rate limits.
    
    Args:
        env: Odoo environment
        action: The action being rate limited
        max_attempts: Maximum attempts allowed in the window
        window_seconds: Time window in seconds
        
    Returns:
        True if within limits, False if exceeded
    """
    user = env.user
    key = f"{user.id}:{action}"
    now = time.time()
    
    # Get or initialize rate limit entry
    entry = _rate_limit_store.get(key, {'count': 0, 'first_attempt': now})
    
    # Reset if window has passed
    if now - entry['first_attempt'] > window_seconds:
        entry = {'count': 0, 'first_attempt': now}
    
    # Check if limit exceeded
    if entry['count'] >= max_attempts:
        _logger.warning(
            "Rate limit exceeded for user %s on action %s",
            user.login, action
        )
        return False
    
    # Increment counter
    entry['count'] += 1
    _rate_limit_store[key] = entry
    
    return True


def _get_client_ip(env) -> str:
    """Get the client IP address from the request.
    
    Args:
        env: Odoo environment
        
    Returns:
        Client IP address or 'unknown'
    """
    try:
        request = env.get('request')
        if request:
            return request.httprequest.remote_addr or 'unknown'
    except Exception:
        pass
    return 'unknown'


def sanitize_input(value: Any, input_type: str = 'string') -> Any:
    """Sanitize user input to prevent injection attacks.
    
    Args:
        value: The input value to sanitize
        input_type: The expected type ('string', 'external_id', 'amount', 'terminal_id')
        
    Returns:
        Sanitized value
        
    Raises:
        ValidationError: If input cannot be sanitized
    """
    if value is None:
        return None
    
    if input_type == 'string':
        # Basic string sanitization
        value = str(value).strip()
        # Remove control characters
        value = re.sub(r'[\x00-\x1F\x7F]', '', value)
        return value
    
    elif input_type == 'external_id':
        # External IDs: alphanumeric, hyphens, underscores only
        value = str(value).strip()
        if not re.match(r'^[a-zA-Z0-9_-]+$', value):
            raise ValidationError(
                _('Invalid external ID format. Use only letters, numbers, hyphens, and underscores.')
            )
        if len(value) > 63:
            raise ValidationError(
                _('External ID exceeds maximum length of 63 characters.')
            )
        return value
    
    elif input_type == 'terminal_id':
        # Terminal IDs: alphanumeric, hyphens, underscores, max 32 chars
        value = str(value).strip()
        if not re.match(r'^[a-zA-Z0-9_-]+$', value):
            raise ValidationError(
                _('Invalid terminal ID format. Use only letters, numbers, hyphens, and underscores.')
            )
        if len(value) > 32:
            raise ValidationError(
                _('Terminal ID exceeds maximum length of 32 characters.')
            )
        return value
    
    elif input_type == 'amount':
        # Amount: positive number with max 2 decimal places
        try:
            amount = float(value)
        except (TypeError, ValueError):
            raise ValidationError(_('Amount must be a valid number.'))
        
        if amount <= 0:
            raise ValidationError(_('Amount must be positive.'))
        
        if amount > 999999.99:
            raise ValidationError(_('Amount exceeds maximum allowed value.'))
        
        # Check decimal places
        if round(amount, 2) != amount:
            raise ValidationError(_('Amount must have at most 2 decimal places.'))
        
        return round(amount, 2)
    
    elif input_type == 'email':
        # Basic email validation
        value = str(value).strip().lower()
        if not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', value):
            raise ValidationError(_('Invalid email format.'))
        return value
    
    else:
        return str(value).strip()


class NexiAuditLog(models.Model):
    """Security audit log for NEXI payment operations."""
    
    _name = 'nexi.audit.log'
    _description = 'NEXI Security Audit Log'
    _order = 'create_date desc'
    _rec_name = 'action'
    
    action = fields.Char(
        string='Action',
        required=True,
        index=True,
        help='The action that was performed'
    )
    
    user_id = fields.Many2one(
        comodel_name='res.users',
        string='User',
        required=True,
        help='The user who performed the action'
    )
    
    message = fields.Text(
        string='Message',
        required=True,
        help='Human-readable description of the action'
    )
    
    details = fields.Text(
        string='Details',
        help='Additional structured data in JSON or text format'
    )
    
    ip_address = fields.Char(
        string='IP Address',
        help='Client IP address'
    )
    
    create_date = fields.Datetime(
        string='Timestamp',
        readonly=True,
        help='When the action occurred'
    )
    
    @api.model_create_multi
    def create(self, vals_list):
        """Override create to redact sensitive data."""
        for vals in vals_list:
            # Redact sensitive data in details
            if vals.get('details'):
                details_str = str(vals['details'])
                sensitive_patterns = [
                    'api_key', 'api_secret', 'password', 'token',
                    'credit_card', 'cvv', 'pin', 'authorization'
                ]
                for pattern in sensitive_patterns:
                    import re
                    details_str = re.sub(
                        rf'({pattern}[\s]*[=:][\s]*)([^\s,}}\]]+)',
                        r'\1***',
                        details_str,
                        flags=re.IGNORECASE
                    )
                vals['details'] = details_str
        
        return super(NexiAuditLog, self).create(vals_list)
    
    @api.model
    def cleanup_old_logs(self, days: int = 90):
        """Remove audit logs older than specified days.
        
        Args:
            days: Number of days to keep logs
        """
        cutoff_date = fields.Datetime.now() - timedelta(days=days)
        
        old_logs = self.search([('create_date', '<', cutoff_date)])
        if old_logs:
            _logger.info('Cleaning up %s old NEXI audit logs', len(old_logs))
            old_logs.unlink()
