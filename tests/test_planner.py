from app.agent.planner import Planner, normalize_steps


def test_parse_object_steps():
    raw = '''```json
{"goal": "g", "steps": [
  {"task": "calculate 1250/25 in Calculator and copy it", "mode": "desktop", "url": "calculator"},
  {"task": "paste into Notepad", "mode": "desktop", "url": "notepad"}]}
```'''
    p = Planner.parse(raw, "orig")
    assert p["goal"] == "g" and len(p["steps"]) == 2
    assert p["capture_mode"] == "desktop" and p["url"] == "calculator"


def test_parse_legacy_strings_and_mode_inference():
    p = Planner.parse('{"goal":"g","steps":["Open https://mail.google.com","Open Notepad"],"capture_mode":"desktop"}', "o")
    assert [s["mode"] for s in p["steps"]] == ["browser", "desktop"]


def test_parse_garbage_falls_back():
    p = Planner.parse("sorry I can't", "Search flights on Google Flights")
    assert p["steps"] == [{"task": "Search flights on Google Flights", "mode": "browser", "url": None}]


def test_normalize_drops_empty():
    assert normalize_steps([{"task": ""}, "x"], "g", "desktop", None) == [{"task": "x", "mode": "desktop", "url": None}]


def test_micro_phases_are_merged_per_app():
    raw = '''{"goal": "g", "steps": [
      {"task": "Open Calculator and calculate 1250 divided by 25.", "mode": "desktop", "url": "calculator"},
      {"task": "Copy the result.", "mode": "desktop", "url": null},
      {"task": "Open Notepad.", "mode": "desktop", "url": "notepad"},
      {"task": "Paste the result.", "mode": "desktop", "url": null}]}'''
    p = Planner.parse(raw, "g")
    assert [s["url"] for s in p["steps"]] == ["calculator", "notepad"]
    assert p["steps"][0]["task"] == "Open Calculator and calculate 1250 divided by 25, then copy the result."
    assert p["steps"][1]["task"] == "Open Notepad, then paste the result."


def test_existing_file_url_kept():
    p = Planner.parse('{"goal":"g","steps":[{"task":"add Line two and save","mode":"desktop",'
                      '"url":"notepad ~/Desktop/NewJev_test1.txt"}]}', "g")
    assert p["steps"][0]["url"] == "notepad ~/Desktop/NewJev_test1.txt"


def test_fallback_plan_guesses_desktop_for_office_goals():
    from app.agent.planner import fallback_plan, guess_mode
    p = fallback_plan("Open Excel, create a blank workbook and enter this table starting at A1")
    assert p["capture_mode"] == "desktop" and p["url"] == "excel"
    assert guess_mode("Search YouTube for lofi") == "browser"
    assert guess_mode("Open the Wikipedia article, copy its height, then paste it into Notepad") == "browser"
    assert fallback_plan("go to https://example.com/x and read it")["url"] == "https://example.com/x"
    assert fallback_plan("anything", forced_mode="desktop")["capture_mode"] == "desktop"
