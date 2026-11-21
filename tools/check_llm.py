"""
Check that the configured Text LLM and Planner LLM answer, and how fast.
Run from the NewJev folder:   .venv\\Scripts\\python tools\\check_llm.py
Optional: test another model without editing .env:
    .venv\\Scripts\\python tools\\check_llm.py --provider openrouter --model openai/gpt-4o-mini
"""
import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

PROMPT = [{"role": "system", "content": 'Reply only with JSON: {"text": "<the value>"}'},
          {"role": "user", "content": 'Goal: type the product "Pens" into the Product field. What text goes in?'}]


def probe(name, llm):
    t0 = time.perf_counter()
    try:
        r = llm.complete(PROMPT)
        ms = round((time.perf_counter() - t0) * 1000)
        ok = "Pens" in (r.content or "")
        print(f"{name:<8} {'OK ' if ok else 'ODD'} {ms:>6} ms  model={r.model}  reply={r.content.strip()[:80]!r}")
    except Exception as exc:
        ms = round((time.perf_counter() - t0) * 1000)
        print(f"{name:<8} FAIL {ms:>6} ms  {type(exc).__name__}: {str(exc)[:200]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider")
    ap.add_argument("--model")
    a = ap.parse_args()
    if a.provider:
        os.environ["TEXT_LLM_PROVIDER"] = a.provider
    if a.model:
        os.environ["TEXT_LLM_MODEL"] = a.model
    from app.config import get_settings
    from app.llm.factory import get_planner_llm, get_text_llm
    s = get_settings()
    print(f"Text LLM:    {s.text_llm_provider} / {s.text_llm_model}  ({s.base_url_for(s.text_llm_provider)})")
    print(f"Planner LLM: {s.planner_llm_provider} / {s.planner_llm_model}  ({s.base_url_for(s.planner_llm_provider)})")
    for i in range(2):  # second call shows warm latency
        probe("text", get_text_llm())
    if not (a.provider or a.model):
        probe("planner", get_planner_llm())
        # every model in the fallback chains, one by one (so a broken fallback is found before it is needed)
        for role, llm in (("text", get_text_llm()), ("planner", get_planner_llm())):
            for label, model in getattr(llm, "chain", [])[1:]:
                probe(f"{role}-fb", model)
                print(f"         ^ {label}")
        if s.vision_llm:
            print(f"Vision LLM:  {s.vision_llm}  (used only when a window exposes few controls)")
        if s.rescue_llm:
            print(f"Rescue LLM:  {s.rescue_llm}")


if __name__ == "__main__":
    main()
