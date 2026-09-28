"""core/tsunami_speech.py の単体テスト（環境変数・Discord不要）。実行: pytest tests/"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.tsunami_speech import (  # noqa: E402
    AlertGate, AlertState, EventGate, build_alert_speech, build_coastal_observation_speech,
    build_offshore_observation_speech, build_tide_time_speech, classify_change,
    format_height_for_speech, format_time_for_speech, grade_from_kind_name, obs_grade_from_height,
    state_from_jma_forecast, state_from_p2p_tsunami,
)


def _jma(*items):
    """(名前, 区分名, 到達時刻, 高さ[, Condition]) から気象庁形式の最小データを作る。"""
    out = []
    for it in items:
        name, kind, arr, h = it[:4]
        cond = it[4] if len(it) > 4 else ""
        d = {"Area": {"Name": name}, "Category": {"Kind": {"Name": kind, "Code": "51"}}}
        if arr:
            d["FirstHeight"] = {"ArrivalTime": arr, **({"Condition": cond} if cond else {})}
        if h:
            d["MaxHeight"] = {"TsunamiHeight": h}
        out.append(d)
    return {"Body": {"Tsunami": {"Forecast": {"Item": out}}}}


T = "2026-04-20T17:00:00+09:00"
ALERT = _jma(("岩手県", "津波警報", T, "3", "ただちに津波来襲と予測"),
             ("北海道太平洋沿岸中部", "津波警報", "2026-04-20T17:30:00+09:00", "3"),
             ("青森県太平洋沿岸", "津波注意報", "2026-04-20T17:20:00+09:00", "1"),
             ("茨城県", "津波予報（若干の海面変動）", "", "<0.2"))


def test_grade_from_kind_name_major_first():
    assert grade_from_kind_name("大津波警報") == "MajorWarning"
    assert grade_from_kind_name("津波警報") == "Warning"
    assert grade_from_kind_name("津波予報（若干の海面変動）") == "Forecast"
    assert grade_from_kind_name("津波なし") is None
    assert grade_from_kind_name("津波警報", "00") is None


def test_jma_state_and_new_speech():
    st = state_from_jma_forecast(ALERT)
    assert st.top_grade == "Warning"
    assert st.areas_of("Warning") == ["岩手県", "北海道太平洋沿岸中部"]  # 到達が早い順
    text, pri = build_alert_speech(st, classify_change(None, st))
    assert pri == 1
    assert text.startswith("津波警報が発表されました。岩手県、北海道太平洋沿岸中部。")
    assert "予想される高さは最大3メートルです。" in text
    assert "岩手県ではただちに津波が来襲すると予測されています。" in text
    assert text.endswith("ただちに避難してください。")


def test_area_truncation():
    st = AlertState(areas={f"予報区{i}": "Watch" for i in range(5)})
    text, _ = build_alert_speech(st, classify_change(None, st))
    assert "等。" in text and text.count("予報区") == 3


def test_classify_changes():
    a = state_from_jma_forecast(ALERT)
    assert classify_change(a, a).kind == "none"
    watch_only = AlertState(areas={"岩手県": "Watch"})
    major = AlertState(areas={"岩手県": "MajorWarning"})
    assert classify_change(watch_only, a).kind == "escalation"
    assert classify_change(a, watch_only).kind == "downgrade"
    assert classify_change(a, AlertState()).kind == "cancel"
    assert classify_change(AlertState(), AlertState()).kind == "none"
    assert classify_change(a, major).kind == "escalation"
    more = state_from_jma_forecast(_jma(*[("宮城県", "津波警報", "", "2")])) 
    more.areas.update(a.areas)
    assert classify_change(a, more).kind == "added"
    height_only = state_from_jma_forecast(ALERT)
    height_only.heights["岩手県"] = "5"
    assert classify_change(a, height_only).kind == "none"


def test_forecast_only_addition_is_silent():
    a = AlertState(areas={"岩手県": "Warning"})
    b = AlertState(areas={"岩手県": "Warning", "茨城県": "Forecast"})
    assert classify_change(a, b).kind == "none"


def test_escalation_and_cancel_text():
    w = AlertState(areas={"岩手県": "Warning"}, heights={"岩手県": "3"})
    m = AlertState(areas={"岩手県": "MajorWarning"}, heights={"岩手県": "10m超"})
    text, pri = build_alert_speech(m, classify_change(w, m))
    assert text.startswith("津波警報が大津波警報に切り替わりました。") and pri == 1
    assert "10メートル超" in text
    text, _ = build_alert_speech(AlertState(), classify_change(w, AlertState()))
    assert text == "津波警報が解除されました。"
    text, _ = build_alert_speech(w, classify_change(m, w))
    assert text == "大津波警報は津波警報に切り替わりました。"


def test_p2p_state_matches_jma_signature():
    p2p = {"areas": [{"name": "岩手県", "grade": "Warning", "immediate": True,
                      "maxHeight": {"description": "3m"}},
                     {"name": "青森県太平洋沿岸", "grade": "Watch"}]}
    s = state_from_p2p_tsunami(p2p)
    assert s.areas == {"岩手県": "Warning", "青森県太平洋沿岸": "Watch"}
    assert s.immediate == {"岩手県"}
    assert state_from_p2p_tsunami({"cancelled": True}).empty


def test_gate_dedupes_across_sources():
    gate = AlertGate()
    p2p = state_from_p2p_tsunami({"areas": [{"name": "岩手県", "grade": "Warning"}]})
    jma = state_from_jma_forecast(_jma(("岩手県", "津波警報", T, "3")))
    d1 = gate.decide(p2p, now=1000)
    assert d1.speak and d1.ews and d1.change.kind == "new"
    d2 = gate.decide(jma, now=1030)          # 同じ状態 → 二重読み上げ・二重EWSなし
    assert not d2.speak and not d2.ews
    d3 = gate.decide(p2p, now=1040)
    assert not d3.speak


def test_gate_cross_source_name_mismatch_within_window():
    gate = AlertGate()
    gate.decide(state_from_p2p_tsunami({"areas": [{"name": "岩手県", "grade": "Warning"}]}), 1000)
    jma = state_from_jma_forecast(_jma(("岩手県（旧表記）", "津波警報", T, "3")))
    assert not gate.decide(jma, 1060).speak         # 90秒以内・同じ最上位区分 → 補助ルールで抑止
    assert not gate.decide(jma, 1500).speak         # 統合済みなので、その後も「追加」扱いにならない


def test_gate_escalation_rings_again_and_cancel_once():
    gate = AlertGate()
    w = AlertState(areas={"岩手県": "Warning"}, source="jma")
    m = AlertState(areas={"岩手県": "MajorWarning"}, source="jma")
    assert gate.decide(w, 0).ews
    assert not gate.decide(w, 100).speak
    d = gate.decide(m, 200)
    assert d.speak and d.ews and d.change.kind == "escalation"
    assert gate.decide(AlertState(source="p2p"), 300).speak
    assert not gate.decide(AlertState(source="jma"), 320).speak  # 解除の二重読み上げなし


def test_gate_state_expires():
    gate = AlertGate()
    w = AlertState(areas={"岩手県": "Warning"}, source="jma")
    gate.decide(w, 0)
    assert gate.decide(w, 7 * 3600).speak            # TTL経過後は新規扱い


def test_watch_new_no_ews():
    gate = AlertGate()
    d = gate.decide(AlertState(areas={"岩手県": "Watch"}, source="jma"), 0)
    assert d.speak and not d.ews and d.priority == 2


def test_height_and_time_formatting():
    assert format_height_for_speech("3") == "3メートル"
    assert format_height_for_speech("10m超") == "10メートル超"
    assert format_height_for_speech(">10") == "10メートル以上"
    assert format_height_for_speech("巨大") == "巨大"
    assert format_height_for_speech("<0.2") == ""
    assert format_height_for_speech("観測中") == ""
    assert format_time_for_speech("2026-04-20T17:00:00+09:00") == "17時00分頃"
    assert format_time_for_speech("17:30") == "17時30分頃"


def test_obs_grade_boundaries():
    g = obs_grade_from_height
    assert [g(x) for x in ("5", "4.9", "1", "0.99", "0.1", "0.05", "微弱", "観測中", "<0.2", "欠測", "")] == [
        "major_warning", "warning", "warning", "watch", "watch", "forecast",
        "forecast", "forecast", "forecast", "unknown", "unknown"]


def _obs(*stations):
    return {"Body": {"Tsunami": {"Observation": {"Item": [
        {"Area": {"Name": a}, "Station": [{"Name": n, "MaxHeight": mh}]} for a, n, mh in stations]}}}}


def test_coastal_observation_speech_picks_max_station():
    d = _obs(("青森県太平洋沿岸", "むつ小川原港", {"Condition": "観測中"}),
             ("岩手県", "宮古", {"TsunamiHeight": "0.4"}),
             ("岩手県", "大船渡", {"TsunamiHeight": "1.2"}))
    text, pri, tier = build_coastal_observation_speech(d)
    assert text == "津波観測に関する情報。岩手県の大船渡で、1.2メートルを観測しました。"
    assert pri == 1 and tier == 3
    text, pri, _ = build_coastal_observation_speech(_obs(("岩手県", "宮古", {"Condition": "微弱"})))
    assert text == "津波観測に関する情報。岩手県の宮古で、津波を観測しています。" and pri == 3
    assert build_coastal_observation_speech(_obs(("岩手県", "宮古", {}))) is None  # 欠測のみ


def test_offshore_speech_only_when_stations_exist():
    assert build_offshore_observation_speech(_obs(("", "浦河沖", {})))[1] == 2
    assert build_offshore_observation_speech({"Body": {"Tsunami": {"Observation": {"Item": []}}}}) is None


def test_event_gate_rise_mode():
    g = EventGate(mode="rise")
    assert g.decide("E1", 1, 0)          # 初回
    assert not g.decide("E1", 1, 10)     # 同階級
    assert g.decide("E1", 3, 20)         # 上昇（cooldown無関係）
    assert not g.decide("E1", 2, 30)     # 下降は読まない
    assert g.decide("E2", 1, 30)         # 別イベント


def test_event_gate_change_mode_cooldown():
    g = EventGate(mode="change", cooldown_sec=120)
    assert g.decide("E1", ("岩手県", "17:00"), 0)
    assert not g.decide("E1", ("岩手県", "17:00"), 500)      # 変化なし
    assert not g.decide("E1", ("岩手県", "16:50"), 60)       # cooldown中
    assert g.decide("E1", ("岩手県", "16:50"), 130)


def test_tide_time_speech():
    d = _jma(("北海道太平洋沿岸東部", "津波注意報", "2026-04-20T17:30:00+09:00", "1"),
             ("岩手県", "津波警報", "2026-04-20T17:00:00+09:00", "3", "ただちに津波来襲と予測"),
             ("茨城県", "津波予報（若干の海面変動）", "", "<0.2"))
    text, pri, val = build_tide_time_speech(d)
    assert text == ("津波の到達予想時刻を発表しました。最も早い到達は、17時00分頃、岩手県です。"
                    "岩手県ではただちに津波が来襲すると予測されています。")
    assert pri == 2 and val == ("岩手県", "2026-04-20T17:00:00+09:00")
    forecast_only = _jma(("茨城県", "津波予報（若干の海面変動）", "", "<0.2"))
    assert build_tide_time_speech(forecast_only) is None
    watch = _jma(("北海道太平洋沿岸東部", "津波注意報", "2026-04-20T17:30:00+09:00", "1"))
    assert build_tide_time_speech(watch)[1] == 3
