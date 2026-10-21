"""Operating-system CA store settings for the executor's Modal images.

Modal's ``debian_slim`` base is a fixed Debian bookworm image whose
ca-certificates package (20230311) still trusts "emSign ECC Root CA - G3" and
Sectigo's "AAA Certificate Services", the roots above the compromised eMudhra
intermediate CAs. Modal caches that base by its definition, so it never picks
up a newer package on its own. Each image therefore installs ca-certificates
from Debian right after the base layer: ``apt_install`` runs ``apt-get update``
and then ``apt-get install``, which upgrades the installed package to the
newest version Debian publishes.

grpcio, which the Google Cloud and OTLP telemetry exporters use, verifies
servers against a CA list compiled into it, and every grpcio release through
1.84.0 still trusts those roots. Setting ``GRPC_DEFAULT_SSL_ROOTS_FILE_PATH``
makes it verify against the refreshed operating-system bundle instead.
"""

OPERATING_SYSTEM_CA_PACKAGE = "ca-certificates"
OPERATING_SYSTEM_CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"
GRPC_CA_BUNDLE_ENV = {"GRPC_DEFAULT_SSL_ROOTS_FILE_PATH": OPERATING_SYSTEM_CA_BUNDLE}
