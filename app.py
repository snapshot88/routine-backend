import json
import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from llama_cpp import Llama

import query_engine as qe

# ---------------------------------------------------------------------------
# Model -- deliberately tiny + a conservative quantization to fit Render's
# free 512MB RAM limit. Q3_K_S trades a little quality for headroom; if you
# confirm the service stays healthy, you can try bumping to q4_k_m.
# ---------------------------------------------------------------------------
llm = Llama.from_pretrained(
    repo_id="Qwen/Qwen2.5-0.5B-Instruct-GGUF",
    filename="qwen2.5-0.5b-instruct-q2_k.gguf",
    n_ctx=512,      # small context -- keeps KV-cache memory minimal
    n_threads=2,    # Render free tier gives 0.1 CPU; more threads won't help
    verbose=False,
)


def call_llm(system_prompt: str, user_prompt: str, max_new_tokens: int = 120) -> str:
    resp = llm.create_chat_completion(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=max_new_tokens,
        temperature=0,
    )
    return resp["choices"][0]["message"]["content"].strip()


# ---------------------------------------------------------------------------
# Tool router (unchanged from the Kaggle-tested version)
# ---------------------------------------------------------------------------
TOOLS = [
    {"name": "day_schedule", "params": {"section": "A|B", "day": "Sunday..Thursday"}},
    {"name": "weekly_contact_minutes", "params": {"course_code": "e.g. CSE4105", "section": "A|B"}},
    {"name": "total_labs_per_week", "params": {"section": "A|B"}},
    {"name": "total_labs_per_week_subgroup", "params": {"subgroup": "A1|A2|B1|B2"}},
    {"name": "lab_rotation_today", "params": {"section": "A|B"}},
    {"name": "teacher_schedule", "params": {"teacher_code": "e.g. RA"}},
    {"name": "free_periods", "params": {"section": "A|B", "day": "Sunday..Thursday"}},
]
FUNCS = {t["name"]: getattr(qe, t["name"]) for t in TOOLS}

ROUTER_PROMPT = f"""You answer questions about a class routine using tools only.
Output ONLY JSON: {{"tool": "<name>", "args": {{...}}}}
Tools: {json.dumps(TOOLS)}
If nothing fits, output {{"tool": "none"}}."""

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten to your exact GitHub Pages URL once it's live
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)


class Question(BaseModel):
    message: str


@app.get("/")
def health():
    return {"status": "ok"}


@app.post("/ask")
def ask(q: Question):
    raw = call_llm(ROUTER_PROMPT, q.message, max_new_tokens=80)
    try:
        call = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
        if call["tool"] == "none":
            return {"answer": "I don't have data to answer that from the routine."}
        result = FUNCS[call["tool"]](**call["args"])
    except Exception:
        return {"answer": "Sorry, I couldn't parse that question -- try rephrasing it."}

    phrasing_prompt = (
        f"Question: {q.message}\nData (ground truth): {json.dumps(result)}\n"
        "Answer in 1-2 short sentences using only this data."
    )
    answer = call_llm("You phrase answers from structured data. Never invent numbers.", phrasing_prompt)
    return {"answer": answer}


if __name__ == "__main__":
    import uvicorn
    # Render injects the PORT env var -- must bind to it, not a hardcoded port
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
