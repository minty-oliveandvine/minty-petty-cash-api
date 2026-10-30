"""Is a module switched on for a company - the Django mirror of Flask's ``_is_module_enabled``
(``blueprints/entity/routes/modules.py``), copied from minty-payment-request-api.

EVERY UNKNOWN ANSWERS NO: no catalogue row, or no map row for the company, is "off". The map
row's ``is_enabled`` is the projection of the module's subscription that the subscription API
writes; the catalogue's ``is_active`` says a module is offered, never who may use it.
"""

from __future__ import annotations

from minty_db.models import EntityFunction, EntityFunctionMap


def is_module_enabled(entity_id: str, function_code: str) -> bool:
    if not entity_id or not function_code:
        return False
    catalog = EntityFunction.objects.filter(function_code=function_code).only("id").first()
    if catalog is None:
        return False
    mapping = (
        EntityFunctionMap.objects.filter(entity_id=entity_id, entity_function_id=catalog.id)
        .only("is_enabled")
        .first()
    )
    return bool(mapping is not None and mapping.is_enabled)
