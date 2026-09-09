#!/usr/bin/env python3
"""Focused unit tests for the local shared Geo Resolver."""

from geo_resolver import classify_text, extract_coordinates, extract_location_candidates, normalize_text


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    check(classify_text("Working Fire", "location.address") is None, "incident label must not be an address")
    check(classify_text("Mva / Traffic Alert", "location.address") is None, "BNN incident type must be rejected")
    check(classify_text("BK-2935", "location.address") == "reference", "box code should remain a reference")
    check(classify_text("Eastern Pkwy & Utica Ave", "details.anywhere") == "intersection", "intersection detection failed")
    check(classify_text("65-30 79th Pl", "raw.unexpected") == "address", "hyphenated NYC address failed")
    check(classify_text("100 Ferry Way", "message") == "address", "NJ address failed")

    payload = {
        "source": "BNN",
        "title": "Working Fire",
        "location": {"address": "2nd Alarm"},
        "raw": {
            "miscellaneous_field": "Eastern Pkwy & Utica Ave",
            "another_field": "540 Ocean Ave",
        },
        "message": "Fire Department Activity",
        "borough": "Brooklyn",
    }
    candidates = extract_location_candidates(payload)
    values = {(item.normalized, item.kind) for item in candidates}
    check((normalize_text("Eastern Pkwy & Utica Ave"), "intersection") in values, "nested intersection missing")
    check((normalize_text("540 Ocean Ave"), "address") in values, "nested address missing")
    check(all(item.normalized != "WORKING FIRE" for item in candidates), "noise leaked into candidates")

    address = next(item for item in candidates if item.kind == "address")
    check(address.source_path == "raw.another_field", "arbitrary source path was not preserved")

    coordinate = extract_coordinates({"deep": {"unknown": {"lng": -74.021, "lat": 40.77}}})
    check(coordinate == (-74.021, 40.77, "deep.unknown"), "nested coordinates failed")
    check(extract_coordinates({"longitude": 500, "latitude": 40}) is None, "invalid coordinate accepted")

    print("CMOS GEO RESOLVER UNIT TESTS: PASS")


if __name__ == "__main__":
    main()
