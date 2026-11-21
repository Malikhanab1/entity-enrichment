# lead-classifier

A fine-tuned [Qwen2.5-0.5B-Instruct](https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct) model for binary classification of social media posts into **leads** vs **non-leads**.

Served locally via Ollama.

---

## What is a lead?

A **lead** is a post from a business owner describing an operational pain-point that could be solved with custom software — e.g. manual workflows, broken processes, time-consuming admin tasks.

A **non-lead** is anything else — job postings, developer questions, product showcases, general opinions, tutorials.

---

## Training

| | |
|---|---|
| Base model | Qwen/Qwen2.5-0.5B-Instruct |
| Method | LoRA fine-tuning (PEFT) |
| Dataset | 1,500 examples (750 leads, 750 non-leads) |
| Epochs | 3 |
| Test accuracy | 100% |
| Task | Binary sequence classification |

---

## Setup

```bash
systemctl start ollama
ollama create lead-classifier -f ./Modelfile
```

---

## Usage

### REST API

```python
import requests, json

response = requests.post("http://localhost:11434/api/generate", json={
    "model": "lead-classifier",
    "prompt": "Your post text here",
    "stream": False,
})

result = json.loads(response.json()["response"])
```

### CLI

```bash
ollama run lead-classifier "Your post text here"
```

---

## Input

Plain text — a social media post, forum message, or similar short-form text.

```
I run a dental clinic and my receptionist spends hours every week manually
confirming appointments over WhatsApp. Is there a way to automate this?
```

No special formatting required. Keep it under 512 tokens for best results.

---

## Output

A JSON object with two fields:

```json
{
  "lead": 1,
  "reason": "Business owner describing a manual appointment workflow pain-point."
}
```

| Field | Type | Values |
|---|---|---|
| `lead` | int | `1` = lead, `0` = not a lead |
| `reason` | string | One-sentence explanation |

---

## Examples

| Input | `lead` | Reason |
|---|---|---|
| "I run a dental clinic and my receptionist spends hours confirming appointments over WhatsApp." | `1` | Business owner with a manual workflow pain-point |
| "I own a restaurant and every night we copy online orders into our POS by hand." | `1` | Operational bottleneck solvable with software |
| "Looking to hire a React developer, $80k, full-time." | `0` | Job posting |
| "How do I implement an AI agent using LangChain?" | `0` | Developer question |
| "Which form builder do you prefer — Typeform or Tally?" | `0` | General opinion question |
