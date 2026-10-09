import math

import pytest

import scores as S


# ---------------------------------------------------------------- baselines

def test_baseline_skips_missing_and_needs_min_n():
    assert S.baseline([1, None, 2]) is None
    b = S.baseline([10, 12, None, 14, 10, 12, 14, 10, 12])
    assert b.n == 8 and b.mean == pytest.approx(11.75)


def test_zscore_uses_sd_floor_and_handles_missing():
    b = S.Baseline(60.0, 0.1, 20)
    assert S.zscore(61, b, min_sd=1.0) == pytest.approx(1.0)
    assert S.zscore(None, b, 1.0) is None
    assert S.zscore(61, None, 1.0) is None


def test_percentile_edges():
    assert S.percentile([], 0.5) is None
    assert S.percentile([5], 0.9) == 5
    assert S.percentile([1, 2, 3, 4], 0.5) == pytest.approx(2.5)


# ---------------------------------------------------------------- recovery

HRV_B = S.Baseline(80.0, 10.0, 28)
RHR_B = S.Baseline(60.0, 2.0, 28)


def test_recovery_requires_overnight_hrv_and_rhr():
    r = S.recovery(None, HRV_B, 60, RHR_B, 90)
    assert r.score is None and r.reason == "no overnight HRV" and r.band is None
    assert S.recovery(80, HRV_B, None, RHR_B).reason == "no resting HR"
    assert S.recovery(80, None, 60, RHR_B).reason == "under 7 days of baseline"


def test_recovery_at_baseline_is_neutral_yellow():
    r = S.recovery(80, HRV_B, 60, RHR_B, S.SLEEP_Z_CENTER)
    assert r.score == round(S.logistic(0)) and r.band == "yellow"
    assert 50 <= r.score <= 60


def test_recovery_direction_and_inverted_rhr():
    good = S.recovery(100, HRV_B, 55, RHR_B, 100).score
    bad = S.recovery(60, HRV_B, 66, RHR_B, 50).score
    assert good >= 67 and bad <= 33
    lower_rhr = S.recovery(80, HRV_B, 57, RHR_B, 85).score
    higher_rhr = S.recovery(80, HRV_B, 63, RHR_B, 85).score
    assert lower_rhr > higher_rhr


def test_recovery_without_sleep_reweights_and_lists_contributions():
    r = S.recovery(90, HRV_B, 58, RHR_B, None)
    labels = [c.label for c in r.contributions]
    assert labels == ["HRV", "Resting HR"] and r.score is not None
    assert all(c.points > 0 for c in r.contributions)


def test_recovery_penalties_only_beyond_free_zone():
    base = S.recovery(80, HRV_B, 60, RHR_B, 85, resp=16.0, resp_base=S.Baseline(16, 0.3, 28)).score
    off = S.recovery(80, HRV_B, 60, RHR_B, 85, resp=17.5, resp_base=S.Baseline(16, 0.3, 28)).score
    skin = S.recovery(80, HRV_B, 60, RHR_B, 85, skin_dev_c=1.5, skin_sd_c=0.5).score
    assert off < base and skin < base
    small = S.recovery(80, HRV_B, 60, RHR_B, 85, skin_dev_c=0.4, skin_sd_c=0.5)
    assert next(c for c in small.contributions if c.label == "Skin temp").points == 0


@pytest.mark.parametrize("score,band", [(100, "green"), (67, "green"), (66, "yellow"), (34, "yellow"), (33, "red"),
                                        (0, "red"), (None, None)])
def test_recovery_bands(score, band):
    assert S.recovery_band(score) == band


def test_recovery_clamps_extreme_z():
    r = S.recovery(500, HRV_B, 20, RHR_B, 100)
    assert 0 <= r.score <= 100


# ---------------------------------------------------------------- strain

def test_trimp_from_zones_and_unknowns():
    assert S.trimp_from_zones({}) is None
    assert S.trimp_from_zones({"bogus": 30}) is None
    t = S.trimp_from_zones({"CARDIO": 30, "FAT_BURN": None})
    assert t == pytest.approx(30 * 0.72 * 0.64 * math.exp(1.92 * 0.72))


def test_trimp_from_samples_caps_gaps_and_needs_hr_bounds():
    pts = [(0, 150), (5, 150), (10, 150), (3600, 150)]     # the hour-long dropout is credited 60 s only
    t = S.trimp_from_samples(pts, 60, 190)
    per_min = S.trimp_minutes(1, (150 - 60) / 130)
    assert t == pytest.approx(per_min * (10 + 60) / 60)
    assert S.trimp_from_samples(pts, None, 190) is None
    assert S.trimp_from_samples(pts, 60, 50) is None
    assert S.trimp_from_samples([], 60, 190) is None


def test_strain_scale_is_log_monotonic_and_capped():
    assert S.strain(None) is None
    assert S.strain(0) == 0
    vals = [S.strain(t) for t in (10, 50, 100, 200, 400)]
    assert vals == sorted(vals)
    assert (vals[1] - vals[0]) > (vals[4] - vals[3]) / 2     # diminishing returns
    assert S.strain(S.STRAIN_CAP_TRIMP) == 21.0 and S.strain(10_000) == 21.0
    assert S.strain(-5) == 0


@pytest.mark.parametrize("rec,target", [(90, (14, 18)), (67, (14, 18)), (50, (10, 14)), (33, (4, 10)), (None, None)])
def test_strain_target(rec, target):
    assert S.strain_target(rec) == target


def test_zone_calibration_from_thresholds():
    th = {"light": (30, 111), "moderate": (112, 139), "vigorous": (140, 174), "peak": (175, 220)}
    z = S.zone_hrr_from_thresholds(th, 56, 189)
    assert z["moderate"] == pytest.approx(((112 + 139) / 2 - 56) / 133, abs=1e-3)
    assert z["peak"] == pytest.approx(((175 + 189) / 2 - 56) / 133, abs=1e-3)   # capped at HRmax
    assert z["FAT_BURN"] == z["moderate"] and z["CARDIO"] == z["vigorous"]
    assert z["light"] < z["moderate"] < z["vigorous"] < z["peak"]
    assert S.zone_hrr_from_thresholds(th, None, 189) == S.ZONE_HRR


# ---------------------------------------------------------------- sleep

def test_baseline_sleep_need_calibration_and_default():
    assert S.baseline_sleep_need([400, 420]) == (S.NEED_DEFAULT_MIN, False)
    need, ok = S.baseline_sleep_need([300] * 10)
    assert ok and need == S.NEED_MIN
    need, _ = S.baseline_sleep_need([600] * 10)
    assert need == S.NEED_MAX


def test_sleep_debt_weights_and_missing():
    assert S.sleep_debt([None, None, None], 480) == 0
    assert S.sleep_debt([480, 480, 480], 480) == 0
    assert S.sleep_debt([240, None, None], 480) == 240
    assert S.sleep_debt([240, 480, 480], 480) == pytest.approx(0.5 * 240)


def test_sleep_need_components():
    n = S.sleep_need(480, 15.0, 100, 30)
    assert n.strain_adj == 20 and n.debt_adj == 50 and n.nap_credit == 30
    assert n.total == 480 + 20 + 50 - 30
    assert S.sleep_need(480, None, 0, 0).total == 480
    assert S.sleep_need(480, 21, 1000, 0).debt_adj == S.DEBT_ADJ_CAP


def test_sleep_performance_and_efficiency():
    assert S.sleep_performance(None, 480) is None
    assert S.sleep_performance(240, 480) == 50
    assert S.sleep_performance(600, 480) == 100
    assert S.efficiency(450, 500) == 90
    assert S.efficiency(450, None) is None


def test_consistency_wraps_midnight_and_needs_three_nights():
    assert S.consistency([0, 10], [480, 490]) is None
    assert S.consistency([1430, 10, 0], [480, 480, 480]) > 90       # 11:50 pm, 12:10 am, midnight are close
    assert S.consistency([0, 0, 0], [480, 480, 480]) == 100
    assert S.consistency([1320, 120, 240], [360, 600, 720]) < 30


# ---------------------------------------------------------------- load, stress, body

def test_acwr_zones_and_insufficient_data():
    assert S.acwr([None] * 28).ratio is None
    assert S.acwr([100.0] * 28).zone == "sweet spot"
    spike = S.acwr([50.0] * 21 + [200.0] * 7)
    assert spike.zone == "high" and spike.ratio > 1.5
    assert S.acwr([0.0] * 28).reason == "no chronic load yet"
    assert S.acwr([100.0] * 21 + [40.0] * 7).zone == "low"


def test_impact_spikes():
    assert S.impact_spikes([60, 60, 60, 60, 120]) == [False, False, False, False, True]
    assert S.impact_spikes([10, 25]) == [False, False]           # under the floor
    assert S.impact_spikes([None, 100, None]) == [False, False, False]


def test_stress_level():
    assert S.stress_level([70] * 5, 60, 190) is None
    assert S.stress_level([60] * 30, 60, 190) == 0
    assert S.stress_level([200] * 30, 60, 190) == 3
    assert S.stress_level([70] * 30, None, 190) is None


def test_weight_trend():
    assert S.weight_trend([]).reason == "needs 2+ weigh-ins"
    assert S.weight_trend([(0, 70), (5, 71)]).reason == "weigh-ins span under 2 weeks"
    t = S.weight_trend([(0, 70.0), (28, 71.0)])          # 0.25 kg/week = 0.35 % of 71 kg
    assert t.kg_per_week == pytest.approx(0.25, abs=0.01) and t.status == "on pace"
    assert S.weight_trend([(0, 70.0), (14, 71.0)]).status == "above pace"
    assert S.weight_trend([(0, 70.0), (28, 70.0)]).status == "below pace"


# ---------------------------------------------------------------- workouts & streaks

def test_dedupe_prefers_specific_type_and_user_started():
    a = {"start": 0, "end": 3600, "type": "WORKOUT", "method": "ACTIVELY_MEASURED"}
    b = {"start": 60, "end": 3500, "type": "STRENGTH_TRAINING", "method": "PASSIVELY_MEASURED"}
    c = {"start": 7200, "end": 9000, "type": "RUNNING", "method": "PASSIVELY_MEASURED"}
    kept = S.dedupe_sessions([a, b, c])
    assert [k["type"] for k in kept] == ["STRENGTH_TRAINING", "RUNNING"]
    small_overlap = {"start": 3000, "end": 9000, "type": "SOCCER"}
    assert len(S.dedupe_sessions([a, small_overlap])) == 2


def test_union_minutes():
    assert S.union_minutes([]) == 0
    assert S.union_minutes([(0, 600), (300, 900), (1200, 1260)]) == pytest.approx(16)


def test_streak():
    assert S.streak([True, True, False, True, True, True]) == S.Streak(3, 3)
    assert S.streak([True, True, None, True]).current == 1
    assert S.streak([True, True, False], today_partial=True).current == 2
    assert S.streak([]) == S.Streak(0, 0)


def test_weight_pace_edges_are_not_off_pace():
    # 0.24 %/wk vs a 0.25 floor is "at the low edge", not "below pace"
    t = S.weight_trend([(0, 63.0), (58, 64.3)])
    assert t.status == "at the low edge"
    assert S.weight_trend([(0, 70.0), (28, 70.0)]).status == "below pace"


def test_impact_limit_and_bedtime():
    assert S.impact_limit([100, 100, 200, 200, 50]) == round(1.3 * 150)
    assert S.impact_limit([None, None]) is None
    assert S.asleep_by(8 * 60 + 30, 9 * 60) == 23 * 60 + 30          # 9 h before 8:30 am → 11:30 pm


def test_hr_zones_and_dominant_zone():
    assert [S.hr_zone(b, 200) for b in (90, 100, 125, 145, 165, 185)] == [0, 1, 2, 3, 4, 5]
    pts = [(i * 10.0, 150.0) for i in range(30)] + [(300 + i * 10.0, 110.0) for i in range(6)]
    zm = S.zone_minutes(pts, 200)
    assert S.dominant_zone(zm) == 3 and abs(sum(zm.values()) - 350 / 60) < 0.1
    assert S.zone_minutes([], 200) is None and S.dominant_zone(None) is None


def test_split_muscles():
    assert S.split_muscles("push (chest/tri)") == ["chest", "shoulders", "triceps"]
    assert S.split_muscles("pull (back/bi)") == ["lats", "biceps", "shoulders"]
    assert set(S.split_muscles("legs/abs")[:4]) == {"abs", "quads", "hamstrings", "glutes"}   # primaries first
    assert S.split_muscles("legs/abs")[-1] == "calves"
    assert S.split_muscles("arms") == ["biceps", "triceps"]
    assert S.split_targets("push") == {"chest": 1.0, "shoulders": 0.5, "triceps": 0.5}
    assert S.split_targets(None) == {}


# ---------------------------------------------------------------- sleep score

def test_sleep_score_blends_four_components():
    sc = S.sleep_score(100, 90, 80, 10)                 # stress 10 % → component 90
    expect = 0.5 * 100 + 0.2 * 90 + 0.15 * 80 + 0.15 * 90
    assert sc.score == round(expect) and sc.band == "optimal"
    assert set(sc.components) == {"sufficiency", "efficiency", "consistency", "stress"}
    assert sum(c["weight"] for c in sc.components.values()) == pytest.approx(1.0, abs=0.01)


def test_sleep_score_bands_and_caps():
    assert S.sleep_score(120, 100, 100, 0).score == 100           # sufficiency is capped at 100
    assert S.sleep_score_band(90) == "optimal" and S.sleep_score_band(89) == "sufficient"
    assert S.sleep_score_band(70) == "sufficient" and S.sleep_score_band(69) == "poor"
    assert S.sleep_score(50, 70, 30, 40).band == "poor"


def test_sleep_score_missing_parts_reweight_and_no_night_means_no_score():
    sc = S.sleep_score(90, 90, None, None)
    assert sc.score == 90 and sc.components["consistency"]["weight"] == 0
    none = S.sleep_score(None, 90, 90, 5)
    assert none.score is None and none.reason == "no night recorded"


def test_sleep_stress_pct():
    base = [float(v) for v in range(40, 140)]            # 100 readings, 20th percentile ≈ 59.8
    assert S.sleep_stress_pct([50, 55, 100, 110, 120, 130], base) == pytest.approx(33.3, abs=0.1)
    assert S.sleep_stress_pct([100] * 10, base) == 0
    assert S.sleep_stress_pct([50] * 3, base) is None      # under 30 minutes of readings
    assert S.sleep_stress_pct([50] * 10, base[:20]) is None  # no usable baseline yet
