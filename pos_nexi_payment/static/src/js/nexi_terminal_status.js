/** @odoo-module **/

/**
 * NEXI Terminal Status Component
 * Displays terminal connection status in POS header
 */

import { Component, useState, onMounted, onWillUnmount } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";
import { rpc } from "@web/core/network/rpc";
import { NexiTerminal, nexiStore } from "./nexi_models";

const STATUS_REFRESH_INTERVAL = 30000; // 30 seconds

/**
 * Terminal Status Indicator Component
 * Shows in POS chrome/header
 */
export class NexiTerminalStatus extends Component {
    static template = "pos_nexi_payment.NexiTerminalStatus";
    static props = {};

    setup() {
        this.store = useState(nexiStore);
        this.pos = useService("pos");
        this.notification = useService("notification");
        
        this.state = useState({
            expanded: false,
            lastRefresh: null
        });

        this.refreshInterval = null;

        onMounted(() => {
            this.refreshTerminals();
            this.startAutoRefresh();
        });

        onWillUnmount(() => {
            this.stopAutoRefresh();
        });
    }

    /**
     * Get overall status for display
     */
    get overallStatus() {
        const terminals = this.store.terminals;
        if (!terminals.length) {
            return {
                class: 'nexi-status-unknown',
                icon: 'fa-question-circle',
                label: _t("NEXI"),
                tooltip: _t("No terminal configured")
            };
        }

        const onlineCount = terminals.filter(t => t.isAvailable()).length;
        const busyCount = terminals.filter(t => t.isBusy()).length;

        if (onlineCount === 0) {
            return {
                class: 'nexi-status-offline',
                icon: 'fa-times-circle',
                label: _t("NEXI Offline"),
                tooltip: _t("All terminals are offline")
            };
        }

        if (busyCount === terminals.length) {
            return {
                class: 'nexi-status-busy',
                icon: 'fa-clock-o',
                label: _t("NEXI Busy"),
                tooltip: _t("All terminals are busy")
            };
        }

        if (onlineCount < terminals.length) {
            return {
                class: 'nexi-status-warning',
                icon: 'fa-exclamation-circle',
                label: _t("NEXI Partial"),
                tooltip: _t("Some terminals are offline")
            };
        }

        return {
            class: 'nexi-status-online',
            icon: 'fa-check-circle',
            label: _t("NEXI Online"),
            tooltip: _t("All terminals are online")
        };
    }

    /**
     * Get detailed terminal list for expanded view
     */
    get terminalList() {
        return this.store.terminals.map(terminal => ({
            ...terminal,
            statusClass: terminal.getStatusClass(),
            statusLabel: terminal.getStatusLabel()
        }));
    }

    /**
     * Check if user can see terminal status
     */
    get canShowStatus() {
        return this.store.terminals.length > 0;
    }

    /**
     * Toggle expanded view
     */
    toggleExpanded() {
        this.state.expanded = !this.state.expanded;
        if (this.state.expanded) {
            this.refreshTerminals();
        }
    }

    /**
     * Refresh terminal status from server
     */
    async refreshTerminals() {
        try {
            if (!this.pos.config) return;
            
            const result = await rpc("/pos_nexi/terminals", {
                config_id: this.pos.config.id
            });

            if (result.success && result.terminals) {
                const terminals = result.terminals.map(t => NexiTerminal.fromServer(t));
                this.store.setTerminals(terminals);
                this.state.lastRefresh = new Date();
            }
        } catch (error) {
            console.error("Failed to refresh NEXI terminals:", error);
        }
    }

    /**
     * Start auto-refresh interval
     */
    startAutoRefresh() {
        this.refreshInterval = setInterval(() => {
            this.refreshTerminals();
        }, STATUS_REFRESH_INTERVAL);
    }

    /**
     * Stop auto-refresh interval
     */
    stopAutoRefresh() {
        if (this.refreshInterval) {
            clearInterval(this.refreshInterval);
            this.refreshInterval = null;
        }
    }

    /**
     * Get last refresh time display
     */
    get lastRefreshDisplay() {
        if (!this.state.lastRefresh) return "";
        return this.state.lastRefresh.toLocaleTimeString();
    }

    /**
     * Handle manual refresh click
     */
    async onManualRefresh(ev) {
        ev.stopPropagation();
        await this.refreshTerminals();
        this.notification.add(_t("Terminal status updated"), { type: "info" });
    }
}

/**
 * Terminal Status Popup Component
 * Shows detailed terminal information
 */
export class NexiTerminalStatusPopup extends Component {
    static template = "pos_nexi_payment.NexiTerminalStatusPopup";
    static props = ["close", "terminals"];

    setup() {
        this.store = useState(nexiStore);
    }

    get terminalsList() {
        return this.props.terminals || this.store.terminals;
    }

    get onlineCount() {
        return this.terminalsList.filter(t => t.isAvailable()).length;
    }

    get totalCount() {
        return this.terminalsList.length;
    }

    onClose() {
        this.props.close();
    }

    async onTestConnection(terminalId) {
        // Test connection to specific terminal
        try {
            const result = await rpc("/pos_nexi/terminal/test", {
                terminal_id: terminalId
            });

            if (result.success) {
                this.notification.add(
                    _t("Terminal connection successful"),
                    { type: "success" }
                );
            } else {
                this.notification.add(
                    result.error || _t("Terminal connection failed"),
                    { type: "danger" }
                );
            }
        } catch (error) {
            this.notification.add(
                _t("Error during connection test"),
                { type: "danger" }
            );
        }
    }
}

/**
 * Last Transaction Summary Component
 * Shows summary of most recent NEXI transaction
 */
export class NexiLastTransaction extends Component {
    static template = "pos_nexi_payment.NexiLastTransaction";
    static props = {};

    setup() {
        this.store = useState(nexiStore);
        this.state = useState({
            lastTransaction: null
        });
    }

    get hasLastTransaction() {
        return this.state.lastTransaction !== null || 
               this.store.currentTransaction !== null;
    }

    get transaction() {
        return this.state.lastTransaction || this.store.currentTransaction;
    }

    get isSuccess() {
        return this.transaction?.isSuccessful();
    }

    get formattedAmount() {
        if (this.transaction) {
            return this.env.utils.formatCurrency(this.transaction.amount);
        }
        return "";
    }

    get transactionTime() {
        if (this.transaction?.create_date) {
            return new Date(this.transaction.create_date).toLocaleString();
        }
        return "";
    }
}

/**
 * Payment Method Item Extension
 * Adds NEXI-specific info to payment method buttons
 */
export class NexiPaymentMethodItem extends Component {
    static template = "pos_nexi_payment.NexiPaymentMethodItem";
    static props = ["paymentMethod"];

    setup() {
        this.store = useState(nexiStore);
    }

    get isNexiMethod() {
        return this.props.paymentMethod.use_payment_terminal === 'nexi';
    }

    get terminalStatus() {
        if (!this.isNexiMethod) return null;
        
        const available = this.store.getAvailableTerminals();
        if (available.length === 0) {
            return {
                available: false,
                class: 'nexi-method-unavailable',
                message: _t("No terminal available")
            };
        }

        return {
            available: true,
            class: 'nexi-method-available',
            message: _t("%s terminals available", available.length)
        };
    }
}
