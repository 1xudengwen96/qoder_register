"""邮箱源适配层。"""

from app.mail.base import (
    MailIdentity,
    MailMessage,
    MailProvider,
    ProviderHealth,
    default_match,
)

__all__ = [
    "MailIdentity",
    "MailMessage",
    "MailProvider",
    "ProviderHealth",
    "default_match",
]
