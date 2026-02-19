FROM odoo:18.0

USER root

# Install additional Python dependencies for NEXI module
RUN pip3 install --no-cache-dir --break-system-packages \
    cryptography>=3.4.8 \
    requests>=2.25.1 \
    python-jose>=3.3.0

# Create directories for the module
RUN mkdir -p /mnt/extra-addons/pos_nexi_payment \
    && chown -R odoo:odoo /mnt/extra-addons

# Switch back to odoo user
USER odoo

# Expose Odoo services
EXPOSE 8069 8072

# Start Odoo
CMD ["odoo"]
