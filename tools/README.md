# Companion tools

External helpers that run **off** the Home Assistant box. They are not part of the
HACS integration — deploy them on your LLM/GPU host.

## `vision_health_job.py` — biannual plant vision-health

Reads the integration's `health.json`, finds plants due for a review, asks a **local
vision model** to compare each plant's latest photo against an older baseline, and
posts the verdict back via `complete_irrigation.set_plant_health`. The integration's
own rail bounds the verdict before storing it, so this job is never trusted blindly.

Stdlib only — no `pip install`. Advisory: it never touches watering.

### Setup

1. **Token** — create an **admin** long-lived access token in HA (the health feed +
   calendar are admin-only as of v1.32). Store it root-only:
   ```bash
   install -d -m 700 ~/.config/vision-health
   printf '%s' '<ADMIN_LLAT>' > ~/.config/vision-health/ha_token
   chmod 600 ~/.config/vision-health/ha_token
   ```
2. **Vision model** — serve an OpenAI-compatible `/v1/chat/completions` endpoint with a
   vision model (e.g. Qwen2.5-VL via vLLM/Ollama/mlx on the RTX 5060).
3. **Point it at your hosts** via env vars (see the module docstring). At minimum:
   ```bash
   export VH_HEALTH_URL=http://<ha-ip>:8123/api/complete_irrigation/health.json
   export VH_HA_URL=http://<ha-ip>:8123
   export VH_VISION_URL=http://127.0.0.1:8000/v1/chat/completions
   export VH_VISION_MODEL=Qwen2.5-VL-7B-Instruct
   ```
4. **Dry run first** (`VH_DRY_RUN=1`) to see verdicts without storing them, then drop
   the flag.

### Schedule

Run weekly from cron / a launchd timer — the integration's `due_for_review` gate makes
each plant actually get assessed only about twice a year:

```cron
0 9 * * 1  cd /path/to/tools && VH_HEALTH_URL=... VH_VISION_URL=... python3 vision_health_job.py >> ~/vision-health.log 2>&1
```

### Safety model

- The job posts a **raw** verdict; `complete_irrigation.set_plant_health` runs it
  through `vision_health.validate_verdict`, which normalizes the state, clamps
  confidence, caps + truncates text, and **strips any actuation-style "care"** (turn
  on / run_zone / switch. / service call). A hallucinating model can inform, never act.
- It only reads `/local/…` photos and calls one fixed HA service — no arbitrary I/O.
