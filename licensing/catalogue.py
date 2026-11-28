"""The first products and tiers (agreed 25 Nov 2026), loaded by migration 0002. Later changes go in the admin.

Safety Compliance counts clients, business units and sites together as `org_units`.
All paid tiers bill annually in USD; Tier 1 is free.
"""

PRODUCTS = [
    {
        "code": "pi-safety",
        "name": "Safety Compliance Insight",
        "limit_names": ["org_units", "resources"],
        "tiers": [
            ("safety-t1", "Tier 1", {"org_units": 5, "resources": 10}, "0", True),
            ("safety-t2", "Tier 2", {"org_units": 15, "resources": 25}, "198", False),
            ("safety-t3", "Tier 3", {"org_units": 50, "resources": 75}, "396", False),
            ("safety-t4", "Tier 4", {"org_units": 500, "resources": 750}, "792", False),
        ],
    },
    {
        "code": "pi-cashflow",
        "name": "PI-Cashflow Management",
        "limit_names": ["users", "cashflow_lines"],
        "tiers": [
            ("cashflow-t1", "Tier 1", {"users": 1, "cashflow_lines": 50}, "0", True),
            ("cashflow-t2", "Tier 2", {"users": 3, "cashflow_lines": 200}, "110", False),
            ("cashflow-t3", "Tier 3", {"users": 5, "cashflow_lines": 400}, "220", False),
            ("cashflow-t4", "Tier 4", {"users": 25, "cashflow_lines": 2000}, "440", False),
        ],
    },
]


def load(Product, Tier):
    for spec in PRODUCTS:
        product, _ = Product.objects.update_or_create(
            code=spec["code"], defaults={"name": spec["name"], "limit_names": spec["limit_names"]}
        )
        for order, (code, name, limits, price, free) in enumerate(spec["tiers"], start=1):
            Tier.objects.update_or_create(
                code=code,
                defaults={
                    "product": product,
                    "name": name,
                    "order": order,
                    "limits": limits,
                    "price_usd": price,
                    "interval": "year",
                    "is_free": free,
                },
            )
