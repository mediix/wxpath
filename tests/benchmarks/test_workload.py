"""Workload schema: parse, hash, alias resolution."""

from pathlib import Path

import pytest

from benchmarks.workload import (
    KINDS,
    Workload,
    bundled_dir,
    list_bundled,
    load,
    resolve_alias,
    to_dict,
)


def test_bundled_workloads_parse():
    rows = list_bundled()
    names = {r.name for r in rows}
    # Five bundled workloads from the v0 plan.
    assert {'single_host_docs_python',
            'thousand_host_broad',
            'cc_sample_100k',
            'anti_bot_tiers',
            'freshness_high_churn'} <= names


def test_single_host_is_runnable_others_are_stubs():
    rows = {r.name: r for r in list_bundled()}
    assert rows['single_host_docs_python'].is_runnable_in_v0() is True
    assert rows['thousand_host_broad'].is_runnable_in_v0() is False
    assert rows['cc_sample_100k'].is_runnable_in_v0() is False
    assert rows['anti_bot_tiers'].is_runnable_in_v0() is False
    assert rows['freshness_high_churn'].is_runnable_in_v0() is False


def test_canonical_hash_is_stable():
    wl_a = load(bundled_dir() / 'single_host.yaml')
    wl_b = load(bundled_dir() / 'single_host.yaml')
    assert wl_a.canonical_hash() == wl_b.canonical_hash()


def test_canonical_hash_changes_with_field():
    wl = load(bundled_dir() / 'single_host.yaml')
    h0 = wl.canonical_hash()
    wl.max_depth += 1
    assert wl.canonical_hash() != h0


def test_to_dict_round_trip(tmp_path: Path):
    wl = load(bundled_dir() / 'single_host.yaml')
    out = tmp_path / 'wl.yaml'
    import yaml
    out.write_text(yaml.safe_dump(to_dict(wl), sort_keys=False))
    wl2 = load(out)
    # Re-parsed workload's canonical hash matches (source_path differs but is not hashed).
    assert wl.canonical_hash() == wl2.canonical_hash()


def test_resolve_alias_bundled_name():
    p = resolve_alias('single_host', bundled_dir())
    assert p.exists()
    assert p.name == 'single_host.yaml'


def test_resolve_alias_path(tmp_path: Path):
    target = tmp_path / 'custom.yaml'
    target.write_text("""
name: custom
kind: single_host
runnable: false
description: stub
seeds: {type: inline, urls: []}
path_expr: "//a"
max_depth: 1
limits: {max_urls: 10, wall_clock_seconds: 5}
settings_overrides: {}
tags: []
""")
    p = resolve_alias(str(target), bundled_dir())
    assert p == target.resolve()


def test_invalid_kind_rejected(tmp_path: Path):
    bad = tmp_path / 'bad.yaml'
    bad.write_text("""
name: bad
kind: not_a_kind
runnable: true
description: x
seeds: {type: inline, urls: []}
path_expr: ""
max_depth: 1
limits: {}
settings_overrides: {}
tags: []
""")
    with pytest.raises(ValueError):
        load(bad)


def test_kinds_constant_matches_workloads():
    bundled_kinds = {r.kind for r in list_bundled()}
    assert bundled_kinds <= KINDS
