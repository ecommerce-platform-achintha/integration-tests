"""Stage 3, product-service: categories (admin write, public read, two levels, slug rules)."""
from __future__ import annotations

import pytest

from tests import data, factories

pytestmark = pytest.mark.stage3

ADMIN = "/api/admin/categories"
PUBLIC = "/api/public/categories"


def category_payload(**overrides) -> dict:
    slug = data.slug("itcat")
    body = {"slug": slug, "name": f"IT Category {slug}", "description": "integration-tests"}
    body.update(overrides)
    return body


def public_slugs(client) -> set[str]:
    return {c["slug"] for c in client.get(PUBLIC, token=None).expect(200).body}


def test_create_parent_and_child(admin, client):
    parent = admin.post(ADMIN, category_payload()).expect(201).body
    assert parent["active"] is True and parent["parentSlug"] is None
    child = admin.post(ADMIN, category_payload(parentSlug=parent["slug"])).expect(201).body
    assert child["parentSlug"] == parent["slug"]
    assert {parent["slug"], child["slug"]} <= public_slugs(client)
    assert admin.get(f"{ADMIN}/{child['slug']}").expect(200).body == child
    admin_slugs = {c["slug"] for c in admin.get(ADMIN).expect(200).body}
    assert {parent["slug"], child["slug"]} <= admin_slugs

    # Two levels at most: a child cannot be a parent, and a category cannot be its own parent
    admin.post(ADMIN, category_payload(parentSlug=child["slug"])).expect(400, code="VALIDATION_FAILED")
    admin.put(f"{ADMIN}/{parent['slug']}", {**category_payload(slug=parent["slug"], name=parent["name"]),
                                             "parentSlug": parent["slug"]}).expect(400, code="VALIDATION_FAILED")
    # A category with children cannot get a parent itself
    other = admin.post(ADMIN, category_payload()).expect(201).body
    admin.put(f"{ADMIN}/{parent['slug']}", {"slug": parent["slug"], "name": parent["name"],
                                             "parentSlug": other["slug"]}).expect(400, code="VALIDATION_FAILED")
    admin.post(ADMIN, category_payload(parentSlug="no-such-parent")).expect(400, code="VALIDATION_FAILED")


def test_slug_and_name_are_unique(admin):
    first = admin.post(ADMIN, category_payload()).expect(201).body
    admin.post(ADMIN, category_payload(slug=first["slug"])).expect(409, code="CATEGORY_SLUG_TAKEN")
    admin.post(ADMIN, category_payload(name=first["name"].upper())).expect(409, code="CATEGORY_NAME_TAKEN")


@pytest.mark.parametrize("slug", ["Upper", "two--dashes", "-leading", "trailing-", "has space", "under_score", ""],
                         ids=["upper", "double-dash", "leading", "trailing", "space", "underscore", "blank"])
def test_slug_rules(admin, slug):
    admin.post(ADMIN, category_payload(slug=slug)).expect(400, code="VALIDATION_FAILED")


def test_update_renames_slug(admin):
    cat = admin.post(ADMIN, category_payload()).expect(201).body
    new_slug = data.slug("itcat-renamed")
    renamed = admin.put(f"{ADMIN}/{cat['slug']}", {"slug": new_slug, "name": cat["name"]}).expect(200).body
    assert renamed["slug"] == new_slug
    admin.get(f"{ADMIN}/{cat['slug']}").expect(404)
    admin.get(f"{ADMIN}/{new_slug}").expect(200)


def test_inactive_category_is_hidden_and_unusable(admin, client, merchant):
    parent = admin.post(ADMIN, category_payload()).expect(201).body
    child = admin.post(ADMIN, category_payload(parentSlug=parent["slug"])).expect(201).body
    admin.put(f"{ADMIN}/{parent['slug']}", {"slug": parent["slug"], "name": parent["name"], "active": False}
              ).expect(200)
    slugs = public_slugs(client)
    # Category.isUsable: an active child of an inactive parent is not usable either
    assert parent["slug"] not in slugs and child["slug"] not in slugs
    for slug in (parent["slug"], child["slug"]):
        merchant.post("/api/merchant/products", factories.simple_product_payload(slug)
                      ).expect(400, code="CATEGORY_NOT_AVAILABLE")
    merchant.post("/api/merchant/products", factories.simple_product_payload("no-such-category")
                  ).expect(400, code="CATEGORY_NOT_AVAILABLE")


def test_delete_only_unused_categories(admin, merchant):
    unused = admin.post(ADMIN, category_payload()).expect(201).body
    admin.delete(f"{ADMIN}/{unused['slug']}").expect(204)
    admin.get(f"{ADMIN}/{unused['slug']}").expect(404)

    with_child = admin.post(ADMIN, category_payload()).expect(201).body
    admin.post(ADMIN, category_payload(parentSlug=with_child["slug"])).expect(201)
    admin.delete(f"{ADMIN}/{with_child['slug']}").expect(409, code="CATEGORY_IN_USE")

    with_product = admin.post(ADMIN, category_payload()).expect(201).body
    factories.create_product(merchant, factories.simple_product_payload(with_product["slug"]))
    admin.delete(f"{ADMIN}/{with_product['slug']}").expect(409, code="CATEGORY_IN_USE")


def test_category_access_control(customer, merchant, assistant, client, category):
    client.get(PUBLIC, token=None).expect(200)
    for actor in (customer, merchant, assistant):
        actor.get(ADMIN).expect(403, code="ACCESS_DENIED")
        actor.post(ADMIN, category_payload()).expect(403, code="ACCESS_DENIED")
        actor.delete(f"{ADMIN}/{category['slug']}").expect(403, code="ACCESS_DENIED")
    client.post(ADMIN, category_payload(), token=None).expect(401)


@pytest.mark.parametrize("actor_name, path", [
    (None, "/api/public/categories"),
    ("admin", "/api/admin/categories"),
    ("super_admin", "/api/super-admin/settings"),
    ("merchant", "/api/merchant/owner/couriers"),
    ("merchant", "/api/merchant/owner/couriers/available"),
    ("merchant", "/api/merchant/owner/bank-accounts/banks"),
], ids=["public-categories", "admin-categories", "settings", "store-couriers", "couriers-available", "banks"])
def test_unpaged_list_endpoints_return_plain_arrays(request, client, actor_name, path):
    """Pins today's shape. These lists are not paged, against the project rule: FINDINGS N3-1."""
    if actor_name is None:
        body = client.get(path, token=None).expect(200).body
    else:
        body = request.getfixturevalue(actor_name).get(path).expect(200).body
    assert isinstance(body, list), body
