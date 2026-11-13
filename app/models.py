"""ORM 模型。表设计见 docs/architecture.md 第 5 章。"""

from __future__ import annotations

import datetime as dt
import enum
import json

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class MailProviderEnum(str, enum.Enum):
    OUTLOOK_TAG = "outlook_tag"
    HOTMAIL = "hotmail"
    ICLOUD_RELAY = "icloud_relay"


class MailStatus(str, enum.Enum):
    POOL = "pool"
    PENDING = "pending"
    USED = "used"
    DROPPED = "dropped"
    RETIRED = "retired"


class AccountState(str, enum.Enum):
    NEW = "new"
    REGISTERED = "registered"
    VERIFIED = "verified"
    CLAIMING = "claiming"
    CLAIMED = "claimed"
    STOCK = "stock"
    SOLD = "sold"
    DEAD = "dead"


class MailIdentity(Base):
    __tablename__ = "mail_identity"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[MailProviderEnum] = mapped_column(Enum(MailProviderEnum), nullable=False)
    address: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    parent_account: Mapped[str | None] = mapped_column(String(320))
    status: Mapped[MailStatus] = mapped_column(Enum(MailStatus), default=MailStatus.POOL, nullable=False)
    qoder_account_id: Mapped[int | None] = mapped_column(ForeignKey("account.id"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())
    used_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_mail_provider_status", "provider", "status"),)


class DeviceProfile(Base):
    __tablename__ = "device_profile"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    machine_id: Mapped[str] = mapped_column(String(128), nullable=False)
    telemetry_id: Mapped[str | None] = mapped_column(String(128))
    mac_address: Mapped[str | None] = mapped_column(String(32))
    volume_serial: Mapped[str | None] = mapped_column(String(64))
    reset_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())
    claim_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    account_id: Mapped[int | None] = mapped_column(Integer)


class Account(Base):
    __tablename__ = "account"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    password: Mapped[str | None] = mapped_column(String(256))
    username: Mapped[str | None] = mapped_column(String(128))
    state: Mapped[AccountState] = mapped_column(Enum(AccountState), default=AccountState.NEW, nullable=False)
    credits: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    plan: Mapped[str | None] = mapped_column(String(64))
    trial_expire_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    register_ip: Mapped[str | None] = mapped_column(String(64))
    device_profile_id: Mapped[int | None] = mapped_column(ForeignKey("device_profile.id"))
    token_json: Mapped[str | None] = mapped_column(Text)
    risk_flags: Mapped[str | None] = mapped_column(Text)  # JSON 数组字符串
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())
    claimed_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_account_state_expire", "state", "trial_expire_at"),
    )

    # ---- helpers -----------------------------------------------------------
    def add_flag(self, flag: str) -> None:
        flags = self.flag_list()
        if flag not in flags:
            flags.append(flag)
        self.risk_flags = json.dumps(flags, ensure_ascii=False)

    def flag_list(self) -> list[str]:
        if not self.risk_flags:
            return []
        try:
            return list(json.loads(self.risk_flags))
        except json.JSONDecodeError:
            return []


class EventLog(Base):
    __tablename__ = "event_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("account.id"))
    mail_identity_id: Mapped[int | None] = mapped_column(ForeignKey("mail_identity.id"))
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    level: Mapped[str] = mapped_column(String(16), default="info", nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[str | None] = mapped_column(Text)  # JSON 字符串
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (Index("ix_event_account_stage", "account_id", "stage"),)


class ProxyEntry(Base):
    __tablename__ = "proxy_pool"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    endpoint: Mapped[str] = mapped_column(String(256), nullable=False, unique=True)
    region: Mapped[str | None] = mapped_column(String(32))
    health: Mapped[str] = mapped_column(String(16), default="ok", nullable=False)
    last_used_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    use_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    fail_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (UniqueConstraint("endpoint", name="uq_proxy_endpoint"),)
