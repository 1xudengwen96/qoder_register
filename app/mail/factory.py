"""邮件源工厂：按配置组装三个 provider，交给聚合器。"""

from __future__ import annotations

from pathlib import Path

from app.config import get, to_plain
from app.mail.aggregator import MailAggregator
from app.mail.base import MailProvider
from app.mail.hotmail import HotmailProvider
from app.mail.icloud import ICloudRelayProvider
from app.mail.outlook import OutlookTagProvider


def taken_addresses(db) -> set[str]:
    """已用过 / 已退役 / 已分配（pending）的地址集合，防止重复取到。

    POOL 状态的地址还在池里可用，不算 taken。
    """
    from app.models import MailIdentity as MailRow, MailStatus

    s = db.get_session()
    try:
        return {
            row.address.lower()
            for row in s.query(MailRow)
            .filter(MailRow.status != MailStatus.POOL).all()
        }
    finally:
        s.close()


def build_mail_layer(cfg, db) -> MailAggregator:
    taken = taken_addresses(db)
    providers: dict[str, MailProvider] = {}

    # 路线 D：手工维护的 Outlook 清单（data/emails.txt），优先级最高
    of_cfg = get(cfg, "mail.outlook_file", "") or ""
    if not of_cfg:
        try:
            from app import secrets

            of_cfg = secrets.get("mail.outlook_file", "") or ""
        except Exception:  # noqa: BLE001
            of_cfg = ""
    if of_cfg and Path(of_cfg).exists():
        from app.mail.outlook_file import OutlookFileProvider

        providers["outlook_file"] = OutlookFileProvider(
            of_cfg,
            taken_addresses=taken,
            imap_proxy=(get(cfg, "mail.imap_proxy", "") or None),
        )

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
        from app.mail.relay_qq import ICloudRelayQQProvider, RelayMatchRules

        agg = to_plain(get(ic_cfg, "aggregator_imap", {}) or {})
        rules_raw = to_plain(get(ic_cfg, "match", {}) or {})
        providers["icloud_relay"] = ICloudRelayQQProvider(
            relay_addresses_file=ic_cfg.relay_addresses_file,
            imap_host=str(agg.get("host") or "imap.qq.com"),
            imap_port=int(agg.get("port") or 993),
            imap_user=str(agg.get("user") or ""),
            imap_password=str(agg.get("password") or ""),
            folder=str(agg.get("folder") or "INBOX"),
            imap_id=str(agg.get("imap_id") or ""),
            quota_total=int(get(ic_cfg, "quota_total", 500)),
            taken_addresses=taken,
            rules=RelayMatchRules.from_config(rules_raw
                                             if isinstance(rules_raw, dict) else {}),
        )

    # mail.provider：明确指定只用哪个邮件源（如 icloud_relay）；留空＝按 strategy 自动。
    # 指定的源若没构建出来（配置缺失/文件不存在）直接报错，不允许静默回落。
    only = str(get(cfg, "mail.provider", "") or "").strip()
    if only:
        if only not in providers:
            raise ValueError(
                f"mail.provider={only!r} 未配置或不可用，可用源: "
                f"{sorted(providers) or '无'}")
        providers = {only: providers[only]}

    strategy = get(cfg, "mail.strategy", "weighted")
    weights_raw = to_plain(get(cfg, "mail.weights", {}) or {})
    weights = {k: int(v) for k, v in weights_raw.items()} if isinstance(weights_raw, dict) else {}
    strict = to_plain(get(cfg, "mail.strict_priority_order", None)) or list(providers)
    return MailAggregator(providers, strategy=strategy, weights=weights, strict_order=list(strict))
