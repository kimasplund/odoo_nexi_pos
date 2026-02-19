/** @odoo-module **/

/**
 * NEXI Payment Frontend Models
 * Represents transactions and terminals in the POS frontend
 */

import { registry } from "@web/core/registry";
import { reactive } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";

/**
 * Frontend representation of a NEXI Transaction
 */
export class NexiTransaction {
    constructor(data = {}) {
        this.id = data.id || null;
        this.external_id = data.external_id || null;
        this.amount = data.amount || 0;
        this.currency_id = data.currency_id || null;
        this.terminal_id = data.terminal_id || null;
        this.pos_order_id = data.pos_order_id || null;
        this.state = data.state || 'pending'; // pending, processing, awaiting_confirm, confirmed, failed, cancelled
        this.payment_method_id = data.payment_method_id || null;
        this.create_date = data.create_date || null;
        this.write_date = data.write_date || null;
        this.error_message = data.last_error || data.error_message || null;
        this.transaction_ref = data.transaction_ref || data.reference || null;
        this.auth_code = data.auth_code || data.authorization_code || null;
        this.receipt_data = data.receipt_data || null;
        // Card details
        this.reference = data.reference || data.transaction_ref || '';
        this.card_scheme = data.card_scheme || '';
        this.authorization_code = data.authorization_code || data.auth_code || '';
        this.card_entry_mode = data.card_entry_mode || '';
        this.card_number_customer = data.card_number_customer || '';
        this.card_number_merchant = data.card_number_merchant || '';
        this.cardholder_verification = data.cardholder_verification || '';
        this.application_name = data.application_name || '';
        this.receipt_details_customer = data.receipt_details_customer || '';
        this.receipt_details_merchant = data.receipt_details_merchant || '';
    }

    /**
     * Get last four digits of card number
     * @returns {string}
     */
    getLastFourDigits() {
        const num = this.card_number_customer || '';
        return num.length >= 4 ? num.slice(-4) : num;
    }

    /**
     * Get human-readable entry mode label
     * @returns {string}
     */
    getEntryModeLabel() {
        const modeMap = {
            'CONTACT_EMV': _t('Chip'),
            'CONTACTLESS_EMV': _t('Contactless'),
            'MAGNETIC_STRIPE': _t('Magnetic stripe'),
            'MANUAL': _t('Manual'),
        };
        return modeMap[(this.card_entry_mode || '').toUpperCase()] || this.card_entry_mode || '';
    }

    /**
     * Check if transaction is in a final state
     * @returns {boolean}
     */
    isFinal() {
        return ['confirmed', 'failed', 'cancelled'].includes(this.state);
    }

    /**
     * Check if transaction was successful
     * @returns {boolean}
     */
    isSuccessful() {
        return this.state === 'confirmed' || this.state === 'awaiting_confirm';
    }

    /**
     * Get display state with user-friendly label
     * @returns {string}
     */
    getDisplayState() {
        const stateMap = {
            'pending': _t('Pending'),
            'processing': _t('Processing'),
            'awaiting_confirm': _t('Awaiting confirmation'),
            'confirmed': _t('Confirmed'),
            'failed': _t('Failed'),
            'cancelled': _t('Cancelled'),
        };
        return stateMap[this.state] || this.state;
    }

    /**
     * Create from server response
     * @param {Object} data - Server response data
     * @returns {NexiTransaction}
     */
    static fromServer(data) {
        return new NexiTransaction(data);
    }
}

/**
 * Frontend representation of a NEXI Terminal
 */
export class NexiTerminal {
    constructor(data = {}) {
        this.id = data.id || null;
        this.name = data.name || _t('Unknown Terminal');
        this.terminal_id = data.terminal_id || null;
        this.status = data.status || 'unknown'; // online, offline, busy, error, unknown
        this.last_seen = data.last_seen || null;
        this.is_default = data.is_default || false;
        this.config_id = data.config_id || null;
    }

    /**
     * Check if terminal is online and available
     * @returns {boolean}
     */
    isAvailable() {
        return this.status === 'online';
    }

    /**
     * Check if terminal is busy with another transaction
     * @returns {boolean}
     */
    isBusy() {
        return this.status === 'busy';
    }

    /**
     * Get CSS class for status indicator
     * @returns {string}
     */
    getStatusClass() {
        const classMap = {
            'online': 'nexi-terminal-online',
            'offline': 'nexi-terminal-offline',
            'busy': 'nexi-terminal-busy',
            'error': 'nexi-terminal-error',
            'unknown': 'nexi-terminal-unknown'
        };
        return classMap[this.status] || 'nexi-terminal-unknown';
    }

    /**
     * Get user-friendly status label
     * @returns {string}
     */
    getStatusLabel() {
        const labelMap = {
            'online': _t('Online'),
            'offline': _t('Offline'),
            'busy': _t('Busy'),
            'error': _t('Error'),
            'unknown': _t('Unknown')
        };
        return labelMap[this.status] || this.status;
    }

    /**
     * Create from server response
     * @param {Object} data - Server response data
     * @returns {NexiTerminal}
     */
    static fromServer(data) {
        return new NexiTerminal(data);
    }
}

/**
 * Reactive store for NEXI payment state management
 */
export function createNexiStore() {
    return reactive({
        // Current transaction
        currentTransaction: null,
        
        // Available terminals
        terminals: [],
        
        // Selected terminal
        selectedTerminal: null,
        
        // UI State
        isProcessing: false,
        showWaitingScreen: false,
        showResultScreen: false,
        errorMessage: null,
        
        // Polling
        pollInterval: null,
        pollCount: 0,
        maxPollCount: 180, // 3 minutes at 1 second intervals
        
        /**
         * Set current transaction
         * @param {NexiTransaction} transaction
         */
        setTransaction(transaction) {
            this.currentTransaction = transaction;
        },

        /**
         * Clear current transaction
         */
        clearTransaction() {
            this.currentTransaction = null;
            this.errorMessage = null;
            this.pollCount = 0;
        },

        /**
         * Set available terminals
         * @param {NexiTerminal[]} terminals
         */
        setTerminals(terminals) {
            this.terminals = terminals;
            // Auto-select default terminal if available
            const defaultTerminal = terminals.find(t => t.is_default && t.isAvailable());
            if (defaultTerminal) {
                this.selectedTerminal = defaultTerminal;
            } else if (terminals.length > 0 && terminals[0].isAvailable()) {
                this.selectedTerminal = terminals[0];
            }
        },

        /**
         * Select a terminal by ID
         * @param {number} terminalId
         */
        selectTerminal(terminalId) {
            const terminal = this.terminals.find(t => t.id === terminalId);
            if (terminal) {
                this.selectedTerminal = terminal;
            }
        },

        /**
         * Get available terminals (online only)
         * @returns {NexiTerminal[]}
         */
        getAvailableTerminals() {
            return this.terminals.filter(t => t.isAvailable());
        },

        /**
         * Check if any terminal is available
         * @returns {boolean}
         */
        hasAvailableTerminal() {
            return this.terminals.some(t => t.isAvailable());
        },

        /**
         * Start processing state
         */
        startProcessing() {
            this.isProcessing = true;
            this.showWaitingScreen = true;
            this.showResultScreen = false;
            this.errorMessage = null;
        },

        /**
         * Stop processing state
         */
        stopProcessing() {
            this.isProcessing = false;
            this.showWaitingScreen = false;
        },

        /**
         * Show result screen
         * @param {boolean} success
         * @param {string} message
         */
        showResult(success, message = null) {
            this.showWaitingScreen = false;
            this.showResultScreen = true;
            if (!success && message) {
                this.errorMessage = message;
            }
        },

        /**
         * Reset store to initial state
         */
        reset() {
            this.currentTransaction = null;
            this.isProcessing = false;
            this.showWaitingScreen = false;
            this.showResultScreen = false;
            this.errorMessage = null;
            this.pollCount = 0;
            if (this.pollInterval) {
                clearInterval(this.pollInterval);
                this.pollInterval = null;
            }
        }
    });
}

// Create singleton store instance
export const nexiStore = createNexiStore();

// Register in services registry
registry.category("services").add("nexi_store", {
    dependencies: [],
    start() {
        return nexiStore;
    },
});
