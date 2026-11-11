"""Temporary: which Gemini models answer a CV-sized request, and how fast. Example data only; prints timings."""
import os, time, json, requests, yaml
import cv, scanner
key = os.environ["GEMINI_API_KEY"]
names = [m["name"].split("/")[-1] for m in requests.get("https://generativelanguage.googleapis.com/v1beta/models",
         params={"key": key, "pageSize": 200}, timeout=30).json().get("models", [])
         if "generateContent" in m.get("supportedGenerationMethods", [])]
flash = [n for n in names if "flash" in n and "lite" not in n and "image" not in n and "tts" not in n
         and "live" not in n and "audio" not in n]
print("flash models:", flash)
master = yaml.safe_load(open("cv_master.example.yaml"))
prompt = cv.PROMPT.format(questions="\n".join(cv.ANSWER_QUESTIONS), salary="placeholder", availability="placeholder",
                          master=yaml.safe_dump(master), profile="Analyst, 2 years, Excel, Power BI.",
                          title="Compliance Specialist", company="Acme", location="Warsaw",
                          description="We need compliance, risk, sanctions screening, Excel, English. " * 60)
for model in flash[:6] + ["gemini-flash-lite-latest"]:
    for thinking in (None, 0, 1024):
        gc = {"temperature": 0.3, "responseMimeType": "application/json", "responseSchema": cv.SCHEMA}
        if thinking is not None:
            gc["thinkingConfig"] = {"thinkingBudget": thinking}
        t = time.time()
        try:
            r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                              params={"key": key}, json={"contents": [{"parts": [{"text": prompt}]}],
                                                         "generationConfig": gc}, timeout=300)
            ok = r.status_code
            extra = r.json().get("error", {}).get("status", "") if r.status_code != 200 else \
                r.json().get("usageMetadata", {}).get("candidatesTokenCount")
        except Exception as e:
            ok, extra = type(e).__name__, ""
        print(f"{model:32} thinking={thinking}: {ok} {extra} in {time.time() - t:.0f}s", flush=True)
        time.sleep(5)
