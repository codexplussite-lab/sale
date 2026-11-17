def call_gemini(prompt: str, api_key: str) -> str:
    models = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]
    last_err = ""
    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        for attempt in range(3):
            try:
                with httpx.Client(timeout=25.0) as client:
                    resp = client.post(
                        url,
                        json={"contents": [{"parts": [{"text": prompt}]}]},
                    )
                if resp.status_code == 200:
                    data = resp.json()
                    return data["candidates"][0]["content"]["parts"][0]["text"].strip()
                last_err = f"{model} HTTP {resp.status_code}: {resp.text[:120]}"
                if resp.status_code in (429, 503):
                    time.sleep(2 * (attempt + 1))
                    continue
                break
            except Exception as exc:
                last_err = str(exc)
                time.sleep(1)
    raise RuntimeError(f"Gemini failed: {last_err}")