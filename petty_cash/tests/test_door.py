"""The door (``core/auth.py``), check by check, as Flask's ``tests/test_petty_cash_api_door.py``
pins its ``guard.py``: who gets in, what every refusal says, and that CORS and ``no-store`` are on
every answer - a bare 401 reads to the browser as a CORS failure."""

from __future__ import annotations

import pytest

from petty_cash.tests.conftest import WEB, headers, make_entity, make_user, url
from shared_models.enums import EntityRole, EntityStatus, SystemRole

pytestmark = pytest.mark.django_db


def _get(client, entity_id, hdrs):
    return client.get(url(entity_id), headers=hdrs)


def test_a_member_reads_the_company_and_who_they_are(client, shop):
    resp = _get(client, shop.entity.id, headers(shop.owner.id))

    assert resp.status_code == 200, resp.content
    assert resp.json() == {
        "company": {
            "id": shop.entity.id,
            "name": "Acme Shop",
            "role": "admin",
            "role_label": "Admin",
            "currency": {"code": "HKD", "decimal_places": 2},
        },
        "person": {
            "id": shop.owner.id,
            "first_name": "Olive",
            "last_name": "Owner",
            "name": "Olive Owner",
            "initials": "OO",
            "email": "owner@test.com",
        },
    }
    assert resp["Cache-Control"] == "no-store"
    assert resp["Access-Control-Allow-Origin"] == WEB


@pytest.mark.parametrize(
    "token_kwargs",
    [
        {"module": "onboarding"},  # an onboarding token is the onboarding API's alone
        {"module": None},
        {"secret": "not-the-key-flask-and-this-service-share"},
        {"expires_in": -120},  # past the 60 s leeway
    ],
)
def test_a_token_that_is_not_a_petty_cash_module_token_is_unauthorized(client, shop, token_kwargs):
    resp = _get(client, shop.entity.id, headers(shop.owner.id, **token_kwargs))

    assert resp.status_code == 401
    assert resp.json() == {"error": "Unauthorized"}
    assert resp["Access-Control-Allow-Origin"] == WEB  # the app must SEE the 401 to re-authenticate
    assert resp["Cache-Control"] == "no-store"


def test_no_token_is_unauthorized(client, shop):
    resp = client.get(url(shop.entity.id), headers={"Origin": WEB})

    assert resp.status_code == 401 and resp.json() == {"error": "Unauthorized"}


def test_an_unknown_or_unapproved_person_is_unauthorized(client, shop):
    pending = make_user("pending@test.com", approved=False)

    assert _get(client, shop.entity.id, headers("1d0a5bb5-0000-4000-8000-000000000000")).status_code == 401
    assert _get(client, shop.entity.id, headers(pending.id)).status_code == 401


def test_a_company_they_do_not_belong_to_is_refused(client, shop):
    stranger = make_user("stranger@test.com")

    resp = _get(client, shop.entity.id, headers(stranger.id))

    assert resp.status_code == 403
    assert resp.json() == {"error": "You don't have access to that company."}


def test_the_token_names_no_company_the_address_does(client, shop):
    """A token minted for the person's own company opens no other company."""
    other_owner = make_user("other@test.com")
    other = make_entity(other_owner, name="Other Co")

    resp = _get(client, other.id, headers(shop.owner.id, entity_id=other.id))

    assert resp.status_code == 403


@pytest.mark.parametrize("bad", ["not-a-uuid", "00000000-0000-4000-8000-000000000000"])
def test_an_address_that_is_no_company_is_refused(client, shop, bad):
    resp = _get(client, bad, headers(shop.owner.id))

    assert resp.status_code == 403
    assert resp.json() == {"error": "You don't have access to that company."}


def test_a_company_still_onboarding_is_a_409(client, db):
    owner = make_user()
    entity = make_entity(owner, status=EntityStatus.ONBOARDING)

    resp = _get(client, entity.id, headers(owner.id))

    assert resp.status_code == 409
    assert resp.json() == {
        "error": "This company is still setting up. Finish setting it up to use Petty Cash."
    }


def test_petty_cash_switched_off_is_refused(client, db):
    owner = make_user()
    entity = make_entity(owner, petty_cash=False)

    resp = _get(client, entity.id, headers(owner.id))

    assert resp.status_code == 403
    assert resp.json() == {
        "error": "Petty Cash isn't switched on for this company. An admin can turn it on in the company's modules."
    }


def test_a_superuser_enters_any_company_with_no_role(client, shop):
    boss = make_user("boss@test.com", system_role=SystemRole.SUPERADMIN)

    resp = _get(client, shop.entity.id, headers(boss.id))

    assert resp.status_code == 200
    assert resp.json()["company"]["role"] is None and resp.json()["company"]["role_label"] is None


def test_a_cashier_reads_their_role(client, shop):
    clerk = make_user("clerk@test.com", first_name="", last_name="")
    from shared_models.models import UserEntity

    UserEntity.objects.create(user_id=clerk.id, entity_id=shop.entity.id, role=EntityRole.CASHIER, approved=True)

    body = _get(client, shop.entity.id, headers(clerk.id)).json()

    assert body["company"]["role"] == "cashier" and body["company"]["role_label"] == "Cashier"
    assert body["person"]["name"] == "clerk@test.com" and body["person"]["initials"] == "?"


def test_the_preflight_is_answered_for_the_app_without_a_token(client, shop):
    resp = client.options(
        url(shop.entity.id, "/reports/opening"),
        headers={"Origin": WEB, "Access-Control-Request-Method": "PUT",
                 "Access-Control-Request-Headers": "authorization,content-type"},
    )

    assert resp.status_code == 200
    assert resp["Access-Control-Allow-Origin"] == WEB
    assert "PUT" in resp["Access-Control-Allow-Methods"]


def test_another_origin_gets_no_cors(client, shop):
    resp = client.get(url(shop.entity.id), headers={**headers(shop.owner.id), "Origin": "https://evil.test"})

    assert "Access-Control-Allow-Origin" not in resp


def test_an_unknown_path_is_a_json_404(client, shop):
    resp = client.get(url(shop.entity.id, "/no-such-thing"), headers=headers(shop.owner.id))

    assert resp.status_code == 404
    assert resp.json() == {"error": "Not Found"}


def test_health_needs_no_token(client):
    resp = client.get("/healthz")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "service": "minty-petty-cash-api"}
