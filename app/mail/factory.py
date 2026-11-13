"""邮件源工厂：按配置组装三个 provider，交给聚合器。"""

from __future__ import annotations

from app.config import get, to_plain
from app.mail.aggregator import MailAggregator
from app.mail.base import MailProvider
from app.mail.hotmail import HotmailProvider
from app.mail.icloud import ICloudRelayProvider
from app.mail.outlook import OutlookTagProvider


def taken_addresses(db) -> set[str]:
    """已用过 / 已退役的地址集合，防止重复取到。"""
    from app.models import MailIdentity as MailRow

    s = db.get_session()
    try:
        return {row.address.lower() for row in s.query(MailRow).all()}
    finally:
        s.close()


def build_mail_layer(cfg, db) -> MailAggregator:
    taken = taken_addresses(db)
    providers: dict[str, MailProvider] = {}

    ot_cfg = get(cfg, "mail.outlook_tag")
    if ot_cfg and get(ot_cfg, "main_accounts"):
        providers["outlook_tag"] = OutlookTagProvider(
            main_accounts=to_plain(ot_cfg.main_accounts),
            tag_prefix=get(ot_cfg, "tag_prefix", "q"),
            max_tags_per_account=int(get(ot_cfg, "max_tags_per_account", 8)),
            taken_addresses=taken,
        )

    hm_cfg = get(cfg, "mail.hotmail")
    if hm_cfg and get(hm_cfg, "credentials_file"):
        providers["hotmail"] = HotmailProvider(
            credentials_file=hm_cfg.credentials_file,
            taken_addresses=taken,
        )

    ic_cfg = get(cfg, "mail.icloud_relay")
    if ic_cfg and get(ic_cfg, "relay_addresses_file"):
        providers["icloud_relay"] = ICloudRelayProvider(
            relay_addresses_file=ic_cfg.relay_addresses_file,
            aggregator_imap={
                "user": get(ic_cfg, "aggregator_imap.user", ""),
                "password": get(ic_cfg, "aggregator_imap.password", ""),
                "host": get(ic_cfg, "aggregator_imap.host", "imap.mail.me.com"),
                "port": int(get(ic_cfg, "aggregator_imap.port", 993)),
            },
            quota_total=int(get(ic_cfg, "quota_total", 100)),
            taken_addresses=taken,
        )

    strategy = get(cfg, "mail.strategy", "weighted")
    weights_raw = to_plain(get(cfg, "mail.weights", {}) or {})
    weights = {k: int(v) for k, v in weights_raw.items()} if isinstance(weights_raw, dict) else {}
    strict = to_plain(get(cfg, "mail.strict_priority_order", None)) or list(providers)
    return MailAggregator(providers, strategy=strategy, weights=weights, strict_order=list(strict))
