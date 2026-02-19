# Changelog

All notable changes to the NEXI POS Payment module will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Planned
- Support for NEXI Pay-by-Link integration
- QR code payment display in POS
- Batch refund processing
- Advanced analytics dashboard
- Mobile app companion for terminal status

## [18.0.1.0.0] - 2024-01-15

### Added
- Initial release for Odoo 18.0
- Full NEXI API client with retry logic
- Terminal management with multi-company support
- Transaction lifecycle management (pending → processing → confirmed)
- Webhook notification handling with HMAC verification
- POS integration with real-time payment flow
- Session management (open/close with terminal lifecycle)
- End-of-day reconciliation reporting
- Comprehensive security measures
  - API key encryption at rest
  - Webhook signature verification
  - Rate limiting support
  - SQL injection prevention
- Role-based access control (User/Manager/Admin)
- Multi-terminal support per POS config
- Refund processing capability
- Automatic transaction cleanup cron job
- Pending transaction polling cron job
- Full test coverage (>90%)
- Complete documentation

### Security
- Implemented API key masking in logs
- Added input validation for all user inputs
- Multi-company record rules
- Terminal deletion protection (cannot delete with transactions)
- Secure webhook endpoint with signature validation

### Technical
- Python 3.10+ support
- Pydantic-style input validation
- Context manager support for API client
- Comprehensive exception hierarchy
- Mock-based unit testing
- Integration test suite

## [Migration Notes]

### Upgrading from Previous Versions

#### To 18.0.1.0.0
This is the initial release for Odoo 18. No migration required.

#### Database Changes
- New tables: `nexi_terminal`, `nexi_transaction`
- Extended tables: `pos_payment_method`, `pos_payment`, `pos_order`, `pos_session`
- New security groups and record rules

### Deprecations
None in this release.

### Breaking Changes
None in this release.

---

## Release History Template

```markdown
## [Version] - YYYY-MM-DD

### Added
- New features

### Changed
- Changes in existing functionality

### Deprecated
- Soon-to-be removed features

### Removed
- Now removed features

### Fixed
- Bug fixes

### Security
- Security improvements
```

---

## Version Numbering

- **Major** (X.0.0): Significant architectural changes, Odoo version updates
- **Minor** (x.Y.0): New features, enhancements
- **Patch** (x.y.Z): Bug fixes, security patches

## Support Lifecycle

| Version | Odoo Version | Support Status | End of Life |
|---------|--------------|----------------|-------------|
| 18.0.1.x | 18.0 | Active | TBD |

---

For detailed migration guides, see the documentation at:
https://docs.yourcompany.com/nexi-pos/migration
