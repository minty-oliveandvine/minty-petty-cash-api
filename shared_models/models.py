"""Django mirrors of the tables Minty's schema owns (``docs/schema/01_schema_rebased.sql``).

EVERY MODEL HERE IS ``managed = False`` AND THIS REPO SHIPS NO MIGRATIONS. Minty is the
owner-of-record for all DDL; a new column is a change there first (ask before any schema
change), then a hand-edit here. The Postgres test mode builds the real schema and reads these
models against it, so a mirror column the schema lacks fails a test.

WHAT THIS SERVICE WRITES - the day's report and what hangs off it: ``report``, ``report_sale``,
``report_expense``, ``attachment``, ``report_expense_attachment``, ``report_cash_count``,
``report_history``, and on submit ``entity_cash_detail`` (the company's cash in stock); a new
supplier adds a ``xero_contact_sync`` row. Everything else here is read-only - identity, the
company and its settings are Flask's.

IDS ARE STRINGS - every uuid column is a ``MintyUUIDField``: the database type is ``uuid``, the
Python value the hyphenated lowercase ``str`` Flask's ``MintyUuid`` gives. Foreign keys are kept
as those plain columns, as Flask's models declare them; the services join explicitly.

MONEY is ``NUMERIC(14,2)``: a ``Decimal`` here, a JSON float rounded to cents on the way out
(Flask's ``Money`` column + ``cents()``) - see ``petty_cash/money.py``.

DEFAULTS FLASK APPLIES IN PYTHON are set explicitly on every write by the services (rule 0 of
the onboarding-api port): the schema's server defaults are declared with ``db_default``.
"""

from django.db import models
from django.db.models.functions import Now

from shared_models.enums import (
    CashType,
    DiscrepancyType,
    EntityRole,
    EntityStatus,
    ExpenseAttachmentRole,
    ModuleCode,
    PublishStatus,
    ReportStatus,
    SaleType,
    SystemRole,
)
from shared_models.fields import CharNField, MintyUUIDField, PgEnumField, new_id


def _money(**kwargs):
    return models.DecimalField(max_digits=14, decimal_places=2, **kwargs)


# ---------------------------------------------------------------------------
# People and companies - read-only
# ---------------------------------------------------------------------------


class User(models.Model):
    """Read-only. The door resolves a token's ``user_id`` to an approved person; a report's
    ``created_by`` and the dashboard's submitter name read it."""

    id = MintyUUIDField(primary_key=True)
    email = models.CharField(max_length=254, unique=True, null=True, blank=True)
    password = models.CharField(max_length=255)  # NOT NULL in the schema; never read here
    first_name = models.CharField(max_length=150, default="")
    last_name = models.CharField(max_length=150, default="")
    username = models.CharField(max_length=150, unique=True)
    system_role = PgEnumField("system_role", choices=SystemRole.choices, default=SystemRole.NORMAL)
    is_active = models.BooleanField(default=True)
    approved = models.BooleanField(default=False)
    xero_email = models.CharField(max_length=100, null=True, blank=True)
    created_at = models.DateTimeField(db_default=Now())
    updated_at = models.DateTimeField(db_default=Now())

    class Meta:
        managed = False
        db_table = "user"


class Entity(models.Model):
    """Read-only. The company."""

    id = MintyUUIDField(primary_key=True)
    name = models.CharField(max_length=100)
    country_code = CharNField(max_length=2, null=True, blank=True)
    currency_id = MintyUUIDField(null=True, blank=True)
    xero_org_id = models.CharField(max_length=36, null=True, blank=True)
    status = PgEnumField("entity_status", choices=EntityStatus.choices, default=EntityStatus.ONBOARDING)
    connected_by_user_id = MintyUUIDField(null=True, blank=True)
    created_at = models.DateTimeField(db_default=Now())
    updated_at = models.DateTimeField(db_default=Now())

    class Meta:
        managed = False
        db_table = "entities"


class UserEntity(models.Model):
    """Read-only. Membership and the role within one company (composite key)."""

    pk = models.CompositePrimaryKey("user_id", "entity_id")
    user_id = MintyUUIDField()
    entity_id = MintyUUIDField()
    role = PgEnumField("entity_role", choices=EntityRole.choices)
    approved = models.BooleanField(default=True)
    created_at = models.DateTimeField(db_default=Now())
    joined_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = "user_entity"


class CountryInfo(models.Model):
    """Read-only. A company's currency falls back to its country's."""

    country_code = CharNField(max_length=2, primary_key=True)
    alpha3_code = CharNField(max_length=3)
    country_name_en = models.CharField(max_length=100)
    currency_id = MintyUUIDField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    display_order = models.IntegerField(default=999)

    class Meta:
        managed = False
        db_table = "country_info"


class CurrencyInfo(models.Model):
    """Read-only. Every amount on the pages is written with ``currency_code``."""

    id = MintyUUIDField(primary_key=True)
    currency_code = CharNField(max_length=3, unique=True)
    currency_name = models.CharField(max_length=100)
    symbol = models.CharField(max_length=10, default="")
    decimal_places = models.SmallIntegerField(default=2)
    is_active = models.BooleanField(default=True)

    class Meta:
        managed = False
        db_table = "currency_info"


class EntityFunction(models.Model):
    """Read-only. The module catalogue."""

    id = MintyUUIDField(primary_key=True)
    function_code = PgEnumField("module_code", choices=ModuleCode.choices, unique=True)
    function_name = models.CharField(max_length=150)
    description = models.TextField(default="", db_default="")
    is_active = models.BooleanField(default=True)
    display_order = models.IntegerField(default=999, db_default=999)

    class Meta:
        managed = False
        db_table = "entity_function"


class EntityFunctionMap(models.Model):
    """Read-only here. Per-company module on/off - what the door's module check reads."""

    pk = models.CompositePrimaryKey("entity_id", "entity_function_id")
    entity_id = MintyUUIDField()
    entity_function_id = MintyUUIDField()
    is_enabled = models.BooleanField(default=False)
    enabled_at = models.DateTimeField(null=True, blank=True)
    disabled_at = models.DateTimeField(null=True, blank=True)
    created_by = MintyUUIDField(null=True, blank=True)
    created_at = models.DateTimeField(db_default=Now())
    updated_at = models.DateTimeField(db_default=Now())

    class Meta:
        managed = False
        db_table = "entity_function_map"


# ---------------------------------------------------------------------------
# The company's Xero lists and Petty Cash settings - read-only, but a new supplier adds a row
# ---------------------------------------------------------------------------


class AccountInfo(models.Model):
    """Read-only. The company's synced Xero accounts."""

    id = MintyUUIDField(primary_key=True, default=new_id)
    entity_id = MintyUUIDField()
    type = models.CharField(max_length=50)
    name = models.CharField(max_length=80)
    xero_account_id = models.CharField(max_length=36, null=True, blank=True)
    xero_code = models.CharField(max_length=50, null=True, blank=True)
    status = models.CharField(max_length=50, null=True, blank=True)
    class_type = models.CharField(max_length=50, null=True, blank=True)
    bank_account_number = models.CharField(max_length=50, null=True, blank=True)
    bank_account_type = models.CharField(max_length=50, null=True, blank=True)
    description = models.CharField(max_length=255, null=True, blank=True)
    created_at = models.DateTimeField(db_default=Now())
    updated_at = models.DateTimeField(db_default=Now())

    class Meta:
        managed = False
        db_table = "account_info"


class EntityAccountXero(models.Model):
    """Read-only. Which accounts are switched on for Petty Cash (``is_active``)."""

    id = MintyUUIDField(primary_key=True, default=new_id)
    account_id = MintyUUIDField()
    type = models.CharField(max_length=50, null=True, blank=True)
    xero_org_id = models.CharField(max_length=36, null=True, blank=True)
    xero_account_id = models.CharField(max_length=36, null=True, blank=True)
    name = models.CharField(max_length=80, null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        managed = False
        db_table = "entity_account_xero"


class XeroContactSync(models.Model):
    """The company's synced Xero contacts - the only suppliers a line can carry. A new supplier
    (``POST /suppliers``) adds a row."""

    id = MintyUUIDField(primary_key=True, default=new_id)
    entity_id = MintyUUIDField(null=True, blank=True)
    xero_contact_id = models.CharField(max_length=36)
    xero_org_id = models.CharField(max_length=36, null=True, blank=True)
    name = models.CharField(max_length=150)
    category = models.CharField(max_length=50, null=True, blank=True)
    created_at = models.DateTimeField(db_default=Now())
    updated_at = models.DateTimeField(db_default=Now())

    class Meta:
        managed = False
        db_table = "xero_contact_sync"


class EntityPettycashSettings(models.Model):
    """Read-only. Onboarding's opening balance and start date (a first report reads them) and
    the Xero roles the dashboard checks."""

    entity_id = MintyUUIDField(primary_key=True)
    opening_balance = _money(null=True, blank=True)
    start_date = models.DateField(null=True, blank=True)
    pettycash_account_id = MintyUUIDField(null=True, blank=True)
    bank_account_id = MintyUUIDField(null=True, blank=True)
    cash_sale_account_id = MintyUUIDField(null=True, blank=True)
    discrepancy_bank_account_id = MintyUUIDField(null=True, blank=True)
    discrepancy_account_id = MintyUUIDField(null=True, blank=True)
    director_account_id = MintyUUIDField(null=True, blank=True)
    cash_sale_contact_id = MintyUUIDField(null=True, blank=True)
    director_contact_id = MintyUUIDField(null=True, blank=True)
    discrepancy_contact_id = MintyUUIDField(null=True, blank=True)
    created_at = models.DateTimeField(db_default=Now())
    updated_at = models.DateTimeField(db_default=Now())

    class Meta:
        managed = False
        db_table = "entity_pettycash_settings"


# ---------------------------------------------------------------------------
# Catalogues - read-only (the sales methods and the cash denominations)
# ---------------------------------------------------------------------------


class SaleInfo(models.Model):
    """Read-only. The global sales-method catalogue. Cash is the ``other`` row whose
    ``value_name`` is ``cash_sales``."""

    CASH_VALUE_NAME = "cash_sales"

    id = MintyUUIDField(primary_key=True, default=new_id)
    type = PgEnumField("sale_type", choices=SaleType.choices, default=SaleType.OTHER)
    sale_name = models.CharField(max_length=80, unique=True)
    value_name = models.CharField(max_length=80, null=True, blank=True)
    display_order = models.IntegerField(null=True, blank=True)
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(db_default=Now())
    updated_at = models.DateTimeField(db_default=Now())

    class Meta:
        managed = False
        db_table = "sale_info"


class EntitySaleSetting(models.Model):
    """Read-only. The company's link to a catalogue method: on/off and its order."""

    pk = models.CompositePrimaryKey("entity_id", "sale_id")
    entity_id = MintyUUIDField()
    sale_id = MintyUUIDField()
    is_active = models.BooleanField(default=True)
    display_order = models.IntegerField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = "entity_sale_setting"


class CashInfo(models.Model):
    """Read-only - NEVER written: Flask's denomination resolver set a company's order onto these
    shared rows in memory and a later commit could write it back; this port never does."""

    id = MintyUUIDField(primary_key=True, default=new_id)
    currency_id = MintyUUIDField()
    type = PgEnumField("cash_type", choices=CashType.choices, null=True, blank=True)
    cash_value = models.DecimalField(max_digits=12, decimal_places=2)
    cash_name = models.CharField(max_length=20, null=True, blank=True)
    description = models.TextField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    display_order = models.IntegerField(default=0)

    class Meta:
        managed = False
        db_table = "cash_info"


class EntityCashSetting(models.Model):
    """Read-only. A company's own on/off and order for a denomination."""

    pk = models.CompositePrimaryKey("entity_id", "cash_id")
    entity_id = MintyUUIDField()
    cash_id = MintyUUIDField()
    is_active = models.BooleanField(default=True)
    display_order = models.IntegerField(null=True, blank=True)
    created_at = models.DateTimeField(db_default=Now())
    updated_at = models.DateTimeField(db_default=Now())

    class Meta:
        managed = False
        db_table = "entity_cash_setting"


class EntityCashDetail(models.Model):
    """The company's cash in stock: rebuilt from the latest submitted report's count on submit."""

    pk = models.CompositePrimaryKey("entity_id", "cash_id")
    entity_id = MintyUUIDField()
    cash_id = MintyUUIDField()
    cash_type = PgEnumField("cash_type", choices=CashType.choices, null=True, blank=True)
    cash_instock = _money(null=True, blank=True)
    description = models.TextField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = "entity_cash_detail"


# ---------------------------------------------------------------------------
# The day's report - what this service writes
# ---------------------------------------------------------------------------


class Report(models.Model):
    """One company-day. Flask's synonyms, spelled out: ``company`` = ``entity_id``,
    ``cash_sales`` = ``cashsale_total``, ``expenses`` = ``expense_total``, ``withdrawal_type`` =
    ``cash_addition_type``, ``date`` = ``created_at``, ``uploaded_by`` = the creator's username
    (written as ``created_by``)."""

    id = MintyUUIDField(primary_key=True, default=new_id)
    entity_id = MintyUUIDField()
    transaction_date = models.DateField()
    next_transaction_date = models.DateField(null=True, blank=True)
    status = PgEnumField("report_status", choices=ReportStatus.choices, default=ReportStatus.DRAFT)
    publishing_status = PgEnumField(
        "publish_status", choices=PublishStatus.choices, default=PublishStatus.UNPUBLISHED
    )
    opening_balance = _money(null=True, blank=True)
    cash_addition = _money(null=True, blank=True)
    adjusted_opening_balance = _money(null=True, blank=True)
    cashsale_total = _money(null=True, blank=True)
    nocashsale_total = _money(null=True, blank=True)
    total_sales = _money(null=True, blank=True)
    expense_total = _money(null=True, blank=True)
    bank_deposit = _money(null=True, blank=True)
    closing_balance = _money(null=True, blank=True)
    safe_box_balance = _money(null=True, blank=True)
    discrepancy_amount = _money(null=True, blank=True)
    discrepancy_type = PgEnumField(
        "discrepancy_type", choices=DiscrepancyType.choices, default=DiscrepancyType.NONE
    )
    discrepancy_reason = models.CharField(max_length=300, null=True, blank=True)
    current_section = models.CharField(max_length=20, null=True, blank=True)
    completed_sections = models.JSONField(null=True, blank=True)
    xero_integrated = models.BooleanField(null=True, blank=True)
    cash_addition_type = models.CharField(max_length=20, null=True, blank=True)
    created_by = MintyUUIDField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(db_default=Now())
    updated_at = models.DateTimeField(db_default=Now())

    class Meta:
        managed = False
        db_table = "report"
        constraints = [
            models.UniqueConstraint(fields=["entity_id", "transaction_date"], name="report_entity_date_key"),
        ]


class ReportSale(models.Model):
    """One method's amount on a report (Flask's ``ReportSaleDetail``)."""

    id = MintyUUIDField(primary_key=True, default=new_id)
    report_id = MintyUUIDField()
    sale_id = MintyUUIDField()
    amount = _money(default=0)
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        managed = False
        db_table = "report_sale"
        constraints = [models.UniqueConstraint(fields=["report_id", "sale_id"], name="report_sale_uq")]


class ReportExpense(models.Model):
    """One expense line. ``account_id`` / ``contact_id`` are the company's ``account_info`` /
    ``xero_contact_sync`` ROW ids - the Xero ids are read through them."""

    id = MintyUUIDField(primary_key=True, default=new_id)
    report_id = MintyUUIDField()
    account_id = MintyUUIDField(null=True, blank=True)
    contact_id = MintyUUIDField(null=True, blank=True)
    item = models.CharField(max_length=150, null=True, blank=True)
    amount = _money(default=0)
    remarks = models.CharField(max_length=300, null=True, blank=True)
    description = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        managed = False
        db_table = "report_expense"


class Attachment(models.Model):
    """A stored file (shared with minty-payment-request-api's bills). Receipts live in B2 at
    ``file_path``."""

    id = MintyUUIDField(primary_key=True, default=new_id)
    original_name = models.CharField(max_length=255)
    stored_name = models.CharField(max_length=255)
    file_path = models.TextField()
    file_extension = models.CharField(max_length=20, default="")
    mime_type = models.CharField(max_length=100)
    file_size = models.BigIntegerField(null=True, blank=True)
    storage_provider = models.CharField(max_length=50, default="s3")
    checksum_sha256 = models.CharField(max_length=128, default="")
    uploaded_by = MintyUUIDField(null=True, blank=True)
    is_deleted = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(db_default=Now())
    updated_at = models.DateTimeField(db_default=Now())

    class Meta:
        managed = False
        db_table = "attachment"


class ReportExpenseAttachment(models.Model):
    """The link between a line and a receipt, in order."""

    id = MintyUUIDField(primary_key=True, default=new_id)
    report_expense_id = MintyUUIDField()
    attachment_id = MintyUUIDField()
    attachment_role = PgEnumField(
        "expense_attachment_role", choices=ExpenseAttachmentRole.choices, default=ExpenseAttachmentRole.RECEIPT
    )
    sort_order = models.IntegerField(default=0)
    xero_attachment_id = models.CharField(max_length=36, default="")
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        managed = False
        db_table = "report_expense_attachment"


class ReportCashCount(models.Model):
    """One denomination's count on a report (a zero count stores no row)."""

    id = MintyUUIDField(primary_key=True, default=new_id)
    report_id = MintyUUIDField()
    cash_id = MintyUUIDField()
    quantity = models.IntegerField(default=0)
    cash_value = models.DecimalField(max_digits=12, decimal_places=2)
    created_at = models.DateTimeField(db_default=Now())
    updated_at = models.DateTimeField(db_default=Now())

    class Meta:
        managed = False
        db_table = "report_cash_count"
        constraints = [models.UniqueConstraint(fields=["report_id", "cash_id"], name="report_cash_count_uq")]


class ReportHistory(models.Model):
    """What changed on a report, by whom."""

    id = MintyUUIDField(primary_key=True, default=new_id)
    report_id = MintyUUIDField()
    user_id = MintyUUIDField(null=True, blank=True)
    action = models.CharField(max_length=50)
    field_changed = models.CharField(max_length=255, null=True, blank=True)
    old_value = models.TextField(null=True, blank=True)
    new_value = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        managed = False
        db_table = "report_history"


# ---------------------------------------------------------------------------
# A report's way to the outside: its share link and Xero's record of it
# ---------------------------------------------------------------------------


class ShareLink(models.Model):
    """A read-only link to one company-day, served by Flask (``/Minty_Report/<path_segment>/``).
    One row per company-day; ``path_segment`` ends in a 32-character secret."""

    id = MintyUUIDField(primary_key=True, default=new_id)
    entity_id = MintyUUIDField()
    path_segment = models.CharField(max_length=255, unique=True)
    token = models.TextField()
    transaction_date = models.DateField()
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        managed = False
        db_table = "share_link"


class XeroReportSync(models.Model):
    """Flask's publish record (``xero/services/publish_record.py``): which Xero objects a report's
    publish created. Deleted with its report - nothing here writes it."""

    id = MintyUUIDField(primary_key=True, default=new_id)
    report_id = MintyUUIDField()
    sync_status = models.CharField(max_length=20, null=True, blank=True)
    reported_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    xero_response_text = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(db_default=Now())
    updated_at = models.DateTimeField(db_default=Now())

    class Meta:
        managed = False
        db_table = "xero_report_sync"


class XeroBankTransfer(models.Model):
    """DELETE-ONLY mirror (only the columns a report's delete names): a deposit Xero recorded."""

    id = MintyUUIDField(primary_key=True, default=new_id)
    sync_report_id = MintyUUIDField()

    class Meta:
        managed = False
        db_table = "xero_bank_transfer"


class XeroBankTransaction(models.Model):
    """DELETE-ONLY mirror (only the columns a report's delete names): an expense Xero recorded."""

    id = MintyUUIDField(primary_key=True, default=new_id)
    sync_report_id = MintyUUIDField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = "xero_bank_transaction"
