"""Access key and TLS trust for the AWS deployment.

On AWS every request must carry x-steadfast-key (checked by the load balancer), and the
ALB's HTTPS certificate is self-signed, so clients trust exactly that certificate
(STEADFAST_CA_BUNDLE, written by the deploy scripts) instead of skipping verification.
Locally neither is set and nothing changes.
"""

import os

ACCESS_KEY_HEADER = "x-steadfast-key"


def auth_headers() -> dict[str, str]:
    key = os.environ.get("STEADFAST_API_KEY")
    return {ACCESS_KEY_HEADER: key} if key else {}


def tls_verify() -> str | bool:
    """Path to the pinned certificate if set, else the default CA store."""
    return os.environ.get("STEADFAST_CA_BUNDLE") or True
