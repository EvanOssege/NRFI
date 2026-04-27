#!/usr/bin/env python3
import importlib.util
import unittest
from pathlib import Path


def _load_dashboard_module():
    repo_root = Path(__file__).resolve().parents[1]
    dash_path = repo_root / "scripts" / "dashboard.py"
    spec = importlib.util.spec_from_file_location("dashboard", dash_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


dashboard = _load_dashboard_module()


def _game(
    matchup,
    game_time="2026-04-27T19:00:00Z",
    ml_pick="AWAY",
    ml_conf="TOSS-UP",
    ml_edge=0.0,
    ml_units=0.0,
    total_lean="PUSH",
    total_conf="TOSS-UP",
    projected_total=4.5,
    primary_line=4.5,
    total_units=0.0,
    nrfi_tier="FADE",
    nrfi_score=35.0,
):
    return {
        "matchup": matchup,
        "game_time": game_time,
        "f5": {
            "ml": {
                "pick": ml_pick,
                "confidence": ml_conf,
                "edge": ml_edge,
                "units": ml_units,
            },
            "total": {
                "lean": total_lean,
                "confidence": total_conf,
                "projected_total": projected_total,
                "primary_line": primary_line,
                "units": total_units,
            },
        },
        "nrfi": {
            "tier": nrfi_tier,
            "score": nrfi_score,
        },
    }


class DashboardStrongestBetSortTests(unittest.TestCase):
    def test_confidence_priority_across_markets(self):
        strong_total = _game(
            "AAA @ BBB",
            ml_pick="PICK",
            total_lean="UNDER",
            total_conf="STRONG",
            projected_total=2.6,
            primary_line=4.5,
            total_units=2.0,
            nrfi_tier="FADE",
            nrfi_score=40.0,
        )
        moderate_ml = _game(
            "CCC @ DDD",
            ml_conf="MODERATE",
            ml_edge=15.0,
            ml_units=5.0,
            total_lean="PUSH",
            nrfi_tier="FADE",
            nrfi_score=40.0,
        )
        ordered = sorted([moderate_ml, strong_total], key=dashboard.strongest_bet_sort_key)
        self.assertEqual(ordered[0]["matchup"], "AAA @ BBB")

    def test_units_break_tie_within_same_confidence(self):
        high_units = _game(
            "EEE @ FFF",
            ml_conf="STRONG",
            ml_edge=8.0,
            ml_units=4.5,
            total_lean="PUSH",
            nrfi_tier="FADE",
            nrfi_score=41.0,
        )
        low_units = _game(
            "GGG @ HHH",
            ml_conf="STRONG",
            ml_edge=20.0,
            ml_units=2.0,
            total_lean="PUSH",
            nrfi_tier="FADE",
            nrfi_score=41.0,
        )
        ordered = sorted([low_units, high_units], key=dashboard.strongest_bet_sort_key)
        self.assertEqual(ordered[0]["matchup"], "EEE @ FFF")

    def test_magnitude_breaks_tie_after_confidence_and_units(self):
        bigger_mag = _game(
            "III @ JJJ",
            ml_conf="LEAN",
            ml_edge=9.2,
            ml_units=1.0,
            total_lean="PUSH",
            nrfi_tier="FADE",
            nrfi_score=42.0,
        )
        smaller_mag = _game(
            "KKK @ LLL",
            ml_conf="LEAN",
            ml_edge=6.5,
            ml_units=1.0,
            total_lean="PUSH",
            nrfi_tier="FADE",
            nrfi_score=42.0,
        )
        ordered = sorted([smaller_mag, bigger_mag], key=dashboard.strongest_bet_sort_key)
        self.assertEqual(ordered[0]["matchup"], "III @ JJJ")

    def test_yrfi_signal_is_included(self):
        yrfi_strong = _game(
            "MMM @ NNN",
            ml_pick="PICK",
            total_lean="PUSH",
            nrfi_tier="FADE",
            nrfi_score=15.0,
        )
        f5_lean = _game(
            "OOO @ PPP",
            ml_conf="LEAN",
            ml_edge=5.0,
            ml_units=1.0,
            total_lean="PUSH",
            nrfi_tier="FADE",
            nrfi_score=44.0,
        )
        ordered = sorted([f5_lean, yrfi_strong], key=dashboard.strongest_bet_sort_key)
        self.assertEqual(ordered[0]["matchup"], "MMM @ NNN")

    def test_no_actionable_edges_falls_back_to_time_then_matchup(self):
        late = _game(
            "ZZZ @ AAA",
            game_time="2026-04-27T21:00:00Z",
            ml_pick="PICK",
            ml_conf="TOSS-UP",
            total_lean="PUSH",
            nrfi_tier="TOSS-UP",
            nrfi_score=35.0,
        )
        early_b = _game(
            "BBB @ CCC",
            game_time="2026-04-27T18:00:00Z",
            ml_pick="PICK",
            ml_conf="TOSS-UP",
            total_lean="PUSH",
            nrfi_tier="TOSS-UP",
            nrfi_score=35.0,
        )
        early_a = _game(
            "AAA @ BBB",
            game_time="2026-04-27T18:00:00Z",
            ml_pick="PICK",
            ml_conf="TOSS-UP",
            total_lean="PUSH",
            nrfi_tier="TOSS-UP",
            nrfi_score=35.0,
        )
        ordered = sorted([late, early_b, early_a], key=dashboard.strongest_bet_sort_key)
        self.assertEqual([g["matchup"] for g in ordered], ["AAA @ BBB", "BBB @ CCC", "ZZZ @ AAA"])


if __name__ == "__main__":
    unittest.main()
