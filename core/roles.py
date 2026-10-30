"""How a company role reads on screen - this service's own behaviour over minty-db's enum."""

from minty_db.enums import EntityRole

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
