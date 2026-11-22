ARG DEBIAN_BASE
FROM ${DEBIAN_BASE}

# Prepare the test OS before boot. No network will be available during dpkg installation.
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates dbus init-system-helpers iproute2 libgcc-s1 python3 systemd-sysv \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /usr/sbin/policy-rc.d \
    && truncate -s 0 /etc/machine-id \
    && ln -sf /etc/machine-id /var/lib/dbus/machine-id
