"""The database's enums, as this service must write and compare them.

Same members as Minty's ``blueprints/shared/enums.py`` (the register is item 18 of
``docs/schema/01_schema_rebased.sql`` in the Minty repo); a test there reads the schema
file and fails when any copy drifts. Use these instead of string literals — the columns
are Postgres enum types and reject any other word.

Petty Cash's own enums (``sale_type``, ``report_status``, ``publish_status``,
``discrepancy_type``, ``cash_type``, ``expense_attachment_role``) are the words Flask's
``blueprints/shared/enums.py`` spells; the ported services compare against these members.
"""

from django.db import models


class SystemRole(models.TextChoices):
    """``system_role`` — the global role of a person. ``superadmin`` was ``superuser``
    in the code until 2026-09; old JWTs may still carry that word (see LEGACY_SUPERUSER)."""

    NORMAL = "normal"
    ADMIN = "admin"
    SUPERADMIN = "superadmin"


#: The pre-rename spelling of the system super admin; JWTs minted before C1 carry it.
#: Not ``admin``: that is a distinct, lesser member of the enum now.
LEGACY_SUPERUSER = frozenset({"superuser"})


def is_superadmin(system_role) -> bool:
    """Is this ``system_role`` value (a row's or a JWT claim's) the system super admin?"""
    word = (str(system_role or "")).strip().lower()
    return word == SystemRole.SUPERADMIN or word in LEGACY_SUPERUSER


class EntityStatus(models.TextChoices):
    """``entity_status`` - in the wizard, or live with / without a Xero org linked."""

    ONBOARDING = "onboarding"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"


class EntityRole(models.TextChoices):
    """``entity_role`` — a person's role within one company (``user_entity.role``)."""

    ENTITY_BASE = "entity_base"
    CASHIER = "cashier"
    SHOP_MANAGER = "shop_manager"
    ACCOUNTANT = "accountant"
    ADMIN = "admin"
    SUPER_ADMIN = "super_admin"


#: How each role reads on screen - Flask's ``entity_role_label`` (the company payload's
#: ``role_label``).
ENTITY_ROLE_LABELS = {
    EntityRole.ENTITY_BASE: "Member",
    EntityRole.CASHIER: "Cashier",
    EntityRole.SHOP_MANAGER: "Shop Manager",
    EntityRole.ACCOUNTANT: "Accountant",
    EntityRole.ADMIN: "Admin",
    EntityRole.SUPER_ADMIN: "Super Admin",
}


def entity_role_label(role: str | None) -> str | None:
    """The on-screen name of a stored role, or None for no role or one this app does not know."""
    try:
        return ENTITY_ROLE_LABELS[EntityRole(role)] if role else None
    except ValueError:
        return None


class ModuleCode(models.TextChoices):
    """``module_code`` - Minty's two modules (schema item 20). PAYMENT_REQUEST was BILL."""

    PETTY_CASH = "PETTY_CASH"
    PAYMENT_REQUEST = "PAYMENT_REQUEST"


class SaleType(models.TextChoices):
    """``sale_type`` - the bucket a sales method belongs to (``sale_info.type``). The Cash
    method itself is the ``other`` catalogue row whose ``value_name`` is ``cash_sales``."""

    ELECTRONIC = "electronic"
    DELIVERY = "delivery"
    OTHER = "other"


class ReportStatus(models.TextChoices):
    """``report_status`` - where a day's report stands."""

    DRAFT = "draft"
    SUBMITTED = "submitted"
    PUBLISHED = "published"
    VOID = "void"


class PublishStatus(models.TextChoices):
    """``publish_status`` - the Xero publish job's own state on ``report.publishing_status``."""

    UNPUBLISHED = "unpublished"
    PUBLISHING = "publishing"
    COMPLETED = "completed"
    FAILED = "failed"


class DiscrepancyType(models.TextChoices):
    """``discrepancy_type`` - the cash count against the expected cash."""

    NONE = "none"
    OVER = "over"
    SHORT = "short"


class CashType(models.TextChoices):
    """``cash_type`` - a denomination is a coin or a note."""

    COIN = "coin"
    NOTE = "note"


class ExpenseAttachmentRole(models.TextChoices):
    """``expense_attachment_role`` - what a file on an expense line is."""

    RECEIPT = "receipt"
    INVOICE = "invoice"
    OTHER = "other"
