# -*- coding: utf-8 -*-
"""NEXI Security Mixin - Security utilities for all NEXI models.

This mixin provides common security functionality for all models
in the NEXI payment module:
- Audit logging
- Input sanitization  
- Rate limiting
- Permission validation
"""

import hashlib
import logging
import re
import time
from typing import Any, Optional

from odoo import _, api, fields, models
from odoo.exceptions import AccessDenied, ValidationError

from ..security.nexi_security import (
    _audit_log,
    _check_rate_limit,
    sanitize_input,
    NexiSecurityError
)

_logger = logging.getLogger(__name__)

# In-memory rate limit storage (key -> {count, first_attempt, locked_until})
_rate_limit_cache = {}


class NexiSecurityMixin(models.AbstractModel):
    """Security mixin for all NEXI models.
    
    Provides security utilities:
    - Audit logging for sensitive operations
    - Input sanitization to prevent injection
    - Rate limiting for API operations
    - Permission validation helpers
    """
    
    _name = 'nexi.security.mixin'
    _description = 'NEXI Security Mixin'
    
    # =========================================================================
    # Audit Logging
    # =========================================================================
    
    def _audit_log(self, action: str, details: dict, 
                   message: Optional[str] = None) -> None:
        """Write a security audit log entry.
        
        Args:
            action: The action being logged (e.g., 'payment_initiated')
            details: Dictionary of additional data to log
            message: Optional human-readable message
        """
        self.ensure_one()
        
        if message is None:
            message = f"{action} on {self._name}({self.id})"
        
        # Add model context to details
        log_details = {
            'model': self._name,
            'record_id': self.id,
            **(details or {})
        }
        
        _audit_log(self.env, action, message, log_details)
    
    @api.model
    def _audit_log_model(self, action: str, details: dict,
                         message: Optional[str] = None) -> None:
        """Write a security audit log entry at the model level.
        
        Args:
            action: The action being logged
            details: Dictionary of additional data to log
            message: Optional human-readable message
        """
        if message is None:
            message = f"{action} on {self._name}"
        
        log_details = {
            'model': self._name,
            **(details or {})
        }
        
        _audit_log(self.env, action, message, log_details)
    
    # =========================================================================
    # Input Sanitization
    # =========================================================================
    
    def _sanitize_input(self, value: Any, input_type: str = 'string') -> Any:
        """Sanitize user input to prevent injection attacks.
        
        Args:
            value: The input value to sanitize
            input_type: The expected type:
                - 'string': Basic string sanitization
                - 'external_id': Alphanumeric + hyphen + underscore (max 63)
                - 'terminal_id': Alphanumeric + hyphen + underscore (max 32)
                - 'amount': Positive number with max 2 decimals
                - 'email': Email address validation
                
        Returns:
            Sanitized value
            
        Raises:
            ValidationError: If input cannot be sanitized
        """
        return sanitize_input(value, input_type)
    
    @api.model
    def _sanitize_external_id(self, external_id: str) -> str:
        """Sanitize external transaction ID.
        
        Args:
            external_id: The external ID to sanitize
            
        Returns:
            Sanitized external ID
            
        Raises:
            ValidationError: If invalid format
        """
        return self._sanitize_input(external_id, 'external_id')
    
    @api.model
    def _sanitize_terminal_id(self, terminal_id: str) -> str:
        """Sanitize terminal ID.
        
        Args:
            terminal_id: The terminal ID to sanitize
            
        Returns:
            Sanitized terminal ID
            
        Raises:
            ValidationError: If invalid format
        """
        return self._sanitize_input(terminal_id, 'terminal_id')
    
    @api.model
    def _sanitize_amount(self, amount) -> float:
        """Sanitize transaction amount.
        
        Args:
            amount: The amount to sanitize
            
        Returns:
            Sanitized amount as float
            
        Raises:
            ValidationError: If invalid amount
        """
        return self._sanitize_input(amount, 'amount')
    
    # =========================================================================
    # Rate Limiting
    # =========================================================================
    
    def _check_rate_limit(self, action: str, max_attempts: int = 10,
                         window_seconds: int = 60) -> bool:
        """Check if operation is within rate limits.
        
        Args:
            action: The action being rate limited
            max_attempts: Maximum attempts in the time window
            window_seconds: Time window in seconds
            
        Returns:
            True if within limits, False if exceeded
        """
        return _check_rate_limit(self.env, action, max_attempts, window_seconds)
    
    def _enforce_rate_limit(self, action: str, max_attempts: int = 10,
                           window_seconds: int = 60) -> None:
        """Enforce rate limit, raising exception if exceeded.
        
        Args:
            action: The action being rate limited
            max_attempts: Maximum attempts in the time window
            window_seconds: Time window in seconds
            
        Raises:
            NexiSecurityError: If rate limit exceeded
        """
        if not self._check_rate_limit(action, max_attempts, window_seconds):
            self._audit_log(
                'rate_limit_exceeded',
                {'action': action, 'max_attempts': max_attempts}
            )
            raise NexiSecurityError(
                _('Too many attempts. Please try again later.')
            )
    
    # =========================================================================
    # Permission Validation
    # =========================================================================
    
    def _validate_permission(self, permission: str) -> bool:
        """Validate that the current user has a specific permission.
        
        Permission groups:
        - 'nexi_user': Basic NEXI operations
        - 'nexi_manager': Refunds and configuration
        - 'nexi_admin': API key management
        - 'refund': Ability to process refunds
        - 'configure': Ability to configure terminals
        - 'api_key_manage': Ability to manage API keys
        
        Args:
            permission: The permission to check
            
        Returns:
            True if has permission
            
        Raises:
            NexiSecurityError: If permission check fails
        """
        user = self.env.user
        
        permission_map = {
            'nexi_user': 'pos_nexi_payment.group_nexi_user',
            'nexi_manager': 'pos_nexi_payment.group_nexi_manager',
            'nexi_admin': 'pos_nexi_payment.group_nexi_admin',
            'refund': 'pos_nexi_payment.group_nexi_manager',
            'configure': 'pos_nexi_payment.group_nexi_manager',
            'api_key_manage': 'pos_nexi_payment.group_nexi_admin',
        }
        
        # Map legacy permissions
        if permission == 'user':
            permission = 'nexi_user'
        elif permission == 'manager':
            permission = 'nexi_manager'
        elif permission == 'admin':
            permission = 'nexi_admin'
        
        group_xmlid = permission_map.get(permission)
        
        if group_xmlid:
            has_perm = user.has_group(group_xmlid)
        else:
            # Unknown permission - deny by default
            has_perm = False
        
        if not has_perm:
            self._audit_log(
                'permission_denied',
                {'permission': permission, 'user': user.login}
            )
            raise NexiSecurityError(
                _('You do not have permission to perform this action: %s') 
                % permission
            )
        
        return True
    
    def _has_permission(self, permission: str) -> bool:
        """Check if user has permission without raising exception.
        
        Args:
            permission: The permission to check
            
        Returns:
            True if has permission, False otherwise
        """
        try:
            return self._validate_permission(permission)
        except NexiSecurityError:
            return False
    
    # =========================================================================
    # Security Utilities
    # =========================================================================
    
    def _generate_request_hash(self, data: dict) -> str:
        """Generate a hash for request deduplication.
        
        Args:
            data: Request data to hash
            
        Returns:
            SHA-256 hash string
        """
        # Sort keys for consistent hashing
        canonical = str(sorted(data.items()))
        return hashlib.sha256(canonical.encode()).hexdigest()
    
    def _redact_sensitive_data(self, data: dict) -> dict:
        """Redact sensitive fields from data for logging.
        
        Args:
            data: Dictionary that may contain sensitive data
            
        Returns:
            Copy of data with sensitive fields redacted
        """
        sensitive_fields = {
            'api_key', 'api_secret', 'password', 'token', 'auth',
            'authorization', 'credit_card', 'cvv', 'pin'
        }
        
        result = {}
        for key, value in data.items():
            key_lower = key.lower()
            if any(sf in key_lower for sf in sensitive_fields):
                if isinstance(value, str) and len(value) > 8:
                    result[key] = f"{value[:4]}...{value[-4:]}"
                else:
                    result[key] = '***'
            elif isinstance(value, dict):
                result[key] = self._redact_sensitive_data(value)
            elif isinstance(value, list):
                result[key] = [
                    self._redact_sensitive_data(item) if isinstance(item, dict) else item
                    for item in value
                ]
            else:
                result[key] = value
        
        return result
    
    def _validate_ip_whitelist(self, allowed_ips: list) -> bool:
        """Validate that current request is from an allowed IP.
        
        Args:
            allowed_ips: List of allowed IP addresses or CIDR ranges
            
        Returns:
            True if IP is allowed
        """
        try:
            request = self.env.get('request')
            if not request:
                return True  # No request context, skip check
            
            client_ip = request.httprequest.remote_addr
            
            if client_ip in allowed_ips:
                return True
            
            # Check CIDR ranges
            import ipaddress
            client_addr = ipaddress.ip_address(client_ip)
            
            for allowed in allowed_ips:
                try:
                    if '/' in allowed:
                        network = ipaddress.ip_network(allowed, strict=False)
                        if client_addr in network:
                            return True
                except ValueError:
                    continue
            
            return False
            
        except Exception as e:
            _logger.warning("IP whitelist validation error: %s", str(e))
            return False
